import os
from pathlib import Path

from gym.spaces import Box

import torch as T
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from tensorboardX import SummaryWriter

class OUActionNoise(object):
    def __init__(self, mu, sigma=0.15, theta=.2, dt=1e-2, x0=None):
        self.theta = theta
        self.mu = mu
        self.sigma = sigma
        self.dt = dt
        self.x0 = x0
        self.reset()

    def __call__(self):
        x = self.x_prev + self.theta * (self.mu - self.x_prev) * self.dt + \
            self.sigma * np.sqrt(self.dt) * np.random.normal(size=self.mu.shape)
        self.x_prev = x
        return x

    def reset(self):
        self.x_prev = self.x0 if self.x0 is not None else np.zeros_like(self.mu)

    def __repr__(self):
        return 'OrnsteinUhlenbeckActionNoise(mu={}, sigma={})'.format(
                                                            self.mu, self.sigma)

class ReplayBuffer(object):
    def __init__(self, max_size, input_shape, actions_shape):
        self.mem_size = max_size
        self.mem_cntr = 0
        self.obs_memory = np.zeros((self.mem_size, *input_shape))
        self.obs_next_memory = np.zeros((self.mem_size, *input_shape))
        self.action_memory = np.zeros((self.mem_size, *actions_shape))
        self.reward_memory = np.zeros(self.mem_size)
        self.terminal_memory = np.zeros(self.mem_size, dtype=np.float32)    #to save the done flags in terminal states

    def store_transition(self, obs, action, reward, obs_next, done):
        index = self.mem_cntr % self.mem_size
        self.obs_memory[index] = obs
        self.obs_next_memory[index] = obs_next
        self.action_memory[index] = action
        self.reward_memory[index] = reward
        self.terminal_memory[index] = 1 - done  #yields 0 in all terminal states, 1 otherwise; to multiply the value by this value; 
        self.mem_cntr += 1

    def get_batch_idxs(self, batch_size):
        max_mem = min(self.mem_cntr, self.mem_size)

        batch_idxs = np.random.choice(max_mem, batch_size)

        return batch_idxs


    def get_samples_from_buffer(self, batch_idxs):

        obs = self.obs_memory[batch_idxs]       #this list picking is from numpy; 
        actions = self.action_memory[batch_idxs]
        rewards = self.reward_memory[batch_idxs]
        obs_next = self.obs_next_memory[batch_idxs]
        terminal = self.terminal_memory[batch_idxs]

        return obs, actions, rewards, obs_next, terminal

class CriticNetwork(nn.Module):
    def __init__(self, beta, n_inputs, fc1_dims, fc2_dims, action_shape):
        super(CriticNetwork, self).__init__()
        self.n_inputs = n_inputs
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.action_shape = action_shape
        self.fc1 = nn.Linear(self.n_inputs, self.fc1_dims)
        f1 = 1./np.sqrt(self.fc1.weight.data.size()[0])     #weight initialization according to DDPD-paper
        T.nn.init.uniform_(self.fc1.weight.data, -f1, f1)
        T.nn.init.uniform_(self.fc1.bias.data, -f1, f1)
        #self.fc1.weight.data.uniform_(-f1, f1)
        #self.fc1.bias.data.uniform_(-f1, f1)
        self.bn1 = nn.LayerNorm(self.fc1_dims)  #Applies _Layer_  Normalization over a mini-batch of inputs as described in the paper Layer Normalization_ .

        self.fc2 = nn.Linear(self.fc1_dims, self.fc2_dims)
        f2 = 1./np.sqrt(self.fc2.weight.data.size()[0])
        #f2 = 0.002
        T.nn.init.uniform_(self.fc2.weight.data, -f2, f2)
        T.nn.init.uniform_(self.fc2.bias.data, -f2, f2)
        #self.fc2.weight.data.uniform_(-f2, f2)
        #self.fc2.bias.data.uniform_(-f2, f2)
        self.bn2 = nn.LayerNorm(self.fc2_dims)

        self.action_value = nn.Linear(*self.action_shape, self.fc2_dims)
        f3 = 3e-3
        self.q = nn.Linear(self.fc2_dims, 1)        #the Critic output is just a single scalar Q-value;
        T.nn.init.uniform_(self.q.weight.data, -f3, f3)
        T.nn.init.uniform_(self.q.bias.data, -f3, f3)
        #self.q.weight.data.uniform_(-f3, f3)
        #self.q.bias.data.uniform_(-f3, f3)

        self.optimizer = optim.Adam(self.parameters(), lr=beta)
        self.device = T.device('cuda:0' if T.cuda.is_available() else 'cpu')

        self.to(self.device)

    def forward(self, state, action):
        state_value = self.fc1(state)
        state_value = self.bn1(state_value)
        state_value = F.relu(state_value)       #activation is done after normalization to avoid cuting off the negative end
        state_value = self.fc2(state_value)
        state_value = self.bn2(state_value)

        action_value = F.relu(self.action_value(action))    #activate the action; (this gets activated twice with relu)
                                                            #TODO: what happens to negative actions? they are entirely zeroed in this setting. No, the self.action_network has weights and bias, so it can hold for it
        state_action_value = F.relu(T.add(state_value, action_value))   #add state and action value together
        state_action_value = self.q(state_action_value)

        return state_action_value   #scalar value

class ActorNetwork(nn.Module):
    def __init__(self, alpha, n_inputs: int, fc1_dims, fc2_dims, n_actions:int):
        super(ActorNetwork, self).__init__()
        self.n_inputs = n_inputs
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.n_actions = n_actions
        self.fc1 = nn.Linear(self.n_inputs, self.fc1_dims)
        f1 = 1./np.sqrt(self.fc1.weight.data.size()[0])
        T.nn.init.uniform_(self.fc1.weight.data, -f1, f1)
        T.nn.init.uniform_(self.fc1.bias.data, -f1, f1)
        #self.fc1.weight.data.uniform_(-f1, f1)
        #self.fc1.bias.data.uniform_(-f1, f1)
        self.bn1 = nn.LayerNorm(self.fc1_dims)

        self.fc2 = nn.Linear(self.fc1_dims, self.fc2_dims)
        #f2 = 0.002
        f2 = 1./np.sqrt(self.fc2.weight.data.size()[0])
        T.nn.init.uniform_(self.fc2.weight.data, -f2, f2)
        T.nn.init.uniform_(self.fc2.bias.data, -f2, f2)
        #self.fc2.weight.data.uniform_(-f2, f2)
        #self.fc2.bias.data.uniform_(-f2, f2)
        self.bn2 = nn.LayerNorm(self.fc2_dims)

        #f3 = 0.004
        f3 = 0.003
        self.mu = nn.Linear(self.fc2_dims, self.n_actions)
        T.nn.init.uniform_(self.mu.weight.data, -f3, f3)
        T.nn.init.uniform_(self.mu.bias.data, -f3, f3)
        #self.mu.weight.data.uniform_(-f3, f3)
        #self.mu.bias.data.uniform_(-f3, f3)

        self.optimizer = optim.Adam(self.parameters(), lr=alpha)
        self.device = T.device('cuda:0' if T.cuda.is_available() else 'cpu')

        self.to(self.device)

    def forward(self, state):
        x = self.fc1(state)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.fc2(x)
        x = self.bn2(x)
        x = F.relu(x)
        x = self.mu(x)
        x = T.tanh(x)

        return x

class Agent_Multi(object):
    def __init__ (self, lr_actor, lr_critic, own_input_shape: int, action_space: Box, tau, 
                 # env, 
                 other_input_shapes: List[Tuple] = [(0, )], #added the number of other agents' observations
                 other_actions_shapes: List[Tuple] = [(0, )],  #added the number of other agents' actions which are part of the state
                 gamma=0.99,
                 max_size=1000000, layer1_size=400,
                 layer2_size=300, batch_size=64, noise_sigma = 0.15, noise_theta = 0.2):
        
        self.init_dict = {
            'lr_actor': lr_actor, 
            'lr_critic': lr_critic, 
            'own_input_shape': own_input_shape, 
            'action_space': action_space, 
            'tau': tau, 
            'other_input_shapes': other_input_shapes,
            'other_actions_shapes': other_actions_shapes,
            'gamma': gamma,
            'max_size': max_size,
            'layer1_size': layer1_size,
            'layer2_size': layer2_size,
            'batch_size': batch_size,
            'noise_sigma': noise_sigma,
            'noise_theta': noise_theta
        }
        
        #default values for noise
        self.noise_sigma = noise_sigma
        self.noise_theta = noise_theta

        self.calculate_grad_norms = False #TODO: set to true for calculating of the critic's max and l2 norm

        # self.env = env #TODO: this is only to set the meta_information, so the env parameter could be eliminated entirely

        self.agent_type = 'DDPD' if other_input_shapes == [(0, )] else 'MADDPG' #determine whether the agent is a multi-agent Agent or works on its own.

        self.action_space = action_space
        self.scale =  (action_space.high - action_space.low) / 2. #to scale the action output from -1...1 into the range from low...high
        self.shift =  (action_space.high + action_space.low) / 2. #to scale the action output from -1...1 into the range from low...high
        self.gamma = gamma
        self.tau = tau
        self.memory = ReplayBuffer(max_size, own_input_shape, action_space.shape)
        self.batch_size = batch_size
        
        # in pytorch multi-dimensional inputs are separated along the last coordinate, https://pytorch.org/docs/stable/nn.html#linear, https://stackoverflow.com/a/58591606/2682209
        self.actor = ActorNetwork(lr_actor, own_input_shape[-1], layer1_size,
                                  layer2_size, n_actions=action_space.shape[-1])
        self.critic = CriticNetwork(lr_critic, 
                        (own_input_shape[-1] + sum([od[-1] for od in other_input_shapes]) + sum([ad[-1] for ad in other_actions_shapes])),
                        layer1_size, layer2_size, action_shape=action_space.shape)

        self.target_actor = ActorNetwork(lr_actor, own_input_shape[-1], layer1_size,
                                  layer2_size, n_actions=action_space.shape[-1])
        self.target_critic = CriticNetwork(lr_critic, 
                        (own_input_shape[-1] + sum([od[-1] for od in other_input_shapes]) + sum([ad[-1] for ad in other_actions_shapes])),
                                layer1_size,layer2_size, action_shape=action_space.shape)

        # self.noise = OUActionNoise(mu=np.zeros(n_actions),sigma=0.15, theta=.2, dt=1/5.)
        self.noise = OUActionNoise(mu=np.zeros(action_space.shape),sigma=self.noise_sigma, theta=self.noise_theta, dt=1/5.)

        writer_name = f"GLIDE-DDPG_input_shape-{own_input_shape}_n_actions-{action_space.shape}_lr_actor-{lr_actor}_lr_critic-{lr_critic}_batch_size-{batch_size}"
        self.writer = SummaryWriter(comment=writer_name)

        print(self.actor)
        print(self.critic)

        self._update_network_parameters(tau=1)   #with tau=1 the target net is updated entirely to the base network
        self.global_step = 0
        self.episode_counter = 0

    def choose_action(self, observation, add_exploration_noise = True):
        self.actor.eval()   #don't calc statistics for layer normalization in action selection
        observation = T.tensor(observation, dtype=T.float).to(self.actor.device)    #convert to Tensor
        mu = self.actor.forward(observation)    # I don't think it's useful to move it to the GPU.to(self.actor.device)
        # if self.writer:
        #     self.writer.add_scalar("exploration noise", noise, global_step=self.global_step)
        self.actor.train()  #switch to training mode
        mu_np = mu.cpu().detach().numpy()
        if add_exploration_noise:
            mu_np = mu_np + self.noise()
        mu_np = mu_np * self.scale + self.shift
        return mu_np

    def remember(self, obs, action, reward, new_obs, done):
        if self.writer:
            self.writer.add_scalar("reward", reward, global_step=self.global_step)
        self.memory.store_transition(obs, action, reward, new_obs, done)
        if done:
            self.episode_counter += 1
        self.global_step += 1
        
    def learn(self, agents_list, own_idx = 0):
        if self.memory.mem_cntr < self.batch_size:
            return
        #determine the samples for minibatch
        batch_idxs = self.memory.get_batch_idxs(self.batch_size)
        # retrieve minibatch from all agents including own
        obs_n, actual_action_n, reward_n, new_obs_n, done_n = \
                zip(*[ag.agent.memory.get_samples_from_buffer(batch_idxs) for ag in agents_list])
        
        #convert to pytorch tensors
        rwd_n_t = [T.tensor(rwd, dtype=T.float).to(self.critic.device) for rwd in reward_n]
        done_n_t = [T.tensor(dn).to(self.critic.device) for dn in done_n]
        new_obs_n_t = [T.tensor(new_obs, dtype=T.float).to(self.critic.device) for new_obs in new_obs_n]
        actual_action_n_t = [T.tensor(actual_action, dtype=T.float).to(self.critic.device) for actual_action in actual_action_n]
        obs_n_t = [T.tensor(obs, dtype=T.float).to(self.critic.device) for obs in obs_n]
        #the state is the concatenation of all observations (which includes doubles, but dunno)
        state_t = T.cat(obs_n_t, dim=1).to(self.critic.device)
        new_state_t = T.cat(new_obs_n_t, dim=1).to(self.critic.device)

        for ag in agents_list:
            ag.agent.target_actor.eval()    #switch target networks to eval mode
            ag.agent.target_critic.eval()
            ag.agent.critic.eval()          #switch critic to eval mode
        
        #calculate the target action of the new state for Bellmann equation for all agents
        target_next_action_n = [ag.agent.target_actor.forward(new_obs) for ag, new_obs in zip(agents_list, new_obs_n_t)]
        #create the input to the target value function (cat([new_state_n, other_next_actions), own_action)
        own_next_action = target_next_action_n.pop(own_idx)
        input_target_value_fn = T.cat([new_state_t, T.cat(target_next_action_n)], dim=1)

        #calculate y = rew + gamma*Q_target(state, own_action)
        target_critic_next_value = self.target_critic.forward(input_target_value_fn, own_next_action) #calculate the target critic value of the new_state for Bellmann equation
        
        target_value = rwd_n_t[own_idx].view(self.batch_size, 1) + self.gamma*target_critic_next_value*done_n_t[own_idx].view(self.batch_size, 1)

        #calculate Q_target(state, own_action)
        own_actual_action = actual_action_n_t.pop(own_idx)
        input_value_fn = T.cat([state_t, T.cat(actual_action_n_t)], dim = 1)
        critic_value = self.critic.forward(input_value_fn, own_actual_action)       #calculate the base critic value of chosen action

        self.critic.train()         #switch critic back to training mode
        self.critic.optimizer.zero_grad()
        critic_loss = F.mse_loss(target_value, critic_value)
        if self.writer:
            self.writer.add_scalar("critic_loss", critic_loss, global_step=self.global_step)
        critic_loss.backward()

        if self.calculate_grad_norms:   #TODO: 
            grad_max_n, grad_means_n = zip(*[(p.grad.abs().max().item(), (p.grad ** 2).mean().sqrt().item())  for p in list(self.critic.parameters())])
            grad_max = max(grad_max_n)
            grad_means = np.mean(grad_means_n)
            self.writer.add_scalar("critic grad_l2",  grad_means, global_step=self.global_step)
            self.writer.add_scalar("critic grad_max", grad_max, global_step=self.global_step)
        
        self.critic.optimizer.step()
        self.critic.eval()          #switch critic back to eval mode for the "loss" calculation of the actor network
        self.actor.optimizer.zero_grad()
        mu = self.actor.forward(obs_n_t[own_idx])
        self.actor.train()
        actor_performance = -self.critic.forward(input_value_fn, mu) # use negative performance as optimizer always does gradiend descend, but we want to ascend
        actor_performance = T.mean(actor_performance)
        if self.writer:
            self.writer.add_scalar("actor_performance", -actor_performance, global_step=self.global_step)
        actor_performance.backward()
        self.actor.optimizer.step()

        self._update_network_parameters()    #update target to base networks with standard tau

    def _update_network_parameters(self, tau=None):
        if tau is None:
            tau = self.tau

        actor_params = self.actor.named_parameters()
        critic_params = self.critic.named_parameters()
        target_actor_params = self.target_actor.named_parameters()
        target_critic_params = self.target_critic.named_parameters()

        critic_state_dict = dict(critic_params)
        actor_state_dict = dict(actor_params)
        target_critic_dict = dict(target_critic_params)
        target_actor_dict = dict(target_actor_params)

        for name in critic_state_dict:
            critic_state_dict[name] = tau*critic_state_dict[name].clone() + \
                                      (1-tau)*target_critic_dict[name].clone()

        self.target_critic.load_state_dict(critic_state_dict)

        for name in actor_state_dict:
            actor_state_dict[name] = tau*actor_state_dict[name].clone() + \
                                      (1-tau)*target_actor_dict[name].clone()
        self.target_actor.load_state_dict(actor_state_dict)

        """
        #Verify that the copy assignment worked correctly
        target_actor_params = self.target_actor.named_parameters()
        target_critic_params = self.target_critic.named_parameters()

        critic_state_dict = dict(target_critic_params)
        actor_state_dict = dict(target_actor_params)
        print('\nActor Networks', tau)
        for name, param in self.actor.named_parameters():
            print(name, T.equal(param, actor_state_dict[name]))
        print('\nCritic Networks', tau)
        for name, param in self.critic.named_parameters():
            print(name, T.equal(param, critic_state_dict[name]))
        input()
        """

    def save_models(self, filename):
        agent_networks_state_dict = {
            'init_dict': self.init_dict,
            'actor': self.actor.state_dict(),
            'critic': self.critic.state_dict(),
            'target_actor': self.target_actor.state_dict(),
            'target_critic': self.target_critic.state_dict(),
            'actor_optimizer': self.actor.optimizer.state_dict(),
            'critic_optimizer': self.critic.optimizer.state_dict()
        }
        
        T.save(agent_networks_state_dict)

    @classmethod
    def init_from_save(cls, filename):
        """
        Instantiate instance of this class from file created by 'save_models' method
        """
        save_dict = torch.load(filename)
        instance = cls(**save_dict['init_dict'])
        instance.init_dict = save_dict['init_dict']
        instance.actor.load_state_dict(save_dict['actor'])
        instance.critic.load_state_dict(save_dict['critic'])
        instance.target_actor.load_state_dict(save_dict['target_actor'])
        instance.target_critic.load_state_dict(save_dict['target_critic'])
        instance.actor.optimizer.load_state_dict(save_dict['actor_optimizer'])
        instance.critic.optimizer.load_state_dict(save_dict['critic_optimizer'])

        return instance

    def reset_noise_source(self):
        self.noise.reset()

    def reduce_noise_sigma(self, sigma_factor = 1, theta_factor = 1):
        self.noise_sigma *= sigma_factor
        self.noise_theta *= theta_factor
        print('Noise set to sigma=%f, theta=%f' % (self.noise_sigma, self.noise_theta))
        self.noise = OUActionNoise(mu=np.zeros(self.own_actions),sigma=self.noise_sigma, theta=self.noise_theta, dt=1/5.)
        self.noise.reset()

    # def check_actor_params(self):
    #     current_actor_params = self.actor.named_parameters()
    #     current_actor_dict = dict(current_actor_params)
    #     original_actor_dict = dict(self.original_actor.named_parameters())
    #     original_critic_dict = dict(self.original_critic.named_parameters())
    #     current_critic_params = self.critic.named_parameters()
    #     current_critic_dict = dict(current_critic_params)
    #     print('Checking Actor parameters')

    #     for param in current_actor_dict:
    #         print(param, T.equal(original_actor_dict[param], current_actor_dict[param]))
    #     print('Checking critic parameters')
    #     for param in current_critic_dict:
    #         print(param, T.equal(original_critic_dict[param], current_critic_dict[param]))
    #     input()

class Agent_Single(Agent_Multi):    #TODO: unify this with Multi-Agent this is just stupid!
    def __init__(self, lr_actor, lr_critic, own_input_shape: int, action_space: Box, tau, 
                 # env, 
                 gamma=0.99,
                 max_size=1000000, layer1_size=400,
                 layer2_size=300, batch_size=64, chkpt_dir='tmp/ddpg', chkpt_postfix='', noise_sigma = 0.15, noise_theta = 0.2):
        
        self.init_dict = {
            'lr_actor': lr_actor, 
            'lr_critic': lr_critic, 
            'own_input_shape': own_input_shape, 
            'action_space': action_space, 
            'tau': tau, 
            'gamma': gamma,
            'max_size': max_size,
            'layer1_size': layer1_size,
            'layer2_size': layer2_size,
            'batch_size': batch_size,
            'noise_sigma': noise_sigma,
            'noise_theta': noise_theta
        }
        
        #default values for noise
        self.noise_sigma = noise_sigma
        self.noise_theta = noise_theta

        # self.env = env #TODO: this is only to set the meta_information, so the env parameter could be eliminated entirely

        self.calculate_grad_norms = False #TODO: set to true for calculating of the critic's max and l2 norm

        self.agent_type = 'DDPD'

        self.action_space = action_space
        self.scale =  (action_space.high - action_space.low) / 2. #to scale the action output from -1...1 into the range from low...high
        self.shift =  (action_space.high + action_space.low) / 2. #to scale the action output from -1...1 into the range from low...high
        self.gamma = gamma
        self.tau = tau
        self.memory = ReplayBuffer(max_size, own_input_shape, action_space.shape)
        self.batch_size = batch_size
        
        self.chkpt_postfix = '_'+chkpt_postfix if chkpt_postfix != '' else ''
        self.chkpt_dir= f"checkpoints/{chkpt_dir}/inputs_{own_input_shape}_actions_{action_space.shape}/layer1_{layer1_size}_layer2_{layer2_size}/"
        Path(self.chkpt_dir).mkdir(parents=True, exist_ok=True)
        
        print(f"set checkpoint directory to: {self.chkpt_dir}")

        self.actor = ActorNetwork(lr_actor, own_input_shape[-1], layer1_size,
                                  layer2_size, n_actions=action_space.shape[-1])
        self.critic = CriticNetwork(lr_critic, 
                        own_input_shape[-1],
                        layer1_size, layer2_size, 
                        action_shape=action_space.shape)

        self.target_actor = ActorNetwork(lr_actor, own_input_shape[-1], layer1_size,
                                  layer2_size, n_actions=action_space.shape[-1])
        self.target_critic = CriticNetwork(lr_critic, 
                                own_input_shape[-1],
                                layer1_size, layer2_size, 
                                action_shape=action_space.shape)


        # self.noise = OUActionNoise(mu=np.zeros(n_actions),sigma=0.15, theta=.2, dt=1/5.)
        self.noise = OUActionNoise(mu=np.zeros(action_space.shape),sigma=self.noise_sigma, theta=self.noise_theta, dt=1/5.)

        writer_name = f"GLIDE-DDPG_input_shape-{own_input_shape}_n_actions-{action_space.shape}_lr_actor-{lr_actor}_lr_critic-{lr_critic}_batch_size-{batch_size}"
        self.writer = SummaryWriter(comment=writer_name)

        print(self.actor)
        print(self.critic)

        self._update_network_parameters(tau=1)   #with tau=1 the target net is updated entirely to the base network
        self.global_step = 0
        self.episode_counter = 0

        #add the agent's settings to the env meta-information:
        # env.set_meta_information(lr_actor=lr_actor, lr_critic=lr_critic, n_inputs = n_inputs, tau=tau, 
        #         batch_size=batch_size,  layer1_size=layer1_size, layer2_size=layer2_size, n_actions = n_actions,
        #         chkpt_dir=chkpt_dir, chkpt_postfix=chkpt_postfix, summary_writer = writer_name)

    def learn(self, agents_list, own_idx = 0):
        if self.memory.mem_cntr < self.batch_size:
            return
        
        batch_idxs = self.memory.get_batch_idxs(self.batch_size)
        state, actual_action, reward, new_state, done = \
                                      self.memory.get_samples_from_buffer(batch_idxs)

        reward = T.tensor(reward, dtype=T.float).to(self.critic.device)
        done = T.tensor(done).to(self.critic.device)
        new_state = T.tensor(new_state, dtype=T.float).to(self.critic.device)
        actual_action = T.tensor(actual_action, dtype=T.float).to(self.critic.device)
        state = T.tensor(state, dtype=T.float).to(self.critic.device)

        self.target_actor.eval()    #switch target networks to eval mode
        self.target_critic.eval()
        self.critic.eval()          #switch critic to eval mode
        target_next_actions = self.target_actor.forward(new_state)   #calculate the target action of the new statefor Bellmann equation
        target_critic_next_value = self.target_critic.forward(new_state, target_next_actions) #calculate the target critic value of the new_state for Bellmann equation
        critic_value = self.critic.forward(state, actual_action)       #calculate the base critic value of chosen action

        target_value = reward.view(self.batch_size, 1) + self.gamma*target_critic_next_value*done.view(self.batch_size, 1)

        self.critic.train()         #switch critic back to training mode
        self.critic.optimizer.zero_grad()
        critic_loss = F.mse_loss(target_value, critic_value)
        if self.writer:
            self.writer.add_scalar("critic_loss", critic_loss, global_step=self.global_step)
        critic_loss.backward()

        if self.calculate_grad_norms:   #TODO: 
            grad_max_n, grad_means_n = zip(*[(p.grad.abs().max().item(), (p.grad ** 2).mean().sqrt().item())  for p in list(self.critic.parameters())])
            grad_max = max(grad_max_n)
            grad_means = np.mean(grad_means_n)
            self.writer.add_scalar("critic grad_l2",  grad_means, global_step=self.global_step)
            self.writer.add_scalar("critic grad_max", grad_max, global_step=self.global_step)
        
        self.critic.optimizer.step()
        self.critic.eval()          #switch critic back to eval mode for the "loss" calculation of the actor network
        self.actor.optimizer.zero_grad()
        mu = self.actor.forward(state)
        self.actor.train()
        actor_performance = -self.critic.forward(state, mu) # use negative performance as optimizer always does gradiend descend, but we want to ascend
        actor_performance = T.mean(actor_performance)
        if self.writer:
            self.writer.add_scalar("actor_performance", -actor_performance, global_step=self.global_step)
        actor_performance.backward()
        self.actor.optimizer.step()

        self._update_network_parameters()    #update target to base networks with standard tau


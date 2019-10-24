from __future__ import print_function
import torch
from torch.nn.parameter import Parameter
import numpy as np
import utils.filters as filters
import tables
import os


class SNNetwork(torch.nn.Module):
    def __init__(self, n_input_neurons, n_hidden_neurons, n_output_neurons, topology, n_basis_feedforward=1, feedforward_filter=filters.base_feedforward_filter,
                 n_basis_feedback=1, feedback_filter=filters.base_feedback_filter, tau_ff=1, tau_fb=1, mu=1, weights_magnitude=0.1, task='supervised', mode='train', save_path=None):

        super(SNNetwork, self).__init__()
        '''
        An SNN network is defined by its topology, i.e., the connections between the neurons. 
        A forward pass in the network consists of transmitting information from the input neurons to the rest of the network, starting with the input neurons.
        The behavior of visible neurons is given by the input during the pass. 
        Hidden neurons produce spikes following a Bernoulli distribution parametrized by the sigmoid of their membrane potential.  
        Parameters: 
        topology: matrix defining the synaptic connections between neurons, of size n_learnable_neurons x n_neurons 
        topology[i, j] = 1 means that there is a feedforward synaptic connection from neuron j to neuron i
        visible neurons: neurons for which the behavior is determined by the input signal
        feedforward_filter: the basis function(s) used to compute contributions from pre-synaptic neurons
        feedback_filter: the basis function(s) used to compute contributions from the history 
        tau_ff, n_basis_feedforward: parameters of the feedforward filter
        tau_fb, n_basis_feedback: parameters of the feedback filter
        weights_magnitude: the weights are initialized following an uniform distribution between [-weights_magnitude, +weights_magnitude]
        minibatch_size
        '''

        ### Network parameters
        self.n_input_neurons = n_input_neurons
        self.n_hidden_neurons = n_hidden_neurons
        self.n_output_neurons = n_output_neurons
        self.n_neurons = n_input_neurons + n_hidden_neurons + n_output_neurons
        self.weights_magnitude = weights_magnitude


        ### Neurons indices
        self.input_neurons = torch.LongTensor([i for i in range(self.n_input_neurons)])
        self.hidden_neurons = torch.LongTensor([self.n_input_neurons + i for i in range(self.n_hidden_neurons)])
        self.output_neurons = torch.LongTensor([self.n_input_neurons + self.n_hidden_neurons + i for i in range(self.n_output_neurons)])


        # In supervised mode, we avoid computations with unnecessary large matrices
        if task == 'supervised':
            self.n_learnable_neurons = n_hidden_neurons + n_output_neurons
            self.n_non_learnable_neurons = n_input_neurons
            self.learnable_neurons = torch.cat((self.hidden_neurons, self.output_neurons))
        else:
            self.n_learnable_neurons = self.n_neurons
            self.n_non_learnable_neurons = 0
            self.learnable_neurons = torch.cat((self.input_neurons, self.hidden_neurons, self.output_neurons))

        self.non_learnable_neurons = torch.tensor([i for i in range(self.n_non_learnable_neurons)])

        assert (self.n_non_learnable_neurons + self.n_learnable_neurons) == self.n_neurons


        # Set train mode, to avoid spurious computations of gradients during test
        self.mode = mode
        if self.mode == 'train':
            self.visible_neurons = torch.cat((self.input_neurons, self.output_neurons))
        elif self.mode == 'test':
            self.visible_neurons = self.input_neurons
        else:
            print('Mode should be one of "train" or "test"')
            raise AttributeError


        # Sanity checks
        assert self.n_learnable_neurons == topology.shape[0], 'The topology of the network should be of shape [n_learnable_neurons, n_neurons]'
        assert self.n_neurons == topology.shape[-1], 'The topology of the network should be of shape [n_learnable_neurons, n_neurons]'
        topology[[i for i in range(self.n_learnable_neurons)], [i for i in self.learnable_neurons]] = 0


        ### Feedforward weights
        self.n_basis_feedforward = n_basis_feedforward
        # Creating the feedforward weights according to the topology.
        # Feedforward weights are a tensor of size [n_learnable_neurons, n_neurons, n_basis_feedforward] for which the block-diagonal elements are 0,
        # and otherwise feedforward_weights[i, j, :] ~ Unif[-weights_magnitude, +weights_magnitude] if topology[i, j] = 1
        self.feedforward_mask = torch.tensor(np.kron(topology, np.ones([self.n_basis_feedforward]))
                                             .reshape([self.n_learnable_neurons, self.n_neurons, self.n_basis_feedforward]), dtype=torch.float)
        self.feedforward_weights = weights_magnitude * (torch.rand([self.n_learnable_neurons, self.n_neurons, self.n_basis_feedforward]) * 2 - 1) * self.feedforward_mask
        self.feedforward_filter = feedforward_filter(tau_fb, self.n_basis_feedforward, mu)
        self.tau_ff = tau_ff


        ### Feedback weights
        self.n_basis_feedback = n_basis_feedback
        # Creating the feedback weights.
        # Feedback weights are a tensor of size [n_neurons, n_basis_feedback],
        # for which learnable elements are initialized as ~ Unif[-weights_magnitude, +weights_magnitude],
        self.feedback_mask = torch.zeros([self.n_neurons, self.n_basis_feedback])
        self.feedback_mask[self.learnable_neurons, :] = 1
        self.feedback_weights = weights_magnitude * (torch.rand([self.n_learnable_neurons, self.n_basis_feedback]) * 2 - 1)
        self.feedback_filter = feedback_filter(tau_fb, self.n_basis_feedback, mu)
        self.tau_fb = tau_fb


        ### Bias
        self.bias = weights_magnitude * (torch.rand([self.n_learnable_neurons]) * 2 - 1)

        # Number of timesteps to keep in memory
        self.memory_length = max(self.tau_ff, self.tau_fb)

        ### State of the network
        self.spiking_history = torch.zeros([self.n_neurons, 1])
        self.potential = torch.zeros([self.n_learnable_neurons])
        self.feedforward_potential = torch.zeros([self.n_learnable_neurons])
        self.feedback_potential = torch.zeros([self.n_learnable_neurons])

        ### Gradients
        self.bias_gradient = torch.zeros(self.bias.shape)
        self.ff_gradient = torch.zeros(self.feedforward_weights.shape)
        self.fb_gradient = torch.zeros(self.feedback_weights.shape)

        # Path to where the weights are saved, if None they will be saved in the current directory
        self.save_path = save_path


    def forward(self, input_signal):

        assert self.n_neurons == (len(self.input_neurons) + len(self.hidden_neurons) + len(self.output_neurons)), "The numbers of neurons don't match"
        assert self.n_neurons == (len(self.learnable_neurons) + len(self.non_learnable_neurons)), "The numbers of neurons don't match"


        ### Compute potential
        feedforward_trace = torch.matmul(self.spiking_history.flip(-1), self.feedforward_filter[:, :self.spiking_history.shape[-1]].transpose(0, 1))
        self.feedforward_potential = torch.sum(self.feedforward_weights * feedforward_trace * self.feedforward_mask, dim=(-1, -2))

        feedback_trace = torch.matmul(self.spiking_history.flip(-1), self.feedback_filter[:, :self.spiking_history.shape[-1]].transpose(0, 1))
        self.feedback_potential = torch.sum(self.feedback_weights * feedback_trace[self.learnable_neurons, :], dim=(-1))

        self.potential = self.feedforward_potential + self.feedback_potential + self.bias


        ### Update spiking history
        self.spiking_history = torch.cat((self.spiking_history[:, - self.memory_length + 1:],
                                          torch.zeros([self.n_neurons, 1])), dim=-1)
        self.spiking_history[self.visible_neurons, -1] = input_signal

        if self.n_hidden_neurons > 0:
            self.spiking_history[self.hidden_neurons, -1] = torch.bernoulli(torch.sigmoid(self.potential.data[self.hidden_neurons - self.n_non_learnable_neurons]))
        if self.mode == 'test':
            self.spiking_history[self.output_neurons, -1] = torch.bernoulli(torch.sigmoid(self.potential.data[self.output_neurons - self.n_non_learnable_neurons]))


        ### Compute log-probabilities
        # noinspection PyTypeChecker
        log_proba = self.spiking_history[self.learnable_neurons, -1] * torch.log(1e-07 + torch.sigmoid(self.potential)) \
                    + (1 - self.spiking_history[self.learnable_neurons, -1]) * torch.log(1. + 1e-07 - torch.sigmoid(self.potential))  # We add 1e-07 for numerical stability of the log

        assert log_proba.shape == torch.Size([self.n_learnable_neurons]), \
            'Wrong log_probability shape, got: ' + str(log_proba.shape) + ', expected: ' + str(torch.Size([self.n_learnable_neurons]))


        ### Compute gradients
        if self.mode == 'train':
            self.bias_gradient = self.spiking_history[self.learnable_neurons, -1] - torch.sigmoid(self.potential)
            assert self.bias_gradient.shape == self.bias.shape, "Wrong bias gradient shape"

            self.ff_gradient = feedforward_trace.unsqueeze(0).repeat(self.n_learnable_neurons, 1, 1) \
                               * self.bias_gradient.unsqueeze(1).repeat(1, self.n_neurons).unsqueeze(2).repeat(1, 1, self.n_basis_feedforward) \
                               * self.feedforward_mask
            assert self.ff_gradient.shape == self.feedforward_weights.shape, "Wrong feedforward weights gradient shape"

            self.fb_gradient = feedback_trace[self.learnable_neurons, :] * self.bias_gradient.unsqueeze(1).repeat(1, self.n_basis_feedback)
            assert self.fb_gradient.shape == self.feedback_weights.shape, "Wrong feedback weights gradient shape"

        return log_proba


    ### Getters
    def get_gradients(self):
        return {'ff_weights': self.ff_gradient, 'fb_weights': self.fb_gradient, 'bias': self.bias_gradient}


    def get_parameters(self):
        return {'ff_weights': self.feedforward_weights, 'fb_weights': self.feedback_weights, 'bias': self.bias}


    def get_history(self):
        return self.spiking_history


    ### Setters
    def reset_internal_state(self):
        self.spiking_history = torch.zeros([self.n_neurons, 1])
        self.potential = torch.zeros(self.potential.shape)
        return


    def reset_weights(self):
        self.feedforward_weights = self.weights_magnitude * (torch.rand(self.feedforward_weights.shape) * 2 - 1) * self.feedforward_mask
        self.feedback_weights = self.weights_magnitude * (torch.rand(self.feedback_weights.shape) * 2 - 1)
        self.bias = self.weights_magnitude * (torch.rand(self.bias.shape) * 2 - 1)
        return


    def set_ff_weights(self, new_weights):
        assert new_weights.shape == self.feedforward_weights.shape, 'Wrong shape, got ' + str(new_weights.shape) + ', expected' + str(self.feedforward_weights.shape)
        self.feedforward_weights = new_weights
        return


    def set_fb_weights(self, new_weights):
        assert new_weights.shape == self.feedback_weights.shape, 'Wrong shape, got ' + str(new_weights.shape) + ', expected' + str(self.feedback_weights.shape)
        self.feedback_weights = new_weights
        return


    def set_bias(self, new_bias):
        assert new_bias.shape == self.bias.shape, 'Wrong shape, got ' + str(new_bias.shape) + ', expected' + str(self.bias.shape)
        self.bias = new_bias
        return


    def set_mode(self, mode):
        if mode == 'train':
            self.visible_neurons = torch.cat((self.input_neurons, self.output_neurons))
            self.mode = 'train'
        elif mode == 'test':
            self.visible_neurons = self.input_neurons
            self.mode = 'test'
        else:
            print('Mode should be one of "train" or "test"')
            raise AttributeError
        return


    ### Misc
    def save(self, path=None):
        if path is None and self.save_path is not None:
            save_path = self.save_path
        elif path is not None:
            save_path = os.getcwd() + path
        else:
            raise FileNotFoundError

        hdf5_file = tables.open_file(save_path, mode='w')
        weights_ff = hdf5_file.create_array(hdf5_file.root, 'ff_weights', self.feedforward_weights.data.numpy())
        weights_fb = hdf5_file.create_array(hdf5_file.root, 'fb_weights', self.feedback_weights.data.numpy())
        bias = hdf5_file.create_array(hdf5_file.root, 'bias', self.feedforward_weights.data.numpy())
        hdf5_file.close()
        return


    def import_weights(self, path):
        hdf5_file = tables.open_file(path, mode='r')
        self.set_ff_weights(torch.tensor((hdf5_file.root['ff_weights'][:])))
        self.set_fb_weights(torch.tensor((hdf5_file.root['fb_weights'][:])))
        self.set_bias(torch.tensor((hdf5_file.root['bias'][:])))
        hdf5_file.close()
        return


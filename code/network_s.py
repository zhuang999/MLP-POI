import torch
import torch.nn as nn
from enum import Enum
import time
import numpy as np
from utils import *
import scipy.sparse as sp
import math
from mlp_ib import TriMixer, TriMixer_adj, MultiLayerPerceptron, MixerBlock, LinearBlock

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Rnn(Enum):
    """ The available RNN units """

    RNN = 0
    GRU = 1
    LSTM = 2

    @staticmethod
    def from_string(name):
        if name == 'rnn':
            return Rnn.RNN
        if name == 'gru':
            return Rnn.GRU
        if name == 'lstm':
            return Rnn.LSTM
        raise ValueError('{} not supported in --rnn'.format(name))


class RnnFactory():
    """ Creates the desired RNN unit. """

    def __init__(self, rnn_type_str):
        self.rnn_type = Rnn.from_string(rnn_type_str)

    def __str__(self):
        if self.rnn_type == Rnn.RNN:
            return 'Use pytorch RNN implementation.'
        if self.rnn_type == Rnn.GRU:
            return 'Use pytorch GRU implementation.'
        if self.rnn_type == Rnn.LSTM:
            return 'Use pytorch LSTM implementation.'

    def is_lstm(self):
        return self.rnn_type in [Rnn.LSTM]

    def create(self, hidden_size):
        if self.rnn_type == Rnn.RNN:
            return nn.RNN(hidden_size, hidden_size) 
        if self.rnn_type == Rnn.GRU:
            return nn.GRU(hidden_size, hidden_size)
        if self.rnn_type == Rnn.LSTM:
            return nn.LSTM(hidden_size, hidden_size)

        
class MLPMixer(nn.Module):
    """ MLPMixer RNN: Applies weighted average using spatial and tempoarl data in combination
    of user embeddings to the output of a generic RNN unit (RNN, GRU, LSTM).
    """

    def __init__(self, input_size, user_count, hidden_size, f_t, f_s, rnn_factory, lambda_loc, lambda_user, use_weight,
                 graph, spatial_graph, friend_graph, use_graph_user, use_spatial_graph, interact_graph, graph_nx, args):
        super().__init__()
        self.input_size = input_size  
        self.user_count = user_count
        self.hidden_size = hidden_size
        self.f_t = f_t  # function for computing temporal weight
        self.f_s = f_s  # function for computing spatial weight

        self.lambda_loc = lambda_loc
        self.lambda_user = lambda_user
        self.use_weight = use_weight
        self.use_graph_user = use_graph_user
        self.use_spatial_graph = use_spatial_graph

        self.I = identity(graph.shape[0], format='coo')
        self.graph = sparse_matrix_to_tensor(
            calculate_random_walk_matrix((graph * self.lambda_loc + self.I).astype(np.float32)))

        self.spatial_graph = spatial_graph
        if interact_graph is not None:
            self.interact_graph = sparse_matrix_to_tensor(calculate_random_walk_matrix(
                interact_graph))  # (M, N)
        else:
            self.interact_graph = None

        self.encoder = nn.Embedding(
            input_size, hidden_size)  # location embedding
        # self.time_encoder = nn.Embedding(24 * 7, hidden_size)  # time embedding
        self.user_encoder = nn.Embedding(
            user_count, hidden_size)  # user embedding
        self.rnn = rnn_factory.create(hidden_size) 
        self.mixer = TriMixer(20, hidden_size) #args.sequence_length
        self.mixer1 = TriMixer(20, hidden_size)
        self.mixer2 = TriMixer(20, hidden_size)
        self.mixer_adj = TriMixer_adj(100)
        #self.mlp = MultiLayerPerceptron(1, 1)
        self.mlp = LinearBlock(hidden_size)
        self.mixer_blocks = torch.nn.ModuleList()
        self.num_layers = 3
        for _ in range(self.num_layers):
            self.mixer_blocks.append(
                    MixerBlock(hidden_size)
                )
        self.mlpmixer = MixerBlock(hidden_size)
        self.mlpmixer1 = MixerBlock(hidden_size)
        self.mlpmixer2 = MixerBlock(hidden_size)
        self.position = nn.Embedding(args.sequence_length, hidden_size)
        self.drop = nn.Dropout(0.5)
        self.graph_nx = graph_nx
        self.fc = nn.Linear(2 * hidden_size, input_size)
        self.fc1 = nn.Linear(2 * hidden_size, input_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)

        self.emb = torch.nn.Parameter(torch.Tensor(1, hidden_size))
        # self.emb = nn.Embedding(1, embed_dim, padding_idx=0)
        # self.emb_sl = nn.Embedding(2, embed_dim, padding_idx=0)
        # self.emb_tu = nn.Embedding(2, embed_dim, padding_idx=0)
        # self.emb_tl = nn.Embedding(2, embed_dim, padding_idx=0)

    def Loss_l2(self):
        base_params = dict(self.named_parameters())
        loss_l2 = 0.
        count = 0
        for key, value in base_params.items():
            if 'bias' not in key and 'pre_model' not in key:
                loss_l2 += torch.sum(value ** 2)
                count += value.nelement()
        return loss_l2

    def forward(self, x, x_real, x_adj, indexs, t, t_slot, s_real, s, y_t, y_t_slot, y_s, h, active_user):
        seq_len, user_len = x.size()
        seq_pad_len, _, adj_len = x_adj.size()
        x_emb = self.encoder(x)
        adj_emb = self.encoder(x_adj)

        
        # if self.use_graph_user:
        #     # I_f = identity(self.friend_graph.shape[0], format='coo')
        #     # friend_graph = (self.friend_graph * self.lambda_user + I_f).astype(np.float32)
        #     # friend_graph = calculate_random_walk_matrix(friend_graph)
        #     # friend_graph = sparse_matrix_to_tensor(friend_graph).to(x.device)
        #     friend_graph = self.friend_graph.to(x.device)
        #     # AX
        #     user_emb = self.user_encoder(torch.LongTensor(
        #         list(range(self.user_count))).to(x.device))
        #     user_encoder_weight = torch.sparse.mm(friend_graph, user_emb).to(
        #         x.device)  # (user_count, hidden_size)
        #
        #     if self.use_weight:
        #         user_encoder_weight = self.user_gconv_weight(
        #             user_encoder_weight)
        #     p_u = torch.index_select(
        #         user_encoder_weight, 0, active_user.squeeze())
        # else:
        #     p_u = self.user_encoder(active_user)  # (1, user_len, hidden_size)
        #     # (user_len, hidden_size)
        #     p_u = p_u.view(user_len, self.hidden_size)

        p_u = self.user_encoder(active_user)  # (1, user_len, hidden_size)
        p_u = p_u.view(user_len, self.hidden_size)
        # # AX,GCN
        # graph = self.graph.to(x.device)
        # loc_emb = self.encoder(torch.LongTensor(
        #     list(range(self.input_size - 2))).to(x.device))
        # encoder_weight = torch.sparse.mm(graph, loc_emb).to(
        #     x.device)  # (input_size, hidden_size)
        # encoder_weight = torch.cat([encoder_weight, self.encoder.weight[-2:]],dim=0) # (input_size, hidden_size)
        #
        # if self.use_spatial_graph:
        #     spatial_graph = (self.spatial_graph *
        #                      self.lambda_loc + self.I).astype(np.float32)
        #     spatial_graph = calculate_random_walk_matrix(spatial_graph)
        #     spatial_graph = sparse_matrix_to_tensor(
        #         spatial_graph).to(x.device)  # sparse tensor gpu
        #     encoder_weight += torch.sparse.mm(spatial_graph,
        #                                       loc_emb).to(x.device)
        #     encoder_weight /= 2 
        #
        # new_x_emb = []
        # for i in range(seq_len):
        #     # (user_len, hidden_size)
        #     temp_x = torch.index_select(encoder_weight, 0, x[i])
        #     new_x_emb.append(temp_x)
        # x_emb = torch.stack(new_x_emb, dim=0)
        #
        # new_adj_emb = []
        # for tt in range(seq_pad_len):
        #     new_adj_seq = []
        #     for i in range(adj_len):
        #         #x_adj (seq_len, user_len, adj_len)
        #         temp_x = torch.index_select(encoder_weight, 0, x_adj[tt, :, i])
        #         new_adj_seq.append(temp_x)
        #     new_adj_emb.append(torch.stack(new_adj_seq, dim=1))
        # x_adj_emb = torch.stack(new_adj_emb, dim=0)
        #
        #
        # # user-poi
        # loc_emb = self.encoder(torch.LongTensor(
        #     list(range(self.input_size - 2))).to(x.device))
        # encoder_weight = loc_emb
        # interact_graph = self.interact_graph.to(x.device)
        # encoder_weight_user = torch.sparse.mm(
        #     interact_graph, encoder_weight).to(x.device)
        #
        # user_preference = torch.index_select(
        #     encoder_weight_user, 0, active_user.squeeze()).unsqueeze(0)
        # # print(user_preference.size())
        # user_loc_similarity = torch.exp(
        #     -(torch.norm(user_preference - x_emb, p=2, dim=-1))).to(x.device)
        # user_loc_similarity = user_loc_similarity.permute(1, 0)


        x_emb = self.mlpmixer(x_emb, t)
        out_w = self.mlpmixer1(x_emb, t)
        
        out_pu = torch.zeros(seq_len, user_len, 2 *
                             self.hidden_size, device=x.device)
        for i in range(seq_len):
            # (user_len, hidden_size * 2)
            out_pu[i] = torch.cat([out_w[i], p_u], dim=1)

        output = self.fc(out_pu)  # (seq_len, user_len, loc_count)

        

        s_distance = torch.zeros(seq_len, seq_len, user_len, device=x.device)+ 1e-10
        for i in range(seq_len):
            sum_w = torch.zeros(user_len, device=x.device)
            for j in range(i+1):
                cos_mask = torch.cosine_similarity(output_s[i], output_s[j], dim=-1) > 0
                cos_sim = torch.cosine_similarity(output_s[i], output_s[j], dim=-1) * cos_mask
                a = torch.exp(cos_sim) + 1e-10
                #a = torch.exp(-(torch.norm(output_s[i] - output_s[j], p=2, dim=-1))) + 1e-10
                s_distance[i,j] = a
                sum_w += a
            s_distance[i] /= sum_w


        return output, None, s_distance


'''
~~~ h_0 strategies ~~~
Initialize RNNs hidden states
'''


def create_h0_strategy(hidden_size, is_lstm):
    if is_lstm:
        return LstmStrategy(hidden_size, FixNoiseStrategy(hidden_size), FixNoiseStrategy(hidden_size))
    else:
        return FixNoiseStrategy(hidden_size)


class H0Strategy():

    def __init__(self, hidden_size):
        self.hidden_size = hidden_size

    def on_init(self, user_len, device):
        pass

    def on_reset(self, user):
        pass

    def on_reset_test(self, user, device):
        return self.on_reset(user)


class FixNoiseStrategy(H0Strategy):
    """ use fixed normal noise as initialization """

    def __init__(self, hidden_size):
        super().__init__(hidden_size)
        mu = 0
        sd = 1 / self.hidden_size
        self.h0 = torch.randn(self.hidden_size, requires_grad=False) * sd + mu

    def on_init(self, user_len, device):
        hs = []
        for i in range(user_len):
            hs.append(self.h0)
        # (1, 200, 10)
        return torch.stack(hs, dim=0).view(1, user_len, self.hidden_size).to(device)

    def on_reset(self, user):
        return self.h0


class LstmStrategy(H0Strategy):
    """ creates h0 and c0 using the inner strategy """

    def __init__(self, hidden_size, h_strategy, c_strategy):
        super(LstmStrategy, self).__init__(hidden_size)
        self.h_strategy = h_strategy
        self.c_strategy = c_strategy

    def on_init(self, user_len, device):
        h = self.h_strategy.on_init(user_len, device)
        c = self.c_strategy.on_init(user_len, device)
        return h, c

    def on_reset(self, user):
        h = self.h_strategy.on_reset(user)
        c = self.c_strategy.on_reset(user)
        return h, c

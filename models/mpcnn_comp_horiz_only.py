import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


class MPCNNCompHorizOnly(nn.Module):

    def __init__(self, n_word_dim, n_holistic_filters, n_per_dim_filters, filter_widths, hidden_layer_units, num_classes, dropout, ext_feats, attention):
        super(MPCNNCompHorizOnly, self).__init__()
        self.arch = 'mpcnn_comp_horiz_only'
        self.n_word_dim = n_word_dim
        self.n_holistic_filters = n_holistic_filters
        self.n_per_dim_filters = n_per_dim_filters
        self.filter_widths = filter_widths
        self.ext_feats = ext_feats
        self.attention = attention
        holistic_conv_layers = []
        per_dim_conv_layers = []

        self.in_channels = n_word_dim if not attention else 2*n_word_dim

        for ws in filter_widths:
            if np.isinf(ws):
                continue

            holistic_conv_layers.append(nn.Sequential(
                nn.Conv1d(self.in_channels, n_holistic_filters, ws),
                nn.Tanh()
            ))

            per_dim_conv_layers.append(nn.Sequential(
                nn.Conv1d(self.in_channels, self.in_channels * n_per_dim_filters, ws, groups=self.in_channels),
                nn.Tanh()
            ))

        self.holistic_conv_layers = nn.ModuleList(holistic_conv_layers)
        self.per_dim_conv_layers = nn.ModuleList(per_dim_conv_layers)

        # compute number of inputs to first hidden layer
        COMP_1_COMPONENTS_HOLISTIC, COMP_1_COMPONENTS_PER_DIM, COMP_2_COMPONENTS = 2 + n_holistic_filters, 2 + self.in_channels, 2
        EXT_FEATS = 4 if ext_feats else 0
        n_feat_h = 3 * len(self.filter_widths) * COMP_2_COMPONENTS
        n_feat = n_feat_h + EXT_FEATS

        self.final_layers = nn.Sequential(
            nn.Linear(n_feat, hidden_layer_units),
            nn.Tanh(),
            nn.Dropout(dropout),
            nn.Linear(hidden_layer_units, num_classes),
            nn.LogSoftmax(1)
        )

    def concat_attention(self, sent1, sent2):
        sent1_transposed = sent1.transpose(1, 2)
        attention_dot = torch.bmm(sent1_transposed, sent2)
        sent1_norms = torch.norm(sent1_transposed, p=2, dim=2, keepdim=True)
        sent2_norms = torch.norm(sent2, p=2, dim=1, keepdim=True)
        attention_norms = torch.bmm(sent1_norms, sent2_norms)
        attention_matrix = attention_dot / attention_norms

        attention_weight_vec1 = F.softmax(attention_matrix.sum(2), 1)
        attention_weight_vec2 = F.softmax(attention_matrix.sum(1), 1)
        attention_weighted_sent1 = attention_weight_vec1.unsqueeze(1).expand(-1, self.n_word_dim, -1) * sent1
        attention_weighted_sent2 = attention_weight_vec2.unsqueeze(1).expand(-1, self.n_word_dim, -1) * sent2
        attention_emb1 = torch.cat((attention_weighted_sent1, sent1), dim=1)
        attention_emb2 = torch.cat((attention_weighted_sent2, sent2), dim=1)
        return attention_emb1, attention_emb2

    def _get_blocks_for_sentence(self, sent):
        block_a = {}
        block_b = {}
        for ws in self.filter_widths:
            if np.isinf(ws):
                sent_flattened, sent_flattened_size = sent.contiguous().view(sent.size(0), 1, -1), sent.size(1) * sent.size(2)
                block_a[ws] = {
                    'max': F.max_pool1d(sent_flattened, sent_flattened_size).view(sent.size(0), -1),
                    'min': F.max_pool1d(-1 * sent_flattened, sent_flattened_size).view(sent.size(0), -1),
                    'mean': F.avg_pool1d(sent_flattened, sent_flattened_size).view(sent.size(0), -1)
                }
                continue

            holistic_conv_out = self.holistic_conv_layers[ws - 1](sent)
            block_a[ws] = {
                'max': F.max_pool1d(holistic_conv_out, holistic_conv_out.size(2)).contiguous().view(-1, self.n_holistic_filters),
                'min': F.max_pool1d(-1 * holistic_conv_out, holistic_conv_out.size(2)).contiguous().view(-1, self.n_holistic_filters),
                'mean': F.avg_pool1d(holistic_conv_out, holistic_conv_out.size(2)).contiguous().view(-1, self.n_holistic_filters)
            }

            per_dim_conv_out = self.per_dim_conv_layers[ws - 1](sent)
            block_b[ws] = {
                'max': F.max_pool1d(per_dim_conv_out, per_dim_conv_out.size(2)).contiguous().view(-1, self.in_channels, self.n_per_dim_filters),
                'min': F.max_pool1d(-1 * per_dim_conv_out, per_dim_conv_out.size(2)).contiguous().view(-1, self.in_channels, self.n_per_dim_filters)
            }
        return block_a, block_b

    def _algo_1_horiz_comp(self, sent1_block_a, sent2_block_a):
        comparison_feats = []
        for pool in ('max', 'min', 'mean'):
            for ws in self.filter_widths:
                x1 = sent1_block_a[ws][pool]
                x2 = sent2_block_a[ws][pool]
                batch_size = x1.size()[0]
                comparison_feats.append(F.cosine_similarity(x1, x2).contiguous().view(batch_size, 1))
                comparison_feats.append(F.pairwise_distance(x1, x2))
        return torch.cat(comparison_feats, dim=1)


    def forward(self, sent1, sent2, ext_feats=None):
        # Attention
        if self.attention:
            sent1, sent2 = self.concat_attention(sent1, sent2)

        # Sentence modeling module
        sent1_block_a, sent1_block_b = self._get_blocks_for_sentence(sent1)
        sent2_block_a, sent2_block_b = self._get_blocks_for_sentence(sent2)

        # Similarity measurement layer
        feat_h = self._algo_1_horiz_comp(sent1_block_a, sent2_block_a)
        combined_feats = [feat_h, ext_feats] if self.ext_feats else [feat_h]
        feat_all = torch.cat(combined_feats, dim=1)

        preds = self.final_layers(feat_all)
        return preds

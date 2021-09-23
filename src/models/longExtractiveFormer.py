import math

from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.utils.checkpoint
from torch.nn import CrossEntropyLoss, MSELoss
from torch.nn import functional as F
from models.reinforced_utils import  DocumentEncoder, SentenceExtractor
from models.longExtractiveFormerAttention import LongFormerAttention
from models.neural import MultiHeadedAttention
from pytorch_transformers import BertModel, BertConfig

from typing import Union, List
from models.neural import gelu

class LongFormerConfig(BertConfig):

    def __init__(self, attention_window: Union[List[int], int] = 10, sep_token_id: int = 2, section_size=100, is_decoder=False, **kwargs):
        """
        attention_window: number of sentences to cover in the attention
        """
        super().__init__(sep_token_id=sep_token_id, **kwargs)
        if type(attention_window) is int:
            attention_window = [attention_window] * self.num_attention_heads
        self.attention_window = attention_window
        # print('attention window size:', attention_window)
        self.pad_token_id = 1
        self.bos_token_id = 0
        self.eos_token_id = 2
        self.is_decoder = is_decoder
        self.section_size = section_size          # fix set the section size properly


class PositionalEncoding(nn.Module):
    def __init__(self, config):
        self.config = config
        max_len = self.config.max_position_embeddings
        self.dim = self.config.hidden_size
        dropout = self.config.hidden_dropout_prob
        pe = torch.zeros(max_len, self.dim)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, self.dim, 2, dtype=torch.float) * -(math.log(10000.0) / self.dim))
        pe[:, 0::2] = torch.sin(position.float() * div_term)
        pe[:, 1::2] = torch.cos(position.float() * div_term)
        pe = pe.unsqueeze(0)
        super(PositionalEncoding, self).__init__()
        self.register_buffer('pe', pe)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, emb, step=None):
        emb = emb * math.sqrt(self.dim)
        if step:
            emb = emb + self.pe[:, step][:, None, :]
        else:
            emb = emb + self.pe[:, :emb.size(1)]
        emb = self.dropout(emb)
        return emb

    def get_emb(self, emb):
        return self.pe[:, :emb.size(1)]


class PositionwiseFeedForward(nn.Module):
    """ A two-layer Feed-Forward-Network with residual layer norm.

    Args:
        d_model (int): the size of input for the first-layer of the FFN.
        d_ff (int): the hidden layer size of the second-layer
            of the FNN.
        dropout (float): dropout probability in :math:`[0, 1)`.
    """

    def __init__(self, config):  # d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.config = config
        self.w_1 = nn.Linear(self.config.hidden_size, self.config.intermediate_size)
        self.w_2 = nn.Linear(self.config.intermediate_size, self.config.hidden_size)
        self.layer_norm = nn.LayerNorm(self.config.hidden_size, eps=1e-6)
        self.actv = gelu
        self.dropout_1 = nn.Dropout(self.config.hidden_dropout_prob)
        self.dropout_2 = nn.Dropout(self.config.hidden_dropout_prob)

    def forward(self, x):
        inter = self.dropout_1(self.actv(self.w_1(self.layer_norm(x))))
        output = self.dropout_2(self.w_2(inter))
        return output + x


class LongTransformerEncoderLayer(nn.Module):
    def __init__(self, config):
        super(LongTransformerEncoderLayer, self).__init__()
        self.config = config
        # longFormerAttention
        self.self_attn = LongFormerAttention(self.config) # fix
        # full attention
        # self.self_attn = MultiHeadedAttention( self.config.num_attention_heads, self.config.hidden_size)
        self.feed_forward = PositionwiseFeedForward(self.config)
        self.layer_norm = nn.LayerNorm(self.config.hidden_size, eps=1e-6)
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)

    def forward(self, iter, inputs,
                attention_mask,
                layer_head_mask=None,
                is_index_masked=None,
                is_index_global_attn=None,
                is_global_attn=None):
        if iter != 0:
            input_norm = self.layer_norm(inputs)
        else:
            input_norm = inputs

        # full attention
        # attention_mask = attention_mask.unsqueeze(1)
        # context = self.self_attn(input_norm, input_norm, input_norm, mask=attention_mask)
        # longFormerAttention
        out = self.self_attn(input_norm,
                                attention_mask,
                                layer_head_mask,
                                is_index_masked,
                                is_index_global_attn,
                                is_global_attn)
        context = out[0]
        out = self.dropout(context) + inputs
        return self.feed_forward(out)


class LongExtTransformerEncoder(nn.Module):

    def __init__(self, config):
        super(LongExtTransformerEncoder, self).__init__()
        self.config = config
        self.pos_emb = PositionalEncoding(self.config)
        self.transformer_inter = nn.ModuleList(
            [LongTransformerEncoderLayer(self.config) for _ in range(self.config.num_hidden_layers)])
        self.section_embedding = nn.Embedding(config.section_size, config.hidden_size)
        self.position_embedding = nn.Embedding(500, config.hidden_size)
        self.length_embedding = nn.Embedding(200, config.hidden_size) # max_src_ntokens_per_sent is 50 fix
        self.documentEncoder = DocumentEncoder(1, self.config.hidden_size, self.config.hidden_size) # fix 1 is for the batch size
        self.sentenceExtractor = SentenceExtractor(self.config)
        self.layer_norm = nn.LayerNorm(self.config.hidden_size, eps=1e-6)

    def forward(self, sent_vecs, sent_lengths, sections, mask, extended_mask, media, references):
        is_index_masked = extended_mask < 0  # masking tokens (-10000) are true in and local(0) or global(+1000) attentions are False
        is_index_global_attn = extended_mask > 0  # indices with global attention are True others False
        is_global_attn = is_index_global_attn.flatten().any().item()  # True if at least one index with global attention




        sentence_embeddings = sent_vecs * mask[:, :, None].float()
        sentence_embeddings = self.pos_emb(sentence_embeddings)
        # inter sentence attention to identify important sentences relatively
        context_embeddings = sentence_embeddings
        for i in range(self.config.num_hidden_layers):
            context_embeddings = self.transformer_inter[i](i, context_embeddings,
                                                           attention_mask=extended_mask,
                                                           layer_head_mask=None,
                                                           is_index_masked=is_index_masked,
                                                           is_index_global_attn=is_index_global_attn,
                                                           is_global_attn=is_global_attn)
        context_embeddings = context_embeddings[:,:torch.sum(mask) ,:]
        sentence_embeddings = sentence_embeddings[:,:torch.sum(mask) ,:]
        sections = sections[:,:torch.sum(mask)]

        context_embeddings = self.layer_norm(context_embeddings)
        section_embedding = self.section_embedding(sections)
        document_embedding = self.documentEncoder(sentence_embeddings)
        position_embedding = self.position_embedding(torch.arange(0, sections.shape[1]).to(sections.device))
        # print(sent_lengths)
        length_embedding = self.length_embedding(sent_lengths)

        sent_scores = self.sentenceExtractor(context_embeddings, # fix
                                             position_embedding,
                                             section_embedding,
                                             context_embeddings,
                                             length_embedding,
                                             document_embedding, media, references)

        return sent_scores.squeeze(-1)

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# --------------------------------------------------------


from typing import Tuple

import torch
import torch.nn as nn
from transformers.models.distilbert.modeling_distilbert import Transformer, TransformerBlock, MultiHeadSelfAttention
from ..merge import merge_source, crossget, merge_wavg, merge_attention_mask
from typing import Optional, Union 
import math
from transformers.modeling_utils import ModuleUtilsMixin 


class CrossGetDistilBertBlock(TransformerBlock):
    def init_margin(self, margin):
        self.margin = margin
   
    def forward(
        self,
        x: torch.Tensor,
        attn_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
    ) -> Tuple[torch.Tensor, ...]:
        """
        Parameters:
            x: torch.tensor(bs, seq_length, dim)
            attn_mask: torch.tensor(bs, seq_length)

        Returns:
            sa_weights: torch.tensor(bs, n_heads, seq_length, seq_length) The attention weights ffn_output:
            torch.tensor(bs, seq_length, dim) The output of the transformer block contextualization.
        """
        # Self-Attention
        sa_output = self.attention(
            query=x,
            key=x,
            value=x,
            mask=attn_mask,
            head_mask=head_mask,
            output_attentions=True,
        )
        ratio = self._tome_info["ratio"].pop()
        sa_output, metric ,sa_weights = sa_output  # (bs, seq_length, dim), (bs, n_heads, seq_length, seq_length)
    
        sa_output = self.sa_layer_norm(sa_output + x)  # (bs, seq_length, dim)

        if ratio < 1.0:
            merge, _ = crossget(
                ratio=ratio,
                metric=metric,
                class_token=self._tome_info["class_token"]
            )
            # weight = self._tome_info["size"] 
            sa_output, self._tome_info["size"] = merge_wavg(merge, sa_output, None)
            # print(attention_mask.shape)

            # attn_mask = torch.where(attn_mask.squeeze_() >= 0, 1, 0)
            attn_mask = merge_attention_mask(merge, attention_mask=attn_mask[..., None]).squeeze_()
        else:
            attn_mask = attn_mask


        # Feed Forward Network
        ffn_output = self.ffn(sa_output)  # (bs, seq_length, dim)
        ffn_output: torch.Tensor = self.output_layer_norm(ffn_output + sa_output)  # (bs, seq_length, dim)

        output = ffn_output
        if output_attentions:
            output = (attn_mask, sa_weights, output)
        return attn_mask, output 



class CrossGetDistilBertAttention(MultiHeadSelfAttention):

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        mask: torch.Tensor,
        head_mask: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
    ) -> Tuple[torch.Tensor, ...]:
        """
        Parameters:
            query: torch.tensor(bs, seq_length, dim)
            key: torch.tensor(bs, seq_length, dim)
            value: torch.tensor(bs, seq_length, dim)
            mask: torch.tensor(bs, seq_length)

        Returns:
            weights: torch.tensor(bs, n_heads, seq_length, seq_length) Attention weights context: torch.tensor(bs,
            seq_length, dim) Contextualized layer. Optional: only if `output_attentions=True`
        """
        bs, q_length, dim = query.size()
        k_length = key.size(1)
        # assert dim == self.dim, f'Dimensions do not match: {dim} input vs {self.dim} configured'
        # assert key.size() == value.size()

        dim_per_head = self.dim // self.n_heads

        mask_reshp = (bs, 1, 1, k_length)

        def shape(x: torch.Tensor) -> torch.Tensor:
            """separate heads"""
            return x.view(bs, -1, self.n_heads, dim_per_head).transpose(1, 2)

        def unshape(x: torch.Tensor) -> torch.Tensor:
            """group heads"""
            return x.transpose(1, 2).contiguous().view(bs, -1, self.n_heads * dim_per_head)

        q = shape(self.q_lin(query))  # (bs, n_heads, q_length, dim_per_head)
        k = shape(self.k_lin(key))  # (bs, n_heads, k_length, dim_per_head)
        v = shape(self.v_lin(value))  # (bs, n_heads, k_length, dim_per_head)

        q = q / math.sqrt(dim_per_head)  # (bs, n_heads, q_length, dim_per_head)
        scores = torch.matmul(q, k.transpose(2, 3))  # (bs, n_heads, q_length, k_length)
        mask = (mask == 0).view(mask_reshp).expand_as(scores)  # (bs, n_heads, q_length, k_length)
        scores = scores.masked_fill(
            mask, torch.tensor(torch.finfo(scores.dtype).min)
        )  # (bs, n_heads, q_length, k_length)

        weights = nn.functional.softmax(scores, dim=-1)  # (bs, n_heads, q_length, k_length)
        weights = self.dropout(weights)  # (bs, n_heads, q_length, k_length)

        # Mask heads if we want to
        if head_mask is not None:
            weights = weights * head_mask

        context = torch.matmul(weights, v)  # (bs, n_heads, q_length, dim_per_head)
        context = unshape(context)  # (bs, q_length, dim)
        context = self.out_lin(context)  # (bs, q_length, dim)

        if output_attentions:
            return (context, k.mean(1), weights)
        else:
            return (context, k.mean(1))


def make_tome_class(transformer_class):
    class CrossGetTransformers(transformer_class, ModuleUtilsMixin):
        """
        Modifications:
        - Initialize r, token size, and token sources.
        """
        def forward(
            self,
            x: torch.Tensor,
            attn_mask: Optional[torch.Tensor] = None,
            head_mask: Optional[torch.Tensor] = None,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: Optional[bool] = None,
        ): 

            len_layers = len(self.layer)
            self._tome_info["ratio"] = [self.ratio if i in [
                len_layers - 1, 
                len_layers - 2,
                len_layers - 3,
                # len_layers - 6,
                # len_layers - 9,
            ] else 1.0 for i in range(len_layers) ]
            # self._tome_info["ratio"] = [self.ratio for i in range(len(self.layer))]
            all_hidden_states = () if output_hidden_states else None
            all_attentions = () if output_attentions else None

            hidden_state = x
            flops = 0
            for i, layer_module in enumerate(self.layer):
                if output_hidden_states:
                    all_hidden_states = all_hidden_states + (hidden_state,)

                layer_outputs = layer_module(
                    x=hidden_state, attn_mask=attn_mask, head_mask=head_mask[i], output_attentions=output_attentions
                )
                hidden_state = layer_outputs[-1]
                # B, T, _ = hidden_state.shape

                attn_mask = layer_outputs[0]
                # print('mask',attn_mask.shape)
                # print('x',hidden_state.shape)
                    
                flops += self.calculate_block_flop(hidden_state.shape)


            # Add last layer
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_state,)

            return hidden_state, all_hidden_states, all_attentions, flops
        
        def calculate_block_flop(self, shape):
            flops = 0
            _, N, C = shape
            mhsa_flops = 4*N*C*C + 2*N*N*C
            flops += mhsa_flops
            ffn_flops = 8*N*C*C
            flops += ffn_flops
            return flops


    return CrossGetTransformers



def apply_patch(
   model: Transformer, trace_source: bool = False, prop_attn: bool = True, margin=0.9, use_attn=False):
    """
    Applies CrossGet to this transformer. Afterward, set r using model.r.

    If you want to know the source of each token (e.g., for visualization), set trace_source = true.
    The sources will be available at model._tome_info["source"] afterward.

    For proportional attention, set prop_attn to True. This is only necessary when evaluating models off
    the shelf. For trianing and for evaluating MAE models off the self set this to be False.
    """
    CrossGetTransformers = make_tome_class(model.__class__)
    print('using', 'cross_get')

    model.__class__ = CrossGetTransformers
    model.ratio = 1.0 
    model.r=0.0
    
    # model.compress_method = 'tome' 
    model._tome_info = {
        "ratio": model.ratio,
        "margin":  [],
        "size": None,
        "use_attn": use_attn,
        "source": None,
        "trace_source": trace_source,
        "prop_attn": prop_attn,
        "class_token": True,
        "distill_token": False,
    }
    current_layer = 0
    margin = margin 
    num_layers = len(model.layer)
    margins = [0.9 - 0.25*(i/num_layers) for i in range(num_layers)]


    for module in model.modules():
        if isinstance(module, TransformerBlock):
            module.__class__ = CrossGetDistilBertBlock 
            module.init_margin(margins[current_layer])
            module._tome_info = model._tome_info
            current_layer +=1
        if isinstance(module, MultiHeadSelfAttention):
            module.__class__ = CrossGetDistilBertAttention 


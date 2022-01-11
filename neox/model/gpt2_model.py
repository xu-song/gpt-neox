# coding=utf-8
#
# Copyright 2021 Biderman et al. This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT model."""

import torch
import torch.nn as nn
from collections import defaultdict
from functools import partial
from neox.model.utils import Lambda, SequentialWrapper, recursive_setattr
from neox.model.norms import get_norm
from neox import mpu
from neox.model.transformer import (
    ParallelTransformerLayerPipe,
    NormPipe,
    ParallelLinearPipe,
    parallel_lm_logits,
    ParallelLinear,
)
from neox.model.gmlp import GMLPBlock
from neox.model.word_embeddings import EmbeddingPipe
from deepspeed.pipe import PipelineModule, LayerSpec, TiedLayerSpec
from typing import Union, List


def gpt2_attention_mask_func(attention_scores, ltor_mask):
    attention_scores.masked_fill_(ltor_mask, -10000.0)
    return attention_scores


def cross_entropy(output, labels, _fp16=False):
    """From pretrain_gpt2:forward_step()"""
    """
    if self.fp16_lm_cross_entropy:
        assert output.dtype == torch.half
        loss = mpu.vocab_parallel_cross_entropy(output, labels)
    else:
        loss = mpu.vocab_parallel_cross_entropy(output.float(), labels)
        return loss
    """
    labels, loss_mask = labels[0], labels[1]
    if _fp16:
        assert output.dtype == torch.half and loss_mask.dtype == torch.half
        losses = mpu.vocab_parallel_cross_entropy(output.contiguous(), labels)
    else:
        losses = mpu.vocab_parallel_cross_entropy(output.float().contiguous(), labels)
    loss_mask = loss_mask.view(-1)
    loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()
    return loss


def _pre_transformer_block(args):
    # used instead of a lambda layer to pass outputs of the word embedding to the transformer block
    # using a custom function means we don't have to have this _inference mode which makes everything tricky
    in_inference = len(args) == 3
    in_train = len(args) == 2
    # data format change for hidden_states to avoid explicit tranposes : [b s h] --> [s b h]
    if in_inference:
        # we need to add a container to cache `presents` from each layer's forward pass
        # inputs/outputs are now (hidden_states, layer_past, presents, attention_mask)
        fn = lambda x: (x[0].transpose(0, 1).contiguous(), x[1], torch.Tensor(), *x[2:])
    elif in_train:
        fn = lambda x: (x[0].transpose(0, 1).contiguous(), *x[1:])
    else:
        raise ValueError("Incorrect number of args in `_pre_transformer_block`")
    return fn(args)


def _post_transformer_block(args):
    # used instead of a lambda layer to pass outputs of the transformer block to the final layer
    # using a custom function means we don't have to have this _inference mode which makes everything tricky
    in_inference = len(args) == 4
    in_train = len(args) == 2
    if in_inference:
        # we can get rid of the mask / pasts now
        # from (hidden_states, layer_past, presents, attention_mask)
        # to (hidden_states.T, presents)
        fn = lambda x: (x[0].transpose(0, 1).contiguous(), x[2])
    elif in_train:
        # Undo data format change and drop mask
        fn = lambda x: x[0].transpose(0, 1).contiguous()
    else:
        raise ValueError("Incorrect number of args in `_post_transformer_block`")
    return fn(args)


class GPT2ModelPipe(PipelineModule, torch.nn.Module):
    """
    GPT2Model adapted for pipeline parallelism.

    In order to work with Deepspeed's PipelineModule, the model must be expressible as a sequence of layers (like a sequential module).

    This is done in `init_specs`, which creates a list of LayerSpec objects for each layer in the model, based on the
    yaml config specified at startup.

    Arguments:

        neox_args: NeoXArguments object containing the model configuration.
        num_tokentypes: Deprecated argument leftover from megatron. TODO: remove [deprecated]
        parallel_output: If True, the logits will be parallelized across model parallel partitions when returned.
                         If False, the logits will be gathered across ranks.
        topology: A deepspeed 3D topology object specifying the data / model / pipe parallel layout.
        inference: Whether to launch the model in inference mode. TODO: expand on this - what does it mean specifically?
        get_key_value: Whether to cache key value pairs during inference.
    """

    def __init__(
        self,
        neox_args,
        parallel_output: bool = True,
        topology=None,
        inference: bool = False,
        get_key_value: bool = True,
    ):
        # initialize variables
        self.neox_args = neox_args
        self._inference = inference
        self.get_key_value = get_key_value if inference else False
        self.parallel_output = parallel_output
        self.hidden_size = self.neox_args.hidden_size
        self.__topology__ = topology

        # initialize layerspecs - this is where the layers are built
        self.specs = self.init_specs()

        # initialize loss function
        loss_fn = partial(cross_entropy, _fp16=self.neox_args.fp16_lm_cross_entropy)
        if self.neox_args.checkpoint_activations:
            interval = self.neox_args.checkpoint_num_layers
        else:
            interval = 0
        super().__init__(
            layers=self.specs,
            loss_fn=loss_fn if not self._inference else None,
            topology=topology,
            activation_checkpoint_interval=interval,
            partition_method=neox_args.pipe_partition_method,
            checkpointable_layers=["GMLPBlock", "ParallelTransformerLayerPipe"],
        )

    def init_specs(self) -> List[LayerSpec]:
        """
        Initializes the list of LayerSpec objects that constitute the model.
        Basically a fancy nn.Sequential.

        Returns:
            List of LayerSpec objects.
        """
        weight_tying = not self.neox_args.no_weight_tying
        specs = []

        #####################
        # WORD EMBEDDINGS:  #
        #####################

        # input will be (input_ids, position_ids, attention_mask) in Training
        # and (input_ids, position_ids, attention_mask, layer_past) in Inference
        if weight_tying:
            # If weight tying is enabled, we need to use deepspeed's `TiedLayerSpec` class
            specs.append(
                TiedLayerSpec(
                    "embed",
                    EmbeddingPipe,
                    self.neox_args,
                    tied_weight_attr="word_embeddings_weight",
                )
            )
        else:
            # otherwise, we use the standard `LayerSpec` class
            specs.append(
                LayerSpec(
                    EmbeddingPipe,
                    self.neox_args,
                )
            )

        ##########################
        # PRE TRANSFORMER BLOCK  #
        ##########################

        # We append this function before the transformer block to rearrange the outputs of the embedding layer
        # Basically, it verifies the number of arguments is as expected, and, if in inference mode, initializes the cache
        # for key value pairs.
        #
        # NB: in inference, the attention mask always needs to be the *last* item in the args when being passed from
        # one stage to the next, because deepspeed is hacks on top of hacks.
        #
        # outputs are now
        #           Train: (hidden_states,  attention_mask)
        #           Inference: (hidden_states, layer_past, attention_mask)

        specs.append(_pre_transformer_block)

        ######################
        # TRANSFORMER LAYERS #
        ######################

        # initializes the transformer layers
        for i in range(self.neox_args.num_layers):
            layer_type = self.neox_args.attention_config[i]
            if layer_type in ["gmlp", "amlp"]:
                specs.append(
                    LayerSpec(
                        GMLPBlock,
                        layer_number=i,
                        neox_args=self.neox_args,
                        mask_fn=gpt2_attention_mask_func,
                    )
                )
            else:
                specs.append(
                    LayerSpec(
                        ParallelTransformerLayerPipe,
                        neox_args=self.neox_args,
                        attention_mask_func=gpt2_attention_mask_func,
                        layer_number=i,
                        get_key_value=self.get_key_value,
                    )
                )

        ###########################
        # POST TRANSFORMER BLOCK  #
        ###########################

        # Has a similar function as the pre transformer block.
        # Specifically, it drops the attention mask + layer_past and transposes the hidden states
        specs.append(_post_transformer_block)

        ###################
        # FINAL LAYERNORM #
        ###################

        # The final normalization layer after the transformer block.

        specs.append(LayerSpec(NormPipe, self.neox_args))

        # outputs are now
        #           Train: hidden_states
        #           Inference: (hidden_states, presents)

        ###########
        # LM HEAD #
        ###########

        # The Language Model Head.
        # Either tied to the embedding layer, or a linear layer.

        if weight_tying:

            def _logits_helper(embedding, lm_output):
                """Just a wrapper to massage inputs/outputs from pipeline."""
                if self._inference and len(lm_output) == 2:
                    hidden_states, presents = lm_output
                    logits = parallel_lm_logits(
                        hidden_states,
                        embedding.word_embeddings_weight,
                        self.parallel_output,
                    )
                    return logits, presents
                else:
                    logits = parallel_lm_logits(
                        lm_output,
                        embedding.word_embeddings_weight,
                        self.parallel_output,
                    )
                    return logits

            specs.append(
                TiedLayerSpec(
                    "embed",
                    EmbeddingPipe,
                    self.neox_args,
                    forward_fn=_logits_helper,
                    tied_weight_attr="word_embeddings_weight",
                )
            )
        else:
            specs.append(
                LayerSpec(
                    ParallelLinearPipe,
                    neox_args=self.neox_args,
                    parallel_output=self.parallel_output,
                    inference=self._inference,
                )
            )

        # output in training should just be logits
        # in inference it will be (logits, presents) (assuming get_key_value) is true
        return specs

    def _set_parallel_output(self, value):
        """
        Sets the parallel output value of the final layer to `value`
        """
        final_layer = list(self.forward_funcs)[-1]
        if isinstance(final_layer, (ParallelLinearPipe, ParallelLinear)):
            final_layer.final_linear.set_parallel_output(value)

    def inference_mode(self, cache=True):
        """
        Sets the model to inference mode.

        Specifically, recursively sets `get_key_value` to `True` for all layers if `cache` is `True`. (enables caching).
        Also sets `parallel_output` to `False` for the final layer, so the output is gathered across ranks

        """
        # first set caching to true if specified
        recursive_setattr(self.forward_funcs, "get_key_value", cache, assert_type=bool)
        # then set parallel output of the final layer to false so we don't have to gather the output manually
        self._set_parallel_output(False)

    def train_mode(self):
        """
        Sets the model to training mode.

        Specifically, recursively sets `get_key_value` to `False` for all layers.
        Also sets `parallel_output` to `True` for the final layer, so the output is *not* gathered across ranks.
        """
        # set caching to false
        recursive_setattr(self.forward_funcs, "get_key_value", False)
        # then set parallel output to true (more efficient training)
        self._set_parallel_output(True)

    def to_sequential(self) -> torch.nn.Sequential:
        """
        Transforms the PipelineModule to a plain nn.Sequential module
        Returns:
            torch.nn.Sequential: the sequential module
        """
        layers = []
        tied_layers = defaultdict(list)
        for n, spec in enumerate(self.specs):
            if isinstance(spec, TiedLayerSpec):
                if spec.key in tied_layers:
                    # receiver
                    layers.append(
                        Lambda(lambda x: spec.forward_fn(tied_layers[spec.key][0], x))
                    )
                else:
                    # owner
                    module = spec.build(log=False)
                    layers.append(module)
                    tied_layers[spec.key].append(module)
            elif isinstance(spec, LayerSpec):
                layers.append(spec.build(log=False))
            elif hasattr(spec, "__call__"):
                # check that it's a callable function
                layers.append(Lambda(spec))
            else:
                raise ValueError(f"Layer number {n} ({spec}) Not recognized")
        model = SequentialWrapper(
            layers,
            self.activation_checkpoint_interval,
            self.activation_checkpoint_func,
            parent_class_name=self.__class__.__name__,
        )
        return model

    @property
    def is_first_stage(self):
        """
        Returns true if this is the first stage of the pipeline
        """
        return self.stage_id == 0

    def get_word_embeddings(self):
        """
        Returns the word embeddings layer, if it's on the current stage
        """
        if self.is_first_stage:
            return self.forward_funcs[0]
#!/usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2020, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
""" contains unit tests to validate transformer quantization support """

import os
import json
import unittest
import torch
import numpy as np
import torch.nn as nn

from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_torch.utils import create_rand_tensors_given_shapes
from aimet_torch.meta import connectedgraph_utils
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper, StaticGridPerTensorQuantizer
from aimet_torch.quantsim import QuantizationSimModel
from aimet_torch import utils
#TODO: import libpymo from aimet_common
from aimet_torch.quantsim import libpymo
from aimet_torch.quantsim_config import quantsim_config as qsim_config
from aimet_torch.model_preparer import prepare_pt_transformer_for_quantsim


def generate_custom_quantsim_config(file_path: str):
    """
    Helper method to generate custom config for transformer models
    :return:
    """

    custom_quantsim_config = {
        "defaults": {
            "ops": {
                "is_output_quantized": "True"
            },
            "params": {
                "is_quantized": "True"
            }
        },
        "params": {},
        "op_type": {
            "LayerNorm": {
                "is_output_quantized": "True",
                "supported_kernels":
                    [
                        {
                            "activation": {
                                "bitwidth": 16,
                                "dtype": "float"
                            },
                            "param": {
                                "bitwidth": 16,
                                "dtype": "float"
                            }
                        }
                    ]
            },
            "GELU": {
                "is_output_quantized": "True",
                "supported_kernels":
                    [
                        {
                            "activation": {
                                "bitwidth": 16,
                                "dtype": "float"
                            },
                            "param": {
                                "bitwidth": 16,
                                "dtype": "float"
                            }
                        }
                    ]
            }
        },
        "supergroups": [],
        "model_input": {},
        "model_output": {}
    }

    with open(file_path, 'w') as f:
        json.dump(custom_quantsim_config, f)


class BertLayerNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-12):
        """
        Construct a layernorm module in the TF style (epsilon inside the square root).
         Refer https://github.com/huggingface/transformers/blob/c35d9d48d91282f7b7776751fc5630b1af1d3b97
        /pytorch_pretrained_bert/modeling.py#L220-L233
        """
        super(BertLayerNorm, self).__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.bias = nn.Parameter(torch.zeros(hidden_size))
        self.variance_epsilon = eps

    def forward(self, x):
        u = x.mean(-1, keepdim=True)
        s = (x - u).pow(2).mean(-1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.variance_epsilon)
        return self.weight * x + self.bias


class ModelWithBertCustomLayerNorm(nn.Module):
    """ Model with PyTorch LayerNorm """

    def __init__(self):
        super(ModelWithBertCustomLayerNorm, self).__init__()
        self.linear1 = torch.nn.Linear(4, 4)
        # default attribute -
        # eps = 1e-05 and elementwise_affine = True
        # parameters : weight and bias
        self.customln1 = torch.nn.LayerNorm(4)
        self.gelu = torch.nn.GELU()

    def forward(self, x):
        x = self.linear1(x)
        x = self.customln1(x)
        x = self.gelu(x)
        return x


class ConvGeLUNet(nn.Module):
    """ A block of Conv2d and GELU """

    def __init__(self):
        super(ConvGeLUNet, self).__init__()
        self.conv = nn.Conv2d(3, 3, 3)
        self.gelu = nn.GELU()

    def forward(self, x):
        return self.gelu(self.conv(x))


class ModelWithPtLayerNorm(nn.Module):
    """ Model with PyTorch LayerNorm """

    def __init__(self):
        super(ModelWithPtLayerNorm, self).__init__()
        self.linear1 = torch.nn.Linear(4, 4)
        # default attribute -
        # eps = 1e-05 and elementwise_affine = True
        # parameters : weight and bias
        self.ln1 = torch.nn.LayerNorm(4)
        self.relu1 = torch.nn.ReLU()

    def forward(self, x):
        x = self.linear1(x)
        x = self.ln1(x)
        x = self.relu1(x)
        return x


class TestQuantizationSimTransformers(unittest.TestCase):

    def test_gelu_static_quantization(self):
        """ Build a model with GELU activation and quantize """

        model = ConvGeLUNet()
        model.eval()
        input_shapes = (1, 3, 32, 32)
        inp_tensor_list = create_rand_tensors_given_shapes(input_shapes)

        def forward_pass(model, args):
            model.eval()
            with torch.no_grad():
                model(*inp_tensor_list)

        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf,
                                   dummy_input=inp_tensor_list)

        #  check quantizer added to gelu
        self.assertTrue(isinstance(sim.model.gelu, StaticGridQuantWrapper))

        # compute encodings
        sim.compute_encodings(forward_pass, None)

        # GELU output quantization is enabled by default.
        self.assertTrue(sim.model.gelu.output_quantizers[0].encoding)
        out_quantizer = sim.model.gelu.output_quantizers[0]
        self.assertTrue(out_quantizer.enabled)
        self.assertEqual(out_quantizer.round_mode, libpymo.RoundingMode.ROUND_NEAREST)
        self.assertEqual(out_quantizer.quant_scheme, QuantScheme.post_training_tf)
        self.assertEqual(out_quantizer.bitwidth, 8)

        forward_pass(sim.model, None)

    def test_compare_pt_with_bert_layernorm(self):
        """
        compare LayerNorm implementation in PyTorch and Huggingface pretrained BERT Model LayerNorm.
        :return:
        """

        # fix seed for test
        torch.manual_seed(10)
        random_input = torch.rand(1, 4, 4)

        # it must be noted here that default eps value used by
        # CustomBertLayerNorm is 1e-12
        # whereas pytorch implementation uses 1e-05
        # set this to same for comparision
        bert_m = BertLayerNorm(4, eps=1e-05)
        pt_m = nn.LayerNorm(4, eps=1e-05)
        bert_m.eval()
        pt_m .eval()

        # get output of layer and compare the two
        hugginface_bert_ln_output = bert_m(random_input).detach().numpy()
        pytorch_ln_output = pt_m(random_input).detach().numpy()
        self.assertTrue(np.allclose(hugginface_bert_ln_output, pytorch_ln_output))

    def test_pytorch_layernorm_quantization(self):
        """
        Build a model with layernorm and quantize
        # Refer :https://pytorch.org/docs/stable/generated/torch.nn.LayerNorm.html
        """

        torch.manual_seed(10)
        model = ModelWithPtLayerNorm()
        model.eval()

        random_input = torch.rand(1, 4, 4)

        def forward_pass(model, args):
            model.eval()
            with torch.no_grad():
                model(*random_input)

        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf,
                                   dummy_input=random_input)

        # Quantize
        sim.compute_encodings(forward_pass, None)

        #  check quantizer added to parameters of LayerNorm
        self.assertTrue(isinstance(sim.model.ln1.param_quantizers['weight'], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.ln1.param_quantizers['bias'], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.ln1.input_quantizers[0], StaticGridPerTensorQuantizer))

        # input / output quantizers for layernorm
        self.assertTrue(isinstance(sim.model.ln1.output_quantizers[0], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.ln1, StaticGridQuantWrapper))

        # validate config after compute encodings
        sim.compute_encodings(forward_pass, None)

        sim.export('./data/', 'two_input_model2', random_input)

        # LayerNorm output quantization is enabled by default
        self.assertTrue(sim.model.ln1.output_quantizers[0].encoding)
        out_quantizer = sim.model.ln1.output_quantizers[0]
        self.assertTrue(out_quantizer.enabled)
        self.assertEqual(out_quantizer.round_mode, libpymo.RoundingMode.ROUND_NEAREST)
        self.assertEqual(out_quantizer.quant_scheme, QuantScheme.post_training_tf)
        self.assertEqual(out_quantizer.bitwidth, 8)

        # LayerNorm input quantization is disabled by default
        # could override with custom config file
        self.assertFalse(sim.model.ln1.input_quantizers[0].encoding)

        #  gamma (weight) quantizer of LayerNorm is enabled by default
        self.assertTrue(sim.model.ln1.param_quantizers['weight'].encoding)
        weight_quantizer = sim.model.ln1.param_quantizers['weight']
        self.assertTrue(weight_quantizer.enabled)
        self.assertEqual(weight_quantizer.round_mode, libpymo.RoundingMode.ROUND_NEAREST)
        self.assertEqual(weight_quantizer.quant_scheme, QuantScheme.post_training_tf)
        self.assertEqual(weight_quantizer.bitwidth, 8)

        # beta (bias) quantizer of LayerNorm is disabled by default
        # override with custom config file
        self.assertFalse(sim.model.ln1.param_quantizers['bias'].encoding)
        forward_pass(sim.model, None)

    def test_custom_bert_layernorm_quantization_custom_hw_config(self):
        """
        Build a model with BERT huggingface custom layernorm and quantize with custom config
        """

        torch.manual_seed(10)
        model = ModelWithBertCustomLayerNorm()
        model.eval()

        file_path = "./data/quantsim_config.json"
        generate_custom_quantsim_config(file_path)

        random_input = torch.rand(1, 4, 4)

        def forward_pass(model, args):
            model.eval()
            with torch.no_grad():
                model(*random_input)

        qsim_config.ENFORCE_TARGET_DTYPE_BITWIDTH_CONFIG = True

        sim = QuantizationSimModel(model, quant_scheme=QuantScheme.post_training_tf,
                                   dummy_input=random_input,
                                   config_file='./data/quantsim_config.json')

        # validate config after compute encodings
        sim.compute_encodings(forward_pass, None)

        # validation rules:
        # AIMET supports overrides ONLY when a lower precision kernel is unavailable.
        # for example :
        # 1) (default) int 8, but only FP16 kernel is available for a given op type --> override supported
        # 2) (default) int 8, but only int 4 kernel is available is available for a given op type --> override not supported

        #  check quantizer added to parameters of LayerNorm
        self.assertTrue(isinstance(sim.model.customln1.param_quantizers['weight'], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.customln1.param_quantizers['bias'], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.customln1.input_quantizers[0], StaticGridPerTensorQuantizer))

        # input / output quantizers for layernorm
        self.assertTrue(isinstance(sim.model.customln1.output_quantizers[0], StaticGridPerTensorQuantizer))
        self.assertTrue(isinstance(sim.model.customln1, StaticGridQuantWrapper))

        # validate config after compute encodings
        sim.compute_encodings(forward_pass, None)

        # LayerNorm output quantization is enabled by default
        # override this with custom config (matches aic100_config.json)
        self.assertTrue(sim.model.customln1.output_quantizers[0].encoding)
        self.assertTrue(sim.model.customln1.output_quantizers[0].bitwidth == 8)
        self.assertTrue(sim.model.gelu.output_quantizers[0].data_type == QuantizationDataType.int)

        #  gamma (weight) quantizer of LayerNorm is enabled by default
        # override this with custom config (matches aic100_config.json)
        self.assertTrue(sim.model.customln1.param_quantizers['weight'].bitwidth == 16)
        self.assertTrue(sim.model.customln1.param_quantizers['weight'].data_type == QuantizationDataType.float)
        self.assertTrue(sim.model.customln1.param_quantizers['bias'].bitwidth == 16)
        self.assertTrue(sim.model.customln1.param_quantizers['bias'].data_type == QuantizationDataType.float)

        self.assertIsNone(sim.model.customln1.param_quantizers['weight'].encoding)
        self.assertIsNone(sim.model.customln1.param_quantizers['bias'].encoding)

        self.assertTrue(sim.model.gelu.output_quantizers[0].bitwidth == 8)
        self.assertTrue(sim.model.gelu.output_quantizers[0].data_type == QuantizationDataType.int)
        self.assertIsNotNone(sim.model.gelu.output_quantizers[0].encoding)

        # clear data dir that was used in this test
        qsim_config.ENFORCE_TARGET_DTYPE_BITWIDTH_CONFIG = False
        if os.path.exists(file_path):
           os.remove(file_path)

    def test_custom_quantizable_multi_head_attn_unit(self):
        """
        compare custom MHA (quantizable version) with torch.nn.MHA implementation
        Note : quantizable version is derived from PyTorch:
        https://github.com/pytorch/pytorch/blob/master/torch/nn/quantizable/modules/activation.py#L11
        + additional updates such as : remove quant/dequant logic , replace functional ops with modules.
        """

        seed = 10
        np.random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)

        embed_dim = 128
        num_heads = 8
        batch_size = 32
        seq_size = 27

        key = torch.rand(seq_size, batch_size, embed_dim)
        query = torch.rand(seq_size, batch_size, embed_dim)
        value = torch.rand(seq_size, batch_size, embed_dim)

        weights_q = torch.rand(embed_dim, embed_dim)
        weights_k = torch.rand(embed_dim, embed_dim)
        weights_v = torch.rand(embed_dim, embed_dim)

        weights_o = torch.rand(embed_dim, embed_dim)

        # 0. Simple calculation
        def SimpleMHA():
            _q = torch.matmul(query, weights_q.T).transpose(0, 1) # (b, s, d)
            _k = torch.matmul(key, weights_k.T).transpose(0, 1)
            _v = torch.matmul(value, weights_v.T).transpose(0, 1)
            # split heads
            def split_heads(v):
                return v.reshape(batch_size, seq_size*num_heads, embed_dim//num_heads)
            def merge_heads(v):
                return v.reshape(batch_size, seq_size, embed_dim)
            _q = split_heads(_q)
            _k = split_heads(_k)
            _v = split_heads(_v)
            mm1 = torch.matmul(_q, _k.transpose(-1, -2)) / (_v.size(-1) ** 0.5)
            w = nn.functional.softmax(mm1, dim=-1) #
            mm2 = torch.matmul(w, _v) # (b, seq_size*num_head)
            mm2 = merge_heads(mm2)
            out = torch.matmul(mm2, weights_o.T)
            return out, w
            # simple_outs = SimpleMHA() # not neccessary # for debug purpose

        # Need to correctly copy pre-trained weights (dummy in this test).
        # Quantized MHA uses linear_Q, Linear_K, Linear_V as opposed to in_proj_weight used in torch.nn.MHA.

        # 1. nn.MultiheadAttention
        nn_mha = nn.MultiheadAttention(embed_dim, num_heads, bias=False)
        with torch.no_grad():
            nn_mha.in_proj_weight.copy_(
                torch.cat([weights_q, weights_k, weights_v], dim=0),
            )
            nn_mha.out_proj.weight.copy_(
                weights_o,
            )
        nn_outputs = nn_mha(query, key, value)

        # 2. AIMET custom MHA
        from aimet_torch.transformers.activation import QuantizableMultiheadAttention
        nncq_mha = QuantizableMultiheadAttention(embed_dim, num_heads, bias=False)
        with torch.no_grad():
            nncq_mha.linear_Q.weight.copy_(weights_q)
            nncq_mha.linear_K.weight.copy_(weights_k)
            nncq_mha.linear_V.weight.copy_(weights_v)
            nncq_mha.out_proj.weight.copy_(weights_o)

        nncq_outputs = nncq_mha(query, key, value)

        for outputs in zip(nn_outputs, nncq_outputs):
            # only checking outputs (without attention_weights)
            for i in range(1, len(outputs)):
                self.assertTrue(np.allclose(outputs[0].detach().numpy(), outputs[i].detach()))

    def test_pt_ops_with_modules(self):
        """
        Test that validates auto replacement of PT MHA with quantizable MHA
        :return:
        """

        seed = 10
        np.random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)

        src = torch.rand(10, 32, 512)

        num_encoder_layers = 12
        default_decoder_layers = 6
        transformer_model = nn.Transformer(nhead=16, num_encoder_layers=num_encoder_layers)
        transformer_model.eval()

        for i in range(num_encoder_layers):
            self.assertTrue(isinstance(transformer_model.encoder.layers[i].self_attn, torch.nn.MultiheadAttention))

        for i in range(default_decoder_layers):
            self.assertTrue(isinstance(transformer_model.decoder.layers[i].self_attn, torch.nn.MultiheadAttention))

        # auto replace PyTorch MHA in given transformer layer with quantizable MHA
        from aimet_torch.transformers.activation import create_quantizable_multihead_attention, QuantizableMultiheadAttention
        utils.replace_modules_of_type1_using_constructor(transformer_model, torch.nn.MultiheadAttention,
                                                         create_quantizable_multihead_attention)

        # validate replacement is done for both encoder and decoder
        for i in range(num_encoder_layers):
            self.assertTrue(isinstance(transformer_model.encoder.layers[i].self_attn, QuantizableMultiheadAttention))

        for i in range(default_decoder_layers):
            self.assertTrue(isinstance(transformer_model.decoder.layers[i].self_attn, QuantizableMultiheadAttention))

        # check if forward pass after replacement works fine
        _ = transformer_model(src=src, tgt=src)

    def test_prepare_model_with_pytorch_transformer_layer_after_act_replacement(self):
        """
        Test that validates auto replacement of functional activation function in
        PyTorch nn.Transformer layer with modules.
        :return:
        """
        seed = 10
        np.random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)

        src = torch.rand(10, 32, 512)
        dummy_input = torch.rand(10, 32, 512)

        def forward_pass(model, args):
            model.eval()
            with torch.no_grad():
                model(dummy_input, dummy_input)

        num_encoder_layers = 12

        # start with a vanilla PyTorch transformer layer
        transformer_model = nn.Transformer(nhead=16, num_encoder_layers=num_encoder_layers)
        transformer_model.eval()

        from torch import fx
        # if torch fx is to be used for tracing of transformer models
        # it can be used on encoder or decoder layer as below
        # (not directly on nn.trasnformer layer because og torch implementation
        # limitation (uses a conditional check in the init)

        # symbolic_traced_encoder_model = fx.symbolic_trace(transformer_model.encoder)
        # encoder_model_transformed = model_preparer.prepare_model(transformer_model.encoder.eval())
        # symbolic_traced_decoder_model = fx.symbolic_trace(transformer_model.decoder)
        # decoder_model_transformed = model_preparer.prepare_model(transformer_model.decoder.eval())

        # auto replace functional activation with module for nn.Transformer layers
        prepare_pt_transformer_for_quantsim(transformer_model)

        # auto replace PyTorch MHA in given transformer layer with quantizable MHA
        from aimet_torch.transformers.activation import create_quantizable_multihead_attention
        utils.replace_modules_of_type1_using_constructor(transformer_model, torch.nn.MultiheadAttention,
                                                         create_quantizable_multihead_attention)

        ops_with_missing_modules = connectedgraph_utils.get_ops_with_missing_modules(transformer_model, (src, src))

        # create quantsim object on updated model
        sim = QuantizationSimModel(transformer_model, dummy_input=(src, src))

        # compute encodings (forward pass)
        sim.compute_encodings(forward_pass, None)

        # validate MHA layers have quantizers
        for i in range(num_encoder_layers):
            self.assertTrue(sim.model.encoder.layers[i].self_attn.linear_Q.output_quantizers[0].encoding)
            self.assertTrue(sim.model.encoder.layers[i].self_attn.linear_K.output_quantizers[0].encoding)
            self.assertTrue(sim.model.encoder.layers[i].self_attn.linear_V.output_quantizers[0].encoding)
            self.assertTrue(sim.model.encoder.layers[i].self_attn.matmul_1.output_quantizers[0].encoding)
            self.assertTrue(sim.model.encoder.layers[i].self_attn.matmul_2.output_quantizers[0].encoding)
            self.assertTrue(sim.model.encoder.layers[i].self_attn.softmax.output_quantizers[0].encoding)



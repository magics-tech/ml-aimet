# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
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

import pytest
import torch
import aimet_common.libpymo as libpymo

from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_torch.qc_quantize_op import LearnedGridQuantWrapper
from aimet_torch.tensor_quantizer import StaticGridPerTensorQuantizer, StaticGridPerChannelQuantizer,\
    StaticGridTensorQuantizer, LearnedGridTensorQuantizer

BUCKET_SIZE = 512


class TestTensorQuantizer:

    def test_get_stats_histogram_per_tensor(self):
        """
        test get_stats_histogram() for per tensor quantizer.
        """
        tensor = torch.randn(5, 5)
        quantizer = StaticGridPerTensorQuantizer(bitwidth=8, round_mode='nearest',
                                                 quant_scheme=QuantScheme.post_training_tf_enhanced,
                                                 use_symmetric_encodings=False, enabled_by_default=True)
        quantizer.update_encoding_stats(tensor)
        quantizer.compute_encoding()
        assert quantizer.encoding, "Encoding shouldn't be None."
        histograms = quantizer.get_stats_histogram()
        assert len(histograms) == 1
        for histogram in histograms:
            print(histogram)
            assert len(histogram) == BUCKET_SIZE

    def test_get_stats_histogram_per_channel(self):
        """
        test get_stats_histogram() for per channel quantizer.
        """
        tensor = torch.randn(10, 5)
        quantizer = StaticGridPerChannelQuantizer(bitwidth=8, round_mode='nearest',
                                                  quant_scheme=QuantScheme.post_training_tf_enhanced,
                                                  num_channels=5, use_symmetric_encodings=False,
                                                  enabled_by_default=True)
        quantizer.update_encoding_stats(tensor)
        quantizer.compute_encoding()
        assert quantizer.encoding, "Encoding shouldn't be None."
        histograms = quantizer.get_stats_histogram()
        assert len(histograms) == 5
        for histogram in histograms:
            assert len(histogram) == BUCKET_SIZE

    def test_get_stats_histogram_with_invalid_combination(self):
        """
        test get_stats_histogram() with invalid inputs.
        """
        # Encoding should be computed.
        quantizer = StaticGridTensorQuantizer(bitwidth=8, round_mode='nearest',
                                              quant_scheme=QuantScheme.post_training_tf_enhanced,
                                              use_symmetric_encodings=False, enabled_by_default=True)
        with pytest.raises(RuntimeError):
            quantizer.get_stats_histogram()

        # quant scheme should be TF-Enhanced.
        quantizer = StaticGridTensorQuantizer(bitwidth=8, round_mode='nearest',
                                              quant_scheme=QuantScheme.post_training_tf,
                                              use_symmetric_encodings=False, enabled_by_default=True)
        with pytest.raises(RuntimeError):
            quantizer.get_stats_histogram()

    def test_learned_grid_set_and_freeze_param_encoding(self):
        """
        test freeze_encoding() method for LearnedGridQuantWrapper.
        """
        conv = torch.nn.Conv2d(1, 32, 5)
        quant_wrapper = LearnedGridQuantWrapper(conv, round_mode='nearest',
                                                quant_scheme=QuantScheme.training_range_learning_with_tf_init,
                                                is_output_quantized=True, activation_bw=8,
                                                weight_bw=8, device='cpu')

        enc_old = libpymo.TfEncoding()
        enc_old.bw, enc_old.max, enc_old.min, enc_old.delta, enc_old.offset = 8, 0.5, -1, 1, 0.2

        enc_new = libpymo.TfEncoding()
        enc_new.bw, enc_new.max, enc_new.min, enc_new.delta, enc_new.offset = 8, 0.4, -0.98, 1, 0.2

        # Set encoding for output quantizer and freeze it.
        quant_wrapper.output_quantizers[0].encoding = enc_old
        quant_wrapper.output_quantizers[0].freeze_encoding()

        enc_cur = quant_wrapper.output_quantizers[0].encoding
        assert enc_cur.min == enc_old.min

        # set encoding one more time but can not set it since it is frozen.
        with pytest.raises(RuntimeError):
            quant_wrapper.output_quantizers[0].encoding = enc_new

        enc_cur = quant_wrapper.output_quantizers[0].encoding
        assert enc_cur.min == enc_old.min

        # Freeze encoding for input quantizer without initializing.
        with pytest.raises(RuntimeError):
            quant_wrapper.input_quantizers[0].freeze_encoding()

    def test_learned_grid_set_freeze_encoding(self):
        """
        test freeze_encoding() method for LearnedGridQuantWrapper.
        """
        conv = torch.nn.Conv2d(1, 32, 5)
        quant_wrapper = LearnedGridQuantWrapper(conv, round_mode='nearest',
                                                quant_scheme=QuantScheme.training_range_learning_with_tf_init,
                                                is_output_quantized=True, activation_bw=8,
                                                weight_bw=8, device='cpu')

        enc = libpymo.TfEncoding()
        enc.bw, enc.max, enc.min, enc.delta, enc.offset = 8, 0.5, -1, 0.01, 50

        # Set encoding for all - input, output and parameters quantizer.
        quant_wrapper.input_quantizers[0].enabled = True
        quant_wrapper.input_quantizers[0].encoding = enc
        quant_wrapper.param_quantizers['weight'].enabled = True
        quant_wrapper.param_quantizers['weight'].encoding = enc
        quant_wrapper.param_quantizers['bias'].enabled = True
        quant_wrapper.param_quantizers['bias'].encoding = enc
        quant_wrapper.output_quantizers[0].enabled = True
        quant_wrapper.output_quantizers[0].encoding = enc

        enc_cur = quant_wrapper.output_quantizers[0].encoding
        assert enc_cur.min == enc.min

        # Freeze encoding only for output quantizer.
        quant_wrapper.output_quantizers[0].freeze_encoding()

        inp = torch.rand((1, 1, 5, 5), requires_grad=True)
        optimizer = torch.optim.SGD(quant_wrapper.parameters(), lr=0.05, momentum=0.5)
        for _ in range(2):
            optimizer.zero_grad()
            out = quant_wrapper(inp)
            loss = out.flatten().sum()
            loss.backward()
            optimizer.step()

        # Check if the min and max parameters have changed.
        assert not quant_wrapper.input0_encoding_min.item() == -1
        assert not quant_wrapper.input0_encoding_max.item() == 0.5
        assert not quant_wrapper.weight_encoding_min.item() == -1
        assert not quant_wrapper.weight_encoding_max.item() == 0.5
        assert not quant_wrapper.bias_encoding_min.item() == -1
        assert not quant_wrapper.bias_encoding_max.item() == 0.5
        # For output quantizer, it should be same as before.
        assert quant_wrapper.output0_encoding_min.item() == -1
        assert quant_wrapper.output0_encoding_max.item() == 0.5

        # Check encoding.getter property.
        assert not quant_wrapper.input_quantizers[0].encoding.min == -1
        assert not quant_wrapper.input_quantizers[0].encoding.max == 0.5
        assert not quant_wrapper.param_quantizers["weight"].encoding.min == -1
        assert not quant_wrapper.param_quantizers["weight"].encoding.max == 0.5
        assert not quant_wrapper.param_quantizers["bias"].encoding.min == -1
        assert not quant_wrapper.param_quantizers["bias"].encoding.max == 0.5
        # For output quantizer, it should be same as before.
        assert quant_wrapper.output_quantizers[0].encoding.min == -1
        assert quant_wrapper.output_quantizers[0].encoding.max == 0.5

    def test_learned_grid_n_and_p_up_to_date(self):
        tensor_quantizer = LearnedGridTensorQuantizer(bitwidth=8,
                                                      round_mode="nearest",
                                                      quant_scheme=QuantScheme.training_range_learning,
                                                      use_symmetric_encodings=True,
                                                      enabled_by_default=True,
                                                      data_type=QuantizationDataType.int)

        assert tensor_quantizer.n() == 0.0
        assert tensor_quantizer.p() == 255.0

        tensor_quantizer.bitwidth = 16

        assert tensor_quantizer.n() == 0.0
        assert tensor_quantizer.p() == 65535.0

        tensor_quantizer.use_strict_symmetric = True

        assert tensor_quantizer.n() == 0.0
        assert tensor_quantizer.p() == 65534.0

    def test_learned_grid_update_encoding_invalid_input(self):
        tensor_quantizer = LearnedGridTensorQuantizer(bitwidth=8,
                                                      round_mode="nearest",
                                                      quant_scheme=QuantScheme.training_range_learning,
                                                      use_symmetric_encodings=True,
                                                      enabled_by_default=True,
                                                      data_type=QuantizationDataType.int)


        enc_new = libpymo.TfEncoding()
        enc_new.bw, enc_new.max, enc_new.min, enc_new.delta, enc_new.offset = 4, 0.4, -0.98, 1, 0.2

        with pytest.raises(RuntimeError):
            tensor_quantizer.encoding = enc_new

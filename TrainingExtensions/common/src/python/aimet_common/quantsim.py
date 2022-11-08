# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2020-2022, Qualcomm Innovation Center, Inc. All rights reserved.
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

""" Common utility for Quantization """

from typing import Union, Tuple, List
import numpy as np

from aimet_common.defs import QuantScheme, QuantizationDataType
import aimet_common.libpymo as libpymo
# Defined below is a quantization encoding format version, which will follow XX.YY.ZZ versioning as described below,
#
#    XX = Major Revision
#    YY = Minor Revision
#    ZZ = Patching version
#
# Change in major revision should indicate substantial change to the format, updates to minor version indicates
# additional information element being added to encoding format and might require update to fully consume the encodings.
# The patching version shall be updated to indicate minor updates to quantization simulation e.g. bug fix etc.
encoding_version = '0.5.1'
ALLOW_EXPERIMENTAL = False

def gate_min_max(min_val: float, max_val: float) -> Tuple[float, float]:
    """
    Gates min and max encoding values to retain zero in the range representation.
    Rules : min at maximum can be zero, max at minimum can be zero and
    if max and min are equal, adds epsilon to maintain range.
    :param min_val: min encoding value
    :param max_val: max encoding value
    :return: gated min and max values
    """

    epsilon = 1e-5
    # For per channel quantization
    if isinstance(min_val, np.ndarray):
        gated_min = np.clip(min_val, None, 0.0)
        gated_max = np.clip(max_val, 0.0, None)
        gated_max = np.clip(gated_max, gated_min + epsilon, None)
    else:
        gated_min = min(min_val, 0.0)
        gated_max = max(max_val, 0.0)
        gated_max = max(gated_max, gated_min + epsilon)

    return gated_min, gated_max


def calculate_delta_offset(min_val: Union[float, np.ndarray], max_val: Union[float, np.ndarray], bitwidth: int) -> \
        Union[Tuple[float, float], Tuple[List, List]]:
    """
    calculates delta and offset given min and max.
    :param min_val: min encoding value
    :param max_val: max encoding value
    :param bitwidth: bitwidth used for quantization
    :return: delta and offset values computed
    """
    min_val, max_val = gate_min_max(min_val, max_val)
    delta = (max_val - min_val) / (2 ** bitwidth - 1)

    if isinstance(delta, np.ndarray):
        offset = np.around(min_val/delta)
        delta = delta.tolist()
        offset = offset.tolist()
    else:
        offset = round(min_val / delta)

    return delta, offset


def recompute_grid_params(current_encoding: libpymo.TfEncoding, bitwidth: int,
                          use_symmetric_encoding: bool) -> libpymo.TfEncoding:
    """
    Recomputed the encoding grid params - min/max/offset and delta.
    :param current_encoding: Encoding associated with the quantizer as TfEncoding
    :param bitwidth: bit width configured for the quantizer
    :param use_symmetric_encoding: symmetric or asymmetric mode
    :return: updated encoding params as libpymo.TfEncoding type.
    """

    MIN_RANGE = 0.01
    min_val = min(0., current_encoding.min)
    max_val = max(0., current_encoding.max, (min_val + MIN_RANGE))
    updated_encoding = libpymo.TfEncoding()

    # check mode used to recompute delta and offset
    if use_symmetric_encoding:
        num_positive_steps = (2 ** (bitwidth - 1)) - 1
        abs_max_val = max(abs(max_val), abs(min_val))
        delta = abs_max_val / num_positive_steps
        offset = -(num_positive_steps + 1)
        # recompute min/max values
        min_val = delta * offset
        max_val = delta * num_positive_steps

    else:
        num_steps = (2 ** bitwidth) - 1
        delta = (max_val - min_val) / num_steps
        # @todo check zero point representation related code
        offset = round(min_val / delta)
        # recompute min/max values
        min_val = delta * offset
        max_val = min_val + delta * num_steps

    updated_encoding.bw = bitwidth
    updated_encoding.min = min_val
    updated_encoding.max = max_val
    updated_encoding.delta = delta
    updated_encoding.offset = offset

    return updated_encoding


def validate_quantsim_inputs(
        quant_scheme: Union[str, QuantScheme],
        rounding_mode: str,
        default_output_bw: int,
        default_param_bw: int,
        data_type: QuantizationDataType = QuantizationDataType.int
):
    """
    Perform sanity checks on inputs to QuantSim
    :param quant_scheme: Quantization scheme. Supported options are 'tf_enhanced' or 'tf' or 'percentile'
                         or using Quant Scheme Enum QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced
                         or QuantScheme.post_training_percentile
    :param rounding_mode: Rounding mode. Supported options are 'nearest' or 'stochastic'
    :param default_output_bw: Default bitwidth (4-31) to use for quantizing layer inputs and outputs
    :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters
    :param data_type: Data type of the quantized values (int or float).
    """
    # sanity checks
    if quant_scheme not in ('tf_enhanced', 'tf', 'percentile') and not isinstance(quant_scheme, QuantScheme):
        raise ValueError('Parameter quantization mode is not a valid selection. Valid selections are '
                         'tf, tf_enhanced, percentile, QuantScheme.post_training_tf, '
                         'QuantScheme.post_training_tf_enhanced, QuantScheme.post_training_percentile')

    if rounding_mode not in ('nearest', 'stochastic'):
        raise ValueError('Parameter round mode is not a valid selection. Valid selections are nearest or '
                         'stochastic')

    if default_param_bw < 4 or default_param_bw > 32:
        raise ValueError('Default bitwidth for parameters must be between 4 and 32, not ' + str(default_param_bw))

    if default_output_bw < 4 or default_output_bw > 32:
        raise ValueError('Activation bitwidth must be between 4 and 32, not ' + str(default_output_bw))

    if ALLOW_EXPERIMENTAL:
        if data_type == QuantizationDataType.float and default_output_bw not in [8, 16]:
            raise ValueError(
                'float data_type can only be used when default_output_bw set to 8 or 16, not ' + str(default_output_bw))

        if data_type == QuantizationDataType.float and default_param_bw not in [8, 16]:
            raise ValueError(
                'float data_type can only be used when default_param_bw set to 8 or 16, not ' + str(default_param_bw))

    else:
        if data_type == QuantizationDataType.float and default_output_bw != 16:
            raise ValueError(
                'float data_type can only be used when default_output_bw set to 16, not ' + str(default_output_bw))

        if data_type == QuantizationDataType.float and default_param_bw != 16:
            raise ValueError(
                'float data_type can only be used when default_param_bw set to 16, not ' + str(default_param_bw))

# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2018-2022, Qualcomm Innovation Center, Inc. All rights reserved.
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
""" Custom PyTorch Op for quantizing weights and activations """

import abc
import contextlib
from enum import Enum
from typing import Dict, Tuple, Union, List
import os
import torch
from torch import nn

import aimet_common.libpymo as libpymo
from aimet_common.utils import AimetLogger
from aimet_common.defs import QuantScheme, QuantizationDataType, MAP_ROUND_MODE_TO_PYMO, MAP_SCALE_MODE_TO_PYMO
from aimet_torch.custom import custom_tensor_utils
from aimet_torch import utils
from aimet_torch.tensor_quantizer import StaticGridPerTensorQuantizer, StaticGridPerChannelQuantizer, TensorQuantizer, \
    LearnedGridTensorQuantizer, ParameterQuantizer
import aimet_torch.quantsim_straight_through_grad as ste
import pdb

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)


class _ConstantTensor:
    def __init__(self, device: Union[str, torch.device]):
        self.eps = torch.tensor([1e-5], device=device)
        self.zero = torch.tensor([0.0], device=device)


_device_map: Dict[torch.device, _ConstantTensor] = {}
def _constant_tensor(device: Union[str, torch.device]) -> _ConstantTensor:
    """
    Factory function to generate constant tensor or return cached object
    :param device: device str ('cpu', 'cuda', ...) or torch.device
    :return: Constant tensor
    """
    if isinstance(device, str):
        device = torch.device(device)
    if device in _device_map:
        return _device_map[device]
    ret = _ConstantTensor(device)
    _device_map[device] = ret
    return ret


class QcQuantizeOpMode(Enum):
    """
    Mode for the Quantization Ops
    """
    PASSTHROUGH = 1
    ANALYSIS = 2
    ACTIVE = 3


QUANTIZER_TYPE_INPUT = 'input'
QUANTIZER_TYPE_OUTPUT = 'output'
TF_ENHANCED_USE_DOWNSAMPLING = bool(int(os.environ.get("AIMET_TFE_USE_DOWNSAMPLING", "0")))
TF_ENHANCED_OFFSET_FACTOR = 0
TF_ENHANCED_STRIDE_FACTOR = 2


def tensor_quantizer_factory(bitwidth: int, round_mode: str, scale_mode: str, quant_scheme: QuantScheme,
                             use_symmetric_encodings: bool, enabled_by_default: bool,
                             data_type: QuantizationDataType = QuantizationDataType.int):
    """
    Instantiates TensorQuantizer depending on the quant_scheme
    :param bitwidth: Quantization bitwidth
    :param round_mode: Rounding mode (e.g. Nearest)
    :param quant_scheme: Quantization scheme (e.g. Range Learning)
    :param use_symmetric_encodings: True if symmetric encoding is used.  False otherwise.
    :param enabled_by_default: True if quantization of tensor is enabled.  False otherwise.
    :return: An instance of StaticGridPerTensorQuantizer
    """

    if quant_scheme in (QuantScheme.post_training_tf_enhanced, QuantScheme.post_training_tf,
                        QuantScheme.post_training_percentile):

        tensor_quantizer = StaticGridPerTensorQuantizer(bitwidth, round_mode, scale_mode, quant_scheme,
                                                        use_symmetric_encodings, enabled_by_default,
                                                        data_type=data_type)

    elif quant_scheme in (QuantScheme.training_range_learning_with_tf_init,
                          QuantScheme.training_range_learning_with_tf_enhanced_init):

        tensor_quantizer = LearnedGridTensorQuantizer(bitwidth, round_mode,scale_mode, quant_scheme, use_symmetric_encodings,
                                                      enabled_by_default, data_type)
    else:
        raise AssertionError("Unsupported quant_scheme: " + str(quant_scheme))

    return tensor_quantizer


class QcQuantizeStandAloneBase(nn.Module):
    """
    Base class for the quantization custom ops
    """

    def __init__(self, activation_bw, round_mode, quant_scheme, is_symmetric, data_type):
        """
        Constructor
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_symmetric: Symmetric or asymmetric quantization
        """
        super(QcQuantizeStandAloneBase, self).__init__()
        self.output_quantizers = [tensor_quantizer_factory(activation_bw, round_mode,
                                                           quant_scheme,
                                                           is_symmetric,
                                                           enabled_by_default=True,
                                                           data_type=data_type)]

        self._mode = QcQuantizeOpMode.ANALYSIS

    @abc.abstractmethod
    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

    def set_output_bw(self, output_bw: int):
        """
        Sets (overrides) the output bitwidth for a particular layer
        :param output_bw: Bitwidth from (4-32)
        :return: None
        """
        self.output_quantizers[0].bitwidth = output_bw

    def set_mode(self, mode):
        """
        Sets a working mode for the custom op
        :param mode:
        :return:
        """
        self._mode = mode

    def _quantize_activation(self, tensor_quantizers, tensors_to_quantize):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param tensor_quantizers: Tensor quantizers to use for updating stats or quantizing
        :param tensors_to_quantize: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        outputs = []
        for index, input_tensor in enumerate(tensors_to_quantize):

            if self._mode is QcQuantizeOpMode.ANALYSIS:

                tensor_quantizers[index].update_encoding_stats(input_tensor)
                output = input_tensor

            elif self._mode is QcQuantizeOpMode.ACTIVE:
                # if we are not in training, then only nearest rounding should be used
                # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
                if self.training:
                    round_mode = tensor_quantizers[index].round_mode
                else:
                    round_mode = libpymo.RoundingMode.ROUND_NEAREST
                output = tensor_quantizers[index].quantize_dequantize(input_tensor, round_mode, self, 'output')

            else:
                output = input_tensor

            outputs.append(output)

        # Flatten if there is only one output - which is by far the most common case
        if len(outputs) == 1:
            outputs = outputs[0]

        return outputs


class QcQuantizeWrapper(nn.Module):
    """
    Base class for the quantization custom ops
    """

    # pylint: disable=too-many-arguments
    def __init__(self, module_to_wrap: nn.Module, weight_bw: int, activation_bw: int, round_mode, scale_mode,
                 quant_scheme: QuantScheme, is_output_quantized=True, is_symmetric=False, num_inputs=1, num_outputs=1,
                 data_type: QuantizationDataType = QuantizationDataType.int):
        """
        Constructor
        :param module_to_wrap: Module that will be wrapped with this custom op
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_output_quantized: True if output tensor quantizer is enabled.  False otherwise.
        :param is_symmetric: True if symmetric encoding is used.  False otherwise.
        :param num_inputs: Number of inputs for this module
        :param num_outputs: Number of outputs for this module
        """
        super(QcQuantizeWrapper, self).__init__()

        if data_type == QuantizationDataType.float and weight_bw != 16:
            raise ValueError('weight_bw=16 is the only supported configuration with floating point data type')

        if data_type == QuantizationDataType.float and activation_bw != 16:
            raise ValueError('activation_bw=16 is the only supported configuration with floating point data type')

        self.output_quantizers = [tensor_quantizer_factory(activation_bw, round_mode, scale_mode,
                                                           quant_scheme,
                                                           is_symmetric,
                                                           enabled_by_default=is_output_quantized,
                                                           data_type=data_type)
                                  for _ in range(num_outputs)]

        self._mode = QcQuantizeOpMode.ANALYSIS
        self._module_to_wrap = module_to_wrap

        # Create quantizer for each parameter and compute encodings
        self.param_quantizers = {}
        for name, _ in module_to_wrap.named_parameters():
            _logger.debug("Adding quantizer for parameter: %s", name)
            self.param_quantizers[name] = tensor_quantizer_factory(weight_bw, round_mode, scale_mode,
                                                                   quant_scheme,
                                                                   is_symmetric,
                                                                   enabled_by_default=True,
                                                                   data_type=data_type)

        # Create quantizer for layer input
        self.input_quantizers = [tensor_quantizer_factory(activation_bw, round_mode, scale_mode,
                                                          quant_scheme,
                                                          is_symmetric,
                                                          enabled_by_default=False,
                                                          data_type=data_type)
                                 for _ in range(num_inputs)]

        self._quant_scheme = quant_scheme

    def get_named_parameters(self):
        """
        Yields parameter name and parameter
        """
        # is_replica is an
        if hasattr(self, '_is_replica') and self._is_replica:
            # pylint: disable = protected-access
            for name, param in self._module_to_wrap._former_parameters.items():
                yield name, param

        else:
            for name, param in self._module_to_wrap.named_parameters():
                yield name, param


    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self._module_to_wrap, name)

    @abc.abstractmethod
    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

    def set_output_bw(self, output_bw: int):
        """
        Sets (overrides) the output bitwidth for a particular layer
        :param output_bw: Bitwidth from (4-32)
        :return: None
        """
        self.output_quantizers[0].bitwidth = output_bw

    def set_mode(self, mode):
        """
        Sets a working mode for the custom op
        :param mode: Mode for the Quantization Ops. Can be ANALYSIS or ACTIVE
        """
        self._mode = mode

    def enable_param_quantizers(self, enabled: bool,
                                param_name_to_exclude: Union[None, Tuple[str]] = ("bias", )) -> None:
        """
        Note: By default, bias quantization is disabled.

        Sets enabled flag for parameter quantizers.
        :param enabled: Enabled flag.
        :param param_name_to_exclude: Param name to be excluded.
        """
        if not param_name_to_exclude:
            param_name_to_exclude = []

        for param_name, param_quantizer in self.param_quantizers.items():
            if not param_name in param_name_to_exclude:
                param_quantizer.enabled = enabled

    def enable_input_quantizers(self, enabled: bool) -> None:
        """
        Sets enabled flag for input quantizers.
        :param enabled: Enabled flag.
        """
        for quantizer in self.input_quantizers:
            quantizer.enabled = enabled

    def enable_output_quantizers(self, enabled: bool) -> None:
        """
        Sets enabled flag for output quantizers.
        :param enabled: Enabled flag.
        """
        for quantizer in self.output_quantizers:
            quantizer.enabled = enabled

    def enable_activation_quantizers(self, enabled: bool) -> None:
        """
        Sets enabled flag for both input and output quantizers.
        :param enabled: Enabled flag.
        """
        self.enable_input_quantizers(enabled)
        self.enable_output_quantizers(enabled)

    def reset_encodings(self):
        """
        Reset encoding stats and set encodings to None for all quantizers
        """
        for quantizer in self.input_quantizers:
            quantizer.reset_encoding_stats()

        for quantizer in self.output_quantizers:
            quantizer.reset_encoding_stats()

        for param_quantizer in self.param_quantizers.values():
            param_quantizer.reset_encoding_stats()

    def enable_per_channel_quantization(self):
        """
        Changes all parameter quantizers (if any) to per-channel mode
        Todo: This needs to change to an abstract method in the future. The purpose to add this method right now
        is to enable per-channel quantization for both only supported wrappers. Supported for static-grid and not
        supported for learned-grid
        """

    def set_activation_encoding(self, module_name: str, activation_encodings: Dict):
        """
        Set encoding for activations from encodings dictionary
        :param module_name: name of module
        :param activation_encodings: activation encodings dictionary
        :return:
        """

        def _set_quantizer_encodings(type_of_quantizer: str, quantizers: List[TensorQuantizer]):
            """
            Sets bitwidth, symmetric mode and encodings for quantizer of type input or output
            :param type_of_quantizer: input or output
            :param quantizers: input or output quantizers
            """
            if type_of_quantizer in activation_encodings[module_name]:
                encodings = activation_encodings[module_name][type_of_quantizer]
                # The number of quantizers and encodings might not be same, suppose 1st output quantizer is disabled out of 4,
                # number of encodings will be 3 but number of output quantizers will still be 4
                for index, quantizer in enumerate(quantizers):
                    ind = str(index)
                    if ind in encodings and not quantizer.enabled:
                        raise RuntimeError("The quantsim passed for loading encodings does not have the same configuration as the"
                                           "quantsim which was used to export the encodings")

                    if quantizer.enabled and encodings[ind]['dtype'] == 'int':
                        encoding, is_symmetric = utils.create_encoding_from_dict(encodings[ind])
                        quantizer.bitwidth = encoding.bw
                        quantizer.use_symmetric_encodings = is_symmetric
                        quantizer.encoding = encoding
                    elif quantizer.enabled and encodings[ind]['dtype'] == 'float':
                        quantizer.bitwidth = encodings[ind]['bitwidth']
                        quantizer.data_type = QuantizationDataType.float

        _logger.info("Setting quantization encodings for activation quantizers of: %s", module_name)

        _set_quantizer_encodings(QUANTIZER_TYPE_INPUT, self.input_quantizers)
        _set_quantizer_encodings(QUANTIZER_TYPE_OUTPUT, self.output_quantizers)

    def set_param_encoding(self, module_name: str, param_encodings: Dict):
        """
        Set encoding for parameter from encodings dictionary
        :param module_name: name of module
        :param param_encodings: parameter encodings dictionary
        """
        for orig_param_name, param_quantizer in self.param_quantizers.items():
            param_name = module_name + '.' + orig_param_name
            if param_name in param_encodings:
                encodings = []
                if param_encodings[param_name][0]['dtype'] == 'int':
                    is_symmetric = False
                    for encoding_dict in param_encodings[param_name]:
                        if encoding_dict['dtype'] == 'int':
                            encoding, is_symmetric = utils.create_encoding_from_dict(encoding_dict)
                            encodings.append(encoding)
                    param_quantizer.bitwidth = encodings[0].bw
                    param_quantizer.use_symmetric_encodings = is_symmetric
                    param_quantizer.encoding = encodings
                elif param_encodings[param_name][0]['dtype'] == 'float':
                    param_quantizer.bitwidth = param_encodings[param_name][0]['bitwidth']
                    param_quantizer.data_type = QuantizationDataType.float
                else:
                    raise RuntimeError("Data type does not match int or float in encodings file")

                _logger.info("Setting quantization encodings for parameter: %s", param_name)

    def freeze_param_encoding(self, module_name: str, param_encodings: Dict):
        """
        Freeze encodings for parameter
        :param module_name: name of module
        :param param_encodings: parameter encodings dictionary
        """
        for orig_param_name, param_quantizer in self.param_quantizers.items():
            param_name = module_name + '.' + orig_param_name
            if param_name in param_encodings:
                param_quantizer.freeze_encoding()
                _logger.info("Freezing quantization encodings for parameter: %s", param_name)


class StaticGridQuantWrapper(QcQuantizeWrapper):
    """ A custom PyTorch module that derives from QcQuantizeWrapper and quantizes modules """

    # pylint: disable=too-many-arguments
    def __init__(self, module_to_wrap: nn.Module, weight_bw: int, activation_bw: int, round_mode, scale_mode, quant_scheme,
                 is_output_quantized=True, is_symmetric=False, num_inputs=1, num_outputs=1,
                 data_type: QuantizationDataType = QuantizationDataType.int):
        """
        Constructor
        :param module_to_wrap: Module that will be wrapped with this custom op
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. TF Enhanced)
        :param is_output_quantized: True if output tensor quantizer is enabled.  False otherwise.
        :param is_symmetric: True if symmetric encoding is used.  False otherwise.
        :param num_inputs: Number of inputs for this module
        :param num_outputs: Number of outputs for this module
        """
        # Translate round mode and quant scheme into pymo types prior to initializing super()
        round_mode = MAP_ROUND_MODE_TO_PYMO[round_mode]
        scale_mode = MAP_SCALE_MODE_TO_PYMO[scale_mode]

        super(StaticGridQuantWrapper, self).__init__(module_to_wrap, weight_bw, activation_bw, round_mode,scale_mode, quant_scheme,
                                                     is_output_quantized, is_symmetric, num_inputs,
                                                     num_outputs, data_type)

    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        # Quantize the inputs
        # pdb.set_trace()
        torch_inputs = custom_tensor_utils.to_torch_tensor(inputs)
        quantized_inputs = self._quantize_activation(self.input_quantizers, torch_inputs)

        # Quantize the parameters
        shadow_params = self._quantize_dequantize_params()

        # Save quantized parameters tensors for backward pass and perform custom backward pass for gating parameters grad
        # during backward pass
        quantized_inputs = SteGatingFuncForParameters.apply(self, *quantized_inputs)

        quantized_inputs = custom_tensor_utils.to_custom_tensor(inputs, quantized_inputs)
        # clone() the outputs of Custom function to avoid incorrect gradient calculation for in-place modification
        # of view (view is created since Custom function's forward return input as-is)
        quantized_inputs = [inp.clone() if isinstance(inp, torch.Tensor) else inp for inp in quantized_inputs]

        # Call the forward of the wrapped module
        wrapped_output = self._module_to_wrap(*quantized_inputs)

        self._restore_shadow_params(shadow_params)

        # Quantize the outputs
        if not isinstance(wrapped_output, (List, Tuple)):
            wrapped_output = [wrapped_output]

        torch_outputs = custom_tensor_utils.to_torch_tensor(wrapped_output)
        output = self._quantize_activation(self.output_quantizers, torch_outputs)
        output = custom_tensor_utils.to_custom_tensor(wrapped_output, output)

        if len(output) == 1:
            output = output[0]

        return output

    def _restore_shadow_params(self, shadow_params):
        # Restore the parameters
        for name, param in self.get_named_parameters():
            param.data.zero_()
            param.data.add_(shadow_params[name].data)

    def _quantize_dequantize_params(self):
        """
        Quantizes and dequantizes a parameter
        """
        def quantize_dequantize(name: str, param: torch.nn.Parameter, is_replica: bool):
            """
            Quantize dequantize param
            """
            # Store current weight for use later on
            shadow_params[name] = param.detach().clone()

            param_quantizer = self.param_quantizers[name]
            if param_quantizer.enabled:

                # If we are in training mode with quant-sim nodes, then we want to calculate encodings for the
                # parameters in every pass
                if self._module_to_wrap.training or param_quantizer.encoding is None:
                    param_quantizer.reset_encoding_stats()
                    param_quantizer.update_encoding_stats(param.data)
                    # Todo: Remove this once we know adjusting parameters encodings will not be an issue.
                    if param_quantizer.quant_scheme == QuantScheme.post_training_percentile:
                        param_quantizer.set_percentile_value(100)
                    param_quantizer.compute_encoding()

                # if we are not in training, then only nearest rounding should be used
                # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
                if self.training:
                    round_mode = param_quantizer.round_mode
                    scale_mode = param_quantizer.scale_mode
                else:
                    ############## Original setting################
                    # round_mode = libpymo.RoundingMode.ROUND_NEAREST 
                    ############## Original setting################

                    round_mode = param_quantizer.round_mode
                    scale_mode = param_quantizer.scale_mode

                if is_replica:
                    param.data = param_quantizer.quantize_dequantize(param.data.clone(), round_mode)
                else:
                    # pdb.set_trace()

                    param.data = param_quantizer.quantize_dequantize(param.data, round_mode, scale_mode)

        shadow_params = {}

        for name, param in self.get_named_parameters():
            is_replica = False
            if hasattr(self, '_is_replica') and self._is_replica:
                is_replica = True
            quantize_dequantize(name, param, is_replica=is_replica)

        return shadow_params

    def compute_weight_encodings(self):
        """
        Compute quantized model weight encoding.
        :return: weight_encoding value (libpymo.TfEncoding type)
        """

        if 'weight' in self.param_quantizers:
            return self.param_quantizers['weight'].encoding

        return None

    def compute_encoding(self):
        """
        Compute the quantization encoding for this layer
        """
        for quantizer in self.input_quantizers:
            quantizer.compute_encoding()

        for quantizer in self.output_quantizers:
            quantizer.compute_encoding()

    def set_percentile_value(self, percentile_value: float):
        """
        Set the percentile value to be used while computing encodings
        """
        for quantizer in self.input_quantizers:
            quantizer.set_percentile_value(percentile_value)

        for quantizer in self.output_quantizers:
            quantizer.set_percentile_value(percentile_value)

    def _quantize_activation(self, tensor_quantizers, tensors_to_quantize):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param tensor_quantizers: Tensor quantizers to use for updating stats or quantizing
        :param tensors_to_quantize: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """
        # pdb.set_trace()
        outputs = []
        for index, input_tensor in enumerate(tensors_to_quantize):
            assert len(tensor_quantizers) > index, \
                f"Not enough tensor quantizers ({len(tensor_quantizers)}) allocated"

            if isinstance(input_tensor, utils.dtypes_to_ignore_for_quantization) or\
                    input_tensor.dtype in utils.torch_dtypes_to_ignore_for_quantization or\
                    not tensor_quantizers[index].enabled:
                # Do not quantize tensors of integer or bool data type or if the quantizer is disabled.
                outputs.append(input_tensor)
                continue

            if not isinstance(input_tensor, torch.Tensor):
                error_msg = f'Expecting quantize activation input of type torch.Tensor but got {type(input_tensor)}'
                _logger.error(error_msg)
                raise AssertionError(error_msg)

            if self._mode is QcQuantizeOpMode.ANALYSIS:
                if TF_ENHANCED_USE_DOWNSAMPLING and self._quant_scheme == QuantScheme.post_training_tf_enhanced:
                    # Update stats using downsampled output to speed up tf enhanced
                    input_tensor_flatten = input_tensor.reshape(-1)
                    downsampled_input = \
                        input_tensor_flatten[TF_ENHANCED_OFFSET_FACTOR::TF_ENHANCED_STRIDE_FACTOR].contiguous()
                    tensor_quantizers[index].update_encoding_stats(downsampled_input)
                else:
                    tensor_quantizers[index].update_encoding_stats(input_tensor)

                output = input_tensor

            elif self._mode is QcQuantizeOpMode.ACTIVE:
                # if we are not in training, then only nearest rounding should be used
                # else we should use whatever the user desires (i.e.. stochastic rounding is a valid option)
                if self.training:
                    round_mode = tensor_quantizers[index].round_mode
                    scale_mode = tensor_quantizers[index].scale_mode
                else:
                    ############## Original setting################
                    # round_mode = libpymo.RoundingMode.ROUND_NEAREST 
                    ###############################################

                    round_mode = tensor_quantizers[index].round_mode
                    scale_mode = tensor_quantizers[index].scale_mode

                output = tensor_quantizers[index].quantize_dequantize(input_tensor, round_mode, scale_mode)

            else:
                output = input_tensor

            outputs.append(output)

        return outputs

    def enable_per_channel_quantization(self):
        """
        Changes all parameter quantizers (if any) to per-channel mode
        """
        new_param_quant_dict = {}
        for param_name, param in self._module_to_wrap.named_parameters():
            param_quantizer = self.param_quantizers[param_name]
            channel_axis = 0
            if isinstance(self._module_to_wrap, (torch.nn.ConvTranspose1d,
                                                 torch.nn.ConvTranspose2d,
                                                 torch.nn.ConvTranspose3d)):
                if len(param.shape) > 1:
                    channel_axis = 1

            per_channel_quantizer = StaticGridPerChannelQuantizer(param_quantizer.bitwidth, param_quantizer.round_mode,
                                                                  param_quantizer.quant_scheme,
                                                                  param_quantizer.use_symmetric_encodings,
                                                                  num_channels=param.shape[channel_axis],
                                                                  enabled_by_default=param_quantizer.enabled,
                                                                  ch_axis=channel_axis,
                                                                  data_type=param_quantizer.data_type)

            new_param_quant_dict[param_name] = per_channel_quantizer
        self.param_quantizers = new_param_quant_dict


# Temporarily added for backwards compatibility
QcPostTrainingWrapper = StaticGridQuantWrapper


class LearnedGridQuantWrapper(QcQuantizeWrapper):
    """
    Learns Min and Max for Encodings of Enabled quantizers for a layer
    """

    # pylint: disable = too-many-arguments
    def __init__(self, module_to_wrap: nn.Module, weight_bw: int, activation_bw: int, round_mode: str,
                 quant_scheme: QuantScheme, device: torch.device, is_output_quantized: bool = True,
                 is_symmetric: bool = False, num_inputs=1, num_outputs=1,
                 data_type: QuantizationDataType = QuantizationDataType.int):
        """
        Constructor
        :param module_to_wrap: Module that will be wrapped with this custom op
        :param weight_bw: Quantization bitwidth for weights
        :param activation_bw: Quantization bitwidth for activations
        :param round_mode: Rounding mode (e.g. Nearest)
        :param quant_scheme: Quantization scheme (e.g. Range Learning)
        :param is_output_quantized: True if output tensor quantizer is enabled.  False otherwise.
        :param is_symmetric: True if symmetric encoding is used.  False otherwise.
        :param device: device on which model is
        :param num_inputs: Number of inputs for this module
        :param num_outputs: Number of outputs for this module
        """

        if data_type != QuantizationDataType.int:
            raise ValueError('Only QuantizationDataType.int is supported for LearnedGridQuantWrapper')

        super(LearnedGridQuantWrapper, self).__init__(module_to_wrap, weight_bw, activation_bw, round_mode,
                                                      quant_scheme, is_output_quantized, is_symmetric, num_inputs,
                                                      num_outputs, data_type)

        self.device = device
        self._initialize_trainable_parameters_and_tensor_quantizers(num_inputs, num_outputs)

    def _initialize_trainable_parameters_and_tensor_quantizers(self, num_inputs, num_outputs):
        for index in range(num_inputs):
            # Initialize trainable parameters to None
            self.register_parameter('input' + str(index) + '_encoding_min', None)
            self.register_parameter('input' + str(index) + '_encoding_max', None)

            # Pass name of tensor quantizer and reference of Wrapper to tensor quantizer
            # Input quantizer
            self.input_quantizers[index].name = 'input' + str(index)
            self.input_quantizers[index].wrapper_ref = self
            self.input_quantizers[index].device = self.device

        for index in range(num_outputs):
            self.register_parameter('output' + str(index) + '_encoding_min', None)
            self.register_parameter('output' + str(index) + '_encoding_max', None)
            # Output quantizer
            self.output_quantizers[index].name = 'output' + str(index)
            self.output_quantizers[index].wrapper_ref = self
            self.output_quantizers[index].device = self.device

        # Param Quantizers
        for name, param in self.get_named_parameters():
            self.register_parameter(name + '_encoding_min', None)

            self.register_parameter(name + '_encoding_max', None)

            # Pass name of tensor quantizer and reference of Wrapper to tensor quantizer
            self.param_quantizers[name].name = name
            self.param_quantizers[name].wrapper_ref = self
            self.param_quantizers[name].device = self.device
            channel_axis = 0
            if isinstance(self._module_to_wrap, (torch.nn.ConvTranspose1d,
                                                 torch.nn.ConvTranspose2d,
                                                 torch.nn.ConvTranspose3d)):
                if len(param.shape) > 1:
                    channel_axis = 1
            self.param_quantizers[name]._ch_axis = channel_axis # pylint: disable = protected-access

    def apply_gating_logic(self):
        """
        Apply gating logic.
        """
        def _apply_logic(encoding_min, encoding_max):
            constant_tensor = _constant_tensor(encoding_min.device)
            zero_tensor = constant_tensor.zero
            eps_tensor = constant_tensor.eps
            encoding_min.data = torch.minimum(zero_tensor, encoding_min.data)
            encoding_max.data = torch.maximum(zero_tensor, encoding_max.data)
            encoding_max.data = torch.maximum(encoding_max.data, encoding_min.data + eps_tensor)

        # Gating input encodings
        for index, input_quantizer in enumerate(self.input_quantizers):
            if input_quantizer.enabled:
                _apply_logic(getattr(self, 'input' + str(index) + '_encoding_min'),
                             getattr(self, 'input' + str(index) + '_encoding_max'))

        # Gating output encodings
        for index, output_quantizer in enumerate(self.output_quantizers):
            if output_quantizer.enabled:
                _apply_logic(getattr(self, 'output' + str(index) + '_encoding_min'),
                             getattr(self, 'output' + str(index) + '_encoding_max'))

        # Gating for parameters
        for name, _ in self._module_to_wrap.named_parameters():
            if self.param_quantizers[name].enabled:
                _apply_logic(getattr(self, name + '_encoding_min'),
                             getattr(self, name + '_encoding_max'))

    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        self.apply_gating_logic()

        # Quantize inputs
        torch_inputs = custom_tensor_utils.to_torch_tensor(inputs)
        quantized_inputs = self._quantize_activation(torch_inputs, self.input_quantizers, 'input')

        with self._quantize_params(quantized_inputs):
            quantized_inputs = custom_tensor_utils.to_custom_tensor(inputs, quantized_inputs)
            # Call the forward of the wrapped module
            wrapped_output = self._module_to_wrap(*quantized_inputs)

        # Quantize the outputs
        if not isinstance(wrapped_output, (List, Tuple)):
            wrapped_output = [wrapped_output]

        torch_outputs = custom_tensor_utils.to_torch_tensor(wrapped_output)
        output = self._quantize_activation(torch_outputs, self.output_quantizers, 'output')
        output = custom_tensor_utils.to_custom_tensor(wrapped_output, output)

        if len(output) == 1:
            output = output[0]

        return output

    @contextlib.contextmanager
    def _quantize_params(self, inputs=None):
        inputs = inputs or [torch.empty(1).to(self.device)]
        shadow_params = {}
        encoding_list_for_params = []

        try:
            for name, param in self.get_named_parameters():
                shadow_params[name] = param.detach().clone()
                # Create a list of encoding parameters for params
                if self.param_quantizers[name].enabled:
                    encoding_list_for_params.append(getattr(self, name + '_encoding_min'))
                    encoding_list_for_params.append(getattr(self, name + '_encoding_max'))

            # Quantize the parameters
            inputs[0] = ParameterQuantizer.apply(inputs[0], self, *encoding_list_for_params)

            # clone() the outputs of Custom function to avoid incorrect gradient calculation for in-place modification
            # of view (view is created since Custom function's forward return input as-is)
            inputs[0] = inputs[0].clone()
            yield

        finally:
            for name, param in self.get_named_parameters():
                if name in shadow_params:
                    param.data.copy_(shadow_params[name].data)

    def _quantize_activation(self, tensors_to_quantize, tensor_quantizers, type_of_quantizer):
        quantized_tensors = []
        for index, tensor_to_quantize in enumerate(tensors_to_quantize):
            assert len(tensor_quantizers) > index,\
                f"Not enough tensor quantizers ({len(tensor_quantizers)}) allocated"

            if isinstance(tensor_to_quantize, utils.dtypes_to_ignore_for_quantization) or\
                    tensor_to_quantize.dtype in utils.torch_dtypes_to_ignore_for_quantization or\
                    not tensor_quantizers[index].enabled:
                # Do not quantize tensors of integer or bool data type or if the quantizer is disabled.
                quantized_tensors.append(tensor_to_quantize)
                continue

            if not isinstance(tensor_to_quantize, torch.Tensor):
                error_msg = (f'Expecting quantize activation input of type torch.Tensor but got '
                             f'{type(tensor_to_quantize)}')
                _logger.error(error_msg)
                raise AssertionError(error_msg)

            encoding_min = getattr(self, type_of_quantizer + str(index) + '_encoding_min')
            encoding_max = getattr(self, type_of_quantizer + str(index) + '_encoding_max')
            quantized_tensors.append(tensor_quantizers[index].quantize_dequantize(tensor_to_quantize, encoding_min,
                                                                                  encoding_max))
        return quantized_tensors


class QcQuantizeStandalone(QcQuantizeStandAloneBase):
    """ A custom PyTorch module that derives from QcQuantizeStandAloneBase and quantizes inputs """

    def forward(self, *inputs):
        """
        Forward-pass routine. This quantizes the weights before delegating to the wrapped module and
        then quantizes the output before returning the same
        :param inputs: Inputs passed to the module in the forward pass
        :return: Quantized output from the wrapped module
        """

        output = self._quantize_activation(self.output_quantizers, list(inputs))

        return output

    def compute_encoding(self):
        """
        Compute the quantization encoding for this op
        :return: None
        """
        self.output_quantizers[0].compute_encoding()


class SteGatingFuncForParameters(torch.autograd.Function):
    """
    Custom gradient function for STE
    """

    # pylint:disable = arguments-differ
    @staticmethod
    def forward(ctx, quant_wrapper_ref, *quantized_inputs):
        """
        Quantize-dequantize the tensor, using the saved encoding for this tensor
        :param ctx: Context object to be used to save information for backward method
        :param quant_wrapper_ref: Reference to quantization wrapper
        :param quantized_inputs: Quantized input tensors
        :return: Tensors as it is as input tensors
        """

        ctx.quantization_wrapper_ref = quant_wrapper_ref
        return quantized_inputs

    @staticmethod
    def backward(ctx, *output_grad):
        quant_wrapper_ref = ctx.quantization_wrapper_ref

        def calc_param_grad(name: str, param: torch.nn.Parameter):
            """
            Calculates parameter gradient
            """
            if quant_wrapper_ref.param_quantizers[name].enabled and param.grad is not None and \
                    quant_wrapper_ref.param_quantizers[name].data_type == QuantizationDataType.int:
                param_quantizer = quant_wrapper_ref.param_quantizers[name]

                if isinstance(param_quantizer.encoding, list):
                    # Stack the encodings
                    max_encodings = [enc.max for enc in param_quantizer.encoding]
                    min_encodings = [enc.min for enc in param_quantizer.encoding]
                    # pylint: disable = protected-access
                    param.grad = ste.compute_dloss_by_dx(param, param.grad, min_encodings, max_encodings,
                                                         param_quantizer._ch_axis)
                else:
                    param.grad = ste.compute_dloss_by_dx(param, param.grad, param_quantizer.encoding.min,
                                                         param_quantizer.encoding.max)

        for name, param in quant_wrapper_ref.get_named_parameters():
            calc_param_grad(name, param)

        return (None, *output_grad)

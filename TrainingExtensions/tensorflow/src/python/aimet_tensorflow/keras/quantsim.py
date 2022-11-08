# /usr/bin/env python3.5
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
""" Quantsim for Keras """

import json
import os
from typing import Union, Dict, Tuple, Optional, List

import tensorflow as tf
from aimet_common import libpymo

from aimet_common.defs import QuantScheme
from aimet_common.utils import AimetLogger, save_json_yaml
from aimet_common.quantsim import encoding_version
from aimet_tensorflow.defs import AxisHandling
from aimet_tensorflow.keras.connectedgraph import ConnectedGraph
from aimet_tensorflow.keras.cross_layer_equalization import GraphSearchUtils
from aimet_tensorflow.keras.quant_sim.qc_quantize_wrapper import QcQuantizeWrapper, QuantizerSettings
from aimet_tensorflow.keras.quant_sim.qc_mha_wrapper import QcQuantizableMultiHeadAttention
from aimet_tensorflow.keras.quant_sim.tensor_quantizer import TensorQuantizer, ActivationTensorQuantizer, \
    ParamPerTensorQuantizer, StaticGridPerChannelQuantizer, ParamPerChannelQuantizer
from aimet_tensorflow.keras.quantsim_config.quantsim_config import QuantSimConfigurator, INPUT_QUANTIZERS, \
    OUTPUT_QUANTIZERS, PARAM_QUANTIZERS
from aimet_tensorflow.keras.utils.common import convert_h5_model_to_pb_model

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

unquantizable_modules = (tf.keras.layers.InputLayer, QcQuantizeWrapper)
substitutable_modules = {
    tf.keras.layers.MultiHeadAttention: QcQuantizableMultiHeadAttention
}


class QuantizationSimModel:
    """
    Implements mechanism to add quantization simulations ops to a model. This allows for off-target simulation of
    inference accuracy. Also allows the model to be fine-tuned to counter the effects of quantization.
    """

    # pylint: disable=too-many-arguments
    # pylint: disable=unused-argument
    def __init__(self, model, quant_scheme: Union[QuantScheme, str] = 'tf_enhanced', rounding_mode: str = 'nearest',
                 default_output_bw: int = 8, default_param_bw: int = 8, in_place: bool = False,
                 config_file: str = None):
        """
        :param model: Model to quantize
        :param quant_scheme: Quantization Scheme, currently supported schemes are post_training_tf and
               post_training_tf_enhanced, defaults to post_training_tf_enhanced
        :param rounding_mode: The round scheme to used. One of: 'nearest' or 'stochastic', defaults to 'nearest'.
        :param default_output_bw: bitwidth to use for activation tensors, defaults to 8
        :param default_param_bw: bitwidth to use for parameter tensors, defaults to 8
        :param in_place: If True, then the given 'model' is modified in-place to add quant-sim nodes.
                Only suggested use of this option is when the user wants to avoid creating a copy of the model
        :param config_file: Path to a config file to use to specify rules for placing quant ops in the model
        """
        self._model_without_wrappers = model
        if not in_place:
            self._model_without_wrappers = tf.keras.models.clone_model(model)
            self._model_without_wrappers.set_weights(model.get_weights())
        self._layer_name_to_quant_wrapper = {}
        self._validate_model()
        self.connected_graph = ConnectedGraph(self._model_without_wrappers)
        self._quantsim_configurator = self._initialize_quantsim_configurator(quant_scheme, rounding_mode,
                                                                             default_output_bw, default_param_bw,
                                                                             config_file)
        self.quant_scheme = quant_scheme
        self.per_channel_quantization_enabled = self._quantsim_configurator.per_channel_quantization_flag
        self.model = self._add_quantization_wrappers(quant_scheme, rounding_mode, default_output_bw, default_param_bw)
        self._disable_quantizers_in_folded_batchnorm()

    def _validate_model(self):
        """
        Check that model is appropriate for quantsim.
        """
        multiple_inbound_node_layers = []

        for layer in self._model_without_wrappers.layers:
            if len(layer.inbound_nodes) > 1:
                multiple_inbound_node_layers.append(layer.name)

        if multiple_inbound_node_layers:
            error_msg = (f'Layers with more than one inbound nodes are unsupported. This may occur if a layer is '
                         f'reused multiple times in the model definition.\n'
                         f'Layers with multiple inbound nodes: {multiple_inbound_node_layers}')
            _logger.error(error_msg)
            raise NotImplementedError(error_msg)

    def _initialize_quantsim_configurator(self, quant_scheme: Union[QuantScheme, str], rounding_mode: str,
                                          default_output_bw: int, default_param_bw: int,
                                          config_file: str) -> QuantSimConfigurator:
        """
        Initialize quantsim configurator

        :param quant_scheme: Quantization Scheme
        :param rounding_mode: The round scheme to used
        :param default_output_bw: bitwidth to use for activation tensors
        :param default_param_bw: bitwidth to use for parameter tensors
        :param config_file: Path to a config file to use to specify rules for placing quant ops in the model
        :return: QuantSimConfigurator
        """
        return QuantSimConfigurator(self.connected_graph, quant_scheme, rounding_mode,
                                    default_output_bw, default_param_bw, config_file)

    def _add_quantization_wrappers(self, quant_scheme, rounding_mode, default_output_bw, default_param_bw):
        """
        Add quantization wrappers to the model and return a new model with the wrappers inserted.
        :param quant_scheme: Quantization scheme to use
        :param rounding_mode: Rounding mode to use
        :param default_output_bw: Default bitwidth for activation quantizers
        :param default_param_bw: Default bitwidth for param quantizers
        """

        def wrap_layer(layer) -> tf.keras.layers.Layer:
            """
            Function to wrap layers with QcQuantizeWrappers, used by keras clone_model()
            :param layer: Layer to wrap
            :return: Wrapped layer, or original layer if layer is not to be wrapped
            """
            activation_quant_settings = QuantizerSettings(default_output_bw, rounding_mode,
                                                          quant_scheme, False, False, False)
            param_quant_settings = QuantizerSettings(default_param_bw, rounding_mode,
                                                     quant_scheme, False, False, False)

            if isinstance(layer, tuple(substitutable_modules.keys())):
                new_class = substitutable_modules[type(layer)]
                config = layer.get_config()
                config["copy_source_weights"] = layer.get_weights()
                return new_class(**config)

            if isinstance(layer, unquantizable_modules) or layer.submodules:
                return layer

            input_quantizers, output_quantizers, param_quantizers = self._get_quantizers_by_layer(layer)
            wrapper = QcQuantizeWrapper(layer, activation_quant_settings, param_quant_settings,
                                        num_inputs=len(layer.inbound_nodes[0].keras_inputs),
                                        input_quantizers=input_quantizers,
                                        output_quantizers=output_quantizers,
                                        param_quantizers=param_quantizers,
                                        per_channel_quantization_enabled=self.per_channel_quantization_enabled)
            self._layer_name_to_quant_wrapper[layer.name] = wrapper
            return wrapper

        return tf.keras.models.clone_model(self._model_without_wrappers, clone_function=wrap_layer)

    def _get_quantizers_by_layer(self, layer: tf.keras.layers.Layer) -> Tuple[Optional[ActivationTensorQuantizer],
                                                                              Optional[ActivationTensorQuantizer],
                                                                              Union[ParamPerTensorQuantizer,
                                                                                    ParamPerChannelQuantizer]]:
        """
        Get input/output/param quantizers from quantizers dictionary or initialize quantizers if layer is not found

        :param layer: Target layer
        :return: tuple of input, output, param quantizers
        """
        quantizers_dict = self._quantsim_configurator.get_quantizers_dict(layer)
        if quantizers_dict is None:
            _logger.warning("%s not found in quantizers dict, will generate quantizers automatically", layer.name)
            input_quantizers = None
            output_quantizers = None
            param_quantizers = None
        else:
            input_quantizers = quantizers_dict.get(INPUT_QUANTIZERS)
            output_quantizers = quantizers_dict.get(OUTPUT_QUANTIZERS)
            param_quantizers = quantizers_dict.get(PARAM_QUANTIZERS)

        return input_quantizers, output_quantizers, param_quantizers

    @staticmethod
    def _quantizer_to_name_tuple(quantizers: List[TensorQuantizer]) -> Tuple[Optional[List[str]]]:
        """
        Converts a list of quantizers to a tuple of quantizer names
        :param quantizers: quantizers
        :return: tuple of quantizer names
        """
        quant_list = []
        if not quantizers:
            return None

        for quantizer in quantizers:
            quant_list.append(quantizer.name)
        return tuple(quant_list)

    def get_quantizer_name_by_layer(self, layer: tf.keras.layers.Layer) -> Tuple[Optional[List[str]],
                                                                                 Optional[List[str]],
                                                                                 Optional[List[str]]]:
        """
        Get the names of input, output and param quantizers
        :param layer: the keras layer
        :return: Tuple of quantizer names
        """
        input_quantizers, output_quantizers, param_quantizers = self._get_quantizers_by_layer(layer)
        output_quantizers_names = self._quantizer_to_name_tuple(output_quantizers)
        input_quantizers_names = self._quantizer_to_name_tuple(input_quantizers)
        parameter_quantizers_names = self._quantizer_to_name_tuple(param_quantizers)

        return input_quantizers_names, output_quantizers_names, parameter_quantizers_names

    def _disable_quantizers_in_folded_batchnorm(self):
        """
        Disable input/output/param quantizers if layer is folded batch normalization
        """
        for quantsim_wrapper in self._layer_name_to_quant_wrapper.values():
            if GraphSearchUtils.is_folded_batch_normalization(quantsim_wrapper.original_layer):
                for q in quantsim_wrapper.input_quantizers:
                    q.disable()
                for q in quantsim_wrapper.output_quantizers:
                    q.disable()
                for q in quantsim_wrapper.param_quantizers:
                    q.disable()

    @staticmethod
    def _get_encoding_dict_for_quantizer(quantizer: TensorQuantizer) -> Union[List[Dict[str, Union[str, int, float]]],
                                                                              Dict[str, Union[str, int, float]]]:
        """
        Get encoding dict for a tensor quantizer.
        :param quantizer: Quantizer to get encoding info from
        :return: Dictionary or List of dictionaries containing encodings info for the tensor quantizer
        """
        quantizer_encodings = [quantizer.encoding] if not isinstance(quantizer.encoding, List) else quantizer.encoding
        return [
            {
                'min': encoding.min,
                'max': encoding.max,
                'scale': encoding.delta,
                'offset': int(encoding.offset),
                'bitwidth': encoding.bw,
                'is_symmetric': str(quantizer.is_symmetric),
                'dtype': 'int'
            }
            for encoding in quantizer_encodings
        ]

    def get_encodings_dict(self) -> Dict[str, Union[str, Dict]]:
        """
        Get encodings dict containing all activation and parameter encodings info in the model
        :return: Dictionary containing all activation and parameter encodings info in the model
        """
        # pylint: disable=protected-access
        activation_encodings = {}
        param_encodings = {}
        for wrapper in self.quant_wrappers():
            for idx, input_quantizer in enumerate(wrapper.input_quantizers):
                if input_quantizer.encoding is not None:
                    # because dense layers in quantizable MHA are not explicitly sublayers, they don't have their
                    # inbound_nodes parameter populated, so the name of the quantizer is used instead
                    if not wrapper._layer_to_wrap.inbound_nodes:
                        tensor_name = "multi_head_attention/" + wrapper.name + "/" + input_quantizer.name
                    else:
                        tensor_name = wrapper._layer_to_wrap.inbound_nodes[0].keras_inputs[idx].name
                    encoding_dict = self._get_encoding_dict_for_quantizer(input_quantizer)
                    activation_encodings[tensor_name] = encoding_dict
            for idx, param_quantizer in enumerate(wrapper.param_quantizers):
                if param_quantizer.encoding is not None:
                    param_name = wrapper._layer_to_wrap.weights[idx].name
                    encoding_dict = self._get_encoding_dict_for_quantizer(param_quantizer)
                    param_encodings[param_name] = encoding_dict
            for idx, output_quantizer in enumerate(wrapper.output_quantizers):
                if output_quantizer.encoding is not None:
                    # because dense layers in quantizable MHA are not explicitly sublayers, they don't have their
                    # inbound_nodes parameter populated, so the name of the quantizer is used instead
                    if not wrapper._layer_to_wrap.inbound_nodes:
                        tensor_name = "multi_head_attention/" + wrapper.name + "/" + output_quantizer.name
                    else:
                        tensor_name = wrapper._layer_to_wrap.output.name
                    encoding_dict = self._get_encoding_dict_for_quantizer(output_quantizer)
                    activation_encodings[tensor_name] = encoding_dict
        encodings_dict = {'version': encoding_version,
                          'activation_encodings': activation_encodings,
                          'param_encodings': param_encodings}
        return encodings_dict

    def compute_encodings(self, forward_pass_callback, forward_pass_callback_args):
        """
        Computes encodings for all quantization sim nodes in the model.

        :param forward_pass_callback: A callback function that is expected to runs forward passes on a model.
               This callback function should use representative data for the forward pass, so the calculated
               encodings work for all data samples.
        :param forward_pass_callback_args: These argument(s) are passed to the forward_pass_callback as-is. Up to
               the user to determine the type of this parameter. E.g. could be simply an integer representing the number
               of data samples to use. Or could be a tuple of parameters or an object representing something more
               complex.
        """
        ops_with_invalid_encodings = []
        self._compute_and_set_parameter_encodings(ops_with_invalid_encodings)

        self._set_op_mode_parameters(libpymo.TensorQuantizerOpMode.quantizeDequantize)

        forward_pass_callback(self.model, forward_pass_callback_args)
        for quant_wrapper in self.quant_wrappers():
            quant_wrapper.compute_encoding(ops_with_invalid_encodings)

        op_mode = self._param_op_mode_after_analysis(self.quant_scheme)

        self._set_op_mode_parameters(op_mode)

        if ops_with_invalid_encodings:
            _logger.info('The following quantizers did not have valid encodings and have been set to passThrough mode: '
                         '%s', ops_with_invalid_encodings)
            _logger.info('This can be due to the quantizers not having been evaluated during the forward pass in '
                         'compute encodings. Evaluation is required to collect statistics needed to compute valid '
                         'encodings.\n'
                         'As a result, the quantizers have been set to passThrough mode, meaning no quantization noise '
                         'will be simulated for these ops if they are evaluated in the future.\n'
                         'If this is not desired, amend the forward pass to evaluate tensors which require these ops '
                         'to be evaluated, and recompute encodings.')

    def _set_op_mode_parameters(self, op_mode: libpymo.TensorQuantizerOpMode):
        """
        Sets quant mode for parameters and if the encodings are invalid, then adds those wrappers
        to wrappers_with_invalid_encodings
        :param op_mode: Quant mode to set to
        """

        for quantizer_info in self.quant_wrappers():
            for param_quantizer in quantizer_info.param_quantizers:
                if param_quantizer.is_enabled():
                    param_quantizer.quant_mode = op_mode

    def export(self, path, filename_prefix, custom_objects=None):
        """
        This method exports out the quant-sim model so it is ready to be run on-target.

        Specifically, the following are saved

        1. The sim-model is exported to a regular Keras model without any simulation ops
        2. The quantization encodings are exported to a separate JSON-formatted file that can
           then be imported by the on-target runtime (if desired)

        :param path: path where to store model pth and encodings
        :param filename_prefix: Prefix to use for filenames of the model pth and encodings files
        :param custom_objects: If there are custom objects to load, Keras needs a dict of them to map them
        """
        model_path = os.path.join(path, filename_prefix)
        self._model_without_wrappers.save(model_path)
        self._model_without_wrappers.save(model_path + '.h5', save_format='h5')
        # Conversion of saved h5 model to pb model for consumption by SNPE/QNN]
        try:
            convert_h5_model_to_pb_model(f'{model_path}.h5', custom_objects=custom_objects)
        except ValueError:
            _logger.error("Could not convert h5 to frozen pb."
                          "Please call export() again with custom_objects defined.")
            raise
        encodings_dict = self.get_encodings_dict()
        encoding_file_path = os.path.join(path, filename_prefix + '.encodings')
        save_json_yaml(encoding_file_path, encodings_dict)

    def _compute_and_set_parameter_encodings(self, ops_with_invalid_encodings: List):
        # pylint: disable=too-many-nested-blocks
        for quantizer_wrapper in self.quant_wrappers():
            for idx, param_quantizer in enumerate(quantizer_wrapper.param_quantizers):
                if param_quantizer.is_enabled():
                    # 0th input to our quant wrapper is the tensor being quantized
                    weight_tensor = quantizer_wrapper.original_layer.get_weights()[idx]

                    # Per-channel
                    if isinstance(param_quantizer, StaticGridPerChannelQuantizer):
                        for index, tensor_quantizer in enumerate(param_quantizer.tensor_quantizer):
                            if param_quantizer.axis_handling == AxisHandling.LAST_TWO_AXES.value:
                                last_two_axes_combined_shape = list(weight_tensor.shape[:-2]) + [-1]
                                channel_slice = weight_tensor.reshape(*last_two_axes_combined_shape)
                                channel_slice = channel_slice.take(index, channel_slice.ndim - 1)
                            elif isinstance(quantizer_wrapper.original_layer, tf.keras.layers.Conv2DTranspose):
                                if len(weight_tensor) == 4:
                                    channel_slice = weight_tensor.take(index, weight_tensor.ndim - 2)
                                else:
                                    # For bias in Transpose layers
                                    channel_slice = weight_tensor.take(index, weight_tensor.ndim - 1)
                            else:
                                channel_slice = weight_tensor.take(index, weight_tensor.ndim - 1)
                            tensor_quantizer.updateStats(channel_slice, False)

                    # Per-tensor
                    else:
                        tensor_quantizer = param_quantizer.tensor_quantizer
                        tensor_quantizer.updateStats(weight_tensor, False)

                    param_quantizer.compute_encoding(ops_with_invalid_encodings)

    def set_and_freeze_param_encodings(self, encoding_path: str):
        """
        Set and freeze parameter encodings from encodings JSON file
        :param encoding_path: path from where to load parameter encodings file
        """
        # Load parameter encodings file
        with open(encoding_path) as json_file:
            param_encodings = json.load(json_file)

        for quant_wrapper in self.quant_wrappers():
            quant_wrapper.set_and_freeze_param_encoding(param_encodings)

    def _param_op_mode_after_analysis(self, quant_scheme) -> libpymo.TensorQuantizerOpMode:
        """
        Returns quant mode to use for parameters after encodings have been computed
        :param quant_scheme: Quantization scheme to use
        :return: Quant mode to use
        """
        if quant_scheme in [QuantScheme.training_range_learning_with_tf_init,
                            QuantScheme.training_range_learning_with_tf_enhanced_init] \
                or self.per_channel_quantization_enabled:
            return libpymo.TensorQuantizerOpMode.quantizeDequantize
        return libpymo.TensorQuantizerOpMode.oneShotQuantizeDequantize

    def quant_wrappers(self):
        """
        Generator for yielding all quantization wrappers
        """
        for layer in self.model.layers:
            if isinstance(layer, QcQuantizeWrapper):
                yield layer
            if isinstance(layer, QcQuantizableMultiHeadAttention):
                yield from layer.quant_wrappers()

    def get_quant_wrapper_for_layer_name(self, layer_name: str) -> QcQuantizeWrapper:
        """
        Return qc quant wrapper corresponding to a layer name
        :param layer_name: Layer name to get quantize wrapper for
        :return: Qc quant wrapper corresponding to a layer name
        """
        return self._layer_name_to_quant_wrapper.get(layer_name)

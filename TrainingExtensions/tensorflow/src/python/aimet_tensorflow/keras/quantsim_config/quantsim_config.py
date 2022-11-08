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
""" Utilities for parsing and applying quantsim configurations from json config file """
from typing import List, Tuple, Dict, Union

from tensorflow.keras import layers

from aimet_common.connected_graph.connectedgraph_utils import get_all_input_ops, get_all_output_ops
from aimet_common.connected_graph.operation import Op
from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_common.graph_pattern_matcher import PatternType
from aimet_common.graph_searcher import GraphSearcher
from aimet_common.quantsim_config.quantsim_config import SupergroupConfigCallback as AimetCommonSupergroupConfigCallback
from aimet_common.quantsim_config.json_config_importer import ConfigType, SupergroupType, OpTypeType, ParamType, \
    DefaultsType, ConfigDictKeys
from aimet_common.quantsim_config.quantsim_config import QuantSimConfigurator as AimetCommonQuantSimConfigurator, \
    get_all_ops_in_neighborhood
from aimet_common.utils import AimetLogger
import aimet_tensorflow.keras.utils.common as keras_common_utils
from aimet_tensorflow.keras.connectedgraph import ConnectedGraph
from aimet_tensorflow.keras.quant_sim.qc_quantize_wrapper import QuantizerSettings
from aimet_tensorflow.keras.quant_sim.tensor_quantizer import ActivationTensorQuantizer, ParamPerTensorQuantizer, \
    ParamPerChannelQuantizer
from aimet_tensorflow.utils.constants import QUANT_ALLOWED_DTYPES

_logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

LayerAffectedQuantizerTupleType = Tuple[List[Tuple[layers.Layer, str]], List[Tuple[layers.Layer, str]],
                                        List[Tuple[layers.Layer, str]], List[Tuple[layers.Layer, str]]]

SETTING = "setting"
AFFECTED_QUANTIZERS = "affected_quantizers"
INPUT_QUANTIZERS = "input_quantizers"
OUTPUT_QUANTIZERS = "output_quantizers"
PARAM_QUANTIZERS = "param_quantizers"


class TreeLikeDictionary(dict):
    """
    A n-ary tree-like autovivification dictionary for storing/updating/fetching
    """

    def __missing__(self, key):
        value = self[key] = type(self)()
        return value


class SupergroupConfigCallback(AimetCommonSupergroupConfigCallback):
    """
    Class acting as a callback for when supergroups are found
    """

    def __init__(self, layer_to_config_dict: TreeLikeDictionary):
        super().__init__()
        self._layer_to_config_dict = layer_to_config_dict

    def __call__(self, _, op_list: List[Op]):
        # Assumes op list is at least of length two
        for index, op in enumerate(op_list):
            layer = op.get_module()
            if index == 0:
                # turn off only output quantization of first op in the list
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_OUTPUT_QUANTIZED][SETTING] = False
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_OUTPUT_QUANTIZED][AFFECTED_QUANTIZERS] = \
                    _get_affected_tensor_quantizers_by_false_setting(
                        op, "output")
            elif index == len(op_list) - 1:
                # turn off only input quantization of last op in the list
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_INPUT_QUANTIZED][SETTING] = False
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_INPUT_QUANTIZED][AFFECTED_QUANTIZERS] = \
                    _get_affected_tensor_quantizers_by_false_setting(
                        op, "input")
            else:
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_INPUT_QUANTIZED][SETTING] = False
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_INPUT_QUANTIZED][AFFECTED_QUANTIZERS] = \
                    _get_affected_tensor_quantizers_by_false_setting(
                        op, "input")

                self._layer_to_config_dict[layer][ConfigDictKeys.IS_OUTPUT_QUANTIZED][SETTING] = False
                self._layer_to_config_dict[layer][ConfigDictKeys.IS_OUTPUT_QUANTIZED][AFFECTED_QUANTIZERS] = \
                    _get_affected_tensor_quantizers_by_false_setting(
                        op, "output")


def _get_affected_tensor_quantizers_by_true_setting(op: Op, direction: str) -> List[Tuple[layers.Layer, str]]:
    """
    Get a list of tensor quantizers that would be affected if the quantization of target direction of op is enabled

    :param op: Op to enable target quantization (input or output) for
    :param direction: Target direction which will be enabled
    :return: List of tuples containing layer and direction that would be affected
    """
    return [(op.get_module(), direction)]


def _get_affected_tensor_quantizers_by_false_setting(op: Op, direction: str) -> List[Tuple[layers.Layer, str]]:
    """
    Get a list of tensor quantizers that would be affected if the quantization of target direction of op is disabled

    :param op: Op to disable target quantization (input or output) for
    :param direction: Target direction which will be disabled
    :return: List of tuples containing layer and direction that would be affected
    """
    affected_tensor_quantizers_by_false_setting = []
    neighboring_ops = get_all_ops_in_neighborhood(op, direction)
    for neighbor_op in neighboring_ops:
        if neighbor_op.type == "Split":
            continue

        if neighboring_ops[neighbor_op] == "input":
            affected_tensor_quantizers_by_false_setting.append(
                (neighbor_op.get_module(), "input"))
        else:
            affected_tensor_quantizers_by_false_setting.append(
                (neighbor_op.get_module(), "output"))

    return affected_tensor_quantizers_by_false_setting


def _initialize_input_quantizers(layer: layers.Layer, quant_settings: QuantizerSettings,
                                 enabled: bool) -> List[ActivationTensorQuantizer]:
    """
    Initialize input quantizers corresponding to layer using quantizer settings

    :param layer: Target tf.keras.layers.Layer
    :param quant_settings: Quantization settings
    :param enabled: Flag for quantized or not
    :return: Input quantizers corresponding to layer
    """
    layer_input_list = layer.inbound_nodes[0].keras_inputs
    num_inputs = len(layer_input_list)
    input_quantizers = []
    for i in range(num_inputs):
        activation_tensor_quantizer = ActivationTensorQuantizer(layer,
                                                                f"{layer.name}_input_quantizer_{i}",
                                                                quant_settings.quant_scheme,
                                                                quant_settings.round_mode,
                                                                quant_settings.bitwidth,
                                                                quant_settings.is_symmetric,
                                                                quant_settings.use_strict_symmetric,
                                                                quant_settings.use_unsigned_symmetric,
                                                                enabled and layer.output.dtype in QUANT_ALLOWED_DTYPES)
        input_quantizers.append(activation_tensor_quantizer)
    return input_quantizers


def _initialize_output_quantizers(layer: layers.Layer, quant_settings: QuantizerSettings,
                                  enabled: bool) -> List[ActivationTensorQuantizer]:
    """
    Initialize output quantizers corresponding to layer using quantizer settings

    :param layer: Target tf.keras.layers.Layer
    :param quant_settings: Quantization settings
    :param enabled: Flag for quantized or not
    :return: Output quantizers corresponding to layer
    """
    output_quantizers = []
    activation_tensor_quantizer = ActivationTensorQuantizer(layer,
                                                            f"{layer.name}_output_quantizer_0",
                                                            quant_settings.quant_scheme,
                                                            quant_settings.round_mode,
                                                            quant_settings.bitwidth,
                                                            quant_settings.is_symmetric,
                                                            quant_settings.use_strict_symmetric,
                                                            quant_settings.use_unsigned_symmetric,
                                                            enabled and layer.output.dtype in QUANT_ALLOWED_DTYPES)
    output_quantizers.append(activation_tensor_quantizer)
    return output_quantizers


def _initialize_param_quantizers(layer: layers.Layer, param_config_dict: TreeLikeDictionary,
                                 quant_settings: QuantizerSettings,
                                 per_channel_quantization_enabled: bool) -> Union[List[ParamPerTensorQuantizer],
                                                                                  List[ParamPerChannelQuantizer]]:
    """
    Initialize param quantizers corresponding to layer using quantizer settings
    :param layer: Target tf.keras.layers.Layer
    :param param_config_dict: Dictionary containing configurations for parameters of certain types
    :param quant_settings: Quantization settings
    :return: Param quantizers corresponding to layer
    """
    param_quantizers = []
    for weight in layer.weights:
        if weight.dtype in QUANT_ALLOWED_DTYPES:
            weight_name = weight.name.split(":")[0]
            param_type = "bias" if "bias" in weight_name else "weight"

            if param_type in param_config_dict:
                is_symmetric = param_config_dict[param_type][ConfigDictKeys.IS_SYMMETRIC].get(
                    SETTING, False)
                enabled = param_config_dict[param_type][ConfigDictKeys.IS_QUANTIZED].get(
                    SETTING, False)
            else:
                is_symmetric = param_config_dict[ConfigDictKeys.IS_SYMMETRIC].get(
                    SETTING, False)
                enabled = param_config_dict[ConfigDictKeys.IS_QUANTIZED].get(
                    SETTING, False)

            if per_channel_quantization_enabled and isinstance(layer,
                                                               keras_common_utils.per_channel_quantizeable_layers):
                num_output_channels, axis_handling = keras_common_utils.get_number_of_outputs_and_axis_handling(
                    layer, weight.shape, param_type
                )

                param_quantizers.append(
                    ParamPerChannelQuantizer(layer,
                                             weight_name,
                                             quant_settings.quant_scheme,
                                             quant_settings.round_mode,
                                             quant_settings.bitwidth,
                                             is_symmetric,
                                             quant_settings.use_strict_symmetric,
                                             quant_settings.use_unsigned_symmetric,
                                             axis_handling,
                                             num_output_channels,
                                             enabled))
            else:

                param_quantizers.append(
                    ParamPerTensorQuantizer(layer,
                                            weight_name,
                                            quant_settings.quant_scheme,
                                            quant_settings.round_mode,
                                            quant_settings.bitwidth,
                                            is_symmetric,
                                            quant_settings.use_strict_symmetric,
                                            quant_settings.use_unsigned_symmetric,
                                            enabled))

    return param_quantizers


class QuantSimConfigurator(AimetCommonQuantSimConfigurator):
    """ Class for parsing and applying quantsim configurations from json config file """

    def __init__(self, connected_graph: ConnectedGraph, quant_scheme: Union[QuantScheme, str], rounding_mode: str,
                 default_output_bw: int, default_param_bw: int, config_file: str):
        # TODO when keras supports fp16, pass default_data_type to below line
        super(QuantSimConfigurator, self).__init__(config_file, QuantizationDataType.int, default_output_bw,
                                                   default_param_bw)
        self._connected_graph = connected_graph
        self._layer_to_affected_quantizer_info_dict = self._create_layer_to_affected_quantizer_info_dict()
        self._layer_to_config_dict = TreeLikeDictionary()
        self._layer_to_quantizers_dict = TreeLikeDictionary()

        self._set_quantsim_configs()
        self.per_channel_quantization_flag = self._parse_per_channel_quantization().get('defaults')
        self._initialize_quantizers_by_layer(
            quant_scheme, rounding_mode, default_output_bw, default_param_bw)

    def _create_layer_to_affected_quantizer_info_dict(self) -> Dict[layers.Layer, LayerAffectedQuantizerTupleType]:
        """
        Create affected tensor quantizers information by layer dictionary
        - List of tensor quantizers to change if op's input quantizer setting is set to True
        - List of tensor quantizers to change if op's output quantizer setting is set to True
        - List of tensor quantizers to change if op's input quantizer setting is set to False
        - List of tensor quantizers to change if op's output quantizer setting is set to False
        :return: Dictionary mapping layer to tuple of lists of affected layer quantization information tuples
        """
        layer_to_tensor_quantizers_dict = {}
        for op in self._connected_graph.ordered_ops:
            affected_quantizers_when_input_enabled = _get_affected_tensor_quantizers_by_true_setting(
                op, "input")
            affected_quantizers_when_output_enabled = _get_affected_tensor_quantizers_by_true_setting(
                op, "output")
            affected_quantizers_when_input_disabled = _get_affected_tensor_quantizers_by_false_setting(
                op, "input")
            affected_quantizers_when_output_disabled = _get_affected_tensor_quantizers_by_false_setting(
                op, "output")

            layer_to_tensor_quantizers_dict[op.get_module()] = (affected_quantizers_when_input_enabled,
                                                                affected_quantizers_when_output_enabled,
                                                                affected_quantizers_when_input_disabled,
                                                                affected_quantizers_when_output_disabled)

        return layer_to_tensor_quantizers_dict

    def _set_default_configs(self, default_configs: DefaultsType):
        """
        Set default configurations for op and param quantizers in model (first level of specificity in configuration
        file)
        :param default_configs: Default configurations for quantizers
        """
        optional_configs = [ConfigDictKeys.STRICT_SYMMETRIC,
                            ConfigDictKeys.UNSIGNED_SYMMETRIC,
                            ConfigDictKeys.PER_CHANNEL_QUANTIZATION]

        for op in self._connected_graph.ordered_ops:
            layer = op.get_module()

            # Initialize reserved config dictionary field by layer
            self._layer_to_config_dict[layer] = TreeLikeDictionary()

            # Set default configs for ops
            for config_key, config_val in default_configs[ConfigDictKeys.OPS].items():
                self._layer_to_config_dict[layer][config_key][SETTING] = config_val
                self._layer_to_config_dict[layer][config_key][AFFECTED_QUANTIZERS] = \
                    self._get_affected_quantizers_by_config(
                        layer, config_key, config_val)

            # Set default configs for params
            for config_key, config_val in default_configs[ConfigDictKeys.PARAMS].items():
                self._layer_to_config_dict[layer][ConfigDictKeys.PARAMS][config_key][SETTING] = config_val

            # Set default configs for optional configs (strict_symmetric, unsigned_symmetric, per_channel_quantization)
            for config_key in optional_configs:
                if config_key in default_configs:
                    self._layer_to_config_dict[layer][config_key][SETTING] = default_configs[config_key]

    def _get_affected_quantizers_by_config(self, layer: layers.Layer, setting_name: str,
                                           quantizer_setting: bool) -> List[Tuple[layers.Layer, str]]:
        """
        Return list of tuples containing affected quantizers by given configuration setting

        :param layer: Current layer
        :param setting_name: Name of quantizer setting
        :param quantizer_setting: Setting value corresponding to setting_name
        :return: List of tuples containing layer and direction that would be affected by given quantizer setting
        """

        input_true_list, output_true_list, input_false_list, output_false_list = \
            self._layer_to_affected_quantizer_info_dict[layer]

        if setting_name == ConfigDictKeys.IS_INPUT_QUANTIZED and quantizer_setting:
            return input_true_list
        if setting_name == ConfigDictKeys.IS_OUTPUT_QUANTIZED and quantizer_setting:
            return output_true_list
        if setting_name == ConfigDictKeys.IS_INPUT_QUANTIZED and not quantizer_setting:
            return input_false_list
        if setting_name == ConfigDictKeys.IS_OUTPUT_QUANTIZED and not quantizer_setting:
            return output_false_list
        if setting_name == ConfigDictKeys.IS_SYMMETRIC:
            # Will modify all input and output quantizers in the False case
            return input_false_list + output_false_list
        _logger.error("Encountered unrecognized case for setting name %s, setting value %s", setting_name,
                      quantizer_setting)
        raise ValueError

    def _set_param_configs(self, param_configs: ParamType):
        """
        Set configurations for all params of specific types (second level of specificity in configuration file)

        :param param_configs: Dictionary containing configurations for parameters of certain types
        """
        for op in self._connected_graph.ordered_ops:
            layer = op.get_module()
            self._update_layer_param_config(layer, param_configs)

    def _set_op_type_configs(self, op_configs: OpTypeType):
        """
        Set configurations for all ops of specific types (third level of specificity in configuration file)

        :param op_configs: Dictionary containing configurations for ops of certain types
        """
        for op in self._connected_graph.ordered_ops:
            layer = op.get_module()

            if op.type in op_configs:
                for config_key, config_val in op_configs[op.type].items():
                    if config_key == ConfigDictKeys.PARAMS:
                        self._update_layer_param_config(layer, config_val)
                    else:
                        self._layer_to_config_dict[layer][config_key][SETTING] = config_val
                        self._layer_to_config_dict[layer][config_key][AFFECTED_QUANTIZERS] = \
                            self._get_affected_quantizers_by_config(
                                layer, config_key, config_val)

    def _update_layer_param_config(self, layer: layers.Layer, param_configs: ParamType):
        """
        Update param config of layer in config dictionary

        :param layer: Target tf.keras.layers.Layer
        :param param_configs: Dictionary containing configurations for parameters of certain types
        """
        for param_type, param_config_dict in param_configs.items():
            for config_key, config_val in param_config_dict.items():
                self._layer_to_config_dict[layer][ConfigDictKeys.PARAMS][param_type][config_key][SETTING] = config_val

    def _set_supergroup_configs(self, supergroups_configs: List[SupergroupType]):
        """
        Set supergroup specific configurations (fourth level of specificity in configuration file)
        :param supergroups_configs: Configurations for supergroups
        """
        patterns_with_callbacks = []
        for supergroup_config in supergroups_configs:
            callback = SupergroupConfigCallback(self._layer_to_config_dict)
            op_list = supergroup_config[ConfigDictKeys.OP_LIST]
            patterns_with_callbacks.append(
                PatternType(pattern=op_list, action=callback))

        if patterns_with_callbacks:
            graph_searcher = GraphSearcher(
                self._connected_graph, patterns_with_callbacks)
            graph_searcher.find_all_patterns_in_graph_apply_actions()

    def _set_model_input_configs(self, model_input_configs: ConfigType):
        """
        Set model input specific configurations (fifth level of specificity in configuration file)
        :param model_input_configs: Configuration for model inputs
        """
        input_ops = get_all_input_ops(self._connected_graph)
        for op in input_ops:
            layer = op.get_module()

            for config_key, config_val in model_input_configs.items():
                self._layer_to_config_dict[layer][config_key][SETTING] = config_val
                self._layer_to_config_dict[layer][config_key][AFFECTED_QUANTIZERS] = \
                    self._get_affected_quantizers_by_config(
                        layer, config_key, config_val)

    def _set_model_output_configs(self, model_output_configs: ConfigType):
        """
        Set model output specific configurations (sixth level of specificity in configuration file)
        :param model_output_configs: Configuration for model outputs
        """
        output_ops = get_all_output_ops(self._connected_graph)
        for op in output_ops:
            layer = op.get_module()

            for config_key, config_val in model_output_configs.items():
                self._layer_to_config_dict[layer][config_key][SETTING] = config_val
                self._layer_to_config_dict[layer][config_key][AFFECTED_QUANTIZERS] = \
                    self._get_affected_quantizers_by_config(
                        layer, config_key, config_val)

    def _initialize_quantizers_by_layer(self, quant_scheme: Union[QuantScheme, str], rounding_mode: str,
                                        default_output_bw: int, default_param_bw: int):
        """
        Initialize quantizers of each layer using configuration dictionary

        :param quant_scheme: Quantization scheme to use
        :param rounding_mode: Rounding mode to use
        :param default_output_bw: Default bitwidth for activation quantizers
        :param default_param_bw: Default bitwidth for param quantizers
        """
        # pylint: disable-msg=too-many-locals
        for layer, config_dict in self._layer_to_config_dict.items():
            # Configs for both Ops and Params
            use_unsigned_symmetric = config_dict[ConfigDictKeys.UNSIGNED_SYMMETRIC].get(
                SETTING, False)
            use_strict_symmetric = config_dict[ConfigDictKeys.STRICT_SYMMETRIC].get(
                SETTING, False)

            # Configs for Ops
            ops_is_symmetric = config_dict[ConfigDictKeys.IS_SYMMETRIC].get(
                SETTING, False)
            input_quantizer_enabled = config_dict[ConfigDictKeys.IS_INPUT_QUANTIZED].get(
                SETTING, False)
            output_quantizer_enabled = config_dict[ConfigDictKeys.IS_OUTPUT_QUANTIZED].get(
                SETTING, False)
            activation_quant_settings = QuantizerSettings(default_output_bw, rounding_mode,
                                                          quant_scheme, ops_is_symmetric,
                                                          use_unsigned_symmetric, use_strict_symmetric)

            # Check if there are conflict cases before initializing input/output quantizers
            self._check_existence_of_conflict_case(
                layer, ConfigDictKeys.IS_INPUT_QUANTIZED, input_quantizer_enabled)
            self._check_existence_of_conflict_case(
                layer, ConfigDictKeys.IS_OUTPUT_QUANTIZED, output_quantizer_enabled)
            self._check_existence_of_conflict_case(
                layer, ConfigDictKeys.IS_SYMMETRIC, ops_is_symmetric)

            # Initialize Input Quantizers
            self._layer_to_quantizers_dict[layer][INPUT_QUANTIZERS] = \
                _initialize_input_quantizers(
                    layer, activation_quant_settings, input_quantizer_enabled)

            # Initialize Output Quantizers
            self._layer_to_quantizers_dict[layer][OUTPUT_QUANTIZERS] = \
                _initialize_output_quantizers(
                    layer, activation_quant_settings, output_quantizer_enabled)

            # Configs for Params
            param_config_dict = config_dict[ConfigDictKeys.PARAMS]
            param_is_symmetric = param_config_dict[ConfigDictKeys.IS_SYMMETRIC].get(
                SETTING, False)
            param_quant_settings = QuantizerSettings(default_param_bw, rounding_mode,
                                                     quant_scheme, param_is_symmetric,
                                                     use_unsigned_symmetric, use_strict_symmetric)

            # Initialize Param Quantizers
            self._layer_to_quantizers_dict[layer][PARAM_QUANTIZERS] = \
                _initialize_param_quantizers(layer, param_config_dict, param_quant_settings,
                                             self.per_channel_quantization_flag)

    def _check_existence_of_conflict_case(self, layer: layers.Layer, config_key: str, current_setting: bool):
        """
        Check if there is conflict case in configs

        :param layer: Target tf.keras.layers.Layer
        :param config_key: Config key to check conflict case such as is_input_quantized, is_symmetric
        :param current_setting: True/False value corresponding to config key
        """

        for affected_layer, direction in self._layer_to_config_dict[layer][config_key][AFFECTED_QUANTIZERS]:
            if config_key in [ConfigDictKeys.IS_INPUT_QUANTIZED, ConfigDictKeys.IS_OUTPUT_QUANTIZED]:
                config_key_to_check = f"is_{direction}_quantized"
            elif config_key == ConfigDictKeys.IS_SYMMETRIC:
                config_key_to_check = config_key
            else:
                raise ValueError("Unsupported case of config key")

            quantizer_setting = self._layer_to_config_dict[affected_layer][config_key_to_check].get(
                SETTING, False)
            if current_setting != quantizer_setting:
                _logger.error("Conflicting tensor quantizer settings for %s, expected: %s, actual: %s", config_key,
                              current_setting, quantizer_setting)
                raise RuntimeError

    def get_quantizers_dict(self, layer: layers.Layer) -> TreeLikeDictionary:
        """
        Get input/output/param quantizer dictionary corresponding to layer

        :param layer: Target layer to obtain quantizer dictionary
        :return: Dictionary containing input/output/param quantizers
        """
        return self._layer_to_quantizers_dict.get(layer)

    def _override_default_act_bw_dtype(self, data_type: QuantizationDataType, bitwidth: int):
        """
        overrides data type and bw default config for input/output quantizers.
        :param data_type: data type as QuantizationDataType
        :param bitwidth: bitwidth to be configured
        :return:
        """

    def _override_default_param_bw_dtype(self, data_type: QuantizationDataType, bitwidth: int):
        """
        overrides data type and bitwidth default config for param quantizers
        :param bitwidth: bitwidth
        :param data_type: data type as QuantizationDataType
        :return:
        """

    def _override_param_bw_dtype(self, quantizer_data, data_type: QuantizationDataType, bitwidth: int):
        """
        overrides data type and bitwidth default config for param quantizers of given data
        :param quantizer_data: object containing which param override will be applied to
        :param bitwidth: bitwidth
        :param data_type: data type as QuantizationDataType
        :return:
        """

    def _generate_and_apply_op_instance_specific_config(self):
        """
        Generate op instance specific configurations - currently supported_kernels and per_channel_quantization fields
        This function uses op specific supported_kernels (if absent use defaults), op specific per_channel_quantization
        fields (if absent use default per_channel_quantization) and generate op instance specific config
        """

# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2021, Qualcomm Innovation Center, Inc. All rights reserved.
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

""" Implementation to automatically prepare pytorch models for AIMET features """

import copy
from re import search
from typing import Any, Optional, Dict, Union, List
import torch
import torch.fx
from aimet_common.utils import AimetLogger
from aimet_torch.utils import get_device

import aimet_torch.elementwise_ops as elementwise_ops

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

# this is a map of torch.nn.functional type to corresponding module type
functional_op_to_module_map = {
    torch.nn.functional.relu: torch.nn.ReLU,
    torch.nn.functional.gelu: torch.nn.GELU
}

# this is a map of torch.fx node based functional names to module.
functional_to_module_map = {

    # Non Linear activation functions
    'relu'          : torch.nn.ReLU,
    'relu6'         : torch.nn.ReLU6,
    'hardtanh'      : torch.nn.Hardtanh,
    'hardwish'      : torch.nn.Hardswish,
    'elu'           : torch.nn.ELU,
    'selu'          : torch.nn.SELU,
    'celu'          : torch.nn.CELU,
    'leaky_relu'    : torch.nn.LeakyReLU,
    'prelu'         : torch.nn.PReLU,
    'rrelu'         : torch.nn.RReLU,
    'glu'           : torch.nn.GLU,
    'gelu'          : torch.nn.GELU,
    'logsigmoid'    : torch.nn.LogSigmoid,
    'hardshrink'    : torch.nn.Hardshrink,
    'tanhshrink'    : torch.nn.Tanhshrink,
    'softsign'      : torch.nn.Softsign,
    'softplus'      : torch.nn.Softplus,
    'softmin'       : torch.nn.Softmin,
    'softmax'       : torch.nn.Softmax,
    'softshrink'    : torch.nn.Softshrink,
    'log_softmax'   : torch.nn.LogSoftmax,
    'tanh'          : torch.nn.Tanh,
    'sigmoid'       : torch.nn.Sigmoid,
    'hardsigmoid'   : torch.nn.Hardsigmoid,
    'silu'          : torch.nn.SiLU,

    # Elementwise operations
    'add'           : elementwise_ops.Add,
    'subtract'      : elementwise_ops.Subtract,
    'mul'           : elementwise_ops.Multiply,
    'div'           : elementwise_ops.Divide,
    'matmul'        : elementwise_ops.MatMul,
}

functional_to_module_special_handling_map = {

    # Operations that require special transformation
    'cat'           : elementwise_ops.Concat,
    'interpolate'   : elementwise_ops.Interpolate,
    'conv2d'        : torch.nn.Conv2d,
}


def conv2d_create_node(symbolic_traced_model: torch.fx.GraphModule, module_name: str, node: torch.fx.node) \
        -> torch.fx.node:
    """
    Create the node to be inserted in the graph model.

    :param symbolic_traced_model: Symbolically traced model
    :param module_name: Qualified module name in symbolic_traced_model hierarchy corresponding to new node
    :param node: Current node in the graph after which new node will be inserted
    :return: torch.fx.node to be inserted in the graph
    """

    n_args = len(node.args)
    # input tensors must be passed as args, not kwargs for QcQuantizeWrapper
    input_tensor = []
    # input and weight is guaranteed to exist, but bias can be None
    # Since None cannot be passed as args in QcQuantizeWrapper, do not add it to input_tensor
    for index, key in [[0, 'input'], [1, 'weight'], [2, ' bias']]:
        value = None
        if n_args > index:
            value = node.args[index]
        elif key in node.kwargs:
            value = node.kwargs[key]

        if value is not None:
            input_tensor.append(value)
        else:
            break

    with symbolic_traced_model.graph.inserting_after(node):
        if isinstance(getattr(symbolic_traced_model, module_name), elementwise_ops.DynamicConv2d):
            new_node = symbolic_traced_model.graph.call_module(module_name, args=tuple(input_tensor))
        else:
            new_node = symbolic_traced_model.graph.call_module(module_name, args=tuple([input_tensor[0]]))
        return new_node


def conv2d_create_module(node: torch.fx.node) -> torch.nn.Module:
    """
    Create the replacement module.

    :param node: Current node in the graph after which new node will be inserted
    :return: New module.
    """

    # Get weight and bias from argument
    params = merge_args_and_kwargs(node, {1: 'weight', 2: 'bias'})

    # Convert F.Conv2D arguments to nn.Conv2D arguments
    kwargs = merge_args_and_kwargs(node, {3: 'stride', 4: 'padding', 5: 'dilation', 6: 'groups'})

    # If weight or bias is from activation of another layer, use dynamic_conv2d
    use_dynamic_conv2d = False
    for key, param in params.items():
        if param.op != 'get_attr':
            use_dynamic_conv2d = True
            break

    if use_dynamic_conv2d:
        module = elementwise_ops.DynamicConv2d(**kwargs)
    else:
        for key, param_node in params.items():
            params[key] = get_node_attr(param_node)

        # Fetch additional info using parameters
        out_channels, in_channels, kernel_size, _ = params['weight'].shape
        bias = 'bias' in params

        # For Depthwise Conv, multiply in_channels by number of groups
        # if groups is not passed as arg, use its default value 1
        kwargs['in_channels'] = in_channels * kwargs.get('groups', 1)
        kwargs['out_channels'] = out_channels
        kwargs['kernel_size'] = kernel_size
        kwargs['bias'] = bias

        module = torch.nn.Conv2d(**kwargs)
        # Replace nn.Conv2D params using F.Conv2D arguments
        module.weight = params['weight']
        if bias:
            module.bias = params['bias']
    return module


def merge_args_and_kwargs(node: torch.fx.node, arguments_to_fetch: Dict) -> Dict:
    """
    Merge args and kwargs into a single kwargs and return it
    :param node: node to fetch args and kwargs from
    :param arguments_to_fetch: dictionary containing arguments' indices in args and keys in kwargs
    :return: single merged kwargs
    """
    n_args = len(node.args)
    kwargs = {}
    for index, key in arguments_to_fetch.items():
        value = None
        if n_args > index:
            value = node.args[index]
        elif key in node.kwargs:
            value = node.kwargs[key]

        if value is not None:
            kwargs[key] = value
    return kwargs


def get_node_attr(node: torch.fx.node):
    """
    Codes modified from https://pytorch.org/docs/stable/fx.html#the-interpreter-pattern

    :param node: node to fetch data from
    :return: value returned from node
    """
    def fetch_attr(target: str):
        target_atoms = target.split('.')
        attr_itr = node.graph.owning_module
        for i, atom in enumerate(target_atoms):
            if not hasattr(attr_itr, atom):
                raise RuntimeError(f"Node referenced nonexistant target {'.'.join(target_atoms[:i])}")
            attr_itr = getattr(attr_itr, atom)
        return attr_itr

    assert node.op == 'get_attr'

    return fetch_attr(node.target)


def concat_create_node(symbolic_traced_model: torch.fx.GraphModule, module_name: str, node: torch.fx.node) \
        -> torch.fx.node:
    """
    Create the node to be inserted in the graph model.

    :param symbolic_traced_model: Symbolically traced model
    :param module_name: Qualified module name in symbolic_traced_model hierarchy corresponding to new node
    :param node: Current node in the graph after which new node will be inserted
    :return: torch.fx.node to be inserted in the graph
    """

    with symbolic_traced_model.graph.inserting_after(node):
        # call_module only accepts tuple as args but node.args[0] can be a list. Convert it into a tuple
        # If node.args[0] is already a tuple, tuple() will do nothing
        new_node = symbolic_traced_model.graph.call_module(module_name, args=tuple(node.args[0]))
        return new_node


def concat_create_module(node: torch.fx.node) -> torch.nn.Module:
    """
    Create the replacement module.

    :param node: Current node in the graph after which new node will be inserted
    :return: New module.
    """

    num_args = len(node.args)
    if num_args == 1 and 'dim' not in node.kwargs:
        # Handle torch.cat being called with default parameter dim
        kwargs = node.kwargs
        module = elementwise_ops.Concat()
    else:
        axis = node.args[1] if num_args > 1 else node.kwargs['dim']
        module = elementwise_ops.Concat(axis)
        kwargs = {'axis': axis}

    for key, value in kwargs.items():
        setattr(module, key, value)

    return module

def interpolate_create_node(symbolic_traced_model: torch.fx.GraphModule, module_name: str, node: torch.fx.node)\
        -> torch.fx.node:
    """
    Create the node to be inserted in the graph model for interpolate.
    :param symbolic_traced_model: Symbolically traced model
    :param module_name: Qualified module name in symbolic_traced_model hierarchy corresponding to new node
    :param node: Current node in the graph after which new node will be inserted
    :return: torch.fx.node to be inserted in the graph
    """
    # Merge args and kwargs.
    args = [node.args[0]]
    for arg in node.kwargs.values():
        args.append(arg)

    with symbolic_traced_model.graph.inserting_after(node):
        new_node = symbolic_traced_model.graph.call_module(module_name, args=tuple(args))
        return new_node


def interpolate_create_module(node: torch.fx.node) -> torch.nn.Module:
    """
    Create the replacement module.

    :param node: Current node in the graph after which new node will be inserted
    :return: New module.
    """
    _ = node.kwargs
    module = elementwise_ops.Interpolate()

    return module


special_handler_functions = {
    # Special handling functions for creating node and module
    'cat': {'node_fn': concat_create_node, 'module_fn': concat_create_module},
    'interpolate': {'node_fn': interpolate_create_node, 'module_fn': interpolate_create_module},
    'conv2d': {'node_fn': conv2d_create_node, 'module_fn': conv2d_create_module}
}


def prepare_model(model: torch.nn.Module, modules_to_exclude: List[torch.nn.Module] = None,
                  concrete_args: Optional[Dict[str, Any]] = None) -> torch.fx.GraphModule:
    """
    Prepare and modify the pytorch model for AIMET features using torch.FX symbolic tracing API.

    #1 Replace torch.nn.functional by torch.nn.Module.
    #2 Create new independent torch.nn.Module instances for reused/duplicate module.

    Example #1 Replace torch.nn.functional by torch.nn.module::

        class ModelWithFunctionalReLU(torch.nn.Module):

            def __init__(self):
                super(ModelWithFunctionalReLU, self).__init__()
                self.conv1 = torch.nn.Conv2d(3, 32, kernel_size=3)

            def forward(self, *inputs):
                x = self.conv1(inputs[0])
                x = torch.nn.functional.relu(x, inplace=True)
                return x

        model = ModelWithFunctionalReLU().eval()
        model_transformed = prepare_model(model)

    This function can replace the ReLU of type torch.nn.functional by type torch.nn.Module and make sure
    both the modified and original model are functionally same.

    Example #2 Create new module for reused/duplicate module::

        class ModelWithDuplicateReLU(torch.nn.Module):

            def __init__(self):
                super(ModelWithDuplicateReLU, self).__init__()
                self.conv1 = torch.nn.Conv2d(3, 32, kernel_size=3)
                self.relu = torch.nn.ReLU(inplace=True)

            def forward(self, *inputs):
                x = self.relu(inputs[0])
                x = self.conv1(x)
                x = self.relu(x)
                return x

        model = ModelWithDuplicateReLU().eval()
        model_transformed = prepare_model(model)

    This function can create new independent torch.nn.ReLU type module for reused module and make sure
    both the modified and original model are functionally same.

    Limitations of torch.fx symbolic trace API:

    #1 Dynamic control flow where conditions depend on some of the input values. This limitation can be overcome by
    binding concrete values to arguments during symbolic tracing::

        def f(x, flag):
            if flag: return x
            else: return x*2

        torch.fx.symbolic_trace(f) # Fails!
        torch.fx.symbolic_trace(f, concrete_args={'flag': True}) # Passes!

    #2 Non-torch functions which does not use __torch_function__ mechanism is not supported by default in symbolic
    tracing. If we do not want to capture them in symbolic tracing then use torch.fx.wrap() API at module-scope level::

        import torch
        import torch.fx
        torch.fx.wrap('len')  # call the API at module-level scope.
        torch.fx.wrap('sqrt') # call the API at module-level scope.

        class ModelWithNonTorchFunction(torch.nn.Module):
            def __init__(self):
                super(ModelWithNonTorchFunction, self).__init__()
                self.conv = torch.nn.Conv2d(3, 4, kernel_size=2, stride=2, padding=2, bias=False)

            def forward(self, *inputs):
                x = self.conv(inputs[0])
                return x / sqrt(len(x))

        model = ModelWithNonTorchFunction().eval()
        model_transformed = prepare_model(model)

    :param model: pytorch Model to be modified.
    :param modules_to_exclude: List of modules to exclude when tracing.
    :param concrete_args: Allows you to partially specialize your function, whether it's to remove control flow or
     data structures. If the model has control flow, torch.fx won't be able to trace the model. Check
     torch.fx.symbolic_trace API in detail.
    :return: Modified pytorch Model
    """
    model.eval()
    device = get_device(model)
    symbolic_traced_model = _trace_model(model, modules_to_exclude, concrete_args)

    # Prepare model and perform checks to make sure the graph is well-formed.
    _prepare_helper(symbolic_traced_model)
    _verify_symbolic_traced_model(symbolic_traced_model)

    symbolic_traced_model.eval()
    symbolic_traced_model.to(device)
    return symbolic_traced_model


def _trace_model(model: torch.nn.Module, modules_to_exclude: Optional[List[torch.nn.Module]],
                 concrete_args: Optional[Dict[str, Any]]):
    """
    Overrides the is_leaf_module() method of parent class when modules_to_exclude list is not None.

    :param model: pytorch Model to be modified.
    :param modules_to_exclude: List of modules to exclude when tracing.
    :param concrete_args: Concrete arguments that should not be treated as Proxies.
    :return: Traced model.
    """
    class Tracer(torch.fx.Tracer):
        """
        Override is_leaf_module() method of parent class.
        """
        def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
            if modules_to_exclude and m in modules_to_exclude:
                return True
            return super(Tracer, self).is_leaf_module(m, module_qualified_name)

    # Symbolic tracing frontend - captures the semantics of the module
    tracer = Tracer()
    graph = tracer.trace(model, concrete_args=concrete_args)
    symbolic_traced_model = torch.fx.GraphModule(tracer.root, graph)

    return symbolic_traced_model


def _prepare_helper(symbolic_traced_model: torch.fx.GraphModule):
    """
    Helper for prepare_model().

    :param symbolic_traced_model: Symbolically traced model.
    """
    unique_nodes = set()

    # Modify the symbolically traced model by iterating over all the nodes
    for node in symbolic_traced_model.graph.nodes:

        # Create new module for functional nodes
        if node.op in ['call_function', 'call_method']:
            functional_name = _find_functional_name_for_node(node)
            if functional_name:
                # Instantiate new module for functional node
                new_module = _create_module_for_functional_node(node, functional_name)
                new_nodule_name = 'module_' + node.name
                setattr(symbolic_traced_model, new_nodule_name, new_module)
                # Create the node for new module in the graph
                _create_node_for_new_module(symbolic_traced_model, node, new_nodule_name, functional_name)
                logger.info("Functional         : Adding new module for node: {%s} ", node.name)

        # Create new module for reused/duplicate nodes
        elif node.target in unique_nodes:
            if node.op == 'call_module':
                # Instantiate new module for reused node
                new_module = _create_module_for_reused_node(node, symbolic_traced_model)
                new_nodule_name = 'module_' + node.name
                setattr(symbolic_traced_model, new_nodule_name, new_module)
                # Create the node for new module in the graph
                _create_node_for_new_module(symbolic_traced_model, node, new_nodule_name)
                logger.info("Reused/Duplicate   : Adding new module for node: {%s} ", node.name)
        else:
            unique_nodes.add(node.target)


def _verify_symbolic_traced_model(symbolic_traced_model: torch.fx.GraphModule):
    """
    Does some checks to make sure the graph is well formed and recompile the forward() method of symbolic_traced
    model from its graph
    :param symbolic_traced_model: Symbolically traced model
    :return: None
    """
    symbolic_traced_model.graph.lint()
    symbolic_traced_model.recompile()


def _create_node_for_new_module(symbolic_traced_model: torch.fx.GraphModule, node: torch.fx.node,
                                module_name: str, functional_name: str = None):
    """
    Insert 'call module' node into graph and replace all the uses of 'node' with newly added node and erase the
    the old node from graph.
    :param symbolic_traced_model: Symbolically traced model
    :param node: Current node in the graph after which new node will be inserted
    :param module_name: Qualified module name in symbolic_traced_model hierarchy corresponding to new node
    :param functional_name: Original functional name
    :return: None
    """
    with symbolic_traced_model.graph.inserting_after(node):
        if functional_name:
            if functional_name in functional_to_module_special_handling_map.keys():
                new_node = special_handler_functions[functional_name]['node_fn'](symbolic_traced_model, module_name, node)
            else:
                new_node = symbolic_traced_model.graph.call_module(module_name, args=node.args)
        else:
            new_node = symbolic_traced_model.graph.call_module(module_name, args=node.args)

        node.replace_all_uses_with(new_node)
    symbolic_traced_model.graph.erase_node(node)


def _find_functional_name_for_node(node: torch.fx.node) -> Union[str, None]:
    """
    For given node, find corresponding functional name from functional_to_module lookup
    :param node: torch.fx Node
    :return: corresponding functional name if found, else None
    """

    combined_ops_map = {**functional_to_module_map, **functional_to_module_special_handling_map}
    for functional_name in combined_ops_map:
        # \b boundary character to find the exact match from the functional_to_module lookup
        pattern = r"\b" + functional_name + r"\b"
        if search(pattern, str(node.target)):
            return functional_name

    return None


def _create_module_for_functional_node(node: torch.fx.node, functional_name: str) -> torch.nn.Module:
    """
    For given node and functional name, create torch.nn.Module with same parameters as functional node parameters
    :param node: torch.fx Node
    :param functional_name: Functional name for given node
    :return: New module
    """
    kwargs = node.kwargs

    # Instantiate new module from lookup
    if functional_name in functional_to_module_map.keys():
        module = functional_to_module_map[functional_name]()
        # Set the parameters for module from node.kwargs
        for key, value in kwargs.items():
            setattr(module, key, value)
    elif functional_name in functional_to_module_special_handling_map:
        module = special_handler_functions[functional_name]['module_fn'](node)
    else:
        raise ValueError("Unsupported module: {}".format(functional_name))
    return module


def _create_module_for_reused_node(node: torch.fx.node, symbolic_traced_model: torch.fx.GraphModule) ->\
        torch.nn.Module:
    """
    For given reused/Duplicate node in symbolically traced model, create new module with same parameters as
    original module
    :param node: Reused/Duplicate torch.fx Node
    :param symbolic_traced_model: Symbolically traced model
    :return: New module
    """
    # Get the original module and return newly deep copied module
    module = _get_module_for_dotted_name(symbolic_traced_model, node.target)
    new_module = copy.deepcopy(module)

    return new_module


def _get_module_for_dotted_name(module: torch.fx.GraphModule, dotted_name: str) -> torch.nn.Module:
    """
    For given dotted name, find the module
    :param module: module to be found
    :param dotted_name: dotted name of module
    :return: module
    """
    if '.' in dotted_name:
        module_name, _, remainder = dotted_name.partition('.')
        return _get_module_for_dotted_name(module._modules[module_name], remainder) # pylint: disable=protected-access

    return getattr(module, dotted_name)


def get_module_for_activation_fn(act_fn: torch.nn.functional):
    """
    returns module instance for functional tyoe handled within PT transformers for activation functions.
    :param act_fn: activation function implemented as a functional.
    :return: module equivalent for the activation function.
    """

    if act_fn not in functional_op_to_module_map:
        logger.error("Unsupported activation function {%s}", act_fn)
        return None
    module = functional_op_to_module_map[act_fn]()
    return module


def prepare_pt_transformer_for_quantsim(transformer_model: torch.nn.Module):
    """
    Replaces functionals with modules for activation function, updates model in-place.
    :param transformer_model: model with PyTorch nn.Transformer layer
    :return: updated model with modules for activation function.
    """

    for module in transformer_model.modules():

        # encoder layer or decoder layer type is the leaf level node to be updated within nn.transformer layer
        if isinstance(module, torch.nn.TransformerEncoderLayer) and not isinstance(module.activation, torch.nn.Module):
            module.activation = get_module_for_activation_fn(module.activation)

        if isinstance(module, torch.nn.TransformerDecoderLayer) and not isinstance(module.activation, torch.nn.Module):
            module.activation = get_module_for_activation_fn(module.activation)

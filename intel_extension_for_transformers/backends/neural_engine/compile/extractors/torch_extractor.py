#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The neural engine torch extractor file."""

from ..graph_utils import LazyImport
from .. import logger
from ..graph.graph import Graph
from ..ops.op import OPERATORS
from ..onnx_utils import graph_node_names_details
from ..graph_utils import names_from_input, get_data_dtype, construct_node
from ..ops.tensor import Tensor
from ..torch_utils import get_node_name, op_maps
from .. import graph_utils as util

torch = LazyImport('torch')

def removeUnusedNode(graph, unused_nodes):
    remove_list = []
    for node in graph.nodes():
        if node.kind() in unused_nodes:
            in_val = node.inputsAt(0)
            out_val = node.outputsAt(0)
            out_val.replaceAllUsesWith(in_val)
            remove_list.append(node)

    # remove ListConstruct followed by cat/stack/einsum
    for node in graph.nodes():
        if node.kind() == 'prim::ListConstruct' and node.outputsAt(0).type().str() == 'Tensor[]':
            out_val = node.outputsAt(0)
            for val_user in out_val.uses():
                next_node = val_user.user
                if next_node.kind() == 'aten::cat' or next_node.kind() == 'aten::stack':
                    for i in range(node.inputsSize()):
                        next_node.addInput(node.inputsAt(i))
                    next_node.addInput(next_node.inputsAt(1))
                    next_node.removeInput(0)
                    next_node.removeInput(0)
                    remove_list.append(node)
                elif next_node.kind() == 'aten::einsum':
                    for i in range(node.inputsSize()):
                        next_node.addInput(node.inputsAt(i))
                    next_node.removeInput(1)
                    remove_list.append(node)

    for node in remove_list:
        node.destroy()

class TorchExtractor(object):
    """The TorchExtractor class.

    Decorate the node in model.graph_def, and the new node has the attributes like input_tensors
    and output_tensors, these tensors record the source/dest op name. All of these nodes
    (in a list) will compose a graph, which is Graph class, as the return object.

    Args:
        model: Torchscript Model

    Return:
        Graph: Graph class, the new graph object
    """
    @classmethod
    def __call__(self, model):
        """The __call__ function of the extractor."""
        graph, _ = torch._C._jit_pass_lower_graph(model.graph, model._c)
        torch._C._jit_pass_dce(graph)
        torch._C._jit_pass_remove_inplace_ops(graph)
        torch._C._jit_pass_lower_all_tuples(graph)
        torch._C._jit_pass_constant_propagation(graph)
        removeUnusedNode(graph, ['aten::dropout', 'prim::NumToTensor', 'aten::to', 'aten::contiguous',
                                 'aten::alias', 'aten::Int', 'aten::ScalarImplicit'])
        logger.info('Start to extarct torch model ops...')
        new_graph = Graph()
        new_graph.framework_modeling_config['framework'] = 'torch'
        graph_nodes_dict = {}
        util.quant_info_init()

        # insert graph.inputs
        model_input_tensors = []
        for idx, in_tensor in enumerate(graph.inputs()):
            dest_ops = []
            for val_user in in_tensor.uses():
                next_node = val_user.user
                dest_ops.append(get_node_name(next_node))

            input_tensor = Tensor(name=in_tensor.debugName(),
                source_op=[],
                dest_op=dest_ops,
                shape=[-1, -1],
                data=None,
                dtype='fp32'  # TODO: check dtype
                )
            graph_nodes_dict[in_tensor.debugName()] = input_tensor
            model_input_tensors.append(input_tensor)

        # parse weights
        for node in graph.nodes():
            if node.kind() == 'prim::Constant' and node.outputsAt(0).type().kind() == 'TensorType':
                out_val = node.outputsAt(0)
                tensor_name = out_val.debugName()
                out_tensor = out_val.toIValue()
                dest_ops = []
                for val_user in out_val.uses():
                    next_node = val_user.user
                    dest_ops.append(get_node_name(next_node))
                if out_tensor.dtype in [torch.qint8]:
                    # extrace min max from tensor
                    # TODO: support per-channel
                    fp32_data = out_tensor.dequantize()
                    fp32_min = fp32_data.min().numpy()
                    fp32_max = fp32_data.max().numpy()
                    min_tensor = Tensor(name=tensor_name + "_min",
                        source_op=[],
                        dest_op=dest_ops,
                        shape=list(fp32_min.shape),
                        data=fp32_min,
                        dtype=get_data_dtype(fp32_min)
                        )
                    max_tensor = Tensor(name=tensor_name + "_max",
                        source_op=[],
                        dest_op=dest_ops,
                        shape=list(fp32_max.shape),
                        data=fp32_max,
                        dtype=get_data_dtype(fp32_max)
                        )
                    #graph_nodes_dict[tensor_name + "_min"] = min_tensor
                    #graph_nodes_dict[tensor_name + "_max"] = max_tensor
                    util.insert_quant_info(tensor_name, [min_tensor, max_tensor, 's8'])

                    # ensure weight is sym quantized.
                    # out_tensor = out_tensor.int_repr()
                    weight_scale = 127.0 / max(abs(fp32_min), abs(fp32_max))
                    out_tensor = torch.round(fp32_data * weight_scale).to(torch.int8)

                if out_tensor.dtype == torch.float64:
                    out_tensor = out_tensor.to(torch.float32)
                weight = out_tensor.detach().numpy()
                weight_tensor = Tensor(name=tensor_name,
                    source_op=[],
                    dest_op=dest_ops,
                    shape=list(out_tensor.shape),
                    data=weight,
                    dtype=get_data_dtype(weight)
                    )
                graph_nodes_dict[tensor_name] = weight_tensor
        input_data_node = construct_node('input_data',
                                            'Input',
                                            output_tensors=model_input_tensors)
        #new_graph.inputs_dict = graph_nodes_dict
        input_tensors_name = [item for item in graph_nodes_dict]
        new_graph.input_tensors_name = input_tensors_name
        for node in graph.nodes():
            if node.kind() not in op_maps:
                logger.error("node kind {} is not mapped.".format(node.kind()))
                import sys; sys.exit(1)
            op_type = op_maps[node.kind()]
            if op_type not in OPERATORS.keys():
                    op_type = "OpAny"
            new_node = OPERATORS[op_type]()
            new_node.ori_node = node
            new_node.extract('torch', node, model, graph_nodes_dict)
            new_graph.insert_nodes(len(new_graph.nodes), [new_node])

        new_graph.insert_nodes(0, [input_data_node])
        return new_graph

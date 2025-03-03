# Copyright 2020-2022 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Operators for array."""
import copy
import functools
import itertools
import numbers
from collections import Counter

import numpy as np

from mindspore import log as logger
from mindspore import context
from mindspore.common.initializer import Zero
from .. import signature as sig
from .._utils import get_broadcast_shape, is_shape_unknown, is_shape_known
from ..primitive import Primitive, PrimitiveWithInfer, PrimitiveWithCheck, prim_attr_register, _run_op
from ..._checkparam import Rel
from ..._checkparam import Validator as validator
from ..._checkparam import _check_3d_int_or_tuple
from ...common import dtype as mstype
from ...common._decorator import deprecated
from ...common.parameter import Parameter
from ...common.tensor import Tensor
from ..._c_expression import Tensor as Tensor_


class _ScatterOp(PrimitiveWithInfer):
    """
    Defines Scatter operators
    """
    __mindspore_signature__ = (
        sig.make_sig('x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    def _check_scatter_shape(self, x_shape, indices_shape, updates_shape, prim_name):
        if indices_shape != [-1] and updates_shape and updates_shape != indices_shape + x_shape[1:]:
            raise ValueError(f"For '{prim_name}', "
                             f"updates_shape = indices_shape + input_x_shape[1:], but got input_x_shape: {x_shape}, "
                             f"indices_shape: {indices_shape}, updates_shape: {updates_shape}.")

    @prim_attr_register
    def __init__(self, use_locking=False):
        """Initialize _ScatterOp"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)

    def infer_shape(self, x_shape, indices_shape, updates_shape):
        self._check_scatter_shape(x_shape, indices_shape, updates_shape, self.name)
        return x_shape

    def infer_dtype(self, x_dtype, indices_dtype, updates_dtype):
        validator.check_tensor_dtype_valid('indices', indices_dtype, [mstype.int32], self.name)
        args = {"x": x_dtype, "updates": updates_dtype}
        validator.check_tensors_dtypes_same_and_valid(args, mstype.number_type, self.name)
        return x_dtype


class UnravelIndex(Primitive):
    """
    Converts an array of flat indices into a tuple of coordinate arrays.

    Inputs:
        - **input_indices** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
        - **input_dims** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.

    Outputs:
        Tensor, the shape of tensor is :math:`(y_1, y_2, ..., y_S)`.

    Raises:
        TypeError: The data type of input0 need be same with input1.
        TypeError: Both input data types are supported only support int32, int64.
        ValueError: Dims shape must be equal to 1 or indices shape must be equal to 1 or 0.
        ValueError: Index out of boundary or index must be greater than 0.
        ValueError: All dimensions must be greater than 0.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Example:
        >>> indices = Tensor(np.array([2, 5]), mindspore.int32)
        >>> dims = Tensor(np.array([3, 3]), mindspore.int32)
        >>> output = ops.UnravelIndex()(indices, dims)
        >>> print(output)
        [[0 2]
         [1 2]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Shape"""


class _ScatterOpDynamic(PrimitiveWithCheck):
    """
    Defines Scatter operators with dynamic shape
    """
    __mindspore_signature__ = (
        sig.make_sig('x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    def _check_scatter_shape(self, x_shape, indices_shape, updates_shape, prim_name):
        # x_shape cannot be dynamic
        if np.any(np.array(x_shape) == -1):
            raise ValueError(f"For '{prim_name}', the 'input_x' does not support dynamic shape, "
                             f"but got the shape of 'input_x' is {x_shape}.")
        # support indices and updates dynamic
        if np.any(np.array(indices_shape) == -1) or np.any(np.array(updates_shape) == -1):
            pass
        elif indices_shape != [-1] and updates_shape and updates_shape != indices_shape + x_shape[1:]:
            raise ValueError(f"For '{prim_name}', "
                             f"updates_shape = indices_shape + input_x_shape[1:], but got input_x_shape: {x_shape}, "
                             f"indices_shape: {indices_shape}, updates_shape: {updates_shape}.")

    @prim_attr_register
    def __init__(self, use_locking=False):
        """Initialize _ScatterOpDynamic"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)

    def check_shape(self, x_shape, indices_shape, updates_shape):
        self._check_scatter_shape(x_shape, indices_shape, updates_shape, self.name)

    def check_dtype(self, x_dtype, indices_dtype, updates_dtype):
        validator.check_tensor_dtype_valid('indices', indices_dtype, [mstype.int32, mstype.int64], self.name)
        args = {"x": x_dtype, "updates": updates_dtype}
        validator.check_tensors_dtypes_same_and_valid(args, mstype.number_type, self.name)


class _ScatterNdOp(_ScatterOp):
    """
    Defines _ScatterNd operators
    """

    def _check_scatter_shape(self, x_shape, indices_shape, updates_shape, prim_name):
        validator.check('the dimension of x', len(x_shape),
                        'the dimension of indices', indices_shape[-1], Rel.GE)
        if indices_shape[:-1] + x_shape[indices_shape[-1]:] != updates_shape:
            raise ValueError(f"For '{prim_name}', updates_shape = "
                             f"indices_shape[:-1] + x_shape[indices_shape[-1]:], but got x_shape: {x_shape}, "
                             f"indices_shape: {indices_shape}, updates_shape: {updates_shape}.")


def _check_infer_attr_reduce(axis, keep_dims, prim_name):
    validator.check_value_type('keep_dims', keep_dims, [bool], prim_name)
    validator.check_value_type('axis', axis, [int, tuple], prim_name)
    if isinstance(axis, tuple):
        for index, value in enumerate(axis):
            validator.check_value_type('axis[%d]' % index, value, [int], prim_name)


class Expand(Primitive):
    """
    Returns a new view of the self tensor with singleton dimensions expanded to a larger size.

    Note:
        Passing -1 as the size for a dimension means not changing the size of that dimension.
        Tensor can be also expanded to a larger number of dimensions, and the new ones will be appended at the front.
        For the new dimensions, the size cannot be set to -1.

    Inputs:
         - **x** (Tensor) - The shape of tensor is (x_1, x_2, ..., x_R).
         - **shape** (Tensor) - The new shape of x.

    Outputs:
         - **y** (Tensor) - Tensor after expansion.

    Raises:
        TypeError: If any input is not Tensor.
        TypeError: If the type of `shape` is not one of the following dtype: int16, int32, int64.
        ValueError: If `shape` is not a 1-D tensor.
        ValueError: If the size of `shape` is less than the size of `x.shape`.
        ValueError: If the expanded `shape` is not equal to the existing shape of `x` at a dimension that is not 1.
        ValueError: If the expanded size < 0 and it is in a leading, non-existing dimension.
        ValueError: If the number of elements of output is more than 1000000.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[1], [2], [3]]), mindspore.float32)
        >>> shape = Tensor(np.array([3,4]), mindspore.int32)
        >>> expand = ops.Expand()
        >>> y = expand(x, shape)
        >>> print(y)
        [[1. 1. 1. 1.]
         [2. 2. 2. 2.]
         [3. 3. 3. 3.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Expand."""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['x', 'shape'], outputs=['y'])


class ExpandDims(PrimitiveWithCheck):
    """
    Adds an additional dimension to `input_x` at the given axis.

    Refer to :func:`mindspore.ops.expand_dims` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_tensor = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> expand_dims = ops.ExpandDims()
        >>> output = expand_dims(input_tensor, 0)
        >>> print(output)
        [[[2. 2.]
          [2. 2.]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ExpandDims"""
        self.init_prim_io_names(inputs=['x', 'axis'], outputs=['output'])

    def infer_value(self, input_x, axis):
        value = None
        if input_x is not None and axis is not None:
            value = Tensor(np.expand_dims(input_x.asnumpy(), axis))
        return value


class DType(Primitive):
    """
    Returns the data type of the input tensor as mindspore.dtype.

    Inputs:
        - **input_x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.

    Outputs:
        mindspore.dtype, the data type of a tensor.

    Raises:
        TypeError: If `input_x` is not a Tensor.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_tensor = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> output = ops.DType()(input_tensor)
        >>> print(output)
        Float32
    """

    @prim_attr_register
    def __init__(self):
        """Initialize DType"""


class SameTypeShape(PrimitiveWithInfer):
    """
    Checks whether the data type and shape of two tensors are the same.

    Refer to :func:`mindspore.ops.same_type_shape` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> input_y = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> output = ops.SameTypeShape()(input_x, input_y)
        >>> print(output)
        [[2. 2.]
         [2. 2.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Same"""

    def __call__(self, x, y):
        """run in PyNative mode"""
        validator.check_value_type('x', x, Tensor, self.name)
        validator.check_value_type('y', y, Tensor, self.name)
        validator.check('x dtype', x.dtype, 'y dtype', y.dtype, Rel.EQ, self.name, TypeError)
        validator.check('x shape', x.shape, 'y shape', y.shape, Rel.EQ, self.name)
        return x

    def __infer__(self, x, y):
        validator.check_subclass('x', x['dtype'], mstype.tensor, self.name)
        validator.check_subclass('y', y['dtype'], mstype.tensor, self.name)
        validator.check('x dtype', x['dtype'], 'y dtype', y['dtype'], Rel.EQ, self.name, TypeError)
        validator.check('x shape', x['shape'], 'y shape', y['shape'], Rel.EQ, self.name)
        return x


class CheckNumerics(Primitive):
    """
    Checks a tensor for NaN and Inf values.

    Inputs:
        - **x** (Tensor) - Input Tensor of any dimension. The data type is float16, float32 or float64.

    Outputs:
        Tensor, has the same shape and data type as `x` if `x` has no nan or inf values.

    Raises:
        TypeError: If `x` data type is not float16, float32, float64.
        RuntimeError: If `x` has nan or inf values.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[1, 3], [2, 4]], dtype=np.float32))
        >>> checknumerics = ops.CheckNumerics()
        >>> output = checknumerics(x)
        >>> print(output)
        [[1. 3.]
         [2. 4.]]
    """

    @prim_attr_register
    def __init__(self):
        """init CheckNumerics"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class Cast(PrimitiveWithInfer):
    """
    Returns a tensor with the new specified data type.

    Inputs:
        - **input_x** (Union[Tensor, Number]) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
          The tensor to be cast.
        - **type** (dtype.Number) - The valid data type of the output tensor. Only constant value is allowed.

    Outputs:
        Tensor, the shape of tensor is the same as `input_x`, :math:`(x_1, x_2, ..., x_R)`.

    Raises:
        TypeError: If `input_x` is neither Tensor nor Number.
        TypeError: If `type` is not a Number.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_np = np.random.randn(2, 3, 4, 5).astype(np.float32)
        >>> input_x = Tensor(input_np)
        >>> type_dst = mindspore.int32
        >>> cast = ops.Cast()
        >>> output = cast(input_x, type_dst)
        >>> print(output.dtype)
        Int32
        >>> print(output.shape)
        (2, 3, 4, 5)
    """

    @prim_attr_register
    def __init__(self):
        # if primitive need setattr in __infer__ need add this flag
        """Initialize Cast"""
        self.init_prim_io_names(inputs=['x', 'dst_type'], outputs=['output'])

    def check_elim(self, x, dtype):
        if isinstance(x, (Tensor, numbers.Number, Parameter)):
            if isinstance(x, Parameter):
                data = x.data
                if data.dtype == dtype:
                    return (True, x)
            if isinstance(x, Tensor) and x.dtype == dtype:
                x = Tensor(x)
                x.set_cast_dtype()
                return (True, x)
            if isinstance(x, numbers.Number):
                return (True, Tensor(x, dtype=dtype))
        return (False, None)

    def __infer__(self, x, t):
        src_type = x['dtype']
        dst_type = t['value']

        validator.check_subclass("input_x", src_type, [mstype.tensor, mstype.number], self.name)
        validator.check_subclass("type", dst_type, mstype.number, self.name)

        if isinstance(src_type, type(mstype.tensor)):
            src_type = x['dtype'].element_type()
        if isinstance(dst_type, type(mstype.tensor)):
            dst_type = dst_type.element_type()
        self.add_prim_attr('DstT', dst_type)
        self.add_prim_attr('SrcT', src_type)
        self.add_prim_attr('dst_type', dst_type)

        value = None
        if x['value'] is not None:
            np_dst_type = mstype.dtype_to_nptype(dst_type)
            if isinstance(x['value'], (int, float)):
                value = Tensor(np.array(x['value']).astype(np_dst_type))
            else:
                value = Tensor(x['value'].asnumpy().astype(np_dst_type))

        out = {'shape': x['shape'],
               'dtype': mstype.tensor_type(t['value']),
               'value': value}
        if 'min_shape' in x and 'max_shape' in x:
            out['min_shape'] = x['min_shape']
            out['max_shape'] = x['max_shape']
        if 'min_value' in x and 'max_value' in x:
            np_dst_type = mstype.dtype_to_nptype(dst_type)
            if isinstance(x['min_value'], (int, float, tuple, list)):
                min_value = Tensor(np.array(x['min_value']).astype(np_dst_type))
            else:
                min_value = Tensor(x['min_value'].asnumpy().astype(np_dst_type))
            min_value = tuple(min_value.asnumpy().tolist())
            if isinstance(x['max_value'], (int, float, tuple, list)):
                max_value = Tensor(np.array(x['max_value']).astype(np_dst_type))
            else:
                max_value = Tensor(x['max_value'].asnumpy().astype(np_dst_type))
            max_value = tuple(max_value.asnumpy().tolist())
            out['min_value'] = min_value
            out['max_value'] = max_value
        return out


class IsSubClass(PrimitiveWithInfer):
    """
    Checks whether this type is a sub-class of another type.

    Inputs:
        - **sub_type** (mindspore.dtype) - The type to be checked. Only constant value is allowed.
        - **type_** (mindspore.dtype) - The target type. Only constant value is allowed.

    Outputs:
        bool, the check result.

    Raises:
        TypeError: If `sub_type` or `type_` is not a Type.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> output = ops.IsSubClass()(mindspore.int32,  mindspore.intc)
        >>> print(output)
        True
    """

    @prim_attr_register
    def __init__(self):
        pass

    def __infer__(self, sub_type, type_):
        sub_type_t = sub_type['value']
        type_v = type_['value']

        validator.check_value_type("sub_type", sub_type_t, [mstype.Type], self.name)
        validator.check_value_type("type_", type_v, [mstype.Type], self.name)

        value = mstype._issubclass_(sub_type_t, type_v)  # pylint: disable=W0212

        out = {'shape': (),
               'dtype': mstype.type_type,
               'value': value}
        return out


class IsInstance(PrimitiveWithInfer):
    """
    Checks whether an object is an instance of a target type.

    Inputs:
        - **inst** (Any Object) - The instance to be checked. Only constant value is allowed.
        - **type_** (mindspore.dtype) - The target type. Only constant value is allowed.

    Outputs:
        bool, the check result.

    Raises:
        TypeError: If `type_` is not a Type.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> inst = 1
        >>> output = ops.IsInstance()(inst, mindspore.int32)
        >>> print(output)
        False
    """

    @prim_attr_register
    def __init__(self):
        pass

    def __infer__(self, inst, type_):
        sub_type_t = inst['dtype']
        type_v = type_['value']

        validator.check_value_type("type_", type_v, [mstype.Type], self.name)

        if type_v == mstype.list_:
            value = isinstance(sub_type_t, list)
        elif type_v == mstype.tuple_:
            value = isinstance(sub_type_t, tuple)
        else:
            value = mstype._issubclass_(sub_type_t, type_v)  # pylint: disable=W0212

        out = {'shape': (),
               'dtype': mstype.type_type,
               'value': value}
        return out


class Col2Im(Primitive):
    r"""
    Combines an array of sliding local blocks into a large containing tensor.

    Consider a batched :attr:`input` tensor containing sliding local blocks,
    e.g., patches of images, of shape :math:`(N, C, \prod(\text{kernel_size}), L)`,
    where :math:`N` is batch dimension, :math:`C` is channel dimension,
    :math:`\prod(\text{kernel_size})` is the block size, and
    :math:`L` is the total number of blocks. This operation combines these
    local blocks into the large :attr:`output` tensor of
    shape :math:`(N, C, \text{output_size}[0], \text{output_size}[1], \dots)`
    by summing the overlapping values.

    .. math::
        L = \prod_d \left\lfloor\frac{\text{output_size}[d] + 2 \times \text{padding}[d] %
            - \text{dilation}[d] \times (\text{kernel_size}[d] - 1) - 1}{\text{stride}[d]} + 1\right\rfloor,

    where :math:`d` is over all spatial dimensions.

    :attr:`output_size` describes the spatial shape of the large containing
    tensor of the sliding local blocks. It is useful to resolve the ambiguity
    when multiple input shapes map to same number of sliding blocks, e.g.,
    with ``stride > 0``.

    The :attr:`padding`, :attr:`stride` and :attr:`dilation` arguments specify
    how the sliding blocks are retrieved.

    :attr:`stride` controls the stride for the sliding blocks.

    :attr:`padding` controls the amount of implicit zero-paddings on both
    sides for :attr:`padding` number of points for each dimension before
    reshaping.

    :attr:`dilation` controls the spacing between the kernel points.

    Args:
        kernel_size (Union[int, tuple[int], list[int]]): The size of the kernel, should be two int
            for height and width. If type is int, it means that height equal with width. Must be specified.
        dilation (Union[int, tuple[int], list[int]]): The size of the dilation, should be two int
            for height and width. If type is int, it means that height equal with width. Default: 1.
        padding (Union[int, tuple[int], list[int]]): The size of the padding, should be two int
            for height and width. If type is int, it means that height equal with width. Default: 1.
        stride (Union[int, tuple[int], list[int]]): The size of the stride, should be two int
            for height and width. If type is int, it means that height equal with width. Default: 0.

    Inputs:
        - **x** (Tensor) - 4D tensor with data type float16 or float32.
        - **output_size** (Tensor) - 1D tensor with 2 elements of data type int32.

    Outputs:
        Tensor, a 4-D Tensor with same type of input `x`.

    Supported Platforms:

    Raises:
        TypeError: If :attr:`kernel_size`, `dilation`, `padding`, `stride` data type is not in
            Union[int, tuple[int], list[int]].
        ValueError: If :attr:`kernel_size`, `dilation`, `padding`, `stride` value is not
            greater than zero or elements number more than 2.
        ValueError: If x.shape[2] != kernel_size[0] * kernel_size[1].
        ValueError: If x.shape[3] does not match the calculated number of sliding blocks.

    Examples:
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> from mindspore import dtype as mstype
        >>> from mindspore.ops.operations.array_ops import Col2Im
        >>> x = Tensor(input_data=np.random.rand(16, 16, 4, 25), dtype=mstype.float32)
        >>> output_size = Tensor(input_data=[8, 8], dtype=mstype.int32)
        >>> col2im = Col2Im(kernel_size=[2, 2], dilation=[2, 2], padding=[2, 2], stride=[2, 2])
        >>> y = col2im(x, output_size)
        >>> print(y.shape)
        (16, 16, 8, 8)
    """

    @prim_attr_register
    def __init__(self, kernel_size, dilation=1, padding=0, stride=1):
        """Initialize Col2Im."""
        self.add_prim_attr("cust_aicpu", self.name)
        self.init_prim_io_names(inputs=['x', 'output_size'], outputs=['y'])
        validator.check_value_type('kernel_size', kernel_size, [int, list, tuple], self.name)
        validator.check_value_type('dilation', dilation, [int, list, tuple], self.name)
        validator.check_value_type('padding', padding, [int, list, tuple], self.name)
        validator.check_value_type('stride', stride, [int, list, tuple], self.name)

        self.kernel_size = (kernel_size, kernel_size) if isinstance(kernel_size, int) else kernel_size
        self.dilation = (dilation, dilation) if isinstance(dilation, int) else dilation
        self.padding = (padding, padding) if isinstance(padding, int) else padding
        self.stride = (stride, stride) if isinstance(stride, int) else stride

        validator.check("kernel_size size", len(self.kernel_size), "", 2, Rel.EQ, self.name)
        validator.check_positive_int_sequence(self.kernel_size, "kernel_size", self.name)
        validator.check("dilation size", len(self.dilation), "", 2, Rel.EQ, self.name)
        validator.check_positive_int_sequence(self.dilation, "dilation", self.name)
        validator.check("padding size", len(self.padding), "", 2, Rel.EQ, self.name)
        validator.check_non_negative_int_sequence(self.padding, "padding", self.name)
        validator.check("stride size", len(self.stride), "", 2, Rel.EQ, self.name)
        validator.check_positive_int_sequence(self.stride, "stride", self.name)

        self.add_prim_attr('kernel_size', self.kernel_size)
        self.add_prim_attr('dilation', self.dilation)
        self.add_prim_attr('padding', self.padding)
        self.add_prim_attr('stride', self.stride)


class Reshape(PrimitiveWithInfer):
    """
    Rearranges the input Tensor based on the given shape.

    Refer to :func:`mindspore.ops.reshape` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> reshape = ops.Reshape()
        >>> output = reshape(input_x, (3, 2))
        >>> print(output)
        [[-0.1  0.3]
         [ 3.6  0.4]
         [ 0.5 -3.2]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Reshape"""
        self.init_prim_io_names(inputs=['tensor', 'shape'], outputs=['output'])

    @staticmethod
    def _get_shape_and_range(x, shape):
        """ get min and max shape when output shape is dynamic"""
        min_shape = None
        max_shape = None
        x_shp = x['shape']
        if is_shape_unknown(shape['shape']):
            out_shape = [-2]
            return out_shape, min_shape, max_shape

        shape_rank = shape['shape'][0]
        if not x_shp:
            # x is a scalar, output shape fixed
            out_shape = [1] * shape_rank
            return out_shape, min_shape, max_shape

        out_shape = [-1] * shape_rank
        if "max_value" in shape and "min_value" in shape:
            min_shape = shape["min_value"]
            max_shape = shape["max_value"]
            if len(min_shape) != shape_rank or len(max_shape) != shape_rank:
                min_shape = [1] * shape_rank
                max_shape = [int(np.prod(max_shape))] * shape_rank
            else:
                for i in range(shape_rank):
                    if min_shape[i] == max_shape[i] and min_shape[i] != 1:
                        out_shape[i] = min_shape[i]
        elif is_shape_unknown(x_shp) and "max_shape" in x:
            # when dynamic memory allocation is supported, max_shape can be left out
            min_shape = [1] * shape_rank
            max_shape = [int(np.prod(x["max_shape"]))] * shape_rank
        return out_shape, min_shape, max_shape

    @staticmethod
    def _update_shape_range(out, x, shape_v, neg_index, dim_prod):
        """ update min and max shape of output when input shape is dynamic"""
        x_min_shape = x['shape']
        x_max_shape = x['shape']
        if 'max_shape' in x:
            x_max_shape = x['max_shape']
        if 'min_shape' in x:
            x_min_shape = x['min_shape']
        max_arr_prod = np.prod(x_max_shape)
        min_arr_prod = np.prod(x_min_shape)
        max_shape = list(shape_v)
        min_shape = list(shape_v)
        if neg_index != -1:
            max_shape[neg_index] = int(max_arr_prod / dim_prod)
            min_shape[neg_index] = int(min_arr_prod / dim_prod)
        out['max_shape'] = tuple(max_shape)
        out['min_shape'] = tuple(min_shape)
        return out

    def _update_shape_and_value(self, out, x, shape_v, dim_prod, neg_index):
        """ update shape, value and min / max value of output when input shape is known"""
        x_shp = x['shape']
        if dim_prod <= 0:
            raise ValueError(f"For '{self.name}', the shape of 'input_x' is {x_shp}, "
                             f"the value of 'input_shape' is {shape_v}. "
                             f"The product of 'input_shape' should > 0, but got {dim_prod}.")
        arr_prod = np.prod(x_shp)
        if neg_index != -1:
            shape_v[neg_index] = int(arr_prod / dim_prod)
            dim_prod *= shape_v[neg_index]
        if dim_prod != arr_prod:
            raise ValueError(f"For '{self.name}', the product of the 'input_x' shape "
                             f"should be equal to product of 'input_shape', but got product of the"
                             f" shape of 'input_x': {arr_prod}, product of 'input_shape': {dim_prod}.")
        out['shape'] = tuple(shape_v)

        if x['value'] is not None:
            out['value'] = Tensor(x['value'].asnumpy().reshape(shape_v))

        if ('min_value' in x and 'max_value' in x):
            ret_min_value = np.array(x['min_value']).reshape(shape_v)
            ret_max_value = np.array(x['max_value']).reshape(shape_v)
            ret_min_value = tuple(ret_min_value.tolist())
            ret_max_value = tuple(ret_max_value.tolist())
            out['min_value'] = ret_min_value
            out['max_value'] = ret_max_value
        return out

    def __infer__(self, x, shape):
        shape_v = shape['value']
        validator.check_subclass("x", x['dtype'], mstype.tensor, self.name)
        # for shape is not constant
        if shape_v is None:
            out_shape, min_shape, max_shape = self._get_shape_and_range(x, shape)
            return {
                'shape': out_shape,
                'dtype': x['dtype'],
                'value': None,
                'max_shape': max_shape,
                'min_shape': min_shape
            }

        if isinstance(shape_v, Tensor_):
            validator.check_tensor_dtype_valid("shape", shape['dtype'], [mstype.int32, mstype.int64], self.name)
            shape_v = shape_v.asnumpy().tolist()
        else:
            validator.check_value_type("shape", shape_v, [tuple], self.name)
            shape_v = list(shape_v)

        neg_index = -1
        dim_prod = 1
        for i, shp_i in enumerate(shape_v):
            validator.check_value_type("shape[%d]" % i, shp_i, [int], self.name)
            if shp_i == -1:
                if neg_index != -1:
                    raise ValueError(f"For '{self.name}', there can be at most one '-1' in 'input_shape', "
                                     f"but got {shape_v}.")
                neg_index = i
            else:
                dim_prod *= shp_i

        out = {'shape': shape_v,
               'dtype': x['dtype'],
               'value': None}

        if is_shape_unknown(x['shape']):
            out = self._update_shape_range(out, x, shape_v, neg_index, dim_prod)
        else:
            out = self._update_shape_and_value(out, x, shape_v, dim_prod, neg_index)
        return out


class Shape(Primitive):
    """
    Returns the shape of the input tensor. And it used to be static shape.

    Refer to :func:`mindspore.ops.shape` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.ones(shape=[3, 2, 1]), mindspore.float32)
        >>> shape = ops.Shape()
        >>> output = shape(input_x)
        >>> print(output)
        (3, 2, 1)
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Shape"""


class TensorShape(Primitive):
    """
    Returns the shape of the input tensor.

    Refer to :func:`mindspore.ops.dyn_shape` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.ones(shape=[3, 2, 1]), mindspore.float32)
        >>> shape = ops.TensorShape()
        >>> output = shape(input_x)
        >>> print(output)
        [3 2 1]
    """

    @prim_attr_register
    def __init__(self):
        """init Shape"""
        self.init_prim_io_names(inputs=['input_x'], outputs=['output'])


class DynamicShape(Primitive):
    """
    Same as operator TensorShape. DynamicShape will be deprecated in the future.
    Please use TensorShape instead.

    Supported Platforms:
        Deprecated
    """

    @deprecated("1.7", "TensorShape", True)
    @prim_attr_register
    def __init__(self):
        """init Shape"""
        self.init_prim_io_names(inputs=['tensor'], outputs=['output'])
        self.add_prim_attr('is_dynamic_shape', True)


class Squeeze(Primitive):
    """
    Return the Tensor after deleting the dimension of size 1 in the specified `axis`.

    If :math:`axis=()`, it will remove all the dimensions of size 1.
    If `axis` is specified, it will remove the dimensions of size 1 in the given `axis`.
    For example, if the dimension is not specified :math:`axis=()`, input shape is (A, 1, B, C, 1, D),
    then the shape of the output Tensor is (A, B, C, D). If the dimension is specified, the squeeze operation
    is only performed in the specified dimension. If input shape is (A, 1, B), input Tensor will not be
    changed when :math:`axis=0` , but when :math:`axis=1` , the shape of the input Tensor will be changed to (A, B).

    Note:
        - Please note that in dynamic graph mode, the output Tensor will share data with the input Tensor,
    and there is no Tensor data copy process.
        - The dimension index starts at 0 and must be in the range `[-input.ndim, input.ndim]`.

    Args:
        axis (Union[int, tuple(int)]): Specifies the dimension indexes of shape to be removed, which will remove
            all the dimensions of size 1 in the given axis parameter. If specified, it must be int32 or int64.
            Default: (), an empty tuple.

    Inputs:
        - **input_x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.

    Outputs:
        Tensor, the shape of tensor is :math:`(x_1, x_2, ..., x_S)`.

    Raises:
        TypeError: If `axis` is neither an int nor tuple.
        TypeError: If `axis` is a tuple whose elements are not all int.
        ValueError: If the corresponding dimension of the specified axis isn't equal to 1.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.ones(shape=[3, 2, 1]), mindspore.float32)
        >>> squeeze = ops.Squeeze(2)
        >>> output = squeeze(input_x)
        >>> print(output)
        [[1. 1.]
         [1. 1.]
         [1. 1.]]
    """

    @prim_attr_register
    def __init__(self, axis=()):
        """Initialize Squeeze"""
        self.init_prim_io_names(inputs=['x'], outputs=['output'])
        validator.check_value_type('axis', axis, [int, tuple], self.name)
        if isinstance(axis, tuple):
            for idx, item in enumerate(axis):
                validator.check_value_type("axis[%d]" % idx, item, [int], self.name)
        else:
            self.axis = (axis,)
            self.add_prim_attr("axis", (axis,))


class Transpose(Primitive):
    """
    Permutes the dimensions of the input tensor according to input permutation.

    Refer to :func:`mindspore.ops.transpose` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]]), mindspore.float32)
        >>> input_perm = (0, 2, 1)
        >>> transpose = ops.Transpose()
        >>> output = transpose(input_x, input_perm)
        >>> print(output)
        [[[ 1.  4.]
          [ 2.  5.]
          [ 3.  6.]]
         [[ 7. 10.]
          [ 8. 11.]
          [ 9. 12.]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Transpose"""
        self.init_prim_io_names(inputs=['x', 'perm'], outputs=['output'])


class ConjugateTranspose(Primitive):
    """
    Permutes the dimensions of the input tensor according to input permutation.

    The type and rank of the output y is the same as the input x. And the shape and value of the input x
    and the output y satisfy:
    y.shape[i] == x.shape[perm[i]] for i in [0, 1, ..., rank(x) - 1]
    y[i,j,k,...,s,t,u] == conj(x[perm[i], perm[j], perm[k],...,perm[s], perm[t], perm[u]])

    Inputs:
        - x (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
        - perm (tuple[int]) - The permutation to be converted. The elements in `perm` are composed of
          the indexes of each dimension of `x`. The length of `perm` and the shape of `x` must be
          the same. Only constant value is allowed. Must be in the range [0, rank(x)).

    Outputs:
        Tensor, the type of output tensor is the same as `x` and the shape of output tensor is decided by the
        shape of `x` and the value of `Conj(perm)`.

    Raises:
        TypeError: If `perm` is not a tuple.
        ValueError: If length of shape of `x` is not equal to length of shape of `perm`.
        ValueError: If the same element exists in `perm`.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import ConjugateTranspose
        >>> x = Tensor(np.array([[1 + 1j,2 + 2j], [3 + 3j, 4 + 4j]]), mindspore.complex64)
        >>> perm = (1, 0)
        >>> conjugate_transpose = ConjugateTranspose()
        >>> output = conjugate_transpose(x, perm)
        >>> print(output)
            [[1.-1.j 3.-3.j]
            [2.-2.j 4.-4.j]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ConjugateTranspose"""
        self.init_prim_io_names(inputs=['x', 'perm'], outputs=['output'])


class Unique(Primitive):
    """
    Returns the unique elements of input tensor and also return a tensor containing the index of each value of input
    tensor corresponding to the output unique tensor.

    The output contains Tensor `y` and Tensor `idx`, the format is probably similar to (`y`, `idx`).
    The shape of Tensor `y` and Tensor `idx` is different in most cases, because Tensor `y` will be duplicated,
    and the shape of Tensor `idx` is consistent with the input.

    To get the same shape between `idx` and `y`, please ref to 'UniqueWithPad' operator.

    Inputs:
        - **input_x** (Tensor) - The input tensor.
          The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.

    Outputs:
        Tuple, containing Tensor objects (`y`, `idx`), `y` is a tensor with the
        same type as `input_x`, and contains the unique elements in `x`.
        `idx` is a tensor containing indices of elements in
        the input corresponding to the output tensor.

    Raises:
        TypeError: If `input_x` is not a Tensor.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([1, 2, 5, 2]), mindspore.int32)
        >>> output = ops.Unique()(input_x)
        >>> print(output)
        (Tensor(shape=[3], dtype=Int32, value= [1, 2, 5]), Tensor(shape=[4], dtype=Int32, value= [0, 1, 2, 1]))
        >>> y = output[0]
        >>> print(y)
        [1 2 5]
        >>> idx = output[1]
        >>> print(idx)
        [0 1 2 1]
        >>> # As can be seen from the above, y and idx shape
        >>> # note that for GPU, this operator must be wrapped inside a model, and executed in graph mode.
        >>> class UniqueNet(nn.Cell):
        ...     def __init__(self):
        ...         super(UniqueNet, self).__init__()
        ...         self.unique_op = ops.Unique()
        ...
        ...     def construct(self, x):
        ...         output, indices = self.unique_op(x)
        ...         return output, indices
        ...
        >>> input_x = Tensor(np.array([1, 2, 5, 2]), mindspore.int32)
        >>> net = UniqueNet()
        >>> output = net(input_x)
        >>> print(output)
        (Tensor(shape=[3], dtype=Int32, value= [1, 2, 5]), Tensor(shape=[4], dtype=Int32, value= [0, 1, 2, 1]))
    """

    @prim_attr_register
    def __init__(self):
        self.init_prim_io_names(inputs=['x'], outputs=['output'])


class UniqueConsecutive(Primitive):
    """
    Returns the elements that are unique in each consecutive group of equivalent elements in the input tensor.

    Refer to :func:`mindspore.ops.unique_consecutive` for more detail.

    Supported Platforms:
        ``GPU``

    Examples:
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> from mindspore import dtype as mstype
        >>> from mindspore.ops.operations.array_ops import UniqueConsecutive
        >>> x = Tensor(np.array([1, 1, 2, 2, 3, 1, 1, 2]), mstype.int32)
        >>> unique_consecutive = UniqueConsecutive(True, True, None)
        >>> output, idx, counts = unique_consecutive(x)
        >>> print(output)
        [1 2 3 1 2]
        >>> print(idx)
        [0 0 1 1 2 3 3 4]
        >>> print(counts)
        [2 2 1 2 1]
    """

    @prim_attr_register
    def __init__(self, return_idx=False, return_counts=False, axis=None):
        self.init_prim_io_names(inputs=['x'], outputs=['output'])
        validator.check_value_type("return_idx", return_idx, [bool], self.name)
        validator.check_value_type("return_counts", return_counts, [bool], self.name)
        validator.check_value_type("axis", axis, [int, type(None)], self.name)
        self.add_prim_attr("return_idx", return_idx)
        self.add_prim_attr("return_counts", return_counts)
        self.add_prim_attr("axis", axis)


class Gather(Primitive):
    r"""
    Returns the slice of the input tensor corresponding to the elements of `input_indices` on the specified `axis`.

    .. warning::
        This is an experimental prototype that is subject to change and/or deletion.

    The following figure shows the calculation process of Gather commonly:

    .. image:: Gather.png

    where params represents the input `input_params`, and indices represents the index to be sliced `input_indices`.

    Refer to :func:`mindspore.ops.gather` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> # case1: input_indices is a Tensor with shape (5, ).
        >>> input_params = Tensor(np.array([1, 2, 3, 4, 5, 6, 7]), mindspore.float32)
        >>> input_indices = Tensor(np.array([0, 2, 4, 2, 6]), mindspore.int32)
        >>> axis = 0
        >>> output = ops.Gather()(input_params, input_indices, axis)
        >>> print(output)
        [1. 3. 5. 3. 7.]
        >>> # case2: input_indices is a Tensor with shape (2, 2). When the input_params has one dimension,
        the output shape is equal to the input_indices shape.
        >>> input_indices = Tensor(np.array([[0, 2], [2, 6]]), mindspore.int32)
        >>> axis = 0
        >>> output = ops.Gather()(input_params, input_indices, axis)
        >>> print(output)
        [[ 1. 3.]
         [ 3. 7.]]
        >>> # case3: input_indices is a Tensor with shape (2, ). input_params is a Tensor with shape (3, 4) and axis is 0.
        >>> input_params = Tensor(np.array([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]), mindspore.float32)
        >>> input_indices = Tensor(np.array([0, 2]), mindspore.int32)
        >>> axis = 0
        >>> output = ops.Gather()(input_params, input_indices, axis)
        >>> print(output)
        [[1.  2.  3.  4.]
         [9. 10. 11. 12.]]
        >>> # case4: input_indices is a Tensor with shape (2, ). input_params is a Tensor with shape (3, 4) and axis is 1.
        >>> input_params = Tensor(np.array([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]), mindspore.float32)
        >>> input_indices = Tensor(np.array([0, 2]), mindspore.int32)
        >>> axis = 1
        >>> output = ops.Gather()(input_params, input_indices, axis)
        >>> print(output)
        [[1.  3.]
         [5.  7.]
         [9. 11.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Gather"""
        self.init_prim_io_names(inputs=['params', 'indices', 'axis'], outputs=['output'])


class GatherV2(PrimitiveWithCheck):
    """
    Same as operator Gather. GatherV2 will be deprecated in the future.
    Please use Gather instead.
    """

    @deprecated("1.1", "Gather", True)
    @prim_attr_register
    def __init__(self):
        """Initialize GatherV2"""
        self.init_prim_io_names(inputs=['params', 'indices', 'axis'], outputs=['output'])

    def __check__(self, params, indices, axis):
        validator.check_subclass("params", params['dtype'], mstype.tensor, self.name)
        validator.check_tensor_dtype_valid("indices", indices['dtype'], mstype.int_type, self.name)
        validator.check_subclass("axis", axis['dtype'], [mstype.number], self.name)
        axis_v = axis['value']
        validator.check_value_type('axis', axis_v, [int], self.name)
        rank = len(params['shape'])
        validator.check_int_range(axis_v, -rank, rank, Rel.INC_LEFT, "axis", self.name)


class SparseGatherV2(PrimitiveWithCheck):
    """
    Returns a slice of input tensor based on the specified indices and axis.

    Inputs:
        - **input_params** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
        - **input_indices** (Tensor) - The shape of tensor is :math:`(y_1, y_2, ..., y_S)`.
          Specifies the indices of elements of the original Tensor, must be in the range
          `[0, input_params.shape[axis])`.
        - **axis** (int) - Specifies the dimension index to gather indices.

    Outputs:
        Tensor, the shape of tensor is :math:`(z_1, z_2, ..., z_N)`.

    Raises:
        TypeError: If `axis` is not an int.

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> input_params = Tensor(np.array([[1, 2, 7, 42], [3, 4, 54, 22], [2, 2, 55, 3]]), mindspore.float32)
        >>> input_indices = Tensor(np.array([1, 2]), mindspore.int32)
        >>> axis = 1
        >>> out = ops.SparseGatherV2()(input_params, input_indices, axis)
        >>> print(out)
        [[2. 7.]
         [4. 54.]
         [2. 55.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SparseGatherV2"""
        self.init_prim_io_names(inputs=['params', 'indices', 'axis'], outputs=['output'])
        self.add_prim_attr('bprop_return_sparse', True)

    def __check__(self, params, indices, axis):
        validator.check_subclass("params", params['dtype'], mstype.tensor, self.name)
        validator.check_tensor_dtype_valid("indices", indices['dtype'], mstype.int_type, self.name)
        validator.check_subclass("axis", axis['dtype'], [mstype.number], self.name)
        axis_v = axis['value']
        validator.check_value_type('axis', axis_v, [int], self.name)
        rank = len(params['shape'])
        validator.check_int_range(axis_v, -rank, rank, Rel.INC_LEFT, "axis", self.name)


class Padding(Primitive):
    """
    Extends the last dimension of the input tensor from 1 to pad_dim_size, by filling with 0.

    Refer to :func:`mindspore.ops.padding` for more details.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import Padding
        >>> x = Tensor(np.array([[8], [10]]), mindspore.float32)
        >>> pad_dim_size = 4
        >>> output = Padding(pad_dim_size)(x)
        >>> print(output)
        [[ 8.  0.  0.  0.]
         [10.  0.  0.  0.]]
    """

    @prim_attr_register
    def __init__(self, pad_dim_size=8):
        """Initialize padding"""
        validator.check_value_type("pad_dim_size", pad_dim_size, [int], self.name)
        validator.check_positive_int(pad_dim_size, "pad_dim_size", self.name)
        self.pad_dim_size = pad_dim_size


class UniqueWithPad(PrimitiveWithCheck):
    """
    Returns unique elements and relative indexes in 1-D tensor, filled with padding num.

    The basic function is the same as the Unique operator, but the UniqueWithPad operator adds a Pad function.
    The returned tuple(`y`, `idx`) after the input Tensor `x` is processed by the unique operator,
    in which the shapes of `y` and `idx` are mostly not equal. Therefore, in order to solve the above situation,
    the UniqueWithPad operator will fill the `y` Tensor with the `pad_num` specified by the user
    to make it have the same shape as the Tensor `idx`.

    Refer to :func:`mindspore.ops.unique_with_pad` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([1, 1, 5, 5, 4, 4, 3, 3, 2, 2,]), mindspore.int32)
        >>> pad_num = 8
        >>> output = ops.UniqueWithPad()(x, pad_num)
        >>> print(output)
        (Tensor(shape=[10], dtype=Int32, value= [1, 5, 4, 3, 2, 8, 8, 8, 8, 8]),
         Tensor(shape=[10], dtype=Int32, value= [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]))
    """

    @prim_attr_register
    def __init__(self):
        """init UniqueWithPad"""

    def __check__(self, x, pad_num):
        validator.check_tensor_dtype_valid("x", x['dtype'], [mstype.int32, mstype.int64], self.name)
        validator.check_subclass("pad_num", pad_num['dtype'], [mstype.int32, mstype.int64], self.name)
        x_shape = list(x['shape'])
        validator.check("rank of x", len(x_shape), "expected", 1, Rel.EQ, self.name)


class Split(PrimitiveWithCheck):
    """
    Splits the input tensor into output_num of tensors along the given axis and output numbers.

    Refer to :func:`mindspore.ops.split` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> split = ops.Split(1, 2)
        >>> x = Tensor(np.array([[1, 1, 1, 1], [2, 2, 2, 2]]), mindspore.int32)
        >>> print(x)
        [[1 1 1 1]
         [2 2 2 2]]
        >>> output = split(x)
        >>> print(output)
        (Tensor(shape=[2, 2], dtype=Int32, value=
        [[1, 1],
         [2, 2]]), Tensor(shape=[2, 2], dtype=Int32, value=
        [[1, 1],
         [2, 2]]))
        >>> split = ops.Split(1, 4)
        >>> output = split(x)
        >>> print(output)
        (Tensor(shape=[2, 1], dtype=Int32, value=
        [[1],
         [2]]), Tensor(shape=[2, 1], dtype=Int32, value=
        [[1],
         [2]]), Tensor(shape=[2, 1], dtype=Int32, value=
        [[1],
         [2]]), Tensor(shape=[2, 1], dtype=Int32, value=
        [[1],
         [2]]))
    """

    @prim_attr_register
    def __init__(self, axis=0, output_num=1):
        """Initialize Split"""
        validator.check_value_type("axis", axis, [int], self.name)
        validator.check_value_type("output_num", output_num, [int], self.name)
        validator.check_positive_int(output_num, "output_num", self.name)
        self.axis = axis
        self.output_num = output_num

    def __check__(self, x):
        validator.check_subclass("x", x['dtype'], mstype.tensor, self.name)
        x_shape = list(x['shape'])
        dim = len(x_shape)
        validator.check_int_range(self.axis, -dim, dim, Rel.INC_LEFT, 'axis value', self.name)
        if is_shape_known(x_shape):
            # only validate when shape fully known
            output_valid_check = x_shape[self.axis] % self.output_num
            if output_valid_check != 0:
                raise ValueError(f"For '{self.name}', the specified axis of 'input_x' must be divided exactly by "
                                 f"'output_num', but got the shape of 'input_x' in 'axis' {self.axis} is "
                                 f"{x_shape[self.axis]}, 'output_num': {self.output_num}.")
        size_splits = [x_shape[self.axis] // self.output_num] * self.output_num
        self.add_prim_attr('size_splits', size_splits)


class Rank(PrimitiveWithInfer):
    """
    Returns the rank of a tensor.

    Refer to :func:`mindspore.ops.rank` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_tensor = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> rank = ops.Rank()
        >>> output = rank(input_tensor)
        >>> print(output)
        2
        >>> print(type(output))
        <class 'int'>
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Rank"""

    def __infer__(self, x):
        validator.check_subclass("x", x['dtype'], mstype.tensor, self.name)
        out = {'shape': None,
               'dtype': None,
               'value': len(x['shape'])}
        return out


class Size(PrimitiveWithInfer):
    r"""
    Returns a Scalar of type int that represents the size of the input Tensor and the total number of elements in the
    Tensor.

    Refer to :func:`mindspore.ops.size` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[2, 2], [2, 2]]), mindspore.float32)
        >>> size = ops.Size()
        >>> output = size(input_x)
        >>> print(output)
        4
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Size"""

    def __infer__(self, x):
        size = 1
        validator.check_subclass("x", x['dtype'], mstype.tensor, self.name)
        shp = x['shape']
        if not shp:
            size = 0
        else:
            size = functools.reduce(lambda x, y: x * y, x['shape'])
        out = {'shape': None,
               'dtype': mstype.int64,
               'value': size}
        return out


class MatrixDiagV3(Primitive):
    """
    Returns a batched diagonal tensor with given batched diagonal values.

    Refer to :func:`mindspore.ops.matrix_diag` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import MatrixDiagV3
        >>> x = Tensor(np.array([[8, 9, 0],
        ...                      [1, 2, 3],
        ...                      [0, 4, 5]]), mindspore.float32)
        >>> k =Tensor(np.array([-1, 1]), mindspore.int32)
        >>> num_rows = Tensor(np.array(3), mindspore.int32)
        >>> num_cols = Tensor(np.array(3), mindspore.int32)
        >>> padding_value = Tensor(np.array(11), mindspore.float32)
        >>> matrix_diag_v3 = MatrixDiagV3(align='LEFT_RIGHT')
        >>> output = matrix_diag_v3(x, k, num_rows, num_cols, padding_value)
        >>> print(output)
        [[ 1.  8. 11.]
         [ 4.  2.  9.]
         [11.  5.  3.]]
        >>> print(output.shape)
        (3, 3)
    """

    @prim_attr_register
    def __init__(self, align="RIGHT_LEFT"):
        """"Initialize MatrixDiagV3"""
        validator.check_value_type("align", align, [str], self.name)
        validator.check_string(align, ['LEFT_RIGHT', 'RIGHT_LEFT', 'LEFT_LEFT', 'RIGHT_RIGHT'], 'align', self.name)
        self.init_prim_io_names(inputs=['x', 'k', 'num_rows', 'num_cols', 'padding_value'], outputs=['y'])


class MatrixDiagPartV3(Primitive):
    r"""
    Returns the diagonal part of a tensor.

    Refer to :func:`mindspore.ops.matrix_diag_part` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[1, 2, 3, 4],
        ...                      [5, 6, 7, 8],
        ...                      [9, 8, 7, 6]]), mindspore.float32)
        >>> k =Tensor(np.array([1, 3]), mindspore.int32)
        >>> padding_value = Tensor(np.array(9), mindspore.float32)
        >>> matrix_diag_part_v3 = ops.operations.array_ops.MatrixDiagPartV3(align='RIGHT_LEFT')
        >>> output = matrix_diag_part_v3(x, k, padding_value)
        >>> print(output)
        [[9. 9. 4.]
         [9. 3. 8.]
         [2. 7. 6.]]
        >>> print(output.shape)
        (3, 3)
    """

    @prim_attr_register
    def __init__(self, align="RIGHT_LEFT"):
        """"Initialize MatrixDiagPartV3"""
        self.add_prim_attr("max_length", 200000000)
        validator.check_value_type("align", align, [str], self.name)
        validator.check_string(align, ['LEFT_RIGHT', 'RIGHT_LEFT', 'LEFT_LEFT', 'RIGHT_RIGHT'], 'align', self.name)
        self.init_prim_io_names(inputs=['x', 'k', 'padding_value'], outputs=['y'])


class MatrixSetDiagV3(Primitive):
    r"""
    Returns a batched matrix tensor with new batched diagonal values.
    Given x and diagonal, this operation returns a tensor with the same shape and values as x, except for the specified
    diagonals of the innermost matrices. These will be overwritten by the values in diagonal. Some diagonals are shorter
    than max_diag_len and need to be padded.
    The diagonal.shape[-2] must be equal to num_diags calculated by k[1] - k[0] + 1. The diagonal.shape[-1] must be
    equal to the longest diagonal value max_diag_len calculated by min(x.shape[-2] + min(k[1], 0), x.shape[-1] +
    min(-k[0], 0)). Let x have r + 1 dimensions [I, J, ..., L, M, N]. The diagonal tensor has rank r with shape [I, J,
    ..., L, max_diag_len] when k is an integer or k[0] == k[1]. Otherwise, it has rank r + 1 with shape [I, J, ..., L,
    num_diags, max_diag_len].

    Args:
        align (string): An optional string from: "RIGHT_LEFT"(default), "LEFT_RIGHT", "LEFT_LEFT", "RIGHT_RIGHT". Align
            is a string specifying how superdiagonals and subdiagonals should be aligned, respectively. "RIGHT_LEFT"
            aligns superdiagonals to the right (left-pads the row) and subdiagonals to the left (right-pads the row).

    Inputs:
        - **x** (Tensor) - Rank r + 1, where r >= 1.
        - **diagonal** (Tensor) - A Tensor. Have the same dtype as x. Rank r when k is an integer or k[0] == k[1].
          Otherwise, it has rank r + 1.
        - **k** (Tensor) - A Tensor of type int32. Diagonal offset(s). Positive value means superdiagonal, 0 refers to
          the main diagonal, and negative value means subdiagonals. k can be a single integer (for a single diagonal) or
          a pair of integers specifying the low and high ends of a matrix band. k[0] must not be larger than k[1]. The
          value of k has restructions, meaning value of k must be in (-x.shape[-2], x.shape[-1]). Input k must be const
          Tensor when taking Graph mode.

    Outputs:
        A Tensor. Has the same type as x.
        Let x has r+1 dimensions [I, J, ..., L, M, N].
        The output is a tensor of rank r+1 with dimensions [I, J, ..., L, M, N], the same as input x.

    Raises:
        TypeError: If any input is not Tensor.
        TypeError: If input `x` and `diagonal` are not the same dtype.
        TypeError: If `k` is not int32 dtype.
        ValueError: If `align` is not a string or not in the valid range.
        ValueError: If rank of `k` is not equal to 0 or 1.
        ValueError: If rank of `x` is not greater equal to 2.
        ValueError: If size of `k` is not equal to 1 or 2.
        ValueError: If k[1] is not greater equal to k[0] in case the size of `k` is 2.
        ValueError: If the `diagonal` rank size don't match with input `x` rank size.
        ValueError: If the `diagonal` shape value don't match with input `x` shape value.
        ValueError: If the diagonal.shape[-2] is not equal to num_diags calculated by k[1] - k[0] + 1.
        ValueError: If the value of `k` is not in (-x.shape[-2], x.shape[-1]).
        ValueError: If the diagonal.shape[-1] is not equal to the max_diag_len calculated by min(x.shape[-2] + min(k[1],
            0), x.shape[-1] + min(-k[0], 0)).

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[7, 7, 7, 7],
        ...                      [7, 7, 7, 7],
        ...                      [7, 7, 7, 7]]), mindspore.float32)
        >>> diagonal = Tensor(np.array([[0, 9, 1],
        ...                             [6, 5, 8],
        ...                             [1, 2, 3],
        ...                             [4, 5, 0]]), mindspore.float32)
        >>> k =Tensor(np.array([-1, 2]), mindspore.int32)
        >>> matrix_set_diag_v3 = ops.MatrixSetDiagV3(align='RIGHT_LEFT')
        >>> output = matrix_set_diag_v3(x, diagonal, k)
        >>> print(output)
        [[1. 6. 9. 7.]
         [4. 2. 5. 1.]
         [7. 5. 3. 8.]]
        >>> print(output.shape)
        (3, 4)
    """

    @prim_attr_register
    def __init__(self, align="RIGHT_LEFT"):
        """"Initialize MatrixSetDiagV3"""
        self.add_prim_attr("max_length", 200000000)
        validator.check_value_type("align", align, [str], self.name)
        validator.check_string(align, ['LEFT_RIGHT', 'RIGHT_LEFT', 'LEFT_LEFT', 'RIGHT_RIGHT'], 'align', self.name)
        self.init_prim_io_names(inputs=['x', 'diagonal', 'k'], outputs=['y'])


class MatrixBandPart(Primitive):
    r"""
    Copy a tensor setting everything outside a central band in each innermost matrix to zero.

    Refer to :func:`mindspore.ops.matrix_band_part` for more details.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import MatrixBandPart
        >>> matrix_band_part = MatrixBandPart()
        >>> x = np.ones([2, 4, 4]).astype(np.float32)
        >>> output = matrix_band_part(Tensor(x), 2, 1)
        >>> print(output)
        [[[1. 1. 0. 0.]
          [1. 1. 1. 0.]
          [1. 1. 1. 1.]
          [0. 1. 1. 1.]]
         [[1. 1. 0. 0.]
          [1. 1. 1. 0.]
          [1. 1. 1. 1.]
          [0. 1. 1. 1.]]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__(name="MatrixBandPart")
        self.init_prim_io_names(inputs=['x', 'lower', 'upper'], outputs=['y'])


class Fill(PrimitiveWithInfer):
    """
    Create a Tensor of the specified shape and fill it with the specified value.

    Refer to :func:`mindspore.ops.fill` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> fill = ops.Fill()
        >>> output = fill(mindspore.float32, (2, 2), 1)
        >>> print(output)
        [[1. 1.]
         [1. 1.]]
        >>> output = fill(mindspore.float32, (3, 3), 0)
        >>> print(output)
        [[0. 0. 0.]
         [0. 0. 0.]
         [0. 0. 0.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Fill"""

    def __infer__(self, dtype, dims, x):
        validator.check_value_type("shape", dims['value'], [tuple], self.name)
        validator.check_value_type("value", x['value'], [numbers.Number, bool], self.name)
        valid_dtypes = [mstype.bool_, mstype.int8, mstype.int16, mstype.int32, mstype.int64,
                        mstype.uint8, mstype.uint16, mstype.uint32, mstype.uint64,
                        mstype.float16, mstype.float32, mstype.float64, mstype.complex64,
                        mstype.complex128]
        validator.check_types_same_and_valid({"value": dtype['value']}, valid_dtypes, self.name)
        x_nptype = mstype.dtype_to_nptype(dtype['value'])
        if is_shape_known(dims['value']):
            for i, item in enumerate(dims['value']):
                validator.check_positive_int(item, f'dims[{i}]', self.name)
            ret = np.full(dims['value'], x['value'], x_nptype)
            out = {
                'value': Tensor(ret),
                'shape': dims['value'],
                'dtype': x['dtype'],
            }
        else:
            out = {
                'value': None,
                'shape': dims['value'],
                'dtype': x['dtype'],
            }
            if ('min_value' in dims and 'max_value' in dims):
                min_ret_shape = dims['min_shape']
                max_ret_shape = dims['max_shape']
                out['min_shape'] = min_ret_shape
                out['max_shape'] = max_ret_shape
        return out


class Fills(Primitive):
    """
    Create a tensor of the same shape and type as the input tensor and fill it with specified value.

    Refer to :func:`mindspore.ops.fills` for more detail.

    Supported Platforms:
        ``GPU``

    Examples:
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> a = Tensor(np.arange(4).reshape((2,2)).astype('float32'))
        >>> fills = ops.Fills()
        >>> output = fills(a, float(1))
        >>> print(output)
        [[1. 1.]
         [1. 1.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Fills."""
        self.init_prim_io_names(inputs=['x', 'value'], outputs=['y'])


class Ones(Primitive):
    r"""
    Creates a tensor filled with value ones.

    Creates a tensor with shape described by the first argument and
    fills it with value ones in type of the second argument.

    Refer to :func:`mindspore.ops.ones` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> ones = ops.Ones()
        >>> output = ones((2, 2), mindspore.float32)
        >>> print(output)
        [[1. 1.]
         [1. 1.]]
        >>> output = ones((3, 3), mindspore.float32)
        >>> print(output)
        [[1. 1. 1.]
         [1. 1. 1.]
         [1. 1. 1.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Ones"""


class Zeros(Primitive):
    r"""
    Creates a tensor filled with value zeros.

    Creates a tensor with shape described by the first argument and
    fills it with value zeros in type of the second argument.

    Inputs:
        - **shape** (Union[tuple[int], int]) - The specified shape of output tensor.
          Only constant positive int is allowed.
        - **type** (mindspore.dtype) - The specified type of output tensor. Only constant value is allowed.

    Outputs:
        Tensor, has the same type and shape as input shape value.

    Raises:
        TypeError: If `shape` is neither int nor tuple.
        TypeError: If `shape` is a tuple whose elements are not all int.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> zeros = ops.Zeros()
        >>> output = zeros((2, 2), mindspore.float32)
        >>> print(output)
        [[0. 0.]
         [0. 0.]]

    """

    @prim_attr_register
    def __init__(self):
        """Initialize Zeros"""


class OnesLike(Primitive):
    """
    Returns a Tensor with a value of 1 and its shape and data type is the same as the input.

    Refer to :func:`mindspore.ops.ones_like` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> oneslike = ops.OnesLike()
        >>> input_x = Tensor(np.array([[0, 1], [2, 1]]).astype(np.int32))
        >>> output = oneslike(input_x)
        >>> print(output)
        [[1 1]
         [1 1]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize OnesLike"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class ZerosLike(Primitive):
    """
    Returns a Tensor with a value of 0 and its shape and data type is the same as the input.

    Inputs:
        - **input_x** (Tensor) - Input Tensor of any dimension. The data type is int32, int64, float16 or float32.

    Outputs:
        Tensor, has the same shape and data type as `input_x` but filled with zeros.

    Raises:
        TypeError: If `input_x` is not a Tensor.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> zeroslike = ops.ZerosLike()
        >>> input_x = Tensor(np.array([[0, 1], [2, 1]]).astype(np.float32))
        >>> output = zeroslike(input_x)
        >>> print(output)
        [[0. 0.]
         [0. 0.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ZerosLike"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class TupleToArray(PrimitiveWithInfer):
    """
    Converts a tuple to a tensor.

    Refer to :func:`mindspore.ops.tuple_to_array` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = (1,2,3)
        >>> print(type(input_x))
        <class 'tuple'>
        >>> output = ops.TupleToArray()(input_x)
        >>> print(type(output))
        <class 'mindspore.common.tensor.Tensor'>
        >>> print(output)
        [1 2 3]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize TupleToArray"""

    def infer_value(self, x):
        validator.check_value_type("x", x, [tuple], self.name)
        validator.check("size of x", len(x), '', 0, Rel.GT, self.name)
        dtype = type(x[0])
        for i, item in enumerate(x):
            validator.check_value_type(f"x[{i}]", item, [numbers.Number], self.name)
        if not all(isinstance(item, dtype) for item in x):
            raise TypeError(f"For \'{self.name}\', all elements of 'input_x' must be have same type.")
        if isinstance(x[0], int):
            ret = np.array(x, np.int32)
        else:
            ret = np.array(x, np.float32)
        return Tensor(ret)

    def __call__(self, x):
        args = list()
        if isinstance(x, range):
            args.append(tuple(x))
        else:
            args.append(x)
        return _run_op(self, self.name, args)


class ScalarToArray(PrimitiveWithInfer):
    """
    Converts a scalar to a `Tensor`.

    Refer to :func:`mindspore.ops.scalar_to_array` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> op = ops.ScalarToArray()
        >>> input_x = 1.0
        >>> print(type(input_x))
        <class 'float'>
        >>> output = op(input_x)
        >>> print(type(output))
        <class 'mindspore.common.tensor.Tensor'>
        >>> print(output)
        1.0
    """

    @prim_attr_register
    def __init__(self):
        pass

    def infer_value(self, x):
        validator.check_value_type("x", x, [int, float], self.name)
        if isinstance(x, int):
            ret = np.array(x, np.int32)
        else:
            ret = np.array(x, np.float32)
        return Tensor(ret)


class ScalarToTensor(PrimitiveWithInfer):
    """
    Converts a scalar to a `Tensor`, and converts the data type to the specified type.

    Refer to :func:`mindspore.ops.scalar_to_tensor` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> op = ops.ScalarToTensor()
        >>> data = 1
        >>> output = op(data, mindspore.float32)
        >>> print(output)
        1.0
    """

    @prim_attr_register
    def __init__(self):
        pass

    def infer_value(self, x, dtype=mstype.float32):
        validator.check_value_type("x", x, [int, float], self.name)
        validator.check_subclass("dtype", dtype, mstype.number, self.name)
        data_type = mstype.dtype_to_nptype(dtype)
        return Tensor(np.array(x, data_type))


class InvertPermutation(PrimitiveWithInfer):
    r"""
    Computes the inverse of an index permutation.

    This operator is mainly used to calculate the inverse of index permutation.
    It requires a 1-dimensional integer tensor x, which represents the index of a zero-based array,
    and exchanges each value with its index position. In other words, For output tensor y and input tensor x,
    this operation calculates the following values:

    :math:`y[x[i]] = i, \quad i \in [0, 1, \ldots, \text{len}(x)-1]`.

    Note:
        These values must include 0. There must be no duplicate values and the
        values can not be negative.

    Inputs:
        - **input_x** (Union(tuple[int], list[int])) - The input is constructed by multiple
          integers, i.e., :math:`(y_1, y_2, ..., y_S)` representing the indices.
          The values must include 0. There can be no duplicate values or negative values.
          Only constant value is allowed. The maximum value must be equal to length of input_x.

    Outputs:
        tuple[int]. It has the same length as the input.

    Raises:
        TypeError: If `input_x` is neither tuple nor list.
        TypeError: If element of `input_x` is not an int.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> invert = ops.InvertPermutation()
        >>> input_data = (3, 4, 0, 2, 1)
        >>> output = invert(input_data)
        >>> print(output)
        (2, 4, 3, 0, 1)
    """

    @prim_attr_register
    def __init__(self):
        """Initialize InvertPermutation"""
        self.set_const_prim(True)

    def __infer__(self, x):
        x_shp = x['shape']
        x_value = x['value']
        if mstype._issubclass_(x['dtype'], mstype.tensor):  # pylint: disable=W0212
            raise ValueError(f"For \'{self.name}\', the value of 'input_x' must be non-Tensor, but got {x['dtype']}")
        if x_value is None:
            raise ValueError(f"For '{self.name}', the value of 'input_x' can not be None, but got {x_value}.")
        validator.check_value_type("shape", x_shp, [tuple, list], self.name)
        for shp in x_shp:
            if shp:
                x_rank = len(np.array(x_value, np.int64).shape)
                raise ValueError(f"For \'{self.name}\', the dimension of 'input_x' must be 1, but got {x_rank}.")
        for i, value in enumerate(x_value):
            validator.check_value_type("input[%d]" % i, value, [int], self.name)
        z = [x_value[i] for i in range(len(x_value))]
        z.sort()

        for i in range(1, len(z)):
            if z[i - 1] == z[i]:
                raise ValueError(f"For '{self.name}', the 'input_x' can not contain duplicate values, "
                                 f"but got duplicated {z[i]} in the 'input_x'.")
        validator.check(f'value min', min(x_value), '', 0, Rel.EQ, self.name)
        validator.check(f'value max', max(x_value), '', len(x_value) - 1, Rel.EQ, self.name)

        y = [None] * len(x_value)
        for i, value in enumerate(x_value):
            validator.check_value_type("input[%d]" % i, value, [int], self.name)
            validator.check(f'value', z[i], f'index', i, Rel.EQ, self.name)
            y[value] = i
            z.append(value)
        return {'shape': x_shp,
                'dtype': x['dtype'],
                'value': tuple(y)}


class Argmax(PrimitiveWithInfer):
    """
    Returns the indices of the maximum value of a tensor across the axis.

    If the shape of input tensor is :math:`(x_1, ..., x_N)`, the shape of the output tensor will be
    :math:`(x_1, ..., x_{axis-1}, x_{axis+1}, ..., x_N)`.

    Args:
        axis (int): Axis where the Argmax operation applies to. Default: -1.
        output_type (:class:`mindspore.dtype`): An optional data type of `mindspore.dtype.int32`.
            Default: `mindspore.dtype.int32`.

    Inputs:
        - **input_x** (Tensor) - Input tensor. :math:`(N,*)` where :math:`*` means, any number of additional dimensions.
          Support data type list as follows:

          - Ascend: Float16, Float32.
          - GPU: Float16, Float32.
          - CPU: Float16, Float32, Float64.

    Outputs:
        Tensor, indices of the max value of input tensor across the axis.

    Raises:
        TypeError: If `axis` is not an int.
        TypeError: If `output_type` is neither int32 nor int64.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[1, 20, 5], [67, 8, 9], [130, 24, 15]]).astype(np.float32))
        >>> output = ops.Argmax(output_type=mindspore.int32)(input_x)
        >>> print(output)
        [1 0 0]
    """

    @prim_attr_register
    def __init__(self, axis=-1, output_type=mstype.int32):
        """Initialize Argmax"""
        self.init_prim_io_names(inputs=['x'], outputs=['output'])
        validator.check_value_type("axis", axis, [int], self.name)
        validator.check_types_same_and_valid({'output': output_type}, [mstype.int32], self.name)
        self.axis = axis
        self.add_prim_attr('output_type', output_type)

    def infer_shape(self, x_shape):
        axis = self.axis
        if axis is None:
            axis = 0
        x_rank = len(x_shape)
        validator.check_int_range(axis, -x_rank, x_rank, Rel.INC_LEFT, "axis", self.name)
        axis = axis + x_rank if axis < 0 else axis
        ouput_shape = [x_shape[i] for i in range(x_rank) if i != axis]
        return ouput_shape

    def infer_dtype(self, x_dtype):
        validator.check_tensor_dtype_valid("input_x", x_dtype, [mstype.float16, mstype.float32, mstype.float64],
                                           self.name)
        return mstype.tensor_type(self.output_type)


class Argmin(Primitive):
    """
    Returns the indices of the minimum value of a tensor across the axis.

    If the shape of input tensor is :math:`(x_1, ..., x_N)`, the shape of the output tensor is
    :math:`(x_1, ..., x_{axis-1}, x_{axis+1}, ..., x_N)`.

    Args:
        axis (int): Axis where the Argmin operation applies to. Default: -1.
        output_type (:class:`mindspore.dtype`): An optional data type of `mindspore.dtype.int32`.
            Default: `mindspore.dtype.int32`.

    Inputs:
        - **input_x** (Tensor) - Input tensor.
          The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.

    Outputs:
        Tensor, indices of the min value of input tensor across the axis.

    Raises:
        TypeError: If `axis` is not an int.
        TypeError: If `output_type` is neither int32 nor int64.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([2.0, 3.1, 1.2]), mindspore.float32)
        >>> index = ops.Argmin()(input_x)
        >>> print(index)
        2
    """

    @prim_attr_register
    def __init__(self, axis=-1, output_type=mstype.int32):
        """Initialize Argmin"""
        self.init_prim_io_names(inputs=['x'], outputs=['output'])
        validator.check_value_type("axis", axis, [int], self.name)
        validator.check_type_name("output_type", output_type, [mstype.int32, mstype.int64], self.name)
        self.axis = axis
        self.add_prim_attr('output_type', output_type)


class ArgminV2(Primitive):
    """
    Returns the indices of the minimum value of a tensor across the axis.

    If the shape of input tensor is :math:`(x_1, ..., x_N)`, the shape of the output tensor is
    :math:`(x_1, ..., x_{axis-1}, x_{axis+1}, ..., x_N)`.

    Note:
        This operator only supports dynamic shape. As for static shape, please use operator `Argmin` instead.

    Inputs:
        - **x** (Tensor) - Input tensor.
          The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.
        - **axis** (int) - Axis where the Argmin operator applies to. Default: -1.

    Outputs:
        Tensor, indices of the min value of input tensor across the axis.

    Raises:
        TypeError: If `axis` is not an int.

    Supported Platforms:
        ``Ascend``

    Examples:
        >>> class ArgMinV2DynatimicShape(nn.Cell):
        ...     def __init__(self, gather_axis=1, argmin_axis=1):
        ...         super(ArgMinV2DynatimicShape, self).__init__()
        ...         self.unique = P.Unique()
        ...         self.gather = P.Gather()
        ...         self.argmin = ArgminV2()
        ...         self.gather_axis = gather_axis
        ...         self.argmin_axis = argmin_axis
        ...     def construct(self, x, indices):
        ...         unique_index, _ = self.unique(indices)
        ...         y = self.gather(x, unique_index, self.gather_axis)
        ...         z = self.argmin(y, self.argmin_axis)
        ...         return z
        >>>
        >>> x = Tensor(np.array([[4, 8, 1, 6], [4, 3, 6, 2], [4, 4, 1, 1]]).astype(np.float32))
        >>> index = Tensor([1, 2], dtype=mindspore.int32)
        >>> net = ArgMinV2DynatimicShape()
        >>> res = net(x, index)
        >>> print(res)
        [1 0 1]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ArgminV2"""
        self.init_prim_io_names(inputs=['x', 'axis'], outputs=['y'])

    def __call__(self, x, axis=-1):
        args = [x, axis]
        output = _run_op(self, self.name, args)
        return output


class ArgMaxWithValue(Primitive):
    """
    Calculates the maximum value with the corresponding index.

    Calculates the maximum value along with the given axis for the input tensor. It returns the maximum values and
    indices.

    Note:
        In auto_parallel and semi_auto_parallel mode, the first output index can not be used.

    .. warning::
        - If there are multiple maximum values, the index of the first maximum value is used.
        - The value range of "axis" is [-dims, dims - 1]. "dims" is the dimension length of "input_x".

    Also see: func: `mindspore.ops.max`.

    Args:
        axis (int): The dimension to reduce. Default: 0.
        keep_dims (bool): Whether to reduce dimension, if true, the output will keep same dimension with the input,
                          the output will reduce dimension if false. Default: False.

    Inputs:
        - **input_x** (Tensor) - The input tensor, can be any dimension. Set the shape of input tensor as
          :math:`(x_1, x_2, ..., x_N)`. And the data type only support mindspore.float16 or float32.

    Outputs:
        tuple (Tensor), tuple of 2 tensors, containing the corresponding index and the maximum value of the input
        tensor.

        - **index** (Tensor) - The index for the maximum value of the input tensor. If `keep_dims` is true, the shape of
          output tensors is :math:`(x_1, x_2, ..., x_{axis-1}, 1, x_{axis+1}, ..., x_N)`. Otherwise, the shape is
          :math:`(x_1, x_2, ..., x_{axis-1}, x_{axis+1}, ..., x_N)` .
        - **output_x** (Tensor) - The maximum value of input tensor, with the same shape as index.

    Raises:
        TypeError: If data type `input_x` is not float16, float32 and float64.
        TypeError: If `keep_dims` is not a bool.
        TypeError: If `axis` is not an int.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([0.0, 0.4, 0.6, 0.7, 0.1]), mindspore.float32)
        >>> index, output = ops.ArgMaxWithValue()(input_x)
        >>> print(index, output)
        3 0.7
        >>> index, output = ops.ArgMaxWithValue(keep_dims=True)(input_x)
        >>> print(index, output)
        [3] [0.7]
    """

    @prim_attr_register
    def __init__(self, axis=0, keep_dims=False):
        """Initialize ArgMaxWithValue"""
        self.init_prim_io_names(inputs=['input_x'], outputs=['index', 'output_x'])
        validator.check_value_type("axis", axis, [int], self.name)
        validator.check_value_type('keep_dims', keep_dims, [bool], self.name)
        self.axis = axis
        self.keep_dims = keep_dims
        self.add_prim_attr('dimension', self.axis)


class ArgMinWithValue(Primitive):
    """
    Calculates the minimum value with corresponding index, and returns indices and values.

    Calculates the minimum value along with the given axis for the input tensor. It returns the minimum values and
    indices.

    Note:
        In auto_parallel and semi_auto_parallel mode, the first output index can not be used.

    .. warning::
        - If there are multiple minimum values, the index of the first minimum value is used.
        - The value range of "axis" is [-dims, dims - 1]. "dims" is the dimension length of "x".

    Also see: func: `mindspore.ops.arg_min_with_value`.

    Args:
        axis (int): The dimension to reduce. Default: 0.
        keep_dims (bool): Whether to reduce dimension, if true the output will keep the same dimension as the input,
                          the output will reduce dimension if false. Default: False.

    Inputs:
        - **x** (Tensor) - The input tensor, can be any dimension. Set the shape of input tensor as
          :math:`(x_1, x_2, ..., x_N)` .

    Outputs:
        tuple (Tensor), tuple of 2 tensors, containing the corresponding index and the minimum value of the input
        tensor.

        - **index** (Tensor) - The index for the minimum value of the input tensor. If `keep_dims` is true, the shape of
          output tensors is :math:`(x_1, x_2, ..., x_{axis-1}, 1, x_{axis+1}, ..., x_N)`. Otherwise, the shape is
          :math:`(x_1, x_2, ..., x_{axis-1}, x_{axis+1}, ..., x_N)` .
        - **values** (Tensor) - The minimum value of input tensor, with the same shape as index.

    Raises:
        TypeError: If `keep_dims` is not a bool.
        TypeError: If `axis` is not an int.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([0.0, 0.4, 0.6, 0.7, 0.1]), mindspore.float32)
        >>> output = ops.ArgMinWithValue()(x)
        >>> print(output)
        (Tensor(shape=[], dtype=Int32, value= 0), Tensor(shape=[], dtype=Float32, value= 0))
        >>> output = ops.ArgMinWithValue(keep_dims=True)(x)
        >>> print(output)
        (Tensor(shape=[1], dtype=Int32, value= [0]), Tensor(shape=[1], dtype=Float32, value= [ 0.00000000e+00]))
    """

    @prim_attr_register
    def __init__(self, axis=0, keep_dims=False):
        """Initialize ArgMinWithValue"""
        self.axis = axis
        self.keep_dims = keep_dims
        validator.check_value_type('keep_dims', keep_dims, [bool], self.name)
        validator.check_value_type('axis', axis, [int], self.name)


class Tile(PrimitiveWithInfer):
    r"""
    Replicates an input tensor with given multiples times.

    Refer to :func:`mindspore.ops.tile` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> tile = ops.Tile()
        >>> input_x = Tensor(np.array([[1, 2], [3, 4]]), mindspore.float32)
        >>> multiples = (2, 3)
        >>> output = tile(input_x, multiples)
        >>> print(output)
        [[1.  2.  1.  2.  1.  2.]
         [3.  4.  3.  4.  3.  4.]
         [1.  2.  1.  2.  1.  2.]
         [3.  4.  3.  4.  3.  4.]]
        >>> multiples = (2, 3, 2)
        >>> output = tile(input_x, multiples)
        >>> print(output)
        [[[1. 2. 1. 2.]
          [3. 4. 3. 4.]
          [1. 2. 1. 2.]
          [3. 4. 3. 4.]
          [1. 2. 1. 2.]
          [3. 4. 3. 4.]]
         [[1. 2. 1. 2.]
          [3. 4. 3. 4.]
          [1. 2. 1. 2.]
          [3. 4. 3. 4.]
          [1. 2. 1. 2.]
          [3. 4. 3. 4.]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Tile"""
        self.init_prim_io_names(inputs=['x', 'multiples'], outputs=['output'])

    def check_elim(self, base_tensor, multiplier):
        if not isinstance(base_tensor, Tensor):
            raise TypeError(f"For '{self.name}', the type of 'input_x' must be Tensor, "
                            f"but got {type(base_tensor).__name__}.")
        if all(v == 1 for v in multiplier) and len(base_tensor.shape) >= len(multiplier):
            ret = Identity()(base_tensor)
            return (True, ret)
        return (False, None)

    def _get_shape_and_range(self, x, multiples):
        """calculate tile shape and value"""
        x_shp = x['shape']
        multiples_v = multiples['value']
        value = None
        if multiples_v is None:
            multiples_v = multiples['min_value']
        if 'max_shape' in x and 'min_shape' in x:
            max_shape = x['max_shape']
            min_shape = x['min_shape']
        else:
            max_shape = list(x_shp)
            min_shape = list(x_shp)
        len_sub = len(multiples_v) - len(x_shp)
        multiples_w = None
        if len_sub == 0:
            multiples_w = multiples_v
        if len_sub > 0:
            for i in range(0, len_sub):
                x_shp.insert(0, 1)
                min_shape.insert(0, 1)
                max_shape.insert(0, 1)
            multiples_w = multiples_v
        elif len_sub < 0:
            raise ValueError(f"For '{self.name}', the length of 'multiples' can not be smaller than "
                             f"the dimension of 'input_x', but got length of 'multiples': {len(multiples_v)} "
                             f"and dimension of 'input_x': {len(x_shp)}.")
        if 'max_value' in multiples and 'min_value' in multiples:
            multiples_v_max = multiples['max_value']
            multiples_v_min = multiples['min_value']
            i = 0
            for a, b in zip(multiples_v_min, multiples_v_max):
                if isinstance(a, (Tensor_, Tensor)):
                    a = a.asnumpy()
                if isinstance(b, (Tensor_, Tensor)):
                    b = b.asnumpy()
                if x_shp[i] >= 0:
                    x_shp[i] *= a
                if a != b:
                    x_shp[i] = -1
                min_shape[i] *= a
                max_shape[i] *= b
                i += 1
        else:
            for i, a in enumerate(multiples_w):
                if x_shp[i] >= 0:
                    x_shp[i] *= a
                max_shape[i] *= a
                min_shape[i] *= a
            if x['value'] is not None:
                value = Tensor(np.tile(x['value'].asnumpy(), multiples_w))
        out_shape = {
            'shape': x_shp,
            'max_shape': max_shape,
            'min_shape': min_shape
        }
        return out_shape, value

    def __infer__(self, x, multiples):
        multiples_v = multiples['value']
        if multiples_v is None:
            if 'max_value' not in multiples or 'min_value' not in multiples:
                if len(multiples['shape']) != 1:
                    raise ValueError(f'For \'{self.name}\', the dim of multiples must be 1.')
                rank = max(len(x['shape']), multiples['shape'][0])
                out_shape = [-1] * rank
                return {
                    'shape': out_shape,
                    'dtype': x['dtype'],
                    'value': None,
                    'max_shape': [1] * rank,
                    'min_shape': [1] * rank
                }
            out_shape, value = self._get_shape_and_range(x, multiples)
            max_shape = out_shape.get('max_shape', None)
            min_shape = out_shape.get('min_shape', None)
            shape = out_shape.get('shape', None)
            return {
                'shape': shape,
                'dtype': x['dtype'],
                'value': value,
                'max_shape': max_shape,
                'min_shape': min_shape
            }

        validator.check_value_type(
            "multiples", multiples_v, [tuple], self.name)
        for i, multiple in enumerate(multiples_v):
            validator.check_positive_int(
                multiple, "multiples[%d]" % i, self.name)
        validator.check_value_type(
            "x[\'dtype\']", x["dtype"], mstype.tensor_type, self.name)
        out_shp, value = self._get_shape_and_range(x, multiples)
        shp = out_shp.get('shape', None)
        out = {'shape': shp,
               'dtype': x['dtype'],
               'value': value}
        if 'max_shape' in x and 'min_shape' in x:
            out['max_shape'] = out_shp.get('max_shape', None)
            out['min_shape'] = out_shp.get('min_shape', None)
        return out


class UnsortedSegmentSum(PrimitiveWithInfer):
    r"""
    Computes the sum of a tensor along segments.

    Refer to :func:`mindspore.ops.unsorted_segment_sum` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor([1, 2, 3, 4], mindspore.float32)
        >>> segment_ids = Tensor([0, 0, 1, 2], mindspore.int32)
        >>> num_segments = 4
        >>> output = ops.UnsortedSegmentSum()(input_x, segment_ids, num_segments)
        >>> print(output)
        [3. 3. 4. 0.]
        >>> input_x = Tensor([1, 2, 3, 4, 2, 5], mindspore.float32)
        >>> segment_ids = Tensor([0, 0, 1, 2, 3, 4], mindspore.int32)
        >>> num_segments = 6
        >>> output = ops.UnsortedSegmentSum()(input_x, segment_ids, num_segments)
        >>> print(output)
        [3. 3. 4. 2. 5. 0.]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize UnsortedSegmentSum"""
        self.init_prim_io_names(inputs=['x', 'segment_ids', 'num_segments'], outputs=['y'])

    def __infer__(self, x, segment_ids, num_segments):
        x_type = x['dtype']
        x_shp = x['shape']
        validator.check_subclass("input_x", x_type, mstype.tensor, self.name)
        validator.check_value_type("x_shape", x_shp, [list], self.name)
        x_shp_len = len(x_shp)
        validator.check_positive_int(x_shp_len, "rank of input_x", self.name)
        segment_ids_shp = segment_ids['shape']
        segment_ids_type = segment_ids['dtype']
        validator.check_subclass("segment_ids", segment_ids_type, mstype.tensor, self.name)
        validator.check_value_type("segment_ids", segment_ids_shp, [list], self.name)
        segment_ids_shp_len = len(segment_ids_shp)
        validator.check_positive_int(segment_ids_shp_len, "rank of segment_ids", self.name)
        validator.check(f'rank of input_x', len(x_shp),
                        'rank of segments_id', len(segment_ids_shp), Rel.GE, self.name)
        if is_shape_known(x_shp) and is_shape_known(segment_ids_shp):
            # only validate when both shapes fully known
            for i, value in enumerate(segment_ids_shp):
                validator.check("ids[%d]" % i, value, 'input[%d]' % i, x_shp[i], Rel.EQ, self.name)
        num_segments_v = num_segments['value']
        num_segments_type = num_segments['dtype']
        validator.check_subclass("num_segments", num_segments_type, [mstype.tensor, mstype.number], self.name)
        if isinstance(num_segments_type, type(mstype.tensor)):
            validator.check_tensor_dtype_valid("num_segments", num_segments_type, [mstype.int32, mstype.int64],
                                               self.name)
            shp = [-1]
        else:
            validator.check_value_type('num_segments', num_segments_v, [int], self.name)
            validator.check_positive_int(num_segments_v, "num_segments", self.name)
            shp = [num_segments_v]

        shp += x_shp[segment_ids_shp_len:]
        out = {'shape': shp,
               'dtype': mstype.tensor_type(x_type.element_type()),
               'value': None}
        if "max_value" in num_segments and "min_value" in num_segments:
            output_max_shape = list(num_segments['max_value'])
            output_min_shape = list(num_segments['min_value'])
        else:
            output_max_shape = [num_segments_v]
            output_min_shape = [num_segments_v]
            if num_segments_v is None:
                output_max_shape = []
                output_min_shape = []
        if 'max_shape' in x and 'min_shape' in x:
            max_output_incoming = x['max_shape']
            min_output_incoming = x['min_shape']
        else:
            max_output_incoming = x_shp
            min_output_incoming = x_shp
        output_max_shape += max_output_incoming[segment_ids_shp_len:]
        output_min_shape += min_output_incoming[segment_ids_shp_len:]
        if len(output_max_shape) == len(shp):
            out['max_shape'] = output_max_shape
            out['min_shape'] = output_min_shape
        return out


class UnsortedSegmentMin(PrimitiveWithCheck):
    r"""
    Computes the minimum of a tensor along segments.

    Refer to :func:`mindspore.ops.unsorted_segment_min` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore import Tensor
        >>> from mindspore import ops
        >>> import numpy as np
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [4, 2, 1]]).astype(np.float32))
        >>> segment_ids = Tensor(np.array([0, 1, 1]).astype(np.int32))
        >>> num_segments = 2
        >>> unsorted_segment_min = ops.UnsortedSegmentMin()
        >>> output = unsorted_segment_min(input_x, segment_ids, num_segments)
        >>> print(output)
        [[1. 2. 3.]
         [4. 2. 1.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize UnsortedSegmentMin"""
        self.init_prim_io_names(inputs=['x', 'segment_ids', 'num_segments'], outputs=['y'])

    def __check__(self, x, segment_ids, num_segments):
        x_shape = x['shape']
        segment_ids_shape = segment_ids['shape']
        valid_type = [mstype.float16, mstype.float32, mstype.int32]
        validator.check_tensor_dtype_valid("x", x['dtype'], valid_type, self.name)
        validator.check_tensor_dtype_valid("segment_ids", segment_ids['dtype'], [mstype.int32], self.name)

        # support vmap : segment_ids_shape support batch rank
        if not hasattr(self, 'batch_rank'):
            validator.check_equal_int(len(segment_ids_shape), 1, "rank of segment_ids_shape", self.name)

        num_segments_type = num_segments['dtype']
        validator.check_subclass("num_segments", num_segments_type, [mstype.number], self.name)
        if is_shape_known(x_shape) and is_shape_known(segment_ids_shape):
            # only validate when both shapes fully known
            validator.check(f'first shape of input_x', x_shape[0],
                            'length of segments_id', segment_ids_shape[0], Rel.EQ, self.name)
        num_segments_v = num_segments['value']
        validator.check_value_type('num_segments', num_segments_v, [int], self.name)
        validator.check_positive_int(num_segments_v, "num_segments", self.name)


class UnsortedSegmentMax(PrimitiveWithCheck):
    r"""
    Computes the maximum along segments of a tensor.

    Refer to :func:`mindspore.ops.unsorted_segment_max` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> # case 1: Only have two num_segments, where is 0 and 1, and segment_ids=[0, 1, 1]
        >>> # num_segments = 2 indicates that there are two types of segment_id,
        >>> # the first number '0' in [0, 1, 1] indicates input_x[0],
        >>> # the second number '1' in [0, 1, 1] indicates input_x[1],
        >>> # the third number '1' in [0, 1, 1] indicates input_x[2],
        >>> # input_x[0], which is [1, 2, 3] will not be compared to other segment_id.
        >>> # Only the same segment_id will be compared.
        >>> from mindspore import Tensor
        >>> from mindspore import ops
        >>> import numpy as np
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [4, 2, 1]]).astype(np.float32))
        >>> segment_ids = Tensor(np.array([0, 1, 1]).astype(np.int32))
        >>> num_segments = 2
        >>> unsorted_segment_max = ops.UnsortedSegmentMax()
        >>> output = unsorted_segment_max(input_x, segment_ids, num_segments)
        >>> print(output)
        [[1. 2. 3.]
         [4. 5. 6.]]
        >>>
        >>> # case 2: The segment_ids=[0, 0, 1, 1].
        >>> # [1, 2, 3] will compare with [4, 2, 0],
        >>> # and [4, 5, 6] will compare with [4, 2, 1].
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 2, 0], [4, 5, 6], [4, 2, 1]]).astype(np.float32))
        >>> segment_ids = Tensor(np.array([0, 0, 1, 1]).astype(np.int32))
        >>> num_segments = 2
        >>> unsorted_segment_max = ops.UnsortedSegmentMax()
        >>> output = unsorted_segment_max(input_x, segment_ids, num_segments)
        >>> print(input_x.shape)
            (4, 3)
        >>> print(output)
            [[4. 2. 3.]
             [4. 5. 6.]]
        >>> # case 3: If the input_x have three dimensions even more, what will happen?
        >>> # The shape of input_x is (2, 4, 3),
        >>> # and the length of segment_ids should be the same as the first dimension of input_x.
        >>> # Because the segment_ids are different, input_x[0] will not be compared to input_x[1].
        >>> input_x = Tensor(np.array([[[1, 2, 3], [4, 2, 0], [4, 5, 6], [4, 2, 1]],
        ...                            [[1, 2, 3], [4, 2, 0], [4, 5, 6], [4, 2, 1]]]).astype(np.float32))
        >>> segment_ids = Tensor(np.array([0, 1]).astype(np.int32))
        >>> num_segments = 2
        >>> unsorted_segment_max = ops.UnsortedSegmentMax()
        >>> output = unsorted_segment_max(input_x, segment_ids, num_segments)
        >>> print(input_x.shape)
            (2, 4, 3)
        >>> print(output)
            [[[1. 2. 3.]
              [4. 2. 0.]
              [4. 5. 6.]
              [4. 2. 1.]]
             [[1. 2. 3.]
              [4. 2. 0.]
              [4. 5. 6.]
              [4. 2. 1.]]]
        >>> # case 4: It has the same input with the 3rd case.
        >>> # Because num_segments is equal to 2, there are two segment_ids, but currently only one 0 is used.
        >>> # the segment_id i is absent in the segment_ids, then output[i] will be filled with
        >>> # the smallest possible value of the input_x's type.
        >>> segment_ids = Tensor(np.array([0, 0]).astype(np.int32))
        >>> output = unsorted_segment_max(input_x, segment_ids, num_segments)
        >>> print(output)
            [[[ 1.0000000e+00  2.0000000e+00  3.0000000e+00]
              [ 4.0000000e+00  2.0000000e+00  0.0000000e+00]
              [ 4.0000000e+00  5.0000000e+00  6.0000000e+00]
              [ 4.0000000e+00  2.0000000e+00  1.0000000e+00]]
             [[-3.4028235e+38 -3.4028235e+38 -3.4028235e+38]
              [-3.4028235e+38 -3.4028235e+38 -3.4028235e+38]
              [-3.4028235e+38 -3.4028235e+38 -3.4028235e+38]
              [-3.4028235e+38 -3.4028235e+38 -3.4028235e+38]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize UnsortedSegmentMax"""
        self.init_prim_io_names(inputs=['x', 'segment_ids', 'num_segments'], outputs=['y'])

    def __check__(self, x, segment_ids, num_segments):
        x_shape = x['shape']
        segment_ids_shape = segment_ids['shape']
        valid_type = [mstype.float16, mstype.float32, mstype.int32]
        validator.check_tensor_dtype_valid("x", x['dtype'], valid_type, self.name)
        validator.check_tensors_dtypes_same_and_valid({"segment_ids": segment_ids['dtype']},
                                                      [mstype.int32, mstype.int64], self.name)

        # support vmap : segment_ids_shape support batch rank
        if not hasattr(self, 'batch_rank'):
            validator.check_equal_int(len(segment_ids_shape), 1, "rank of segment_ids_shape", self.name)

        num_segments_type = num_segments['dtype']
        validator.check_subclass("num_segments", num_segments_type, [mstype.number], self.name)
        if is_shape_known(x_shape) and is_shape_known(segment_ids_shape):
            # only validate when both shapes fully known
            validator.check(f'first shape of input_x', x_shape[0],
                            'length of segments_id', segment_ids_shape[0], Rel.EQ, self.name)
        num_segments_v = num_segments['value']
        validator.check_value_type('num_segments', num_segments_v, [int], self.name)
        validator.check_positive_int(num_segments_v, "num_segments", self.name)


class UnsortedSegmentProd(PrimitiveWithInfer):
    """
    Computes the product of a tensor along segments.

    Refer to :func:`mindspore.ops.unsorted_segment_prod` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> from mindspore import Tensor
        >>> from mindspore import ops
        >>> import numpy as np
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [4, 2, 1]]).astype(np.float32))
        >>> segment_ids = Tensor(np.array([0, 1, 0]).astype(np.int32))
        >>> num_segments = 2
        >>> unsorted_segment_prod = ops.UnsortedSegmentProd()
        >>> output = unsorted_segment_prod(input_x, segment_ids, num_segments)
        >>> print(output)
        [[4. 4. 3.]
         [4. 5. 6.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize UnsortedSegmentProd"""
        self.init_prim_io_names(inputs=['x', 'segment_ids', 'num_segments'], outputs=['y'])

    def __infer__(self, x, segment_ids, num_segments):
        x_type = x['dtype']
        x_shape = x['shape']
        segment_ids_shape = segment_ids['shape']
        validator.check_subclass("input_x", x_type, mstype.tensor, self.name)
        validator.check_value_type("x_shape", x_shape, [list], self.name)
        valid_type = [mstype.float16, mstype.float32, mstype.int32]
        validator.check_tensor_dtype_valid("x", x['dtype'], valid_type, self.name)
        validator.check_tensor_dtype_valid("segment_ids", segment_ids['dtype'], [mstype.int32], self.name)

        # support vmap : segment_ids_shape support batch rank
        if not hasattr(self, 'batch_rank'):
            validator.check_equal_int(len(segment_ids_shape), 1, "rank of segment_ids_shape", self.name)

        validator.check(f'first shape of input_x', x_shape[0],
                        'length of segments_id', segment_ids_shape[0], Rel.EQ, self.name)
        num_segments_v = num_segments['value']
        validator.check_value_type('num_segments', num_segments_v, [int], self.name)
        validator.check_positive_int(num_segments_v, "num_segments", self.name)
        segment_ids_shape_len = len(segment_ids_shape)
        out_shape = [num_segments_v]
        out_shape += x_shape[segment_ids_shape_len:]
        out = {'shape': out_shape,
               'dtype': mstype.tensor_type(x_type.element_type()),
               'value': None}
        return out


class Concat(PrimitiveWithCheck):
    r"""
    Connect tensor in the specified axis.

    Refer to :func:`mindspore.ops.concat` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x1 = Tensor(np.array([[0, 1], [2, 1]]).astype(np.float32))
        >>> input_x2 = Tensor(np.array([[0, 1], [2, 1]]).astype(np.float32))
        >>> op = ops.Concat()
        >>> output = op((input_x1, input_x2))
        >>> print(output)
        [[0. 1.]
         [2. 1.]
         [0. 1.]
         [2. 1.]]
        >>> op = ops.Concat(1)
        >>> output = op((input_x1, input_x2))
        >>> print(output)
        [[0. 1. 0. 1.]
         [2. 1. 2. 1.]]
    """

    @prim_attr_register
    def __init__(self, axis=0):
        """Initialize Concat"""
        self.axis = axis
        validator.check_value_type("axis", axis, [int], self.name)

    def infer_value(self, input_x):
        value = None
        if input_x is not None:
            value = Tensor(np.concatenate([x.asnumpy() for x in input_x], axis=self.axis))
        return value


class ParallelConcat(PrimitiveWithInfer):
    r"""
    Concats tensor in the first dimension.

    Concats input tensors along with the first dimension.

    The difference between Concat and ParallelConcat is that Concat requires all of the inputs be computed
    before the operation will begin but doesn't require that the input shapes be known during graph construction.
    Parallel concat will copy pieces of the input into the output as they become available, in some situations
    this can provide a performance benefit.

    Note:
        The input tensors are all required to have size 1 in the first dimension.

    Inputs:
        - **values** (tuple, list) - A tuple or a list of input tensors. The data type and shape of these
          tensors must be the same. The data type is Number except float64.

    Outputs:
        Tensor, data type is the same as `values`.

    Raises:
        ValueError: If length of shape of `values` is less than 1.
        ValueError: The data type and shape of these tensors are not the same.

    Supported Platforms:
        ``Ascend``

    Examples:
        >>> data1 = Tensor(np.array([[0, 1]]).astype(np.int32))
        >>> data2 = Tensor(np.array([[2, 1]]).astype(np.int32))
        >>> op = ops.ParallelConcat()
        >>> output = op((data1, data2))
        >>> print(output)
        [[0 1]
         [2 1]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ParallelConcat"""

    def __infer__(self, values):
        x_shp = values['shape']
        x_type = values['dtype']

        validator.check_int(len(x_shp), 1, Rel.GE, f'x_shp length', self.name)

        args = {f"x_type[{i}]": elem for i, elem in enumerate(x_type)}
        validator.check_tensors_dtypes_same_and_valid(args, mstype.number_type + (mstype.bool_,), self.name)

        first_elem = x_shp[0]
        for i, elem in enumerate(x_shp[1:]):
            j = i + 1
            validator.check_equal_int(elem[0], 1, f'x_shp[{j}][0]', self.name)
            validator.check(f"x_shp[0] shape", first_elem, f"x_shp[{j}] shape", elem, Rel.EQ, self.name)

        ret_shp = x_shp[0].copy()
        ret_shp[0] = len(x_shp)
        self.add_prim_attr('shape', ret_shp)
        self.add_prim_attr('N', len(x_shp))

        out = {'shape': ret_shp,
               'dtype': x_type[0],
               'value': None}
        return out


def _get_stack_shape(value, x_shape, x_type, axis, prim_name):
    """for stack output shape"""
    validator.check_value_type("shape", x_shape, [tuple, list], prim_name)
    validator.check_int(len(x_shape), 1, Rel.GE, "len of input_x", prim_name)
    validator.check_subclass("input_x[0]", x_type[0], mstype.tensor, prim_name)
    rank_base = len(x_shape[0])
    n = len(x_shape)
    out_shape = x_shape[0]
    validator.check_int_range(axis, -rank_base - 1, rank_base, Rel.INC_BOTH, 'axis', prim_name)
    if axis < 0:
        axis = axis + rank_base + 1
    for i in range(1, n):
        validator.check('x_type[%d]' % i, x_type[i], 'base', x_type[0], Rel.EQ, prim_name, TypeError)
        if x_shape[i] != x_shape[0]:
            raise ValueError(f"For \'{prim_name}\' element {i} shape in input can not pack with first element")

    out = {}
    if is_shape_unknown(out_shape):
        if 'min_shape' in value:
            x_min_shp = value['min_shape']
            ret_min_shp = x_min_shp[0].copy()
            ret_min_shp.insert(axis, n)
            out['min_shape'] = ret_min_shp
        if 'max_shape' in value:
            x_max_shp = value['max_shape']
            ret_max_shp = x_max_shp[0].copy()
            ret_max_shp.insert(axis, n)
            out['max_shape'] = ret_max_shp
        out_shape.insert(axis, n)
        out['shape'] = out_shape
        return out

    out_shape.insert(axis, n)
    return out_shape


class Pack(PrimitiveWithInfer):
    """
    Same as operator Stack. Pack will be deprecated in the future.
    Please use Stack instead.
    """

    @deprecated("1.1", "Stack", True)
    @prim_attr_register
    def __init__(self, axis=0):
        """Initialize Pack"""
        validator.check_value_type("axis", axis, [int], self.name)
        self.axis = axis

    def __infer__(self, value):
        x_shape = value['shape']
        x_type = value['dtype']
        self.add_prim_attr('num', len(x_shape))
        all_shape = _get_stack_shape(value, x_shape, x_type, self.axis, self.name)
        out = {'shape': all_shape,
               'dtype': x_type[0],
               'value': None}
        return out


class Stack(PrimitiveWithInfer):
    r"""
    Stacks a list of tensors in specified axis.

    Refer to :func:`mindspore.ops.stack` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> data1 = Tensor(np.array([0, 1]).astype(np.float32))
        >>> data2 = Tensor(np.array([2, 3]).astype(np.float32))
        >>> stack = ops.Stack()
        >>> output = stack([data1, data2])
        >>> print(output)
        [[0. 1.]
         [2. 3.]]
    """

    @prim_attr_register
    def __init__(self, axis=0):
        """Initialize Stack"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])
        validator.check_value_type("axis", axis, [int], self.name)
        self.axis = axis

    def __infer__(self, value):
        x_shape = value['shape']
        x_type = value['dtype']
        self.add_prim_attr('num', len(x_shape))
        self.add_prim_attr('N', len(x_shape))
        all_shape = _get_stack_shape(value, x_shape, x_type, self.axis, self.name)
        out = {}
        tuple_value = value['value']
        input_array = []
        infered_value = None
        if tuple_value is not None:
            for item in tuple_value:
                npy_item = item.asnumpy()
                input_array.append(npy_item)
            infered_value = Tensor(np.stack(input_array, axis=self.axis))

        if 'min_shape' in all_shape and 'max_shape' in all_shape:
            out = {'shape': all_shape.get('shape'),
                   'min_shape': all_shape.get('min_shape'),
                   'max_shape': all_shape.get('max_shape'),
                   'dtype': x_type[0],
                   'value': infered_value}
        else:
            shape = all_shape.get('shape') if isinstance(all_shape, dict) else all_shape
            out = {'shape': shape,
                   'dtype': x_type[0],
                   'value': infered_value}

        if 'min_value' in value and 'max_value' in value:
            min_value_array = []
            max_value_array = []
            infered_min_value = None
            infered_max_value = None
            for i in range(len(value['min_value'])):
                cur_min_value = value['min_value'][i]
                cur_max_value = value['max_value'][i]
                if cur_min_value is None or cur_max_value is None:
                    return out
                if isinstance(cur_min_value, Tensor_):
                    cur_min_value = cur_min_value.asnumpy()
                elif isinstance(cur_min_value, tuple):
                    cur_min_value = np.array(cur_min_value)
                if isinstance(cur_max_value, Tensor_):
                    cur_max_value = cur_max_value.asnumpy()
                elif isinstance(cur_max_value, tuple):
                    cur_max_value = np.array(cur_max_value)
                min_value_array.append(cur_min_value)
                max_value_array.append(cur_max_value)
            infered_min_value = np.stack(min_value_array, axis=self.axis)
            infered_max_value = np.stack(max_value_array, axis=self.axis)
            infered_min_value = tuple(infered_min_value.tolist())
            infered_max_value = tuple(infered_max_value.tolist())
            out['min_value'] = infered_min_value
            out['max_value'] = infered_max_value
        return out


class Unpack(PrimitiveWithInfer):
    """
    Same as operator Unstack. Unpack will be deprecated in the future.
    Please use Unstack instead.
    """

    @deprecated("1.1", "Unstack", True)
    @prim_attr_register
    def __init__(self, axis=0):
        """Initialize Unpack"""
        validator.check_value_type("axis", axis, [int], self.name)
        self.axis = axis

    def __infer__(self, x):
        validator.check_subclass("x", x['dtype'], mstype.tensor, self.name)
        x_shape = list(x['shape'])
        dim = len(x_shape)
        validator.check_int_range(self.axis, -dim, dim, Rel.INC_LEFT, 'axis value', self.name)
        if self.axis < 0:
            self.axis = self.axis + dim
        output_num = x_shape[self.axis]
        validator.check_value_type("num", output_num, [int], self.name)
        validator.check_positive_int(output_num, "output_num", self.name)
        self.add_prim_attr('num', output_num)
        output_valid_check = x_shape[self.axis] - output_num
        validator.check_int(output_valid_check, 0, Rel.EQ,
                            "The dimension which to unstack divides output_num", self.name)
        out_shapes = []
        out_dtypes = []
        out_shape = x_shape[:self.axis] + x_shape[self.axis + 1:]
        for _ in range(output_num):
            out_shapes.append(tuple(out_shape))
            out_dtypes.append(x['dtype'])
        out_shapes = tuple(out_shapes)
        out_dtypes = tuple(out_dtypes)
        out = {'shape': out_shapes,
               'dtype': out_dtypes,
               'value': None}
        return out


class Unstack(Primitive):
    r"""
    Unstacks tensor in specified axis.

    Refer to :func:`mindspore.ops.unstack` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> unstack = ops.Unstack()
        >>> input_x = Tensor(np.array([[1, 1, 1, 1], [2, 2, 2, 2]]))
        >>> output = unstack(input_x)
        >>> print(output)
        (Tensor(shape=[4], dtype=Int64, value= [1, 1, 1, 1]), Tensor(shape=[4], dtype=Int64, value= [2, 2, 2, 2]))
    """

    @prim_attr_register
    def __init__(self, axis=0):
        """Initialize Unstack"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])
        validator.check_value_type("axis", axis, [int], self.name)


class Slice(Primitive):
    """
    Slices a tensor in the specified shape.

    Refer to :func:`mindspore.ops.slice` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> data = Tensor(np.array([[[1, 1, 1], [2, 2, 2]],
        ...                         [[3, 3, 3], [4, 4, 4]],
        ...                         [[5, 5, 5], [6, 6, 6]]]).astype(np.int32))
        >>> slice_op = ops.Slice()
        >>> output = slice_op(data, (1, 0, 0), (1, 1, 3))
        >>> print(output)
        [[[3 3 3]]]
        >>> output = slice_op(data, (1, 0, 0), (1, 1, 2))
        >>> print(output)
        [[[3 3]]]
        >>> output = slice_op(data, (1, 0, 0), (1, 1, 1))
        >>> print(output)
        [[[3]]]
        >>> output = slice_op(data, (1, 1, 0), (1, 1, 3))
        >>> print(output)
        [[[4 4 4]]]
        >>> output = slice_op(data, (1, 0, 1), (1, 1, 2))
        >>> print(output)
        [[[3 3]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize slice"""
        self.init_prim_io_names(inputs=['x', 'begin', 'size'], outputs=['output'])


class Coalesce(Primitive):
    """
    Returns the coalesced sparse tensor of the input.

    Inputs:
        - **x_indices** (Tensor) - A 2-D Tensor, represents the indices of the nonzero elements of the sparse tensor.
          Supported data type is int64. It's elements should be non-negative. The shape is :math:`(y, x)`.
        - **x_values** (Tensor) - A 1-D Tensor, represents the values corresponding to the indices in `x_indices`.
          Supported data types are float16 and float32. The shape is :math:`(x,)`.
        - **x_shape** (Tensor) - A 1-D Tensor, specifies the shape of the sparse tensor.
          Supported data type is int64. The shape is :math:`(y,)`.

    Outputs:
        - **y_indices** (Tensor) - A 2-D Tensor, represents the indices of the nonzero elements of the sparse tensor.
          Data type is int64. It's elements are non-negative. The shape is :math:`(y, z)`.
          `z` represents the number of different indices in `x_indices`.
        - **y_values** (Tensor) - A 1-D Tensor, represents the values corresponding to the indices in `y_indices`.
          Data type is the same as `x_values`'s. The shape is :math:`(z,)`.
        - **y_shape** (Tensor) - A 1-D Tensor, specifies the shape of the sparse tensor.
          Data type is int64. The shape is :math:`(y,)`.

    Raises:
        TypeError: If the data type of `x_values` is neither float32 nor float16.
        TypeError: If any of the data types of `x_indices` and `x_shape` is not int64.
        ValueError: If any of `x_values` and `x_shape` is not a 1-D tensor.
        ValueError: If `x_indices` is not a 2-D tensor.
        ValueError: If sizes of second dimension of `x_indices` and first dimension of `x_values` are not the same.
        ValueError: If sizes of first dimension of `x_indices` and first dimension of `x_shape` are not the same.
        ValueError: If any of the values of elements of `x_indices` is negative.
        ValueError: If any of the values of elements of `x_indices` exceed the limit set by `x_shape`.

    Supported Platforms:
        ``CPU``

    Examples:
        >>> x_indices = Tensor([[0, 0, 1], [1, 1, 2]], dtype=ms.int64)
        >>> x_values = Tensor([1, 5, 4], dtype=ms.float32)
        >>> x_shape = Tensor([3, 3], dtype=ms.int64)
        >>> coalesce = ops.Coalesce()
        >>> y_indices, y_values, y_shape = coalesce(x_indices, x_values, x_shape)
        >>> print(y_indices)
        [[0 1]
         [1 2]]
        >>> print(y_values)
        [6. 4.]
        >>> print(y_shape)
        [3 3]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Coalesce."""
        self.init_prim_io_names(inputs=['x_indices', 'x_values', 'x_shape'],
                                outputs=['y_indices', 'y_values', 'y_shape'])


class ReverseV2(PrimitiveWithInfer):
    """
    Reverses specific dimensions of a tensor.

    .. warning::
        The value range of "axis" is [-dims, dims - 1]. "dims" is the dimension length of "input_x".

    Args:
        axis (Union[tuple(int), list(int)): The indices of the dimensions to reverse.

    Inputs:
        - **input_x** (Tensor) - The target tensor. The data type is Number except float64.
          The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.

    Outputs:
        Tensor, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `axis` is neither list nor tuple.
        TypeError: If element of `axis` is not an int.

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> input_x = Tensor(np.array([[1, 2, 3, 4], [5, 6, 7, 8]]), mindspore.int32)
        >>> op = ops.ReverseV2(axis=[1])
        >>> output = op(input_x)
        >>> print(output)
        [[4 3 2 1]
         [8 7 6 5]]
        >>> op = ops.ReverseV2(axis=[1, 0])
        >>> output = op(input_x)
        >>> print(output)
        [[8 7 6 5]
         [4 3 2 1]]
    """

    @prim_attr_register
    def __init__(self, axis):
        """Initialize ReverseV2."""
        validator.check_value_type('axis', axis, [list, tuple], self.name)
        for i, each in enumerate(axis):
            validator.check_value_type(f'axis[{i}]', each, [int], self.name)
        self.axis = axis
        self.init_prim_io_names(inputs=['x'], outputs=['output'])

    def infer_shape(self, x_shape):
        dim = len(x_shape)
        for i, each in enumerate(self.axis):
            validator.check_int_range(each, -dim, dim, Rel.INC_LEFT, f'axis[{i}]', self.name)
        normalized_axis = []
        for i, v in enumerate(self.axis):
            if v < 0:
                normalized_axis.append(v + dim)
            else:
                normalized_axis.append(v)

        if len(normalized_axis) != len(set(normalized_axis)):
            duplicated = [item for item, count in Counter(normalized_axis).items() if count > 1]
            raise ValueError(f"For '{self.name}', the 'axis' cannot contain duplicate dimensions,"
                             f" but got duplicated elements {duplicated}.")

        return x_shape

    def infer_dtype(self, x_dtype):
        validator.check_tensor_dtype_valid('x', x_dtype, (mstype.bool_,) + mstype.number_type, self.name)
        return x_dtype


class Rint(Primitive):
    """
    Returns an integer that is closest to x element-wise.

    Inputs:
        - **input_x** (Tensor) - The target tensor, which must be one of the following types:
          float16, float32. The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.

    Outputs:
        Tensor, has the same shape and type as `input_x`.

    Raises:
        TypeError: If dtype of `input_x` is not in [float16, float32, float64].

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([-1.6, -0.1, 1.5, 2.0]), mindspore.float32)
        >>> op = ops.Rint()
        >>> output = op(input_x)
        >>> print(output)
        [-2.  0.  2.  2.]
        >>> input_x = Tensor(np.array([[-2.0, -1.9, -1.8, -1.7, -1.6],
        ...                            [-2.0, -1.9, -1.8, -1.7, -1.6]]), mindspore.float32)
        >>> output = op(input_x)
        >>> print(output)
        [[-2. -2. -2. -2. -2.]
         [-2. -2. -2. -2. -2.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Rint."""
        self.init_prim_io_names(inputs=['x'], outputs=['output'])


class Select(Primitive):
    r"""
    The conditional tensor determines whether the corresponding element in the output must be
    selected from :math:`x` (if True) or :math:`y` (if False) based on the value of each
    element.

    It can be defined as:

    .. math::
        out_i = \begin{cases}
        x_i, & \text{if } condition_i \\
        y_i, & \text{otherwise}
        \end{cases}

    Inputs:
        - **condition** (Tensor[bool]) - The condition tensor, decides which element is chosen.
          The shape is :math:`(x_1, x_2, ..., x_N, ..., x_R)`.
        - **x** (Tensor) - The first tensor to be selected and the shape is :math:`(x_1, x_2, ..., x_N, ..., x_R)`.
        - **y** (Tensor) - The second tensor to be selected and the shape is :math:`(x_1, x_2, ..., x_N, ..., x_R)`.

    Outputs:
        Tensor, has the same shape as `x`.

    Raises:
        TypeError: If `x` or `y` is not a Tensor.
        ValueError: If shape of `x` is not equal to shape of `y` or shape of `condition`.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> select = ops.Select()
        >>> input_cond = Tensor([True, False])
        >>> input_x = Tensor([2,3], mindspore.float32)
        >>> input_y = Tensor([1,2], mindspore.float32)
        >>> output = select(input_cond, input_x, input_y)
        >>> print(output)
        [2. 2.]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Select."""
        self.init_prim_io_names(inputs=['condition', 'x', 'y'], outputs=['output'])


def _compute_slicing_length(begin, end, stride, x_shape, i):
    """Computes the length of the slicing."""
    if i >= len(x_shape):
        raise ValueError(f"For 'StridedSlice', the index must be less than "
                         f"the dimension of 'input_x', but got the dimension of 'input_x': {len(x_shape)} "
                         f"and the index: {i}.")
    x_dim = x_shape[i]
    if stride > 0:
        # When slicing forward, convert begin and end to positive numbers.
        if begin >= x_dim or end < -x_dim:
            # When slicing forward, if begin >= x_dim or end < -x_dim, the length of the slicing is 0.
            slicing_length = 0
        else:
            if -x_dim <= begin < 0:
                begin += x_dim
            if begin < -x_dim:
                # When slicing forward, if begin < -x_dim, set begin = 0, which means start from the 0th element.
                begin = 0
            if -x_dim <= end < 0:
                end += x_dim
            if end > x_dim:
                # When slicing forward, if end > x_dim, set end = x_dims, which means slice to the last element.
                end = x_dim
            if begin >= end:
                # When slicing forward, if begin >= end, the length of the slicing is 0.
                slicing_length = 0
            else:
                slicing_length = 1 + (end - 1 - begin) // stride
    else:
        # When slicing backward, convert begin and end to negative numbers.
        if begin < -x_dim or end >= x_dim:
            # When slicing backward, if begin < -x_dim or end >= x_dim, the length of the slicing is 0.
            slicing_length = 0
        else:
            if 0 <= begin < x_dim:
                begin += -x_dim
            if begin >= x_dim:
                begin = -1
            if 0 <= end < x_dim:
                end += -x_dim
            if end < -x_dim - 1:
                # Slicing to the 0th element.
                end = -x_dim - 1
            if begin <= end:
                slicing_length = 0
            else:
                slicing_length = 1 + (end + 1 - begin) // stride
    return slicing_length


class StridedSlice(PrimitiveWithInfer):
    r"""

    Extracts a strided slice of a tensor.

    Given an input tensor, this operation inserts a dimension of length 1 at the dimension.
    This operation extracts a fragment of size (end-begin)/stride from the given 'input_tensor'.
    Starting from the beginning position, the fragment continues adding stride to the index until
    all dimensions are not less than the ending position.

    Given a `input_x[m1, m2, ..., mn]`, `begin`, `end` and `strides` will be vectors of length n.

    In each mask field (`begin_mask`, `end_mask`, `ellipsis_mask`, `new_axis_mask`, `shrink_axis_mask`)
    the ith bit will correspond to the ith m.

    If the ith bit of `begin_mask` is set, `begin[i]` is ignored and the fullest possible range in that dimension
    is used instead. `end_mask` is analogous, except with the end range.

    As for a 5*6*7 tensor, `x[2:,:3,:]` is equivalent to `x[2:5,0:3,0:7]`.

    If the ith bit of `ellipsis_mask` is set, as many unspecified dimensions as needed will be inserted between
    other dimensions. Only one non-zero bit is allowed in `ellipsis_mask`.

    As for a 5*6*7*8 tensor, `x[2:,...,:6]` is equivalent to `x[2:5,:,:,0:6]`.
    `x[2:,...]` is equivalent to `x[2:5,:,:,:]`.

    If the ith bit of `new_axis_mask` is set, `begin`, `end` and `strides` are ignored and a new length 1
    dimension is added at the specified position in tthe output tensor.

    As for a 5*6*7 tensor, `x[:2, newaxis, :6]` will produce a tensor with shape :math:`(2, 1, 7)` .

    If the ith bit of `shrink_axis_mask` is set, ith size shrinks the dimension by 1, taking on the value
    at index `begin[i]`, `end[i]` and `strides[i]` are ignored.

    As for a 5*6*7 tensor, `x[:, 5, :]` will result in `shrink_axis_mask` equal to 4.

    Note:
        The stride may be negative value, which causes reverse slicing.
        The shape of `begin`, `end` and `strides` must be the same.
        `begin` and `end` are zero-indexed. The element of `strides` must be non-zero.

    Args:
        begin_mask (int): Starting index of the slice. Default: 0.
        end_mask (int): Ending index of the slice. Default: 0.
        ellipsis_mask (int): An int mask. Default: 0.
        new_axis_mask (int): An int mask. Default: 0.
        shrink_axis_mask (int): An int mask. Default: 0.

    Inputs:
        - **input_x** (Tensor) - The input Tensor.
        - **begin** (tuple[int]) - A tuple which represents the location where to start. Only
          constant value is allowed.
        - **end** (tuple[int]) - A tuple or which represents the maximum location where to end.
          Only constant value is allowed.
        - **strides** (tuple[int]) - A tuple which represents the stride is continuously added
          before reaching the maximum location. Only constant value is allowed.

    Outputs:
        Tensor, The output is explained by following example.

        In the 0th dimension, begin is 1, end is 2, and strides is 1,
        because :math:`1+1=2\geq2`, the interval is :math:`[1,2)`.
        Thus, return the element with :math:`index = 1` in 0th dimension, i.e., [[3, 3, 3], [4, 4, 4]].

        In the 1st dimension, similarly, the interval is :math:`[0,1)`.
        Based on the return value of the 0th dimension, return the element with :math:`index = 0`,
        i.e., [3, 3, 3].

        In the 2nd dimension, similarly, the interval is :math:`[0,3)`.
        Based on the return value of the 1st dimension, return the element with :math:`index = 0,1,2`,
        i.e., [3, 3, 3].

        Finally, the output is [3, 3, 3].

    Raises:
        TypeError: If `begin_mask`, `end_mask`, `ellipsis_mask`, `new_axis_mask` or `shrink_axis_mask` is not an int.
        TypeError: If `begin`, `end` or `strides` is not a tuple.
        ValueError: If `begin_mask`, `end_mask`, `ellipsis_mask`, `new_axis_mask` or `shrink_axis_mask` is less than 0.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor([[[1, 1, 1], [2, 2, 2]], [[3, 3, 3], [4, 4, 4]],
        ...                   [[5, 5, 5], [6, 6, 6]]], mindspore.float32)
        >>> #         [[[1. 1. 1.]
        >>> #           [2. 2. 2.]]
        >>> #
        >>> #          [[3. 3. 3.]
        >>> #           [4. 4. 4.]]
        >>> #
        >>> #          [[5. 5. 5.]
        >>> #           [6. 6. 6.]]]
        >>> # In order to visually view the multi-dimensional array, write the above as follows：
        >>> #         [
        >>> #             [
        >>> #                 [1,1,1]
        >>> #                 [2,2,2]
        >>> #             ]
        >>> #             [
        >>> #                 [3,3,3]
        >>> #                 [4,4,4]
        >>> #             ]
        >>> #             [
        >>> #                 [5,5,5]
        >>> #                 [6,6,6]
        >>> #             ]
        >>> #         ]
        >>> strided_slice = ops.StridedSlice()
        >>> output = strided_slice(input_x, (1, 0, 2), (3, 1, 3), (1, 1, 1))
        >>> # Take this " output = strided_slice(input_x, (1, 0, 2), (3, 1, 3), (1, 1, 1)) " as an example,
        >>> # start = [1, 0, 2] , end = [3, 1, 3], stride = [1, 1, 1], Find a segment of (start, end),
        >>> # note that end is an open interval
        >>> # To facilitate understanding, this operator can be divided into three steps:
        >>> # Step 1: Calculation of the first dimension:
        >>> # start = 1, end = 3, stride = 1, So can take 1st, 2nd rows, and then gets the final output at this time.
        >>> # output_1th =
        >>> # [
        >>> #     [
        >>> #         [3,3,3]
        >>> #         [4,4,4]
        >>> #     ]
        >>> #     [
        >>> #         [5,5,5]
        >>> #         [6,6,6]
        >>> #     ]
        >>> # ]
        >>> # Step 2: Calculation of the second dimension
        >>> # 2nd dimension, start = 0, end = 1, stride = 1. So only 0th rows can be taken, and the output at this time.
        >>> # output_2nd =
        >>> # [
        >>> #     [
        >>> #         [3,3,3]
        >>> #     ]
        >>> #     [
        >>> #         [5,5,5]
        >>> #     ]
        >>> # ]
        >>> # Step 3: Calculation of the third dimension
        >>> # 3nd dimension,start = 2, end = 3, stride = 1, So can take 2th cols,
        >>> # and you get the final output at this time.
        >>> # output_3ed =
        >>> # [
        >>> #     [
        >>> #         [3]
        >>> #     ]
        >>> #     [
        >>> #         [5]
        >>> #     ]
        >>> # ]
        >>> # The final output after finishing is:
        >>> print(output)
        [[[3.]]
         [[5.]]]
        >>> # another example like :
        >>> output = strided_slice(input_x, (1, 0, 0), (2, 1, 3), (1, 1, 1))
        >>> print(output)
        [[[3. 3. 3.]]]
    """

    @prim_attr_register
    def __init__(self,
                 begin_mask=0,
                 end_mask=0,
                 ellipsis_mask=0,
                 new_axis_mask=0,
                 shrink_axis_mask=0):
        """Initialize StridedSlice"""
        self.init_prim_io_names(inputs=['x', 'begin', 'end', 'strides'], outputs=['output'])

        validator.check_non_negative_int(begin_mask, 'begin_mask', self.name)
        validator.check_non_negative_int(end_mask, 'end_mask', self.name)
        validator.check_non_negative_int(ellipsis_mask, 'ellipsis_mask', self.name)
        if len(tuple(filter(lambda x: x == '1', bin(ellipsis_mask)[-1:1:-1]))) > 1:
            raise ValueError(f"For '{self.name}', only support one ellipsis in the index, but got {end_mask}.")
        validator.check_non_negative_int(new_axis_mask, 'new_axis_mask', self.name)
        validator.check_non_negative_int(shrink_axis_mask, 'shrink_axis_mask', self.name)

    def _check_and_get_value(self, slice_input, name):
        """Check begin, end, strides. Get its length and value."""
        slice_value = slice_input['value']
        has_special_value = False
        if "min_value" in slice_input and "max_value" in slice_input:
            slice_min = slice_input["min_value"]
            slice_max = slice_input["max_value"]
            has_special_value = True
        else:
            slice_min = slice_value
            slice_max = slice_value
        if slice_value is None:
            validator.check_tensor_dtype_valid(name, slice_input['dtype'], [mstype.int64], self.name)
            slice_shape = slice_input['shape']
            if len(slice_shape) != 1:
                raise ValueError(f"For '{self.name}', both the 'begins', 'ends', and 'strides' must be 1-D, "
                                 f"but got '{name}' shape: {slice_shape}.")
            # not support scalar
            slices = {
                'value': slice_value,
                'min_value': slice_min,
                'max_value': slice_max
            }
            return slices, slice_shape[0], has_special_value

        if isinstance(slice_value, Tensor_):
            validator.check_tensor_dtype_valid(name, slice_input['dtype'], [mstype.int64], self.name)
            slice_value = slice_value.asnumpy().tolist()
        elif not isinstance(slice_value, tuple):
            raise TypeError(f"For '{self.name}', both the 'begin', 'end', and 'strides' must be a tuple or Tensor, "
                            f"but got '{name}': {slice_value}.")

        if tuple(filter(lambda x: not isinstance(x, int), slice_value)):
            raise TypeError(f"For '{self.name}', the elements of 'begin', 'end', and 'strides' must be int, "
                            f"but got {name}: {slice_value}.")

        if name == 'strides':
            if slice_value is not None and tuple(filter(lambda x: x == 0, slice_value)):
                raise ValueError(f"For '{self.name}', 'strides' cannot contain 0, but got 'strides': {slice_value}.")

        slices = {
            'value': slice_value,
            'min_value': slice_min,
            'max_value': slice_max
        }
        return slices, len(slice_value), has_special_value

    def _check_and_get_shape(self, x):
        """Check the shape of x. Get its shape and min/max_shape."""
        x_shape = x['shape']
        min_shape = None
        max_shape = None
        if "min_shape" in x and "max_shape" in x:
            min_shape = x["min_shape"]
            max_shape = x["max_shape"]
        return x_shape, min_shape, max_shape

    def __infer__(self, x, begin, end, strides):
        x_shape, min_shape, max_shape = self._check_and_get_shape(x)
        begin_v, begin_len, begin_specical_value = self._check_and_get_value(begin, 'begin')
        end_v, end_len, end_specical_value = self._check_and_get_value(end, 'end')
        strides_v, strides_len = self._check_and_get_value(strides, 'strides')[0:2]

        if begin_len != strides_len or end_len != strides_len:
            raise ValueError(f"For '{self.name}', 'begin', 'end' and 'strides' must be the same length, but got "
                             f"'begin' length: {begin_len}, 'end' length: {end_len}, 'strides' length: {strides_len}.")

        bd_has_min_max_value = False
        if begin_specical_value or end_specical_value:
            bd_has_min_max_value = True

        if bd_has_min_max_value and is_shape_known(x_shape):
            ret_shape = [-1] * len(x_shape)
            ret_min_shape = list(x_shape)
            ret_max_shape = list(x_shape)
            for i, _ in enumerate(ret_shape):
                ret_min_shape[i] = end_v['min_value'][i] - begin_v['min_value'][i]
                ret_max_shape[i] = end_v['max_value'][i] - begin_v['max_value'][i]
            i = 0
            for a, b in zip(ret_min_shape, ret_max_shape):
                if a == b:
                    ret_shape[i] = a
                i += 1
            return {'shape': tuple(ret_shape),
                    'dtype': x['dtype'],
                    'value': None,
                    'max_shape': tuple(ret_max_shape),
                    'min_shape': tuple(ret_min_shape)}

        if None in (begin_v['value'], end_v['value'], strides_v['value']) or is_shape_unknown(x_shape):
            ret_shape, ret_min_shape, ret_max_shape = \
                self._compute_dynamic_slicing_shape(x_shape, begin_len, max_shape)
            rets = {'shape': ret_shape,
                    'dtype': x['dtype'],
                    'value': None}

            if max_shape is not None and min_shape is not None:
                rets['min_shape'] = ret_min_shape
                rets['max_shape'] = ret_max_shape

            if is_shape_known(x_shape):
                return self._compute_max_min_shape(rets, x_shape, ret_shape)

            return rets

        ret_shape = self._compute_slicing_shape(x_shape, begin_v['value'], end_v['value'], strides_v['value'])
        if all(ret_shape):
            value = None
        else:
            init_func = Zero()
            init_func.__enable_zero_dim__ = True
            value = Tensor(dtype=x['dtype'].element_type(), shape=ret_shape, init=init_func)

        if "max_value" in x and "min_value" in x:
            validator.check_value_type("min_value", x["min_value"], [tuple, list], self.name)
            validator.check_value_type("max_value", x["max_value"], [tuple, list], self.name)
            max_value_np = np.array(x["max_value"])
            min_value_np = np.array(x["min_value"])
            slice_index = []
            for begin_i, end_i, strides_i in zip(begin_v['value'], end_v['value'], strides_v['value']):
                s = slice(begin_i, end_i, strides_i)
                slice_index.append(s)
            slice_index = tuple(slice_index)
            max_value_slice = max_value_np[slice_index]
            min_value_slice = min_value_np[slice_index]
            max_value_slice = tuple(max_value_slice.tolist())
            min_value_slice = tuple(min_value_slice.tolist())
            return {'shape': ret_shape,
                    'dtype': x['dtype'],
                    'value': value,
                    'max_value': max_value_slice,
                    'min_value': min_value_slice}

        return {'shape': ret_shape,
                'dtype': x['dtype'],
                'value': value}

    def _compute_max_min_shape(self, rets, x_shape, ret_shape):
        """compute max/min shape"""
        ret_min_shape = [1] * len(x_shape)
        ret_max_shape = x_shape
        for i, val in enumerate(ret_shape):
            if val > 0:
                ret_min_shape[i] = val
                ret_max_shape[i] = val
        rets['max_shape'] = ret_max_shape
        rets['min_shape'] = ret_min_shape
        return rets

    def _compute_slicing_shape(self, x_shape, begin_v, end_v, strides_v):
        """Computes the shape of the slicing."""
        x_rank = len(x_shape)
        slice_len = len(begin_v)

        # After the integer is converted to binary, it is a str and the first two chars are the flag char '0b'.
        begin_pos = bin(self.begin_mask)[-1:1:-1]
        end_pos = bin(self.end_mask)[-1:1:-1]
        ellipsis_pos = bin(self.ellipsis_mask)[-1:1:-1]
        new_axis_pos = bin(self.new_axis_mask)[-1:1:-1]
        shrink_axis_pos = bin(self.shrink_axis_mask)[-1:1:-1]

        ret_shape = []
        i, j = 0, 0
        has_ellipsis = False
        while i < x_rank or j < slice_len:
            if j < slice_len:
                begin, end, stride = begin_v[j], end_v[j], strides_v[j]

                if j < len(ellipsis_pos) and ellipsis_pos[j] == '1':
                    # When there is ellipsis, the latter part of the ellipsis will be processed separately.
                    has_ellipsis = True
                    break
                if j < len(begin_pos) and begin_pos[j] == '1':
                    begin = -1 if strides_v[j] < 0 else 0
                if j < len(end_pos) and end_pos[j] == '1':
                    end = -(x_shape[i] + 1) if strides_v[j] < 0 else x_shape[i]
                if j < len(new_axis_pos) and new_axis_pos[j] == '1':
                    ret_shape.append(1)
                    j += 1
                    continue
                if j < len(shrink_axis_pos) and shrink_axis_pos[j] == '1':
                    if (not -x_shape[i] <= begin < x_shape[i]) or stride < 0:
                        raise IndexError(f"For '{self.name}', the 'strides[{i}]' cannot be negative number and "
                                         f"'begin[{i}]' must be in [-{x_shape[i]}, {x_shape[i]}) "
                                         f"when 'shrink_axis_mask' is greater than 0, "
                                         f"but got 'shrink_axis_mask': {self.shrink_axis_mask}, "
                                         f"'strides[{i}]': {stride}, 'begin[{i}]': {begin}.")
                    j += 1
                    i += 1
                    continue
            else:
                begin, end, stride = 0, x_shape[i], 1

            slicing_length = _compute_slicing_length(begin, end, stride, x_shape, i)
            ret_shape.append(slicing_length)
            i += 1
            j += 1
        if has_ellipsis:
            # When there is ellipsis, handle the second half of the ellipsis split.
            ellipsis_occupied_dims = x_rank - i - (slice_len - (j + 1)) + \
                                     len(tuple(filter(lambda x: x == '1', new_axis_pos[j + 1:slice_len])))
            ret_shape.extend(x_shape[i:i + ellipsis_occupied_dims])
            j += 1
            i += ellipsis_occupied_dims

            while i < x_rank or j < slice_len:
                begin, end, stride = begin_v[j], end_v[j], strides_v[j]

                if j < len(begin_pos) and begin_pos[j] == '1':
                    begin = -1 if strides_v[j] < 0 else 0
                if j < len(end_pos) and end_pos[j] == '1':
                    end = -(x_shape[i] + 1) if strides_v[j] < 0 else x_shape[i]
                if j < len(new_axis_pos) and new_axis_pos[j] == '1':
                    ret_shape.append(1)
                    j += 1
                    continue
                if j < len(shrink_axis_pos) and shrink_axis_pos[j] == '1':
                    if (not -x_shape[i] <= begin < x_shape[i]) or stride < 0:
                        raise IndexError(f"For '{self.name}', the 'strides[{i}]' can not be negative number and "
                                         f"'begin[{i}]' must be in [-{x_shape[i]}, {x_shape[i]}) "
                                         f"when 'shrink_axis_mask' is greater than 0, "
                                         f"but got 'shrink_axis_mask': {self.shrink_axis_mask}, "
                                         f"'strides[{i}]': {stride}, 'begin[{i}]': {begin}.")
                    j += 1
                    i += 1
                    continue

                slicing_length = _compute_slicing_length(begin, end, stride, x_shape, i)
                ret_shape.append(slicing_length)
                i += 1
                j += 1
        return ret_shape

    def _compute_dynamic_slicing_shape(self, x_shape, slice_len, max_shape):
        """Computes the shape of the slicing for dynamic shape, mask is currently not supported."""
        x_rank = len(x_shape)
        new_axis_pos = bin(self.new_axis_mask)[-1:1:-1]
        shrink_axis_pos = bin(self.shrink_axis_mask)[-1:1:-1]
        if self.ellipsis_mask:
            raise ValueError("Ellipsis Mask is currently not supported in dynamic shape.")
        ret_shape = []
        ret_min_shape = []
        ret_max_shape = []
        i, j = 0, 0
        while i < x_rank or j < slice_len:
            slicing_length = -1 if x_shape[i] != 1 else 1
            if j < slice_len:
                if j < len(new_axis_pos) and new_axis_pos[j] == '1':
                    ret_shape.append(1)
                    ret_min_shape.append(1)
                    ret_max_shape.append(1)
                    j += 1
                    continue
                if j < len(shrink_axis_pos) and shrink_axis_pos[j] == '1':
                    j += 1
                    i += 1
                    continue
            else:
                if i >= len(x_shape):
                    raise ValueError(f"For 'StridedSlice', the index must be less than or equal to "
                                     f"the dimension of 'input_x', but got the dimension of 'input_x': {len(x_shape)} "
                                     f"and the index: {i}.")
                begin, end, stride = 0, x_shape[i], 1
                if end > 0:
                    slicing_length = _compute_slicing_length(begin, end, stride, x_shape, i)
            ret_shape.append(slicing_length)
            if max_shape is not None:
                ret_min_shape.append(1)
                ret_max_shape.append(max_shape[i])
            i += 1
            j += 1
        return ret_shape, ret_min_shape, ret_max_shape


class Diag(PrimitiveWithInfer):
    r"""

    Constructs a diagonal tensor with a given diagonal values.

    Refer to :func:`mindspore.ops.diag` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> input_x = Tensor([1, 2, 3, 4])
        >>> diag = ops.Diag()
        >>> output = diag(input_x)
        >>> print(output)
        [[1, 0, 0, 0],
         [0, 2, 0, 0],
         [0, 0, 3, 0],
         [0, 0, 0, 4]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Diag"""

    def infer_dtype(self, x_type):
        validator.check_subclass('input_x', x_type, mstype.tensor, self.name)
        return x_type

    def infer_shape(self, x_shape):
        validator.check("x rank", len(x_shape), "", 1, Rel.GE)
        ret_shape = copy.deepcopy(x_shape)
        ret_shape = ret_shape + ret_shape
        return ret_shape

    def infer_value(self, x):
        if x is None:
            return None
        # do constant-folding only when x rank is 1
        if len(x.shape) != 1:
            return None
        ret = np.diag(x.asnumpy())
        return Tensor(ret)


class DiagPart(PrimitiveWithInfer):
    r"""

    Extracts the diagonal part from given tensor.

    Assume input has dimensions :math:`[D_1,..., D_k, D_1,..., D_k]`, the output is a tensor
    of rank k with dimensions :math:`[D_1,..., D_k]` where:
    :math:`output[i_1,..., i_k] = input[i_1,..., i_k, i_1,..., i_k]`.

    Inputs:
        - **input_x** (Tensor) - The input tensor of rank 2k, k is not zero.

    Outputs:
        Tensor, the extracted diagonal has the same dtype as the `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        ValueError: If rank of `input_x` is not even or zero.
        ValueError: If input_shape[i] is not equal to input_shape[i + len(input_shape)/2].

    Supported Platforms:
        ``Ascend``

    Examples
        >>> input_x = Tensor([[1, 0, 0, 0],
        ...                   [0, 2, 0, 0],
        ...                   [0, 0, 3, 0],
        ...                   [0, 0, 0, 4]])
        >>> diag_part = ops.DiagPart()
        >>> output = diag_part(input_x)
        >>> print(output)
        [1 2 3 4]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize DiagPart"""

    def infer_dtype(self, x_type):
        validator.check_subclass('input_x', x_type, mstype.tensor, self.name)
        return x_type

    def infer_shape(self, x_shape):
        if len(x_shape) % 2 != 0 or \
                not x_shape:
            raise ValueError(f"For \'{self.name}\', the dimension of 'input_x' must be non-zero and even, "
                             f"but got dimension {len(x_shape)}, with shapes {x_shape}.")
        length = len(x_shape) // 2
        for i in range(length):
            validator.check('input_shape[i + len(input_shape)/2]', x_shape[i + length],
                            'input_shape[i]', x_shape[i], Rel.EQ, self.name)
        ret_shape = x_shape[0:length]
        return ret_shape

    def infer_value(self, x):
        if x is None:
            return None
        # do constant-folding only when x rank is 2
        if len(x.shape) != 2:
            return None
        ret = np.diag(x.asnumpy())
        return Tensor(ret)


class Mvlgamma(Primitive):
    r"""
    Computes the multivariate log-gamma function with dimension p element-wise.

    The following tex shows the mathematical calculation process of Mvlgamma:

    .. math::

        \log (\Gamma_{p}(a))=C+\sum_{i=1}^{p} \log (\Gamma(a-\frac{i-1}{2}))

    where :math:`C = \log(\pi) \times \frac{p(p-1)}{4}` and :math:`\Gamma(\cdot)` is the Gamma function.

    Args:
        p(int): The number of dimensions. And the value of `p` must be greater than or equal to 1.

    Inputs:
        - **x** (Tensor) - The tensor to compute the multivariate log-gamma function,
          which must be one of the following types: float32, float64.
          The shape is :math:`(N,*)`, where :math:`*` means any number of additional dimensions.
          And the value of any element in `x` must be greater than (p - 1) / 2.

    Outputs:
        Tensor, has the same shape and type as `x`.

    Raises:
        TypeError: If dtype of `x` is neither float32 nor float64.
        TypeError: If `p` is not an int.
        ValueError: If `p` is not greater than or equal to 1.
        ValueError: If all elements of `x` are not greater than (p-1)/2.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[3, 4, 5], [4, 2, 6]]), mindspore.float32)
        >>> op = ops.Mvlgamma(p=3)
        >>> y = op(x)
        >>> print(y)
        [[ 2.694925   5.402975   9.140645 ]
         [ 5.402975   1.5963125 13.640454 ]]
    """

    @prim_attr_register
    def __init__(self, p):
        """Initialize Mvlgamma."""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])
        validator.check_value_type('p', p, [int], self.name)
        validator.check_positive_int(p, 'p', self.name)


class Eye(Primitive):
    """
    Creates a tensor with ones on the diagonal and zeros in the rest.

    Refer to :func:`mindspore.ops.eye` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> eye = ops.Eye()
        >>> output = eye(2, 2, mindspore.int32)
        >>> print(output)
        [[1 0]
         [0 1]]
        >>> print(output.dtype)
        Int32
        >>> output = eye(1, 2, mindspore.float64)
        >>> print(output)
        [[1. 0.]]
        >>> print(output.dtype)
        Float64
    """

    @prim_attr_register
    def __init__(self):
        """Initialize Eye"""
        self.init_prim_io_names(inputs=['n', 'm', 't'], outputs=['output'])


class ScatterNd(Primitive):
    r"""
    Scatters a tensor into a new tensor depending on the specified indices.

    The following figure shows the calculation process of inserting two slices in the first dimension of a rank-3
    with two matrices of new values:

    .. image:: ScatterNd.png

    Refer to :func:`mindspore.ops.scatter_nd` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> op = ops.ScatterNd()
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2],
        ...                             [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[1, 1, 1, 1], [2, 2, 2, 2],
        ...                             [3, 3, 3, 3], [4, 4, 4, 4]]]), mindspore.float32)
        >>> shape = (4, 4, 4)
        >>> output = op(indices, updates, shape)
        >>> print(output)
        [[[1. 1. 1. 1.]
          [2. 2. 2. 2.]
          [3. 3. 3. 3.]
          [4. 4. 4. 4.]]
         [[0. 0. 0. 0.]
          [0. 0. 0. 0.]
          [0. 0. 0. 0.]
          [0. 0. 0. 0.]]
         [[1. 1. 1. 1.]
          [2. 2. 2. 2.]
          [3. 3. 3. 3.]
          [4. 4. 4. 4.]]
         [[0. 0. 0. 0.]
          [0. 0. 0. 0.]
          [0. 0. 0. 0.]
          [0. 0. 0. 0.]]]
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([3.2, 1.1]), mindspore.float32)
        >>> shape = (3, 3)
        >>> output = op(indices, updates, shape)
        >>> # In order to facilitate understanding, explain the operator pseudo-operation process step by step:
        >>> # Step 1: Generate an empty Tensor of the specified shape according to the shape
        >>> # [
        >>> #     [0. 0. 0.]
        >>> #     [0. 0. 0.]
        >>> #     [0. 0. 0.]
        >>> # ]
        >>> # Step 2: Modify the data at the specified location according to the indicators
        >>> # 0th row of indices is [0, 1], 0th row of updates is 3.2.
        >>> # means that the empty tensor in the 0th row and 1st col set to 3.2
        >>> # [
        >>> #     [0. 3.2. 0.]
        >>> #     [0. 0.   0.]
        >>> #     [0. 0.   0.]
        >>> # ]
        >>> # 1th row of indices is [1, 1], 1th row of updates is 1.1.
        >>> # means that the empty tensor in the 1th row and 1st col set to 1.1
        >>> # [
        >>> #     [0. 3.2. 0.]
        >>> #     [0. 1.1  0.]
        >>> #     [0. 0.   0.]
        >>> # ]
        >>> # The final result is as follows:
        >>> print(output)
        [[0. 3.2 0.]
         [0. 1.1 0.]
         [0. 0.  0.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize ScatterNd"""
        self.init_prim_io_names(inputs=['indices', 'update', 'shape'], outputs=['output'])


class ResizeNearestNeighbor(Primitive):
    r"""
    Resizes the input tensor by using the nearest neighbor algorithm.

    Resizes the input tensor to a given size by using the nearest neighbor algorithm. The nearest
    neighbor algorithm selects the value of the nearest point and does not consider the
    values of neighboring points at all, yielding a piecewise-constant interpolant.

    Args:
        size (Union[tuple, list]): The target size. The dimension of size must be 2.
        align_corners (bool): Whether the centers of the 4 corner pixels of the input
                              and output tensors are aligned. Default: False.

    Inputs:
        - **input_x** (Tensor) - The input tensor. The shape of the tensor is :math:`(N, C, H, W)`.

    Outputs:
        Tensor, the shape of the output tensor is  :math:`(N, C, NEW\_H, NEW\_W)`.
        The data type is the same as the `input_x`.

    Raises:
        TypeError: If `size` is neither tuple nor list.
        TypeError: If `align_corners` is not a bool.
        ValueError: If length of `size` is not equal to 2.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_tensor = Tensor(np.array([[[[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]]]), mindspore.float32)
        >>> resize = ops.ResizeNearestNeighbor((2, 2))
        >>> output = resize(input_tensor)
        >>> print(output)
        [[[[-0.1  0.3]
           [ 0.4  0.5]]]]
    """

    @prim_attr_register
    def __init__(self, size, align_corners=False):
        """Initialize ResizeNearestNeighbor"""
        validator.check_value_type("size", size, [tuple, list], self.name)
        validator.check_value_type("align_corners", align_corners, [bool], self.name)
        validator.check_equal_int(len(size), 2, "length of size", self.name)
        for i, value in enumerate(size):
            validator.check_non_negative_int(value, f'{i}th value of size', self.name)
        self.init_prim_io_names(inputs=['image_in'], outputs=['image_out'])


class ResizeNearestNeighborV2(Primitive):
    r"""
    Resizes the input tensor to specific size by using the nearest neighbor algorithm.

    Resizes the input tensor to a given size by using the nearest neighbor algorithm. The nearest
    neighbor algorithm selects the value of the nearest point and does not consider the
    values of neighboring points at all, yielding a piecewise-constant interpolant.

    Args:
        align_corners: An optional `bool`. Defaults to `False`.
            If true, the centers of the 4 corner pixels of the input and output tensors are
            aligned, preserving the values at the corner pixels.
        half_pixel_centers: An optional `bool`. Defaults to `False`.
        data_format: An optional `string` that describes the format of the input `x`. Defaults to `NHWC`.

    Inputs:
        - **x** (Tensor) - 4-D with shape `[batch, height, width, channels]` or `[batch, channels, height, width]`
          depending on the attr 'data_format'. Support type [`int8`, `uint8`, `int16`, `uint16`, `int32`, `int64`,
          `float16`, `float32`, `float64`].
        - **size** (Tensor) - A 1-D int32 Tensor of 2 elements: [`new_height, new_width`]. The new size for the images.

    Outputs:
        Tensor `y`, has the same type as input `x` with the shape of `[batch, channels, new_height, new_width]` or
        `[batch, new_height, new_width, channels]` depending on attr 'data_format'.

    Raises:
        TypeError: If `x` or `size` is not a Tensor.
        TypeError: If `x` data type not in support list.
        TypeError: If `size` data type is not int32.
        TypeError: If `align_corners` or `half_pixel_centers` is not `bool` value.
        TypeError: If `data_format` is not `str`.
        ValueError: If `data_format` not in [`NHWC`, `NCHW`].
        ValueError: If any value of `size` is non positive.
        ValueError: If the dimension of `x` is not 4.
        ValueError: If the dimension of `size` is not 1.
        ValueError: If the elements number of `size` is not 2.
        ValueError: If attr `half_pixel_centers` and `align_corners` are True at the same time.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> input_tensor = Tensor(np.ones((1, 4, 4, 1)), mstype.float32)
        >>> size = Tensor([2, 2], mstype.int32)
        >>> resize = ops.ResizeNearestNeighborV2()
        >>> output = resize(input_tensor, size)
        >>> print(output)
        [[[[1.]
           [1.]]
          [[1.]
           [1.]]]]
        >>> print(output.shape)
        (1, 2, 2, 1)
    """

    @prim_attr_register
    def __init__(self, align_corners=False, half_pixel_centers=False, data_format='NHWC'):
        """Initialize ResizeNearestNeighborV2"""
        self.init_prim_io_names(inputs=['x', 'size'], outputs=['y'])

        validator.check_bool(align_corners, 'align_corners', self.name)
        validator.check_bool(half_pixel_centers, 'half_pixel_centers', self.name)
        validator.check_value_type('data_format', data_format, [str], self.name)
        self.format = validator.check_string(data_format, ['NHWC', 'NCHW'], 'data_format', self.name)
        self.add_prim_attr('data_format', self.format)


class GatherNd(Primitive):
    r"""
    Gathers slices from a tensor by indices.

    Refer to :func:`mindspore.ops.gather_nd` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> op = ops.GatherNd()
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [1, 1]]), mindspore.int32)
        >>> output = op(input_x, indices)
        >>> print(output)
        [-0.1  0.5]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize GatherNd"""
        self.init_prim_io_names(inputs=['input_x', 'indices'], outputs=['y'])


class ScatterUpdate(Primitive):
    r"""
    Updates tensor values by using input indices and value.

    Using given values to update tensor value, along with the input indices.

    for each `i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :] = \text{updates}[i, ..., j, :]

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: True.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index of input tensor. With int32 data type.
          If there are duplicates in indices, the order for updating is undefined.
        - **updates** (Tensor) - The tensor to update the input tensor, has the same type as input,
          and updates.shape = indices.shape + input_x.shape[1:].

    Outputs:
        Tensor, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32.
        ValueError: If the shape of `updates` is not equal to `indices.shape + input_x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> np_x = np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]])
        >>> input_x = mindspore.Parameter(Tensor(np_x, mindspore.float32), name="x")
        >>> indices = Tensor(np.array([0, 1]), mindspore.int32)
        >>> np_updates = np.array([[2.0, 1.2, 1.0], [3.0, 1.2, 1.0]])
        >>> updates = Tensor(np_updates, mindspore.float32)
        >>> op = ops.ScatterUpdate()
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[2. 1.2  1.]
         [3. 1.2  1.]]
    """
    __mindspore_signature__ = (
        sig.make_sig('x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    @prim_attr_register
    def __init__(self, use_locking=True):
        """Initialize ScatterUpdate"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class ScatterNdUpdate(Primitive):
    r"""
    Updates tensor values by using input indices and value.

    Using given values to update tensor value, along with the input indices.

    `input_x` has rank P and `indices` has rank Q where `Q >= 2`.

    `indices` has shape :math:`(i_0, i_1, ..., i_{Q-2}, N)` where `N <= P`.

    The last dimension of `indices` (with length `N` ) indicates slices along the `N` th dimension of `input_x`.

    `updates` is a tensor of rank `Q-1+P-N`. Its shape is:
    :math:`(i_0, i_1, ..., i_{Q-2}, x\_shape_N, ..., x\_shape_{P-1})`.

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: True.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index of input tensor, with int32 or int64 data type.
        - **updates** (Tensor) - The tensor to be updated to the input tensor, has the same type as input.
          The shape is `indices.shape[:-1] + x.shape[indices.shape[-1]:]`.

    Outputs:
        Tensor, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32 or an int64.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> np_x = np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]])
        >>> input_x = mindspore.Parameter(Tensor(np_x, mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[0, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> op = ops.ScatterNdUpdate()
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[1.   0.3   3.6]
         [0.4  2.2  -3.2]]
    """

    __mindspore_signature__ = (
        sig.make_sig('input_x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    @prim_attr_register
    def __init__(self, use_locking=True):
        """Initialize ScatterNdUpdate"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['input_x', 'indices', 'value'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class ScatterMax(_ScatterOpDynamic):
    r"""
    Updates the value of the input tensor through the maximum operation.

    Using given values to update tensor value through the max operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each :math:`i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :]
        = max(\text{input_x}[\text{indices}[i, ..., j], :], \text{updates}[i, ..., j, :])

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do max operation whose data type must be mindspore.int32 or
          mindspore.int64.
        - **updates** (Tensor) - The tensor that performs the maximum operation with `input_x`,
          the data type is the same as `input_x`, the shape is `indices.shape + input_x.shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32 or an int64.
        ValueError: If the shape of `updates` is not equal to `indices.shape + x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.
        RuntimeError: On the Ascend platform, the input data dimension of `input_x` , `indices`
                      and `updates` is greater than 8 dimensions.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]), mindspore.float32),
        ...                     name="input_x")
        >>> indices = Tensor(np.array([[0, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.ones([2, 2, 3]) * 88, mindspore.float32)
        >>> scatter_max = ops.ScatterMax()
        >>> output = scatter_max(input_x, indices, updates)
        >>> print(output)
        [[88. 88. 88.]
         [88. 88. 88.]]
    """


class ScatterMin(_ScatterOpDynamic):
    r"""
    Updates the value of the input tensor through the minimum operation.

    Using given values to update tensor value through the min operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each :math:`i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :]
        = min(\text{input_x}[\text{indices}[i, ..., j], :], \text{updates}[i, ..., j, :])

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do min operation whose data type must be mindspore.int32 or
          mindspore.int64.
        - **updates** (Tensor) - The tensor doing the min operation with `input_x`,
          the data type is same as `input_x`, the shape is `indices.shape + input_x.shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32 or an int64.
        ValueError: If the shape of `updates` is not equal to `indices.shape + input_x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.
        RuntimeError: On the Ascend platform, the input data dimension of `input_x` , `indices`
                      and `updates` is greater than 8 dimensions.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[0.0, 1.0, 2.0], [0.0, 0.0, 0.0]]), mindspore.float32),
        ...                     name="input_x")
        >>> indices = Tensor(np.array([[0, 0], [1, 1]]), mindspore.int32)
        >>> update = Tensor(np.ones([2, 2, 3]), mindspore.float32)
        >>> scatter_min = ops.ScatterMin()
        >>> output = scatter_min(input_x, indices, update)
        >>> print(output)
        [[0. 1. 1.]
         [0. 0. 0.]]
    """


class ScatterAdd(_ScatterOpDynamic):
    r"""
    Updates the value of the input tensor through the addition operation.

    Using given values to update tensor value through the add operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each `i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :] \mathrel{+}= \text{updates}[i, ..., j, :]

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Note:
        This is an in-place update operator. Therefore, the `input_x` will be updated after the operation is completed.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock.
            If true, `input_x` will be protected by the lock.
            Otherwise, the calculation result is undefined. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do min operation whose data type must be mindspore.int32 or
          mindspore.int64.
        - **updates** (Tensor) - The tensor doing the min operation with `input_x`,
          the data type is same as `input_x`, the shape is `indices.shape + x.shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32 or an int64.
        ValueError: If the shape of `updates` is not equal to `indices.shape + x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.ones([2, 2, 3]), mindspore.float32)
        >>> scatter_add = ops.ScatterAdd()
        >>> output = scatter_add(input_x, indices, updates)
        >>> print(output)
        [[1. 1. 1.]
         [3. 3. 3.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [1, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [0.0, 0.0, 0.0] + [1.0, 1.0, 1.0] = [1.0, 1.0, 1.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] + [3.0, 3.0, 3.0] = [3.0, 3.0, 3.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [3.0, 3.0, 3.0] + [7.0, 7.0, 7.0] = [10.0, 10.0, 10.0]
        >>> # input_x[1] = [10.0, 10.0, 10.0] + [9.0, 9.0, 9.0] = [19.0, 19.0, 19.0]
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_add = ops.ScatterAdd()
        >>> output = scatter_add(input_x, indices, updates)
        >>> print(output)
        [[ 1.  1.  1.]
         [19. 19. 19.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[1, 0], [1, 1]]
        >>> # step 1: [1, 0]
        >>> # input_x[0] = [0.0, 0.0, 0.0] + [3.0, 3.0, 3.0] = [3.0, 3.0, 3.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] + [1.0, 1.0, 1.0] = [1.0, 1.0, 1.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [1.0, 1.0, 1.0] + [7.0, 7.0, 7.0] = [8.0, 8.0, 8.0]
        >>> # input_x[1] = [8.0, 8.0, 8.0] + [9.0, 9.0, 9.0] = [17.0, 17.0, 17.0]
        >>> indices = Tensor(np.array([[1, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_add = ops.ScatterAdd()
        >>> output = scatter_add(input_x, indices, updates)
        >>> print(output)
        [[ 3.  3.  3.]
         [17. 17. 17.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [0, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [0.0, 0.0, 0.0] + [1.0, 1.0, 1.0] = [1.0, 1.0, 1.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] + [3.0, 3.0, 3.0] = [3.0, 3.0, 3.0]
        >>> # step 2: [0, 1]
        >>> # input_x[0] = [1.0, 1.0, 1.0] + [7.0, 7.0, 7.0] = [8.0, 8.0, 8.0]
        >>> # input_x[1] = [3.0, 3.0, 3.0] + [9.0, 9.0, 9.0] = [12.0, 12.0, 12.0]
        >>> indices = Tensor(np.array([[0, 1], [0, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_add = ops.ScatterAdd()
        >>> output = scatter_add(input_x, indices, updates)
        >>> print(output)
        [[ 8.  8.  8.]
         [12. 12. 12.]]
    """

    @prim_attr_register
    def __init__(self, use_locking=False):
        """Initialize ScatterAdd"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class ScatterSub(Primitive):
    r"""
    Updates the value of the input tensor through the subtraction operation.

    Using given values to update tensor value through the subtraction operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each `i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :] \mathrel{-}= \text{updates}[i, ..., j, :]

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do min operation whose data type must be mindspore.int32 or
          mindspore.int64.
        - **updates** (Tensor) - The tensor doing the min operation with `input_x`,
          the data type is same as `input_x`, the shape is `indices_shape + x_shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32.
        ValueError: If the shape of `updates` is not equal to `indices_shape + x_shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[0, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]]), mindspore.float32)
        >>> scatter_sub = ops.ScatterSub()
        >>> output = scatter_sub(input_x, indices, updates)
        >>> print(output)
        [[-1. -1. -1.]
         [-1. -1. -1.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [1, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [0.0, 0.0, 0.0] - [1.0, 1.0, 1.0] = [-1.0, -1.0, -1.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] - [3.0, 3.0, 3.0] = [-3.0, -3.0, -3.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [-3.0, -3.0, -3.0] - [7.0, 7.0, 7.0] = [-10.0, -10.0, -10.0]
        >>> # input_x[1] = [-10.0, -10.0, -10.0] - [9.0, 9.0, 9.0] = [-19.0, -19.0, -19.0]
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_sub = ops.ScatterSub()
        >>> output = scatter_sub(input_x, indices, updates)
        >>> print(output)
        [[ -1.  -1.  -1.]
         [-19. -19. -19.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[1, 0], [1, 1]]
        >>> # step 1: [1, 0]
        >>> # input_x[0] = [0.0, 0.0, 0.0] - [3.0, 3.0, 3.0] = [-3.0, -3.0, -3.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] - [1.0, 1.0, 1.0] = [-1.0, -1.0, -1.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [-1.0, -1.0, -1.0] - [7.0, 7.0, 7.0] = [-8.0, -8.0, -8.0]
        >>> # input_x[1] = [-8.0, -8.0, -8.0] - [9.0, 9.0, 9.0] = [-17.0, -17.0, -17.0]
        >>> indices = Tensor(np.array([[1, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_sub = ops.ScatterSub()
        >>> output = scatter_sub(input_x, indices, updates)
        >>> print(output)
        [[ -3.  -3.  -3.]
         [-17. -17. -17.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [0, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [0.0, 0.0, 0.0] - [1.0, 1.0, 1.0] = [-1.0, -1.0, -1.0]
        >>> # input_x[1] = [0.0, 0.0, 0.0] - [3.0, 3.0, 3.0] = [-3.0, -3.0, -3.0]
        >>> # step 2: [0, 1]
        >>> # input_x[0] = [-1.0, -1.0, -1.0] - [7.0, 7.0, 7.0] = [-8.0, -8.0, -8.0]
        >>> # input_x[1] = [-3.0, -3.0, -3.0] - [9.0, 9.0, 9.0] = [-12.0, -12.0, -12.0]
        >>> indices = Tensor(np.array([[0, 1], [0, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_sub = ops.ScatterSub()
        >>> output = scatter_sub(input_x, indices, updates)
        >>> print(output)
        [[ -8.  -8.  -8.]
         [-12. -12. -12.]]
    """
    __mindspore_signature__ = (
        sig.make_sig('input_x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    @prim_attr_register
    def __init__(self, use_locking=False):
        """Initialize ScatterSub"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class Triu(Primitive):
    """
    Returns a tensor with elements below the kth diagonal zeroed.

    Args:
        diagonal (int): The index of diagonal. Default: 0

    Inputs:
        - **x** (Tensor) -  The input tensor. The data type is Number. (N,∗)
        where ∗ means, any number of additional dimensions.

    Outputs:
        - **y** (Tensor) - A tensor has the same shape and data type as input.

    Raises:
        TypeError: If `diagonal` is not an int.
        TypeError: If `x` is not an Tensor.
        ValueError: If length of shape of x is less than 1.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> triu = P.Triu()
        >>> result = triu(x)
        >>> print(result)
        [[ 1  2  3  4]
         [ 0  6  7  8]
         [ 0  0 12 13]
         [ 0  0  0 17]]
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> triu = P.Triu(diagonal=1)
        >>> result = triu(x)
        >>> print(result)
        [[ 0  2  3  4]
         [ 0  0  7  8]
         [ 0  0  0 13]
         [ 0  0  0  0]]
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> triu = P.Triu(diagonal=-1)
        >>> result = triu(x)
        >>> print(result)
        [[ 1  2  3  4]
         [ 5  6  7  8]
         [ 0 11 12 13]
         [ 0  0 16 17]]
    """

    @prim_attr_register
    def __init__(self, diagonal=0):
        """Initialize Triu"""
        validator.check_value_type("diagonal", diagonal, [int], self.name)
        self.diagonal = diagonal
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class ScatterMul(_ScatterOp):
    r"""
    Updates the value of the input tensor through the multiply operation.

    Using given values to update tensor value through the mul operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each `i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :] \mathrel{*}= \text{updates}[i, ..., j, :]

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do min operation whose data type must be mindspore.int32.
        - **updates** (Tensor) - The tensor doing the min operation with `input_x`,
          the data type is same as `input_x`, the shape is `indices.shape + x.shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32.
        ValueError: If the shape of `updates` is not equal to `indices.shape + x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([0, 1]), mindspore.int32)
        >>> updates = Tensor(np.array([[2.0, 2.0, 2.0], [2.0, 2.0, 2.0]]), mindspore.float32)
        >>> scatter_mul = ops.ScatterMul()
        >>> output = scatter_mul(input_x, indices, updates)
        >>> print(output)
        [[2. 2. 2.]
         [4. 4. 4.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [1, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [1.0, 1.0, 1.0] * [1.0, 1.0, 1.0] = [1.0, 1.0, 1.0]
        >>> # input_x[1] = [2.0, 2.0, 2.0] * [3.0, 3.0, 3.0] = [6.0, 6.0, 6.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [6.0, 6.0, 6.0] * [7.0, 7.0, 7.0] = [42.0, 42.0, 42.0]
        >>> # input_x[1] = [42.0, 42.0, 42.0] * [9.0, 9.0, 9.0] = [378.0, 378.0, 378.0]
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_mul = ops.ScatterMul()
        >>> output = scatter_mul(input_x, indices, updates)
        >>> print(output)
        [[  1.   1.   1.]
         [378. 378. 378.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]), mindspore.float32), name="x")
        >>> # for indices = [[1, 0], [1, 1]]
        >>> # step 1: [1, 0]
        >>> # input_x[0] = [1.0, 1.0, 1.0] * [3.0, 3.0, 3.0] = [3.0, 3.0, 3.0]
        >>> # input_x[1] = [2.0, 2.0, 2.0] * [1.0, 1.0, 1.0] = [2.0, 2.0, 2.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [2.0, 2.0, 2.0] * [7.0, 7.0, 7.0] = [14.0, 14.0, 14.0]
        >>> # input_x[1] = [14.0, 14.0, 14.0] * [9.0, 9.0, 9.0] = [126.0, 126.0, 126.0]
        >>> indices = Tensor(np.array([[1, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_mul = ops.ScatterMul()
        >>> output = scatter_mul(input_x, indices, updates)
        >>> print(output)
        [[  3.   3.   3.]
         [126. 126. 126.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [0, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [1.0, 1.0, 1.0] * [1.0, 1.0, 1.0] = [1.0, 1.0, 1.0]
        >>> # input_x[1] = [2.0, 2.0, 2.0] * [3.0, 3.0, 3.0] = [6.0, 6.0, 6.0]
        >>> # step 2: [0, 1]
        >>> # input_x[0] = [1.0, 1.0, 1.0] * [7.0, 7.0, 7.0] = [7.0, 7.0, 7.0]
        >>> # input_x[1] = [6.0, 6.0, 6.0] * [9.0, 9.0, 9.0] = [54.0, 54.0, 54.0]
        >>> indices = Tensor(np.array([[0, 1], [0, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[7.0, 7.0, 7.0], [9.0, 9.0, 9.0]]]), mindspore.float32)
        >>> scatter_mul = ops.ScatterMul()
        >>> output = scatter_mul(input_x, indices, updates)
        >>> print(output)
        [[ 7.  7.  7.]
         [54. 54. 54.]]
    """


class ScatterDiv(_ScatterOpDynamic):
    r"""
    Updates the value of the input tensor through the divide operation.

    Using given values to update tensor value through the div operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    for each :math:`i, ..., j` in `indices.shape`:

    .. math::

        \text{input_x}[\text{indices}[i, ..., j], :] \mathrel{/}= \text{updates}[i, ..., j, :]

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Args:
        use_locking (bool): Whether to protect the assignment by a lock. Default: False.

    Inputs:
        - **input_x** (Parameter) - The target tensor, with data type of Parameter.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
        - **indices** (Tensor) - The index to do min operation whose data type must be mindspore.int32 or
          mindspore.int64.
        - **updates** (Tensor) - The tensor doing the min operation with `input_x`,
          the data type is same as `input_x`, the shape is `indices.shape + input_x.shape[1:]`.

    Outputs:
        Tensor, the updated `input_x`, has the same shape and type as `input_x`.

    Raises:
        TypeError: If `use_locking` is not a bool.
        TypeError: If `indices` is not an int32 or an int64.
        ValueError: If the shape of `updates` is not equal to `indices.shape + input_x.shape[1:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.
        RuntimeError: On the Ascend platform, the input data dimension of `input_x` , `indices`
                      and `updates` is greater than 8 dimensions.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([[6.0, 6.0, 6.0], [2.0, 2.0, 2.0]]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([0, 1]), mindspore.int32)
        >>> updates = Tensor(np.array([[2.0, 2.0, 2.0], [2.0, 2.0, 2.0]]), mindspore.float32)
        >>> scatter_div = ops.ScatterDiv()
        >>> output = scatter_div(input_x, indices, updates)
        >>> print(output)
        [[3. 3. 3.]
         [1. 1. 1.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[105.0, 105.0, 105.0],
        ...                                      [315.0, 315.0, 315.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [1, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [105.0, 105.0, 105.0] / [1.0, 1.0, 1.0] = [105.0, 105.0, 105.0]
        >>> # input_x[1] = [315.0, 315.0, 315.0] / [3.0, 3.0, 3.0] = [105.0, 105.0, 105.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [105.0, 105.0, 105.0] / [5.0, 5.0, 5.0] = [21.0, 21.0, 21.0]
        >>> # input_x[1] = [21.0, 21.0, 21.0] / [7.0, 7.0, 7.0] = [3.0, 3.0, 3.0]
        >>> indices = Tensor(np.array([[0, 1], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[5.0, 5.0, 5.0], [7.0, 7.0, 7.0]]]), mindspore.float32)
        >>> scatter_div = ops.ScatterDiv()
        >>> output = scatter_div(input_x, indices, updates)
        >>> print(output)
        [[105. 105. 105.]
         [  3.   3.   3.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[105.0, 105.0, 105.0],
        ...                                      [315.0, 315.0, 315.0]]), mindspore.float32), name="x")
        >>> # for indices = [[1, 0], [1, 1]]
        >>> # step 1: [1, 0]
        >>> # input_x[0] = [105.0, 105.0, 105.0] / [3.0, 3.0, 3.0] = [35.0, 35.0, 35.0]
        >>> # input_x[1] = [315.0, 315.0, 315.0] / [1.0, 1.0, 1.0] = [315.0, 315.0, 315.0]
        >>> # step 2: [1, 1]
        >>> # input_x[1] = [315.0, 315.0, 315.0] / [5.0, 5.0, 5.0] = [63.0 63.0 63.0]
        >>> # input_x[1] = [63.0 63.0 63.0] / [7.0, 7.0, 7.0] = [9.0, 9.0, 9.0]
        >>> indices = Tensor(np.array([[1, 0], [1, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[5.0, 5.0, 5.0], [7.0, 7.0, 7.0]]]), mindspore.float32)
        >>> scatter_div = ops.ScatterDiv()
        >>> output = scatter_div(input_x, indices, updates)
        >>> print(output)
        [[35. 35. 35.]
         [ 9.  9.  9.]]
        >>> # for input_x will be updated after the operation is completed. input_x need to be re-initialized.
        >>> input_x = Parameter(Tensor(np.array([[105.0, 105.0, 105.0],
        ...                                      [315.0, 315.0, 315.0]]), mindspore.float32), name="x")
        >>> # for indices = [[0, 1], [0, 1]]
        >>> # step 1: [0, 1]
        >>> # input_x[0] = [105.0, 105.0, 105.0] / [1.0, 1.0, 1.0] = [105.0, 105.0, 105.0]
        >>> # input_x[1] = [315.0, 315.0, 315.0] / [3.0, 3.0, 3.0] = [105.0, 105.0, 105.0]
        >>> # step 2: [0, 1]
        >>> # input_x[0] = [105.0, 105.0, 105.0] / [5.0, 5.0, 5.0] = [21.0, 21.0, 21.0]
        >>> # input_x[1] = [105.0, 105.0, 105.0] / [7.0, 7.0, 7.0] = [15.0, 15.0, 15.0]
        >>> indices = Tensor(np.array([[0, 1], [0, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]],
        ...                            [[5.0, 5.0, 5.0], [7.0, 7.0, 7.0]]]), mindspore.float32)
        >>> scatter_div = ops.ScatterDiv()
        >>> output = scatter_div(input_x, indices, updates)
        >>> print(output)
        [[21. 21. 21.]
         [15. 15. 15.]]
    """


class ScatterNdAdd(_ScatterNdOp):
    r"""
    Applies sparse addition to individual values or slices in a tensor.

    Using given values to update tensor value through the add operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_add` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import ScatterNdAdd
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> use_locking = False
        >>> scatter_nd_add = ScatterNdAdd(use_locking)
        >>> output = scatter_nd_add(input_x, indices, updates)
        >>> print(output)
        [ 1. 10.  9.  4. 12.  6.  7. 17.]
        >>> input_x = Parameter(Tensor(np.zeros((4, 4, 4)), mindspore.int32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.int32)
        >>> use_locking = False
        >>> scatter_nd_add = ScatterNdAdd(use_locking)
        >>> output = scatter_nd_add(input_x, indices, updates)
        >>> print(output)
        [[[1 1 1 1]
          [2 2 2 2]
          [3 3 3 3]
          [4 4 4 4]]
         [[0 0 0 0]
          [0 0 0 0]
          [0 0 0 0]
          [0 0 0 0]]
         [[5 5 5 5]
          [6 6 6 6]
          [7 7 7 7]
          [8 8 8 8]]
         [[0 0 0 0]
          [0 0 0 0]
          [0 0 0 0]
          [0 0 0 0]]]
    """


class ScatterNdSub(Primitive):
    r"""
    Applies sparse subtraction to individual values or slices in a tensor.

    Using given values to update tensor value through the subtraction operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_sub` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import ScatterNdSub
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> use_locking = False
        >>> scatter_nd_sub = ScatterNdSub(use_locking)
        >>> output = scatter_nd_sub(input_x, indices, updates)
        >>> print(output)
        [ 1. -6. -3.  4. -2.  6.  7. -1.]
        >>> input_x = Parameter(Tensor(np.zeros((4, 4, 4)), mindspore.int32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.int32)
        >>> use_locking = False
        >>> scatter_nd_sub = ScatterNdSub(use_locking)
        >>> output = scatter_nd_sub(input_x, indices, updates)
        >>> print(output)
        [[[-1 -1 -1 -1]
          [-2 -2 -2 -2]
          [-3 -3 -3 -3]
          [-4 -4 -4 -4]]
         [[ 0  0  0  0]
          [ 0  0  0  0]
          [ 0  0  0  0]
          [ 0  0  0  0]]
         [[-5 -5 -5 -5]
          [-6 -6 -6 -6]
          [-7 -7 -7 -7]
          [-8 -8 -8 -8]]
         [[ 0  0  0  0]
          [ 0  0  0  0]
          [ 0  0  0  0]
          [ 0  0  0  0]]]
    """

    __mindspore_signature__ = (
        sig.make_sig('input_x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    @prim_attr_register
    def __init__(self, use_locking=False):
        """Initialize ScatterNdSub"""
        validator.check_value_type('use_locking', use_locking, [bool], self.name)
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class ScatterNdMul(_ScatterNdOp):
    r"""
    Applies sparse multiplication to individual values or slices in a tensor.

    Using given values to update parameter value through the multiplication operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_mul` for more detail.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import ScatterNdMul
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> scatter_nd_mul = ScatterNdMul()
        >>> output = scatter_nd_mul(input_x, indices, updates)
        >>> print(output)
        [ 1. 16. 18.  4. 35.  6.  7. 72.]
        >>> input_x = Parameter(Tensor(np.ones((4, 4, 4)), mindspore.int32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.int32)
        >>> scatter_nd_mul = ScatterNdMul()
        >>> output = scatter_nd_mul(input_x, indices, updates)
        >>> print(output)
        [[[1 1 1 1]
          [2 2 2 2]
          [3 3 3 3]
          [4 4 4 4]]
         [[1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]]
         [[5 5 5 5]
          [6 6 6 6]
          [7 7 7 7]
          [8 8 8 8]]
         [[1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]]]
    """


class ScatterNdDiv(_ScatterNdOp):
    r"""
    Applies sparse division to individual values or slices in a tensor.

    Using given values to update tensor value through the division operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_div` for more details.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> use_locking = False
        >>> scatter_nd_div = ops.ScatterNdDiv(use_locking)
        >>> output = scatter_nd_div(input_x, indices, updates)
        >>> print(output)
        [1.         0.25       0.5        4.         0.71428573 6.
         7.         0.8888889 ]
        >>> input_x = Parameter(Tensor(np.ones((4, 4, 4)), mindspore.float32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.float32)
        >>> use_locking = False
        >>> scatter_nd_div = ops.ScatterNdDiv(use_locking)
        >>> output = scatter_nd_div(input_x, indices, updates)
        >>> print(output)
        [[[1.         1.         1.         1.        ]
          [0.5        0.5        0.5        0.5       ]
          [0.33333334 0.33333334 0.33333334 0.33333334]
          [0.25       0.25       0.25       0.25      ]]
         [[1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]]
         [[0.2        0.2        0.2        0.2       ]
          [0.16666667 0.16666667 0.16666667 0.16666667]
          [0.14285715 0.14285715 0.14285715 0.14285715]
          [0.125      0.125      0.125      0.125     ]]
         [[1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]
          [1.         1.         1.         1.        ]]]
    """


class ScatterNdMax(_ScatterNdOp):
    r"""
    Applies sparse maximum to individual values or slices in a tensor.

    Using given values to update parameter value through the maximum operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_max` for more details.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> from mindspore.ops.operations.array_ops import ScatterNdMax
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> scatter_nd_max = ScatterNdMax()
        >>> output = scatter_nd_max(input_x, indices, updates)
        >>> print(output)
        [ 1. 8. 6.  4. 7.  6.  7. 9.]
        >>> input_x = Parameter(Tensor(np.ones((4, 4, 4)), mindspore.int32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.int32)
        >>> scatter_nd_max = ScatterNdMax()
        >>> output = scatter_nd_max(input_x, indices, updates)
        >>> print(output)
        [[[1 1 1 1]
          [2 2 2 2]
          [3 3 3 3]
          [4 4 4 4]]
         [[1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]]
         [[5 5 5 5]
          [6 6 6 6]
          [7 7 7 7]
          [8 8 8 8]]
         [[1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]
          [1 1 1 1]]]
    """


class ScatterNdMin(_ScatterNdOp):
    r"""
    Applies sparse minimum to individual values or slices in a tensor.

    Using given values to update tensor value through the minimum operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Refer to :func:`mindspore.ops.scatter_nd_min` for more details.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> input_x = Parameter(Tensor(np.ones(8) * 10, mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> use_locking = False
        >>> scatter_nd_min = ops.ScatterNdMin(use_locking)
        >>> output = scatter_nd_min(input_x, indices, updates)
        >>> print(output)
        [10.  8.  6. 10.  7. 10. 10.  9.]
        >>> input_x = Parameter(Tensor(np.ones((4, 4, 4)) * 10, mindspore.int32))
        >>> indices = Tensor(np.array([[0], [2]]), mindspore.int32)
        >>> updates = Tensor(np.array([[[1, 1, 1, 1], [2, 2, 2, 2], [3, 3, 3, 3], [4, 4, 4, 4]],
        ...                            [[5, 5, 5, 5], [6, 6, 6, 6], [7, 7, 7, 7], [8, 8, 8, 8]]]), mindspore.int32)
        >>> use_locking = False
        >>> scatter_nd_min = ops.ScatterNdMin(use_locking)
        >>> output = scatter_nd_min(input_x, indices, updates)
        >>> print(output)
        [[[ 1  1  1  1]
          [ 2  2  2  2]
          [ 3  3  3  3]
          [ 4  4  4  4]]
         [[10 10 10 10]
          [10 10 10 10]
          [10 10 10 10]
          [10 10 10 10]]
         [[ 5  5  5  5]
          [ 6  6  6  6]
          [ 7  7  7  7]
          [ 8  8  8  8]]
         [[10 10 10 10]
          [10 10 10 10]
          [10 10 10 10]
          [10 10 10 10]]]
    """


class ScatterNonAliasingAdd(Primitive):
    """
    Applies sparse addition to the input using individual values or slices.

    Using given values to update tensor value through the add operation, along with the input indices.
    This operation outputs the `input_x` after the update is done, which makes it convenient to use the updated value.

    Inputs of `input_x` and `updates` comply with the implicit type conversion rules to make the data types consistent.
    If they have different data types, the lower priority data type will be converted to
    the relatively highest priority data type.

    Inputs:
        - **input_x** (Parameter) - The target parameter. The data type must be float16, float32 or int32.
        - **indices** (Tensor) - The index to perform the addition operation whose data type must be mindspore.int32.
        - **updates** (Tensor) - The tensor that performs the addition operation with `input_x`,
          the data type is the same as `input_x`, the shape is `indices.shape[:-1] + x.shape[indices.shape[-1]:]`.

    Outputs:
        Parameter, the updated `input_x`.

    Raises:
        TypeError: If dtype of `indices` is not int32.
        TypeError: If dtype of `input_x` is not one of float16, float32, int32.
        ValueError: If the shape of `updates` is not equal to `indices.shape[:-1] + x.shape[indices.shape[-1]:]`.
        RuntimeError: If the data type of `input_x` and `updates` conversion of Parameter
                      is required when data type conversion of Parameter is not supported.

    Supported Platforms:
        ``Ascend``

    Examples:
        >>> input_x = Parameter(Tensor(np.array([1, 2, 3, 4, 5, 6, 7, 8]), mindspore.float32), name="x")
        >>> indices = Tensor(np.array([[2], [4], [1], [7]]), mindspore.int32)
        >>> updates = Tensor(np.array([6, 7, 8, 9]), mindspore.float32)
        >>> scatter_non_aliasing_add = ops.ScatterNonAliasingAdd()
        >>> output = scatter_non_aliasing_add(input_x, indices, updates)
        >>> print(output)
        [ 1. 10.  9.  4. 12.  6.  7. 17.]
    """

    __mindspore_signature__ = (
        sig.make_sig('input_x', sig.sig_rw.RW_WRITE, dtype=sig.sig_dtype.T),
        sig.make_sig('indices', dtype=sig.sig_dtype.T1),
        sig.make_sig('updates', dtype=sig.sig_dtype.T)
    )

    @prim_attr_register
    def __init__(self):
        """Initialize ScatterNonAliasingAdd"""
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])
        self.add_prim_attr('side_effect_mem', True)


class SpaceToDepth(PrimitiveWithInfer):
    r"""
    Rearrange blocks of spatial data into depth.

    The output tensor's `height` dimension is :math:`height / block\_size`.

    The output tensor's `weight` dimension is :math:`weight / block\_size`.

    The depth of output tensor is :math:`block\_size * block\_size * input\_depth`.

    The input tensor's height and width must be divisible by `block_size`.
    The data format is "NCHW".

    Args:
        block_size (int): The block size used to divide spatial data. It must be >= 2.

    Inputs:
        - **x** (Tensor) - The target tensor. The data type is Number. It must be a 4-D tensor.

    Outputs:
        Tensor, the same data type as `x`. It must be a 4-D tensor. Tensor of shape
        :math:`(N, ( C_{in} * \text{block_size} * 2), H_{in} / \text{block_size}, W_{in} / \text{block_size})`.

    Raises:
        TypeError: If `block_size` is not an int.
        ValueError: If `block_size` is less than 2.
        ValueError: If length of shape of `x` is not equal to 4.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.random.rand(1,3,2,2), mindspore.float32)
        >>> block_size = 2
        >>> space_to_depth = ops.SpaceToDepth(block_size)
        >>> output = space_to_depth(x)
        >>> print(output.shape)
        (1, 12, 1, 1)
    """

    @prim_attr_register
    def __init__(self, block_size):
        """Initialize SpaceToDepth"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])
        validator.check_value_type('block_size', block_size, [int], self.name)
        validator.check('block_size', block_size, self.name, 2, Rel.GE)
        self.block_size = block_size
        self.add_prim_attr("data_format", "NCHW")

    def infer_shape(self, x_shape):
        validator.check('x dimension', len(x_shape), self.name, 4, Rel.EQ)
        out_shape = copy.deepcopy(x_shape)
        for i in range(2):
            if out_shape[i + 2] % self.block_size != 0:
                msg_prefix = "2nd" if i + 2 == 2 else "3rd"
                raise ValueError(f"For '{self.name}', the shape of output with index {i + 2} must be divided "
                                 f"exactly by 'block_size', but got the {msg_prefix} dimension "
                                 f"of output: {out_shape[i + 2]} and "
                                 f"'block_size': {self.block_size}.")
            out_shape[i + 2] //= self.block_size

        out_shape[1] *= self.block_size * self.block_size
        return out_shape

    def infer_dtype(self, x_dtype):
        validator.check_subclass("x_dtype", x_dtype, mstype.tensor, self.name)
        return x_dtype


class DepthToSpace(Primitive):
    r"""
    Rearrange blocks of depth data into spatial dimensions.

    This is the reverse operation of SpaceToDepth.

    The depth of output tensor is :math:`input\_depth / (block\_size * block\_size)`.

    The output tensor's `height` dimension is :math:`height * block\_size`.

    The output tensor's `weight` dimension is :math:`weight * block\_size`.

    The input tensor's depth must be divisible by `block_size * block_size`.
    The data format is "NCHW".

    Args:
        block_size (int): The block size used to divide depth data. It must be >= 2.

    Inputs:
        - **x** (Tensor) - The target tensor. It must be a 4-D tensor with shape :math:`(N, C_{in}, H_{in}, W_{in})`.
          The data type is Number.

    Outputs:
        Tensor of shape :math:`(N, C_{in} / \text{block_size} ^ 2, H_{in} * \text{block_size},
        W_{in} * \text{block_size})`.

    Raises:
        TypeError: If `block_size` is not an int.
        ValueError: If `block_size` is less than 2.
        ValueError: If length of shape of `x` is not equal to 4.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.random.rand(1, 12, 1, 1), mindspore.float32)
        >>> block_size = 2
        >>> depth_to_space = ops.DepthToSpace(block_size)
        >>> output = depth_to_space(x)
        >>> print(output.shape)
        (1, 3, 2, 2)
    """

    @prim_attr_register
    def __init__(self, block_size):
        """Initialize DepthToSpace"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])
        validator.check_value_type('block_size', block_size, [int], self.name)
        validator.check('block_size', block_size, '', 2, Rel.GE, self.name)
        self.block_size = block_size
        self.add_prim_attr("data_format", "NCHW")


class SpaceToBatch(PrimitiveWithInfer):
    r"""
    SpaceToBatch is deprecated. Please use :class:`mindspore.ops.SpaceToBatchND` instead.
    Divides spatial dimensions into blocks and combines the block size with the original batch.

    This operation will divide spatial dimensions (H, W) into blocks with `block_size`, the output tensor's H and W
    dimension is the corresponding number of blocks after division. The output tensor's batch dimension is the
    product of the original batch and the square of block_size. Before division, the spatial dimensions
    of the input are zero padded according to paddings if necessary.

    Args:
        block_size (int): The block size of dividing blocks with value greater than or equal to 2.
        paddings (Union[tuple, list]): The padding values for H and W dimension, containing 2 subtraction lists.
            Each subtraction list contains 2 integer value. All values must be greater than 0.
            paddings[i] specifies the paddings for the spatial dimension i, which corresponds to the
            input dimension i+2. It is required that input_shape[i+2]+paddings[i][0]+paddings[i][1]
            is divisible by block_size.

    Inputs:
        - **input_x** (Tensor) - The input tensor. It must be a 4-D tensor. The data type is Number.

    Outputs:
        Tensor, the output tensor with the same data type as input. Assume input shape is :math:`(n, c, h, w)` with
        :math:`block\_size` and :math:`paddings`. The shape of the output tensor will be :math:`(n', c', h', w')`,
        where

        :math:`n' = n*(block\_size*block\_size)`

        :math:`c' = c`

        :math:`h' = (h+paddings[0][0]+paddings[0][1])//block\_size`

        :math:`w' = (w+paddings[1][0]+paddings[1][1])//block\_size`

    Raises:
        TypeError: If `block_size` is not an int.
        ValueError: If `block_size` is less than 2.

    Supported Platforms:
        Deprecated

    Examples:
        >>> block_size = 2
        >>> paddings = [[0, 0], [0, 0]]
        >>> space_to_batch = ops.SpaceToBatch(block_size, paddings)
        >>> input_x = Tensor(np.array([[[[1, 2], [3, 4]]]]), mindspore.float32)
        >>> output = space_to_batch(input_x)
        >>> print(output)
        [[[[1.]]]
         [[[2.]]]
         [[[3.]]]
         [[[4.]]]]
    """

    @prim_attr_register
    def __init__(self, block_size, paddings):
        """Initialize SpaceToBatch"""
        logger.warning("WARN_DEPRECATED: The usage of SpaceToBatch is deprecated."
                       " Please use SpaceToBatchND.")
        validator.check_value_type('block_size', block_size, [int], self.name)
        validator.check('block_size', block_size, self.name, 2, Rel.GE, self.name)
        self.block_size = block_size
        validator.check('paddings shape', np.array(paddings).shape, self.name, (2, 2), Rel.EQ, self.name)
        for elem in itertools.chain(*paddings):
            validator.check_non_negative_int(elem, 'paddings element', self.name)
            validator.check_value_type('paddings element', elem, [int], self.name)
        self.paddings = paddings

    def infer_dtype(self, x_dtype):
        validator.check_tensor_dtype_valid('input_x', x_dtype, mstype.number_type, self.name)
        return x_dtype

    def infer_shape(self, x_shape):
        validator.check_equal_int(len(x_shape), 4, 'rank of input_x', self.name)
        out_shape = copy.deepcopy(x_shape)
        for i in range(2):
            padded = out_shape[i + 2] + self.paddings[i][0] + self.paddings[i][1]
            if padded % self.block_size != 0:
                msg_ndim = "2nd" if i + 2 == 2 else "3rd"
                raise ValueError(f"For '{self.name}', the shape of the output tensor must be "
                                 f"divisible by 'block_size', but got the {msg_ndim} dimension of output: {padded} and "
                                 f"'block_size': {self.block_size}. Please check the official homepage "
                                 f"for more information about the output tensor.")
            out_shape[i + 2] = padded // self.block_size
        out_shape[0] *= self.block_size * self.block_size
        return out_shape


class BatchToSpace(PrimitiveWithInfer):
    r"""
    Divides batch dimension with blocks and interleaves these blocks back into spatial dimensions.

    This operation will divide batch dimension N into blocks with block_size, the output tensor's N dimension
    is the corresponding number of blocks after division. The output tensor's H, W dimension is product of
    original H, W dimension and block_size with given amount to crop from dimension, respectively.

    Args:
        block_size (int): The block size of division, has the value not less than 2.
        crops (Union[list(int), tuple(int)]): The crop value for H and W dimension, containing 2 subtraction lists.
            Each list contains 2 integers.
            All values must be not less than 0. crops[i] specifies the crop values for the spatial dimension i, which
            corresponds to the input dimension i+2. It is required that

            :math:`input\_shape[i+2]*block\_size >= crops[i][0]+crops[i][1]`

    Inputs:
        - **input_x** (Tensor) - The input tensor. It must be a 4-D tensor, dimension 0 must be divisible by
          product of `block_shape`. The data type is float16 or float32.

    Outputs:
        Tensor, the output tensor with the same type as input. Assume input shape is (n, c, h, w) with block_size
        and crops. The output shape will be (n', c', h', w'), where

        :math:`n' = n//(block\_size*block\_size)`

        :math:`c' = c`

        :math:`h' = h*block\_size-crops[0][0]-crops[0][1]`

        :math:`w' = w*block\_size-crops[1][0]-crops[1][1]`

    Raises:
        TypeError: If `block_size` or element of `crops` is not an int.
        TypeError: If `crops` is neither list nor tuple.
        ValueError: If `block_size` is less than 2.

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> block_size = 2
        >>> crops = [[0, 0], [0, 0]]
        >>> batch_to_space = ops.BatchToSpace(block_size, crops)
        >>> input_x = Tensor(np.array([[[[1]]], [[[2]]], [[[3]]], [[[4]]]]), mindspore.float32)
        >>> output = batch_to_space(input_x)
        >>> print(output)
        [[[[1.  2.]
           [3.  4.]]]]

    """

    @prim_attr_register
    def __init__(self, block_size, crops):
        """Initialize BatchToSpace"""
        logger.warning("WARN_DEPRECATED: The usage of BatchToSpace is deprecated."
                       " Please use BatchToSpaceND.")
        validator.check_value_type('block_size', block_size, [int], self.name)
        validator.check('block_size', block_size, '', 2, Rel.GE, self.name)
        self.block_size = block_size
        validator.check_value_type('crops type', crops, [list, tuple], self.name)
        validator.check('crops shape', np.array(crops).shape, self.name, (2, 2))
        for elem in itertools.chain(*crops):
            validator.check_non_negative_int(elem, 'crops element', self.name)
            validator.check_value_type('crops element', elem, [int], self.name)
        self.crops = crops

    def infer_dtype(self, x_dtype):
        validator.check_tensor_dtype_valid('input_x', x_dtype, mstype.number_type, self.name)
        return x_dtype

    def infer_shape(self, x_shape):
        validator.check('rank of input_x', len(x_shape), self.name, 4)
        out_shape = copy.deepcopy(x_shape)
        for i in range(2):
            x_block_prod = out_shape[i + 2] * self.block_size
            crops_sum = self.crops[i][0] + self.crops[i][1]
            validator.check("x block shape prod", x_block_prod, 'crops sum', crops_sum, Rel.GT, self.name)
            out_shape[i + 2] = x_block_prod - crops_sum
        block_size_prod = self.block_size * self.block_size
        if out_shape[0] % block_size_prod != 0:
            raise ValueError(f"For '{self.name}', the shape of output with index 0 must be divided exactly "
                             f"by block_size_prod, but got the shape of output: {out_shape} and "
                             f"block_size_prod: {block_size_prod}.")
        out_shape[0] = out_shape[0] // block_size_prod
        return out_shape


class SpaceToBatchND(PrimitiveWithInfer):
    r"""
    Divides spatial dimensions into blocks and combines the block size with the original batch.

    This operation will divide spatial dimensions into blocks with `block_shape`, and then the output tensor's spatial
    dimension is the corresponding number of blocks after division. The output tensor's batch dimension is the
    product of the original batch and all elements in `block_shape`.
    Before division, the spatial dimensions of the input are zero padded according to paddings if necessary.

    Args:
        block_shape (Union[list(int), tuple(int), int]): The block shape of dividing block
            with all elements greater than 1. If `block_shape` is a list or tuple,
            the length of `block_shape` is the number of spatial dimensions, called M later.
            If `block_shape` is an int, the block size of M dimensions are the same, equal to `block_shape`.
            In this case of Ascend, M must be 2.
        paddings (Union[tuple, list]): The padding values for spatial dimensions, containing M subtraction list.
            Each contains 2 integer values. All values must be greater than 0.
            `paddings[i]` specifies the paddings for the spatial dimension i,
            which corresponds to the input dimension i + offset.
            For each i, input_shape[i + offset]+paddings[i][0]+paddings[i][1]
            should be divisible by block_shape[i].

    Inputs:
        - **input_x** (Tensor) - The input tensor. The input tensor must be a 4-D tensor on Ascend.

    Outputs:
        Tensor, the output tensor with the same data type as the input.
        Assume the input shape is :math:`(n, c_1, ... c_k, w_1, ..., w_M)` with
        :math:`block\_shape` and :math:`paddings`.
        The shape of the output tensor will be :math:`(n', c_1, ... c_k, w'_1, ..., w'_M)`,
        where

        :math:`n' = n*(block\_shape[0]*...*block\_shape[M])`

        :math:`w'_i = (w_i+paddings[i][0]+paddings[i][1])//block\_shape[i]`

    Raises:
        TypeError: If `block_shape` is not one of list, tuple, int.
        TypeError: If `paddings` is neither list nor tuple.
        ValueError: If `block_shape` is not one dimensional when `block_shape` is a list or tuple.
        ValueError: If the length of `block_shape` is not 2 on Ascend.
        ValueError: If shape of `paddings` is not (2, M), where M is the length of `block_shape`.
        ValueError: If the element of `block_shape` is not an integer larger than 1.
        ValueError: If the element of `paddings` is not an integer larger than 0.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> block_shape = [2, 2]
        >>> paddings = [[0, 0], [0, 0]]
        >>> space_to_batch_nd = ops.SpaceToBatchND(block_shape, paddings)
        >>> input_x = Tensor(np.array([[[[1, 2], [3, 4]]]]), mindspore.float32)
        >>> output = space_to_batch_nd(input_x)
        >>> print(output)
        [[[[1.]]]
         [[[2.]]]
         [[[3.]]]
         [[[4.]]]]
    """

    @prim_attr_register
    def __init__(self, block_shape, paddings):
        """Initialize SpaceToBatchND"""
        validator.check_value_type('paddings type', paddings, [list, tuple], self.name)
        validator.check('paddings length', len(paddings), '', 2, Rel.EQ, self.name)

        if isinstance(block_shape, int):
            block_shape = (block_shape,) * np.array(paddings).shape[0]

        self.add_prim_attr("block_shape", block_shape)
        validator.check_value_type('block_shape type', block_shape, [list, tuple], self.name)
        validator.check('block_shape shape', len(np.array(block_shape).shape), '', 1, Rel.EQ, self.name)
        block_rank = len(block_shape)
        if context.get_context("device_target") == "Ascend":
            validator.check('block_shape length', block_rank, '', 2, Rel.EQ, self.name)
        for elem in block_shape:
            validator.check('block_shape element', elem, '', 1, Rel.GE, self.name)
            validator.check_value_type('block_shape element', elem, [int], self.name)
        self.block_shape = block_shape

        validator.check('paddings shape', np.array(paddings).shape, '', (block_rank, 2), Rel.EQ, self.name)
        for elem in itertools.chain(*paddings):
            validator.check_non_negative_int(elem, 'paddings element', self.name)
            validator.check_value_type('paddings element', elem, [int], self.name)
        self.paddings = paddings

    def infer_dtype(self, x_dtype):
        validator.check_tensor_dtype_valid('input_x', x_dtype, mstype.number_type, self.name)
        return x_dtype

    def infer_shape(self, x_shape):
        x_rank = len(x_shape)
        if context.get_context("device_target") == "Ascend":
            validator.check_equal_int(x_rank, 4, 'x_shape rank', self.name)
        out_shape = copy.deepcopy(x_shape)

        block_shape_prod = 1
        offset = len(x_shape) - len(self.block_shape)
        if offset <= 0:
            raise ValueError(f"For '{self.name}', the dim of the input should be larger than that of the blocks, "
                             f"but the shape of the inputs is {x_shape} "
                             f"while the shape of blocks is {self.block_shape}.")
        for i in range(len(self.block_shape)):
            padded = out_shape[i + offset] + self.paddings[i][0] + \
                     self.paddings[i][1]
            if padded % self.block_shape[i] != 0:
                raise ValueError(f"For '{self.name}', the padded must be divisible by 'block_shape', "
                                 f"where padded = input_x_shape[i + 2] + paddings[i][0] + paddings[i][1], "
                                 f"but got input_x_shape[{i + offset}]: {out_shape[i + offset]}, "
                                 f"paddings[{i}][0]: {self.paddings[i][0]} and paddings[{i}][1]: {self.paddings[i][1]}."
                                 f" Please check the official api documents for "
                                 f"more information about the output tensor.")
            out_shape[i + offset] = padded // self.block_shape[i]
            block_shape_prod = block_shape_prod * self.block_shape[i]
        out_shape[0] *= block_shape_prod
        return out_shape


class BatchToSpaceND(Primitive):
    r"""
    Divides batch dimension with blocks and interleaves these blocks back into spatial dimensions.

    Refer to :func:`mindspore.ops.batch_to_space_nd` for more detail.

    Supported Platforms:
        ``Ascend`` ``CPU``
    """

    @prim_attr_register
    def __init__(self, block_shape, crops):
        """Initialize BatchToSpaceND"""
        if isinstance(block_shape, int):
            block_shape = (block_shape,) * 2
        self.add_prim_attr("block_shape", block_shape)
        validator.check_value_type('block_shape type', block_shape, [list, tuple], self.name)
        validator.check('block_shape shape', len(np.array(block_shape).shape), '', 1, Rel.EQ, self.name)
        block_rank = len(block_shape)
        validator.check('block_shape length', block_rank, '', 2, Rel.EQ, self.name)
        for elem in block_shape:
            validator.check('block_shape element', elem, '', 1, Rel.GE, self.name)
            validator.check_value_type('block_shape element', elem, [int], self.name)
        self.block_shape = block_shape

        validator.check_value_type('crops type', crops, [list, tuple], self.name)
        validator.check('crops length', len(crops), '', 2, Rel.EQ, self.name)
        validator.check('crops shape', np.array(crops).shape, '', (block_rank, 2), Rel.EQ, self.name)
        for elem in itertools.chain(*crops):
            validator.check_non_negative_int(elem, 'crops element', self.name)
            validator.check_value_type('crops element', elem, [int], self.name)
        self.crops = crops


class BroadcastTo(Primitive):
    """
    Broadcasts input tensor to a given shape.

    Refer to :func:`mindspore.ops.broadcast_to` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``
    """

    @prim_attr_register
    def __init__(self, shape):
        """Initialize BroadcastTo"""
        validator.check_value_type("shape", shape, (tuple), self.name)
        validator.check("dimension of x", len(shape), "", 0, Rel.GT, self.name)
        for ix, i in enumerate(shape):
            validator.check_value_type('target shape index -> ' + str(ix), i, [int], self.name)
            validator.check("shape element", i, "shape element min limit", -1, Rel.GE, self.name)
        self.shape = shape


class Meshgrid(PrimitiveWithInfer):
    """
    Generates coordinate matrices from given coordinate tensors.

    Given N one-dimensional coordinate tensors, returns a tuple outputs of N N-D
    coordinate tensors for evaluating expressions on an N-D grid.

    Refer to :func:`mindspore.ops.meshgrid` for more detail.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> x = Tensor(np.array([1, 2, 3, 4]).astype(np.int32))
        >>> y = Tensor(np.array([5, 6, 7]).astype(np.int32))
        >>> z = Tensor(np.array([8, 9, 0, 1, 2]).astype(np.int32))
        >>> inputs = (x, y, z)
        >>> meshgrid = ops.Meshgrid(indexing='xy')
        >>> output = meshgrid(inputs)
        >>> print(output)
        (Tensor(shape=[3, 4, 5], dtype=Int32, value=
         [[[1, 1, 1, 1, 1],
           [2, 2, 2, 2, 2],
           [3, 3, 3, 3, 3],
           [4, 4, 4, 4, 4]],
          [[1, 1, 1, 1, 1],
           [2, 2, 2, 2, 2],
           [3, 3, 3, 3, 3],
           [4, 4, 4, 4, 4]],
          [[1, 1, 1, 1, 1],
           [2, 2, 2, 2, 2],
           [3, 3, 3, 3, 3],
           [4, 4, 4, 4, 4]]]),
         Tensor(shape=[3, 4, 5], dtype=Int32, value=
         [[[5, 5, 5, 5, 5],
           [5, 5, 5, 5, 5],
           [5, 5, 5, 5, 5],
           [5, 5, 5, 5, 5]],
          [[6, 6, 6, 6, 6],
           [6, 6, 6, 6, 6],
           [6, 6, 6, 6, 6],
           [6, 6, 6, 6, 6]],
          [[7, 7, 7, 7, 7],
           [7, 7, 7, 7, 7],
           [7, 7, 7, 7, 7],
           [7, 7, 7, 7, 7]]]),
         Tensor(shape=[3, 4, 5], dtype=Int32, value=
         [[[8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2]],
          [[8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2]],
          [[8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2],
           [8, 9, 0, 1, 2]]]))
    """

    @prim_attr_register
    def __init__(self, indexing="xy"):
        """Initialize Meshgrid."""
        validator.check_value_type("indexing", indexing, (str), self.name)
        validator.check_string(indexing.lower(), ["xy", "ij"], "indexing", self.name)
        self.indexing = indexing

    def infer_shape(self, x_shape):
        validator.check_value_type("shape", x_shape, [tuple], self.name)
        validator.check_int(len(x_shape), 2, Rel.GE, "len of input", self.name)
        n = len(x_shape)
        shape_0 = []
        for s in x_shape:
            validator.check_int(len(s), 1, Rel.EQ, 'each input rank', self.name)
            shape_0.append(s[0])
        if self.indexing == "xy":
            shape_0[0], shape_0[1] = shape_0[1], shape_0[0]
        out_shape = tuple(tuple(shape_0) for _ in range(n))
        return out_shape

    def infer_dtype(self, x_type):
        validator.check_subclass("input[0]", x_type[0], mstype.tensor, self.name)
        n = len(x_type)
        for i in range(1, n):
            validator.check('x_type[%d]' % i, x_type[i], 'base', x_type[0], Rel.EQ, self.name, TypeError)
        return x_type


class ReverseSequence(PrimitiveWithInfer):
    """
    Reverses variable length slices.

    Args:
        seq_dim (int): The dimension where reversal is performed. Required.
        batch_dim (int): The input is sliced in this dimension. Default: 0.

    Inputs:
        - **x** (Tensor) - The input to reverse, supporting all number types including bool.
        - **seq_lengths** (Tensor) - Must be a 1-D vector with int32 or int64 types.

    Outputs:
        Reversed tensor with the same shape and data type as input.

    Raises:
        TypeError: If `seq_dim` or `batch_dim` is not an int.
        ValueError: If value of `batch_dim` is equal to or greater than length of shape of `x` .

    Supported Platforms:
        ``Ascend`` ``GPU``

    Examples:
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.float32)
        >>> seq_lengths = Tensor(np.array([1, 2, 3]))
        >>> reverse_sequence = ops.ReverseSequence(seq_dim=1)
        >>> output = reverse_sequence(x, seq_lengths)
        >>> print(output)
        [[1. 2. 3.]
         [5. 4. 6.]
         [9. 8. 7.]]
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.float32)
        >>> seq_lengths = Tensor(np.array([1, 2, 3]))
        >>> reverse_sequence = ops.ReverseSequence(seq_dim=0, batch_dim=1)
        >>> output = reverse_sequence(x, seq_lengths)
        >>> print(output)
        [[1. 5. 9.]
         [4. 2. 6.]
         [7. 8. 3.]]
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.float32)
        >>> seq_lengths = Tensor(np.array([2, 2, 3]))
        >>> reverse_sequence = ops.ReverseSequence(seq_dim=1)
        >>> output = reverse_sequence(x, seq_lengths)
        >>> print(output)
        [[2. 1. 3.]
         [5. 4. 6.]
         [9. 8. 7.]]
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.float32)
        >>> seq_lengths = Tensor(np.array([3, 2, 3]))
        >>> reverse_sequence = ops.ReverseSequence(seq_dim=1)
        >>> output = reverse_sequence(x, seq_lengths)
        >>> print(output)
        [[3. 2. 1.]
         [5. 4. 6.]
         [9. 8. 7.]]
        >>> x = Tensor(np.array([[1, 2, 3, 4], [5, 6, 7, 8]]), mindspore.float32)
        >>> seq_lengths = Tensor(np.array([4, 4]))
        >>> reverse_sequence = ops.ReverseSequence(seq_dim=1)
        >>> output = reverse_sequence(x, seq_lengths)
        >>> print(output)
        [[4. 3. 2. 1.]
         [8. 7. 6. 5.]]
    """

    @prim_attr_register
    def __init__(self, seq_dim, batch_dim=0):
        """Initialize ReverseSequence"""
        self.init_prim_io_names(inputs=['x', 'seq_lengths'], outputs=['y'])
        validator.check_value_type("seq_dim", seq_dim, [int], self.name)
        self.seq_dim_ = seq_dim
        validator.check_value_type("batch_dim", batch_dim, [int], self.name)
        self.batch_dim_ = batch_dim

    def infer_shape(self, x, seq_lengths):
        validator.check_int_range(self.seq_dim_, 0, len(x), Rel.INC_LEFT, "seq_dim", self.name)
        validator.check_int_range(self.batch_dim_, 0, len(x), Rel.INC_LEFT, "batch_dim", self.name)
        validator.check("batch_dim", self.batch_dim_, "seq_dim", self.seq_dim_, Rel.NE, self.name)
        validator.check("seq_lengths rank", len(seq_lengths), "expected", 1, Rel.EQ, self.name)
        validator.check("seq_lengths vector size", seq_lengths[0],
                        "input size along batch_dim", x[self.batch_dim_], Rel.EQ, self.name)
        return x

    def infer_dtype(self, x, seq_lengths):
        validator.check_tensor_dtype_valid("x_dtype", x, mstype.number_type + (mstype.bool_,), self.name)
        validator.check_tensor_dtype_valid("seq_lengths_dtype", seq_lengths, [mstype.int32, mstype.int64], self.name)
        return x


class EditDistance(Primitive):
    r"""
    Computes the Levenshtein Edit Distance. It is used to measure the similarity of two sequences. The inputs are
    variable-length sequences provided by SparseTensors (hypothesis_indices, hypothesis_values, hypothesis_shape)
    and (truth_indices, truth_values, truth_shape).

    .. math::

        \operatorname{lev}_{a, b}(i, j)=\left\{\begin{array}{ll}
        \max (i, j)  \qquad \qquad \qquad \qquad \qquad \quad \  \text { if } \min (i, j)=0 \\
        \min \left\{\begin{array}{ll}
        \operatorname{lev}_{a, b}(i-1, j)+1 & \\
        \operatorname{lev}_{a, b}(i, j-1)+1 & \text { otherwise. } \\
        \operatorname{lev}_{a, b}(i-1, j-1)+1_{\left(a_{i} \neq b_{j}\right)}
        \end{array}\right. &
        \end{array}\right.

    Where the :math:`a` indicates the hypothesis and the :math:`a` indicates the truth. For ease of understanding,
    i and j here in may be considered as lengths of a and b.

    Args:
        normalize (bool): If true, edit distances are normalized by length of truth. Default: True.

    Inputs:
        - **hypothesis_indices** (Tensor) - The indices of the hypothesis list SparseTensor. With int64 data type.
          The shape of tensor is :math:`(N, R)`.
        - **hypothesis_values** (Tensor) - The values of the hypothesis list SparseTensor.
          Must be 1-D vector with length of N.
        - **hypothesis_shape** (Tensor) - The shape of the hypothesis list SparseTensor.
          Must be R-length vector with int64 data type. Only constant value is allowed.
        - **truth_indices** (Tensor) - The indices of the truth list SparseTensor. With int64 data type.
          The shape of tensor is :math:`(M, R)`.
        - **truth_values** (Tensor) - The values of the truth list SparseTensor. Must be 1-D vector with length of M.
        - **truth_shape** (Tensor) - The shape of the truth list SparseTensor.
          Must be R-length vector with int64 data type. Only constant value is allowed.

    Outputs:
        Tensor, a dense tensor with rank `R-1` and float32 data type.

    Raises:
        TypeError: If `normalize` is not a bool.

    Supported Platforms:
        ``Ascend``

    Examples:
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> import mindspore.nn as nn
        >>> import mindspore.ops as ops
        >>> class EditDistance(nn.Cell):
        ...     def __init__(self, hypothesis_shape, truth_shape, normalize=True):
        ...         super(EditDistance, self).__init__()
        ...         self.edit_distance = ops.EditDistance(normalize)
        ...         self.hypothesis_shape = hypothesis_shape
        ...         self.truth_shape = truth_shape
        ...
        ...     def construct(self, hypothesis_indices, hypothesis_values, truth_indices, truth_values):
        ...         return self.edit_distance(hypothesis_indices, hypothesis_values, self.hypothesis_shape,
        ...                                   truth_indices, truth_values, self.truth_shape)
        ...
        >>> hypothesis_indices = Tensor(np.array([[0, 0, 0], [1, 0, 1], [1, 1, 1]]).astype(np.int64))
        >>> hypothesis_values = Tensor(np.array([1, 2, 3]).astype(np.float32))
        >>> hypothesis_shape = Tensor(np.array([1, 1, 2]).astype(np.int64))
        >>> truth_indices = Tensor(np.array([[0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 0, 1]]).astype(np.int64))
        >>> truth_values = Tensor(np.array([1, 3, 2, 1]).astype(np.float32))
        >>> truth_shape = Tensor(np.array([2, 2, 2]).astype(np.int64))
        >>> edit_distance = EditDistance(hypothesis_shape, truth_shape)
        >>> output = edit_distance(hypothesis_indices, hypothesis_values, truth_indices, truth_values)
        >>> print(output)
        [[1. 1.]
         [1. 1.]]
    """

    @prim_attr_register
    def __init__(self, normalize=True):
        """Initialize EditDistance"""
        self.normalize = validator.check_value_type("normalize", normalize, [bool], self.name)
        self.set_const_input_indexes([2, 5])


class TransShape(PrimitiveWithInfer):
    """
    Transforms the shape of input tensor to target shape.

    Inputs:
        - **input_x** (Tensor) - A input tensor.
        - **out_shape** (tuple[int]) - The shape of output data.

    Outputs:
        Tensor, a tensor whose data type is same as 'input_x', and the shape is the same as the `out_shape`.
    """

    @prim_attr_register
    def __init__(self):
        """Initialize TransShape."""
        self.__setattr_flag__ = True

    def __infer__(self, x, shape):
        shp = shape['value']
        dtype = x['dtype']
        validator.check_tensor_dtype_valid('x', dtype, mstype.number_type + (mstype.bool_,), self.name)
        self.add_prim_attr('out_shape', tuple(shp))
        return {'shape': shp,
                'dtype': dtype,
                'value': None}


class Sort(Primitive):
    """
    Sorts the elements of the input tensor along a given dimension in ascending order by value.

    Args:
        axis (int): The dimension to sort along. Default: -1.
        descending (bool): Controls the sorting order. If descending is True then the elements
            are sorted in descending order by value. Default: False.

    .. warning::
        Currently, only the data type of Float16 is supported. If use Float32, it may cause loss
        of accuracy.

    Inputs:
        - **x** (Tensor) - The input to sort, with float16 or float32 data type.
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.

    Outputs:
        - **y1** (Tensor) - A tensor whose values are the sorted values, with the same shape and data type as input.
        - **y2** (Tensor) - The indices of the elements in the original input tensor. Data type is int32.

    Raises:
        TypeError: If `axis` is not an int.
        TypeError: If `descending` is not a bool.
        TypeError: If dtype of `x` is neither float16 nor float32.
        ValueError: If `axis` is not in range of [-len(x.shape), len(x.shape)).

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[8, 2, 1], [5, 9, 3], [4, 6, 7]]), mindspore.float16)
        >>> sort = ops.Sort()
        >>> output = sort(x)
        >>> # The output below is based on the Ascend platform.
        >>> print(output)
        (Tensor(shape=[3, 3], dtype=Float16, value=
        [[ 1.0000e+00,  2.0000e+00,  8.0000e+00],
         [ 3.0000e+00,  5.0000e+00,  9.0000e+00],
         [ 4.0000e+00,  6.0000e+00,  7.0000e+00]]), Tensor(shape=[3, 3], dtype=Int32, value=
        [[2, 1, 0],
         [2, 0, 1],
         [0, 1, 2]]))
    """

    @prim_attr_register
    def __init__(self, axis=-1, descending=False):
        """Initialize Sort"""
        self.axis = validator.check_value_type("axis", axis, [int], self.name)
        self.descending = validator.check_value_type("descending", descending, [bool], self.name)
        self.init_prim_io_names(inputs=['x'], outputs=['y1', 'y2'])


class EmbeddingLookup(PrimitiveWithCheck):
    """
    Returns a slice of input tensor based on the specified indices.

    This Primitive has the similar functionality as GatherV2 operating on `axis = 0`, but has one more inputs:
    `offset`.

    Inputs:
        - **input_params** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
          This represents a Tensor slice, instead of the entire Tensor. Currently, the dimension is restricted to be 2.
        - **input_indices** (Tensor) - The shape of tensor is :math:`(y_1, y_2, ..., y_S)`.
          Specifies the indices of elements of the original Tensor. Values can be out of range of `input_params`,
          and the exceeding part will be filled with 0 in the output. Values do not support negative and the result
          is undefined if values are negative. The data type should be int32 or int64.
        - **offset** (int) - Specifies the offset value of this `input_params` slice. Thus the real indices
          are equal to `input_indices` minus `offset`.

    Outputs:
        Tensor, the shape of tensor is :math:`(z_1, z_2, ..., z_N)`. The data type is the same with `input_params`.

    Raises:
        TypeError: If dtype of `input_indices` is not int.
        ValueError: If length of shape of `input_params` is greater than 2.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> input_params = Tensor(np.array([[8, 9], [10, 11], [12, 13], [14, 15]]), mindspore.float32)
        >>> input_indices = Tensor(np.array([[5, 2], [8, 5]]), mindspore.int32)
        >>> offset = 4
        >>> output = ops.EmbeddingLookup()(input_params, input_indices, offset)
        >>> print(output)
        [[[10. 11.]
          [ 0.  0.]]
         [[ 0.  0.]
          [10. 11.]]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize EmbeddingLookup."""
        self.__setattr_flag__ = True
        self.init_prim_io_names(inputs=['params', 'indices', 'offset'],
                                outputs=['output'])
        self.add_prim_attr('bprop_return_sparse', True)

    def __check__(self, params, indices, offset):
        validator.check_subclass("params", params['dtype'], mstype.tensor, self.name)
        validator.check_tensor_dtype_valid("indices", indices['dtype'], mstype.int_type, self.name)
        validator.check_subclass("offset", offset['dtype'], mstype.int_, self.name)
        indices_shp = indices['shape']
        if not indices_shp:
            raise ValueError(f"For '{self.name}', the dimension of 'input_indices' should not "
                             f"be zero, but got {len(indices_shp)}.")
        params_shp = params['shape']
        if len(params_shp) > 2:
            raise ValueError(f"For '{self.name}', the dimension of 'input_params' must <= 2, "
                             f"but got {len(params_shp)}.")


class GatherD(Primitive):
    """
    Gathers elements along an axis specified by dim.

    Refer to :func:`mindspore.ops.gather_elements` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[1, 2], [3, 4]]), mindspore.int32)
        >>> index = Tensor(np.array([[0, 0], [1, 0]]), mindspore.int32)
        >>> dim = 1
        >>> output = ops.GatherD()(x, dim, index)
        >>> print(output)
        [[1 1]
         [4 3]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize GatherD"""
        self.init_prim_io_names(inputs=['x', 'dim', 'index'], outputs=['output'])
        self.set_const_input_indexes([1])


class Identity(Primitive):
    """
    Returns a Tensor with the same shape and contents as input.

    Inputs:
        - **x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`. The data type is Number.

    Outputs:
        Tensor, the shape of tensor and the data type are the same as `input_x`, :math:`(x_1, x_2, ..., x_R)`.

    Raises:
        TypeError: If `x` is not a Tensor.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> x = Tensor(np.array([1, 2, 3, 4]), mindspore.int64)
        >>> output = ops.Identity()(x)
        >>> print(output)
        [1 2 3 4]
    """

    @prim_attr_register
    def __init__(self):
        pass


class IdentityN(Primitive):
    """
    Return a tuple of tensors with the same shapes and contents as the input.

    This op can be used to override the gradient for complicated functions. For
    example, suppose y = f(x) and we wish to apply a custom function g for backprop
    such that dx = g(dy).

    Inputs:
        - **x** (Tensors) - tuple(Tensor) or List(Tensor). The data type is RealNumber.

    Outputs:
        Tensors - tuple(Tensor), the shape of tensor and the data type are the same as input `x`.

    Raises:
        TypeError: If `x` is not tuple(Tensor) or List(Tensor).
        TypeError: If input `x` type is not RealNumber.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = [Tensor(np.array([1, 2, 3, 4]), mstype.int64), Tensor(np.array([4, 3, 1, 1]), mstype.int64)]
        >>> output = ops.IdentityN()(x)
        >>> print(np.allclose(output[0].asnumpy(), x[0].asnumpy()))
        True
        >>> print(np.allclose(output[1].asnumpy(), x[1].asnumpy()))
        True
        >>> print(output)
        (Tensor(shape=[4], dtype=Int64, value= [1, 2, 3, 4]), Tensor(shape=[4], dtype=Int64, value= [4, 3, 1, 1]))
    """

    @prim_attr_register
    def __init__(self):
        """Initialize IdentityN"""
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class Range(PrimitiveWithCheck):
    r"""
    Creates a sequence of numbers that begins at `start` and extends by increments of
    `delta` up to but not including `limit`.

    Refer to :func:`mindspore.ops.range` for more detailed.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> start = Tensor(0, mstype.int32)
        >>> limit = Tensor(10, mstype.int32)
        >>> delta = Tensor(4, mstype.int32)
        >>> output = ops.Range()(start, limit, delta)
        >>> print(output)
        [0 4 8]
    """

    @prim_attr_register
    def __init__(self, maxlen=1000000):
        self.init_prim_io_names(inputs=['start', 'limit', 'delta'], outputs=['output'])
        validator.check_value_type("maxlen", maxlen, [int], self.name)
        validator.check_positive_int(maxlen, "maxlen", self.name)
        self.maxlen = maxlen
        self.add_prim_attr('maxlen', maxlen)

    def check_shape(self, start_shape, limit_shape, delta_shape):
        validator.check("start_shape", len(start_shape), "", 0, Rel.EQ, self.name)
        validator.check("limit_shape", len(limit_shape), "", 0, Rel.EQ, self.name)
        validator.check("delta_shape", len(delta_shape), "", 0, Rel.EQ, self.name)

    def check_dtype(self, start_dtype, limit_dtype, delta_dtype):
        valid_dtypes = [mstype.int32, mstype.float32]
        inputs = {"start": start_dtype, "limit": limit_dtype, "delta": delta_dtype}
        validator.check_tensors_dtypes_same_and_valid(inputs, valid_dtypes, self.name)

    def infer_value(self, start_value, limit_value, delat_value):
        """Infer the value of input for Range."""
        if start_value is not None and limit_value is not None and delat_value is not None:
            start = np.asscalar(start_value.asnumpy())
            limit = np.asscalar(limit_value.asnumpy())
            delat = np.asscalar(delat_value.asnumpy())
            return Tensor(np.arange(start, limit, delat), dtype=start_value.dtype)
        return None


class RangeV2(Primitive):
    """
    Creates a sequence of numbers that begins at `start`, ends at `limit` but not including `limit`
    and extends by increments of `delta`.

    The types of all 3 inputs must be the same. The type of the resulting tensor is
    the same as the type of the inputs.

    Args:
        maxlen (int): Memory that can fit `maxlen` many elements
            will be allocated for the output. Optional, must be positive, defaults to 1000000.
            If the output has more than `maxlen` elements, a `ValueError` will occur.

    Inputs:
        - **start** (Tensor) - A scalar Tensor. The first number in the sequence. Must have
          type: int32 or float32 or int64 or float64
        - **limit** (Tensor) - A scalar Tensor. Upper limit of the sequence, exclusive. Must
          have type: int32 or float32 or int64 or float64
        - **delta** (Tensor) - A scalar Tensor. Number that increments `start`. Must have
          type: int32 or float32 or int64 or float64

    Outputs:
       A 1D Tensor, with the same type as the inputs.

    Raises:
        TypeError: If datatype of `start`, `limit` and `delta` not supported.
        TypeError: If datatype of `start`, `limit` and `delta` not same.
        TypeError: If attr `max_len` is not int.
        TypeError: If `start` or `limit` or `delta` is not scalar Tensor.
        ValueError: If value of `max_len` is negative.
        ValueError: If `delta` >= 0 when `start` > `limit`.
        ValueError: If `delta` <= 0 when `start` < `limit`.
        ValueError: If the output has more than `maxlen` elements

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> start = Tensor(0, mstype.int32)
        >>> limit = Tensor(10, mstype.int32)
        >>> delta = Tensor(4, mstype.int32)
        >>> output = ops.RangeV2()(start, limit, delta)
        >>> print(output)
        [0 4 8]
    """

    @prim_attr_register
    def __init__(self, maxlen=1000000):
        """Initialize RangeV2"""
        self.init_prim_io_names(inputs=['start', 'limit', 'delta'], outputs=['output'])
        validator.check_value_type("maxlen", maxlen, [int], self.name)
        validator.check_positive_int(maxlen, "maxlen", self.name)


class MaskedFill(Primitive):
    """
    Fills elements with value where mask is True.

    Refer to :func:`mindspore.ops.masked_fill` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input = Tensor(np.array([1., 2., 3., 4.]), mindspore.float32)
        >>> mask = Tensor(np.array([True, True, False, True]), mindspore.bool_)
        >>> output = ops.MaskedFill()(input, mask, 0.5)
        >>> print(output)
        [0.5 0.5 3.  0.5]
    """

    @prim_attr_register
    def __init__(self):
        self.init_prim_io_names(inputs=['input', 'mask', 'value'], outputs=['output'])


class MaskedSelect(PrimitiveWithCheck):
    """
    Returns a new 1-D Tensor which indexes the `x` tensor according to the boolean `mask`.
    The shapes of the `mask` tensor and the `x` tensor don't need to match, but they must be broadcastable.

    Inputs:
        - **x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
        - **mask** (Tensor[bool]) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.

    Outputs:
        A 1-D Tensor, with the same type as x.

    Raises:
        TypeError: If `x` or `mask` is not a Tensor.
        TypeError: If dtype of `mask` is not bool.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([1, 2, 3, 4]), mindspore.int32)
        >>> mask = Tensor(np.array([1, 0, 1, 0]), mindspore.bool_)
        >>> output = ops.MaskedSelect()(x, mask)
        >>> print(output)
        [1 3]
    """

    @prim_attr_register
    def __init__(self):
        self.init_prim_io_names(inputs=['x', 'mask'], outputs=['output'])

    def check_shape(self, x_shape, mask_shape):
        get_broadcast_shape(x_shape, mask_shape, self.name, arg_name1="x", arg_name2="mask")

    def check_dtype(self, x_dtype, mask_dtype):
        validator.check_tensor_dtype_valid('mask', mask_dtype, [mstype.bool_], self.name)
        validator.check_tensor_dtype_valid('x', x_dtype, (mstype.bool_,) + mstype.number_type, self.name)


class SearchSorted(PrimitiveWithInfer):
    """
    Find the indices from the innermost dimension of `sequence` such that the order of the innermost dimension
    within `sequence` would be preserved when the corresponding values in `values` were inserted before the indices.

    Args:
        out_int32 (bool): Output datatype. Optional. If True, the output datatype will be int32;
                          if False, the output datatype will be int64. Default is False.
        right (bool): Search Strategy. Optional. If True, return the last suitable index found.
                      If False, return the first such index. Default is False.

    Inputs:
        - **sequence** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R-1, x_R)` or `(x_1)`.
                                  It must contain monitonically increasing sequence on the innermost dimension.
        - **values** (Tensor) - The shape of tensor is : math:`(x_1, x_2, ..., x_R-1, x_S)`.

    Outputs:
        Tensor containing the indices from the innermost dimension of the input sequence such that,
        if insert the corresponding value in the values tensor, the order of the tensor sequence would be preserved.
        The shape of tensor is :math:`(x_1, x_2, ..., x_R-1, x_S)`,
        whose datatype is int32 if out_int32 is True, otherwise int64, and shape is the same as the shape of values.

    Raises:
        ValueError: If `sequence` and `values` do not have proper shapes.

    Supported Platforms:
        ``CPU``

    Examples:
        >>> sequence = Tensor(np.array([[0, 1, 3, 5, 7], [2, 4, 6, 8, 10]]), mindspore.float32)
        >>> values = Tensor(np.array([[3, 6, 9], [3, 6, 9]]), mindspore.float32)
        >>> output = ops.SearchSorted()(sequence, values)
        >>> print(output)
        [[2 4 5]
         [1 2 4]]
    """

    @prim_attr_register
    def __init__(self, out_int32=False, right=False):
        """Initialize SearchSorted"""
        self.out_int32 = validator.check_value_type("out_int32", out_int32, [bool], self.name)
        self.right = validator.check_value_type("right", right, [bool], self.name)
        self.init_prim_io_names(inputs=['sequence', 'values'], outputs=['positions'])

    def infer_shape(self, sequence_shape, values_shape):
        if len(sequence_shape) != 1 and sequence_shape[:-1] != values_shape[:-1]:
            raise ValueError(f"For '{self.name}', the 'sequence' must be 1 dimensional or "
                             f"all dimensions except the last dimension of 'sequence' "
                             f"must be the same as all dimensions except the last dimension of 'values'. "
                             f"but got shape of 'sequence': {sequence_shape} "
                             f"and shape of 'values': {values_shape}.")
        return values_shape

    def infer_dtype(self, sequence_dtype, values_dtype):
        args = {"sequence_dtype": sequence_dtype, "values_dtype": values_dtype}
        validator.check_tensors_dtypes_same_and_valid(args, mstype.number_type, self.name)
        return mstype.tensor_type(mstype.int32) if self.out_int32 else mstype.tensor_type(mstype.int64)


class _TensorScatterOp(PrimitiveWithInfer):
    """
    Defines TensorScatter Base Operators
    """

    def infer_shape(self, input_x_shape, indices_shape, updates_shape):
        if len(indices_shape) < 2:
            raise ValueError(f"For '{self.name}', the dimension of 'indices' cannot be less than 2,"
                             f" but got {len(indices_shape)}.")

        if indices_shape[-1] > len(input_x_shape):
            raise ValueError(f"For '{self.name}', the last dimension of 'indices' must be less than or equal to "
                             f"the dimension of 'input_x', but got the "
                             f"last dimension of 'indices': {indices_shape[-1]} and the dimension of 'input_x': "
                             f"{len(input_x_shape)}.")

        updates_shape_check = indices_shape[:-1] + input_x_shape[indices_shape[-1]:]
        if self._check_shape(updates_shape_check, updates_shape) is False:
            raise ValueError(f"For '{self.name}', the shape of 'update' must be equal to updates_shape_check, "
                             f"where updates_shape_check = indices_shape[:-1] + input_x_shape[indices_shape[-1]:] "
                             f"but got the shape of 'update': {updates_shape}, "
                             f"updates_shape_check: {updates_shape_check}, indices_shape: {indices_shape} and "
                             f"input_x_shape: {input_x_shape}. Please check input_x_shape and indices_shape.")

        return input_x_shape

    def infer_dtype(self, input_x_dtype, indices_dtype, updates_dtype):
        validator.check_tensor_dtype_valid('indices', indices_dtype, [mstype.int32, mstype.int64], self.name)
        args = {"input_x": input_x_dtype, "updates": updates_dtype}
        validator.check_tensors_dtypes_same_and_valid(args, mstype.number_type, self.name)
        return input_x_dtype

    def _check_shape(self, expect, real):
        """check shape"""
        if -2 in expect or -2 in real:
            return True
        if len(expect) != len(real):
            return False
        for a, b in zip(expect, real):
            if a == -1 or b == -1:
                continue
            if a != b:
                return False
        return True


class TensorScatterUpdate(_TensorScatterOp):
    """
    Creates a new tensor by updating the positions in `input_x` indicated by
    `indices`, with values from `update`. This operation is almost equivalent to using
    ScatterNd, except that the updates are applied on `input_x` instead of a zero tensor.

    `indices` must have rank at least 2, the last axis is the depth of each index
    vectors. For each index vector, there must be a corresponding value in `update`. If
    the depth of each index tensor matches the rank of `input_x`, then each index
    vector corresponds to a scalar in `input_x` and each `update` updates a scalar. If
    the depth of each index tensor is less than the rank of `input_x`, then each index
    vector corresponds to a slice in `input_x`, and each `update` updates a slice.

    The order in which updates are applied is nondeterministic, meaning that if there
    are multiple index vectors in `indices` that correspond to the same position, the
    value of that position in the output will be nondeterministic.

    Inputs:
        - **input_x** (Tensor) - The target tensor. The dimension of input_x must be no less than indices.shape[-1].
          The shape is :math:`(N,*)` where :math:`*` means,any number of additional dimensions.
          The data type is Number.
        - **indices** (Tensor) - The index of input tensor whose data type is int32 or int64.
          The rank must be at least 2.
        - **update** (Tensor) - The tensor to update the input tensor, has the same type as input, and

          :math:`update.shape = indices.shape[:-1]+input_x.shape[indices.shape[-1]:]`

    Outputs:
        Tensor, has the same shape and type as `input_x`.

    Raises:
        TypeError: If dtype of `indices` is neither int32 nor int64.
        ValueError: If length of shape of `input_x` is less than the last dimension of shape of `indices`.
        ValueError: If the value of `input_x` are not match with input `indices`.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [1, 1]]), mindspore.int32)
        >>> update = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> op = ops.TensorScatterUpdate()
        >>> output = op(input_x, indices, update)
        >>> print(output)
        [[ 1.   0.3  3.6]
         [ 0.4  2.2 -3.2]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterUpdate")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])

    def _infer_specified_value(self, input_x_value, indices_value, updates_value):
        """Calculate min/max value for output of TensorScatterUpdate op"""
        if isinstance(input_x_value, tuple):
            input_x_value = list(input_x_value)
        if isinstance(input_x_value, (Tensor, Tensor_)):
            input_x_value = input_x_value.asnumpy()
        if indices_value is None or updates_value is None:
            return None
        indices = indices_value.asnumpy()
        input_x = np.array(input_x_value)
        updates = np.array(updates_value)
        for i, indice in enumerate(indices):
            input_x[indice] = updates[i]
        output = tuple(input_x.tolist())
        return output

    def _infer_min_value(self, input_x_value, indices_value, updates_value):
        return self._infer_specified_value(input_x_value, indices_value, updates_value)

    def _infer_max_value(self, input_x_value, indices_value, updates_value):
        return self._infer_specified_value(input_x_value, indices_value, updates_value)

    def infer_dtype(self, input_x_dtype, indices_dtype, updates_dtype):
        validator.check_tensor_dtype_valid('indices', indices_dtype, [mstype.int32, mstype.int64], self.name)
        args = {"input_x": input_x_dtype, "updates": updates_dtype}
        validator.check_tensors_dtypes_same_and_valid(args, (mstype.bool_,) + mstype.number_type, self.name)
        return input_x_dtype


class TensorScatterMax(_TensorScatterOp):
    """
    By comparing the value at the position indicated by `indices` in `x` with the value in the `updates`,
    the value at the index will eventually be equal to the largest one to create a new tensor.

    Refer to :func:`mindspore.ops.tensor_scatter_max` for more detail.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterMax()
        >>> # 5, Perform the max operation for the first time:
        >>> #      first_input_x = Max(input_x[0][0], updates[0]) = [[2.2, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the max operation for the second time:
        >>> #      second_input_x = Max(input_x[0][0], updates[0]) = [[2.2, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[ 2.2  0.3  3.6]
         [ 0.4  0.5 -3.2]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterMax")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class TensorScatterMin(_TensorScatterOp):
    """
    By comparing the value at the position indicated by `indices` in `input_x` with the value in the `updates`,
    the value at the index will eventually be equal to the smallest one to create a new tensor.

    Refer to :func:`mindspore.ops.tensor_scatter_min` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterMin()
        >>> # 5, Perform the min operation for the first time:
        >>> #      first_input_x = Min(input_x[0][0], updates[0]) = [[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the min operation for the second time:
        >>> #      second_input_x = Min(input_x[0][0], updates[1]) = [[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[ -0.1  0.3  3.6]
         [ 0.4  0.5 -3.2]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterMin")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class TensorScatterSub(_TensorScatterOp):
    """
    Creates a new tensor by subtracting the values from the positions in `input_x` indicated by
    `indices`, with values from `updates`. When multiple values are provided for the same
    index, the result of the update will be to subtract these values respectively. This operation is almost
    equivalent to using :class:`mindspore.ops.ScatterNdSub` , except that the updates are applied on output `Tensor`
    instead of input `Parameter`.
    Refer to :func:`mindspore.ops.tensor_scatter_sub` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterSub()
        >>> # 5, Perform the subtract operation for the first time:
        >>> #      first_input_x = input_x[0][0] - updates[0] = [[-1.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the subtract operation for the second time:
        >>> #      second_input_x = input_x[0][0] - updates[1] = [[-3.3, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[-3.3000002  0.3        3.6      ]
         [ 0.4        0.5       -3.2      ]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterSub")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class TensorScatterAdd(_TensorScatterOp):
    """
    Creates a new tensor by adding the values from the positions in `input_x` indicated by
    `indices`, with values from `updates`. When multiple values are given for the same
    index, the updated result will be the sum of all values. This operation is almost
    equivalent to using ScatterNdAdd, except that the updates are applied on output `Tensor`
    instead of input `Parameter`.

    Refer to :func:`mindspore.ops.tensor_scatter_add` for more detail.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterAdd()
        >>> # 5, Perform the addition operation for the first time:
        >>> #      first_input_x = input_x[0][0] + updates[0] = [[0.9, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the addition operation for the second time:
        >>> #      second_input_x = input_x[0][0] + updates[1] = [[3.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[ 3.1  0.3  3.6]
         [ 0.4  0.5 -3.2]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterAdd")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class TensorScatterMul(_TensorScatterOp):
    """
    Creates a new tensor by multiplying the values from the positions in `input_x` indicated by
    `indices`, with values from `updates`. When multiple values are provided for the same
    index, the result of the update will be to multiply these values respectively. This operation is almost
    equivalent to using ScatterNdSub, except that the updates are applied on output `Tensor`
    instead of input `Parameter`.

    Refer to :func:`mindspore.ops.tensor_scatter_mul` for more detail.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.2]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterMul()
        >>> # 5, Perform the multiply operation for the first time:
        >>> #      first_input_x = input_x[0][0] * updates[0] = [[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the multiply operation for the second time:
        >>> #      second_input_x = input_x[0][0] * updates[1] = [[-0.22, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[-0.22  0.3   3.6  ]
         [ 0.4   0.5   -3.2 ]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterMul")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class TensorScatterDiv(_TensorScatterOp):
    """
    Creates a new tensor by dividing the values from the positions in `input_x` indicated by
    `indices`, with values from `updates`. When divided values are provided for the same
    index, the result of the update will be to divided these values respectively. Except that
    the updates are applied on output `Tensor` instead of input `Parameter`.

    Refer to :func:`mindspore.ops.tensor_scatter_div` for more detail.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> input_x = Tensor(np.array([[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]), mindspore.float32)
        >>> indices = Tensor(np.array([[0, 0], [0, 0]]), mindspore.int32)
        >>> updates = Tensor(np.array([1.0, 2.0]), mindspore.float32)
        >>> # Next, demonstrate the approximate operation process of this operator:
        >>> # 1, indices[0] = [0, 0], indices[1] = [0, 0]
        >>> # 2, And input_x[0, 0] = -0.1
        >>> # 3, So input_x[indices] = [-0.1, -0.1]
        >>> # 4, Satisfy the above formula: input_x[indices].shape=(2) == updates.shape=(2)
        >>> op = ops.TensorScatterDiv()
        >>> # 5, Perform the division operation for the first time:
        >>> #      first_input_x = input_x[0][0] / updates[0] = [[-0.1, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> # 6, Perform the division operation for the second time:
        >>> #      second_input_x = input_x[0][0] * updates[1] = [[-0.05, 0.3, 3.6], [0.4, 0.5, -3.2]]
        >>> output = op(input_x, indices, updates)
        >>> print(output)
        [[-0.05  0.3  3.6  ]
         [ 0.4   0.5  -3.2 ]]
    """

    @prim_attr_register
    def __init__(self):
        super().__init__("TensorScatterDiv")
        self.init_prim_io_names(inputs=['input_x', 'indices', 'updates'], outputs=['y'])


class ListDiff(Primitive):
    r"""Computes the difference between two lists of numbers.

    Given a list `x` and a list `y`, this operation returns a list `out` that
    represents all values that are in `x` but not in `y`. The returned list `out`
    is sorted in the same order that the numbers appear in `x` (duplicates are
    preserved). This operation also returns a list `idx` that represents the
    position of each `out` element in `x`. In other words:

    `out[i] = x[idx[i]] for i in [0, 1, ..., len(out) - 1]`

    Inputs:
        - **x**: A 1-D `Tensor`. Values to keep. type support list [float16, float32,
          float64, uint8, uint16, int8, int16, int32, int64]
        - **y**: A 1-D `Tensor`. Must have the same type as `x`. 1-D. Values to remove.

    Outputs:
        - **out**: A 1-D `Tensor`. Has the same type as `x`.
        - **idx**: A 1-D `Tensor` of type `out_idx`.

    Raises:
        ValueError: If `x` or `y` shape is not 1D.
        TypeError: If `x` or `y` is not a Tensor.
        TypeError: If `x` or `y` datetype not in support list.
        TypeError: If `x` has different data type with `y`.
        TypeError: If attr `out_idx` not in [mstype.int32, mstype.int64].

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor(np.arange(1, 7, 1), dtype=mstype.int32) # [1, 2, 3, 4, 5, 6]
        >>> y = Tensor([1, 3, 5], dtype=mstype.int32)
        >>> op = ops.ListDiff() # out_idx default is mstype.int32
        >>> out, idx = op(x, y)
        >>> print(out)
        [2 4 6]
        >>> print(idx)
        [1 3 5]
    """

    @prim_attr_register
    def __init__(self, out_idx=mstype.int32):
        """Initialize ListDiff"""
        self.init_prim_io_names(inputs=['x', 'y'], outputs=['out', 'idx'])
        validator.check_value_type("out_idx", out_idx, [mstype.Type], self.name)
        validator.check("out_idx", out_idx, "", [mstype.int32, mstype.int64], Rel.IN, self.name, excp_cls=TypeError)
        self.out_idx = out_idx
        self.add_prim_attr('out_idx', out_idx)


class SplitV(Primitive):
    r"""
    Splits the input tensor into num_split tensors along the given dimension.

    The `input_x` tensor will be split into sub-tensors with individual shapes given by `size_splits` along the split
    dimension. This requires that `input_x.shape(split_dim)` is equal to the sum of `size_splits`.

    The shape of `input_x` is :math:`(x_1, x_2, ..., x_M, ..., x_R)`. The rank of `input_x` is `R`. Set the given
    `split_dim` as M, and :math:`-R \le M < R`. Set the given `num_split` as `N`, the given `size_splits` as
    :math:`(x_{m_1}, x_{m_2}, ..., x_{m_N})`, :math:`x_M=\sum_{i=1}^Nx_{m_i}`. The output is a list of tensor objects,
    for the :math:`i`-th tensor, it has the shape of :math:`(x_1, x_2, ..., x_{m_i}, ..., x_R)`. :math:`x_{m_i}` is the
    :math:`M`-th dimension of the :math:`i`-th tensor. Then, the shape of the output tensor is

    .. math::

        ((x_1, x_2, ..., x_{m_1}, ..., x_R), (x_1, x_2, ..., x_{m_2}, ..., x_R), ...,
         (x_1, x_2, ..., x_{m_N}, ..., x_R))

    Args:
        size_splits (Union[tuple, list]): The list containing the sizes of each output tensor along the split
                                          dimension. Must sum to the dimension of value along `split_dim`.
                                          Can contain one -1 indicating that dimension is to be inferred.
        split_dim (int): The dimension along which to split. Must be in the range [-len(input_x.shape),
                         len(input_x.shape)).
        num_split (int): The number of output tensors. Must be positive int.

    Inputs:
        - **input_x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ...,x_M ..., x_R)`.

    Outputs:
        Tensor, a list of `num_split` Tensor objects with the shape :math:`((x_1, x_2, ..., x_{m_1}, ..., x_R),
        (x_1, x_2, ..., x_{m_2}, ..., x_R), ..., (x_1, x_2, ..., x_{m_N}, ..., x_R))`, :math:`x_M=\sum_{i=1}^Nx_{m_i}`.
        The data type is the same with `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `size_splits` is not a tuple or a list.
        TypeError: If element of `size_splits` is not an int.
        TypeError: If `split_dim` or `num_split` is not an int.
        ValueError: If rank of the `size_splits` is not equal to `num_split`.
        ValueError: If sum of the `size_splits` is not equal to the dimension of value along `split_dim`.
        ValueError: If `split_dim` is out of the range [-len(input_x.shape), len(input_x.shape)).
        ValueError: If the `num_split` is less than or equal to 0.

    Supported Platforms:
        ``Ascend``

    Examples:
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.int32)
        >>> op = ops.SplitV(size_splits=[1, -1], split_dim=1, num_split=2)
        >>> output = op(input_x)
        >>> print(output)
        (Tensor(shape=[3, 1], dtype=Int32, value=
        [[1],
         [4],
         [7]]), Tensor(shape=[3, 2], dtype=Int32, value=
        [[2, 3],
         [5, 6],
         [8, 9]]))
        >>> input_x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.int32)
        >>> op = ops.SplitV(size_splits=[2, 1], split_dim=0, num_split=2)
        >>> output = op(input_x)
        >>> print(output)
        (Tensor(shape=[2, 3], dtype=Int32, value=
        [[1, 2, 3],
         [4, 5, 6]]), Tensor(shape=[1, 3], dtype=Int32, value=
        [[7, 8, 9]]))
    """

    @prim_attr_register
    def __init__(self, size_splits, split_dim, num_split):
        """Initialize SplitV"""
        validator.check_value_type("size_splits", size_splits, [tuple, list], self.name)
        for elements_of_size_splits in size_splits:
            validator.check_value_type("elements of size_splits", elements_of_size_splits, [int], self.name)
            if elements_of_size_splits != -1 and elements_of_size_splits < 1:
                raise ValueError(f"For \'{self.name}\', all elements of size_splits must be positive (except at most "
                                 f"one default value -1), but got: {elements_of_size_splits}.")
        validator.check_value_type("split_dim", split_dim, [int], self.name)
        validator.check_value_type("num_split", num_split, [int], self.name)
        validator.check_positive_int(num_split, "num_split", self.name)
        self.init_prim_io_names(inputs=['input_x'], outputs=['output'])


class TensorScatterElements(Primitive):
    """
    Updates the value of the output tensor through the reduction operation.
    Refer to :func:`mindspore.ops.tensor_scatter_elements` for more detail.

    .. warning::
        The order in which updates are applied is nondeterministic, meaning that if there
        are multiple index vectors in `indices` that correspond to the same position, the
        value of that position in the output will be nondeterministic.

    Supported Platforms:
        ``Ascend`` ``CPU`` ``GPU``

    Examples:
        >>> op = ops.TensorScatterElements(0, "none")
        >>> data = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]), mindspore.float32)
        >>> indices = Tensor(np.array([[1, 0, 2], [0, 2, 1]]), mindspore.int32)
        >>> updates = Tensor(np.array([[0, 0, 0], [0, 0, 0]]), mindspore.float32)
        >>> output = op(data, indices, updates)
        >>> print(output)
        [[ 0.0  0.0  3.0]
         [ 0.0  5.0  0.0]
         [ 7.0  0.0  0.0]]
        >>> op = ops.TensorScatterElements(1, "add")
        >>> data = Tensor(np.array([[1, 2, 3, 4, 5]), mindspore.int32)
        >>> indices = Tensor(np.array([[2, 4]), mindspore.int32)
        >>> updates = Tensor(np.array([[8, 8]]), mindspore.int32)
        >>> output = op(data, indices, updates)
        >>> print(output)
        [[ 1  2  11  4  13]]
    """

    @prim_attr_register
    def __init__(self, axis=0, reduction="none"):
        """Initialize TensorScatterElements"""
        validator.check_value_type("axis", axis, [int], self.name)
        validator.check_value_type("reduction", reduction, [str], self.name)
        validator.check_string(reduction, ["none", "add"], "reduction", self.name)
        self.init_prim_io_names(
            inputs=['data', 'indices', 'updates'], outputs=['y'])


class ExtractVolumePatches(Primitive):
    """
    Extract patches from input and put them in the "depth" output dimension. 3D extension of extract_image_patches.

    Args:
        kernel_size (Union[int, tuple[int], list[int]]): A list of ints which's length is 3 or 5.
            The size of the sliding window for each dimension of input. Must be: [1, 1, k_d, k_h, k_w] or
            [k_d, k_h, k_w]. If k_d = k_h = k_w, you can enter an integer.
        strides (Union[int, tuple[int], list[int]]): A list of ints which's length is 3 or 5.
            How far the centers of two consecutive patches are in input. Must be: [1, 1, s_d, s_h, s_w] or
            [s_d, s_h, s_w]. If s_d = s_h = s_w, you can enter an integer.
        padding (str): A string from: "SAME", "VALID". The type of padding algorithm to use.

    Inputs:
        - **input_x** (Tensor) - A Tensor. Must be one of the following types: float16, float32.
          5-D Tensor with shape :math:`(x_n, x_c, x_d, x_h, x_w)`.

    Outputs:
        Tensor, has the same type as input.
        If padding is VALID, the shape is :math:`(x_n, k_d * k_h * k_w * x_c, 1 + (x_d - k_d) / s_d,
        1 + (x_h - k_h) / s_h, 1 + (x_w - k_w) / s_w)`; if padding is SAME, the shape is :math:`(
        x_n, k_d * k_h * k_w * x_c, (x_d + s_d - 1) / s_d, (x_h + s_h - 1) / s_h, (x_w + s_w - 1) / s_w)`.

    Raises:
        TypeError: If dtype of input_x is neither float16 nor float32.
        TypeError: If kernel_size or strides is not a list, a tuple or an int.
        TypeError: If input_x is not a tensor.
        TypeError: If padding is not str.
        ValueError: If the length of kernel_size is neither 3 nor 5 and kernel_size is not an integer.
        ValueError: If the length of strides is neither 3 nor 5 and strides is not an integer.
        ValueError: If padding is neither "VALID" nor "SAME".
        ValueError: If elements of kernel_size or strides are not positive integer.
        ValueError: If input_x is not a tensor in dimension 5.
        ValueError: If input_x's shape has zero.
        ValueError: If one of kernel_size or strides' first two numbers is not 1.
        ValueError: If padding = "VALID" and input - kernel_size is less than 0 in d, h or w dimension.
        ValueError: If padding = "SAME" and :math:`padding_needed = ((input_x + strides - 1) / strides - 1) *
                    strides + kernel_size - input` is less than 0 in d, h or w dimension.
        ValueError: If x_h is not 1 or x_w is not 1 and x_w + padding_needed - k_w - s_w is less than 0.
        ValueError: If x_d * x_h * x_w is greater than 2048.

    Supported Platforms:
        ``Ascend``

    Example:
        >>> kernel_size = (1, 1, 2, 2, 2)
        >>> strides = (1, 1, 1, 1, 1)
        >>> padding = "VALID"
        >>> input_x = P.Reshape()(Tensor(np.arange(1, 28), mstype.float16), (1, 1, 3, 3, 3))
        >>> output_y = P.ExtractVolumePatches(kernel_size, strides, padding)(input_x)
        >>> print(output_y.shape)
        (1, 8, 2, 2, 2)
    """

    @prim_attr_register
    def __init__(self, kernel_size, strides, padding):
        validator.check_value_type("kernel_size", kernel_size, (int, list, tuple), self.name)
        validator.check_value_type("strides", strides, (int, list, tuple), self.name)
        if isinstance(kernel_size, (list, tuple)):
            kernel_size = tuple(kernel_size)
            if len(kernel_size) == 5:
                validator.check_int(kernel_size[0], 1, Rel.EQ, "kernel_size[0]", self.name)
                validator.check_int(kernel_size[1], 1, Rel.EQ, "kernel_size[1]", self.name)
        if isinstance(strides, (list, tuple)):
            strides = tuple(strides)
            if len(strides) == 5:
                validator.check_int(strides[0], 1, Rel.EQ, "strides[0]", self.name)
                validator.check_int(strides[1], 1, Rel.EQ, "strides[1]", self.name)
        self.kernel_size = _check_3d_int_or_tuple("kernel_size", kernel_size, self.name,
                                                  allow_five=True, ret_five=True, greater_zero=True)
        self.strides = _check_3d_int_or_tuple("strides", strides, self.name,
                                              allow_five=True, ret_five=True, greater_zero=True)
        self.add_prim_attr("kernel_size", self.kernel_size)
        self.add_prim_attr("strides", self.strides)
        validator.check_value_type("padding_dtype", padding, (str), self.name)
        self.padding = validator.check_string(padding.upper(), ['VALID', 'SAME'], 'padding', self.name)
        self.add_prim_attr("padding", self.padding)


class Lstsq(Primitive):
    r"""
    Computes the solutions of the least squares and minimum norm problems of full-rank
    matrix `x` of size :math:`(m \times n)` and matrix `a` of size :math:`(m \times k)`.

    If :math:`m \geq n`, `lstsq` solves the least-squares problem:

    .. math::

       \begin{array}{ll}
       \min_y & \|xy-a\|_2.
       \end{array}

    If :math:`m < n`, `lstsq` solves the least-norm problem:

    .. math::

       \begin{array}{llll}
       \min_y & \|y\|_2 & \text{subject to} & xy = a.
       \end{array}

    Inputs:
        - **x** (Tensor) - The m by n matrix `x`. The input tensor whose data type is float16, float32 or float64.
        - **a** (Tensor) - The m by k matrix `a`. The input tensor whose data type is float16, float32 or float64.

    Outputs:
        Tensor, the least squares or minimum norm problems solution, which has shape :math:`(n \times k)`.
        The data type is the same with `x`.

    Raises:
        TypeError: If the input `x` or `a` is not a Tensor.
        TypeError: If dtype of `x` or `a` is not one of: float16, float32, float64.
        TypeError: If the dtypes of `x` and `a` are not the same.
        ValueError: If the dimension of `x` is not equal to 2.
        ValueError: If the dimension of `a` is not equal to 2 or 1.
        ValueError: If the length of x_dims[0] is not equal to the length of a_dims[0].

    Supported Platforms:
        ``CPU``

    Examples:
        >>> x = Tensor(np.array([[2,1,5],[3,5,1],[1,1,1]]),mindspore.float32)
        >>> a = Tensor(np.array([[10,5],[15,8],[7,4]]),mindspore.float32)
        >>> op = ops.Lstsq()
        >>> output = op(x, a)
        >>> print(output)
        [[17.000002  11.000002 ]
         [-6.5000005 -4.500001 ]
         [-3.500002  -2.5000017]]
    """

    @prim_attr_register
    def __init__(self, fast=True, l2_regularizer=0.0):
        """Initialize Lstsq"""
        validator.check_type_name("fast", fast, True, self.name)
        validator.check_type_name("l2_regularizer", l2_regularizer, 0.0, self.name)
        self.fast = fast
        self.l2_regularizer = l2_regularizer


class LowerBound(Primitive):
    """
    Returns a tensor that contains the index for finding the lower bound of the value
    of the input values element in the input sorted_x.

    Args:
        out_type (:class:`mindspore.dtype`): An optional data type of `mindspore.dtype.int32` and
            `mindspore.dtype.int64`. Default: `mindspore.dtype.int32`.

    Inputs:
        - **sorted_x** (Tensor) - The input tensor whose dtype is real number and the data of each row must be sorted
          in ascending order. The rank must be 2.
        - **values** (Tensor) - The input tensor whose dtype is the same as `sorted_x` and the first dimension of the
          shape of `values` must be equal to that of `sorted_x`. The rank must be 2.

    Outputs:
        Tensor, whose dtype is determined by `out_type` and whose shape is the same as that of `values`.

    Raises:
        TypeError: If `sorted_x` is not a Tensor.
        TypeError: If `values` is not a Tensor.
        TypeError: If `out_type` is invalid.
        TypeError: If the type of `sorted_x` is not the same as that of `values`.
        ValueError: If rank of the `sorted_x` is not equal to 2.
        ValueError: If rank of the `values` is not equal to 2.
        ValueError: If the first dimension of the shape of `sorted_x` is not equal to that of `values`.

    Supported Platforms:
        ``CPU`` ``GPU``

    Examples:
        >>> import mindspore
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> import mindspore.ops as ops
        >>> lowerbound = ops.LowerBound(out_type = mindspore.int32)
        >>> sorted_x = Tensor(np.arange(12).reshape(3, 4).astype(np.int8))
        >>> values = Tensor(np.array([[3], [4], [8]]).astype(np.int8))
        >>> output = lowerbound(sorted_x, values)
        >>> print(output)
        [[3]
         [0]
         [0]]
    """

    @prim_attr_register
    def __init__(self, out_type=mstype.int32):
        """Initialize LowerBound"""
        valid_values = (mstype.int32, mstype.int64)
        validator.check_type_name("out_type", out_type, valid_values, self.name)
        self.init_prim_io_names(inputs=['sorted_x', 'values'], outputs=['y'])


class UpperBound(Primitive):
    """
    Returns a tensor that contains the index for finding the upper bound of the value of
    the input values element in the input sorted_x.

    Args:
        out_type (:class:`mindspore.dtype`): An optional data type of `mindspore.dtype.int32`
            and `mindspore.dtype.int64`. Default: `mindspore.dtype.int32`.

    Inputs:
        - **sorted_x** (Tensor) - The input tensor whose dtype is real number. The rank must be 2.
          Each row of the sorted_x needs to be sorted in ascending order.
        - **values** (Tensor) - The input tensor whose dtype is the same as `sorted_x`. The rank must be 2.
          The shape[0] of the two inputs must be consistent.

    Outputs:
        Tensor, whose dtype is determined by `out_type` and whose shape is consistent with `values`.

    Raises:
        TypeError: If `sorted_x` is not a Tensor.
        TypeError: If `values` is not a Tensor.
        TypeError: If the type of `sorted_x` is not the same as that of `values`.
        ValueError: If rank of the `sorted_x` is not equal to 2.
        ValueError: If rank of the `values` is not equal to 2.
        ValueError: If the number of rows of `sorted_x` is not consistent with that of `values`.

    Supported Platforms:
        ``CPU`` ``GPU``

    Examples:
        >>> import mindspore
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> import mindspore.ops as ops
        >>> upperbound = ops.UpperBound(out_type = mindspore.int32)
        >>> sorted_x = Tensor(np.arange(12).reshape(3, 4).astype(np.int8))
        >>> values = Tensor(np.array([[3], [6], [9]]).astype(np.int8))
        >>> output = upperbound(sorted_x, values)
        >>> print(output)
        [[4]
         [3]
         [2]]
    """

    @prim_attr_register
    def __init__(self, out_type=mstype.int32):
        """Initialize UpperBound"""
        valid_values = (mstype.int32, mstype.int64)
        validator.check_type_name("out_type", out_type, valid_values, self.name)
        self.init_prim_io_names(inputs=['sorted_x', 'values'], outputs=['y'])


class Cummax(Primitive):
    """
    Returns the cumulative maximum of elements and the index.

    Refer to :func:`mindspore.ops.cummax` for more detail.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> import mindspore
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> import mindspore.ops as ops
        >>> cummax = ops.Cummax(axis=0)
        >>> x = Tensor(np.array([[3, 4, 6, 10], [1, 6, 7, 9], [4, 3, 8, 7], [1, 3, 7, 9]]).astype(np.float32))
        >>> output = cummax(x)
        >>> print(output[0])
        [[ 3.  4.  6. 10.]
         [ 3.  6.  7. 10.]
         [ 4.  6.  8. 10.]
         [ 4.  6.  8. 10.]]
        >>> print(output[1])
        [[0 0 0 0]
         [0 1 1 0]
         [2 1 2 0]
         [2 1 2 0]]
    """

    @prim_attr_register
    def __init__(self, axis):
        """Initialize Cummax"""
        validator.check_value_type("axis", axis, [int], self.name)
        self.init_prim_io_names(inputs=['x'], outputs=['y', 'indices'])


class RightShift(Primitive):
    r"""
    Shift the value of each position of the tensor to the right several bits.
    The inputs are two tensors, dtypes of them must be consistent, and the
    shapes of them could be broadcast.

    .. math::

        \begin{aligned}
        &out_{i} =x_{i} >> y_{i}
        \end{aligned}

    Inputs:
        - **input_x** (Tensor) - The target tensor, will be shifted to the right
          by y in element-wise.
        - **input_y** (Tensor) - The tensor must have the same type as input_x.

    Outputs:
        - **output** (Tensor) - The output tensor, has the same type as input_x.

    Raises:
        TypeError: If `input_x` or `input_y` is not tensor.
        TypeError: If `input_x` and `input_y` could not be broadcast.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> rightshift = ops.RightShift()
        >>> input_x = Tensor(np.array([1, 2, 3]).astype(np.uint8))
        >>> input_y = Tensor(np.array([1, 1, 1]).astype(np.uint8))
        >>> output = rightshift(input_x, input_y)
        >>> print(output)
        [0 1 1]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize RightShift."""
        self.init_prim_io_names(inputs=['input_x', 'input_y'], outputs=['output'])


class LogSpace(Primitive):
    r"""
    Returns a one-dimensional tensor of size steps whose values are evenly spaced from base**start to base**end,
    inclusive, on a logarithmic scale with base.

    .. math::
        \begin{aligned}
        &step = (end - start)/(steps - 1)\\
        &output = [base^{start}, base^{start + 1 * step}, ... , base^{start + (steps-2) * step}, base^{end}]
        \end{aligned}

    Args:
        steps (int): The steps must be a non-negative integer. default: 10
        base (int): The base must be a non-negative integer. default: 10
        dtype (mindspore.dtype): The dtype of output,
          include mindspore.float16, mindspore.float32 or mindspore.float64(for GPU).


    Inputs:
        - **start** (Tensor) - Start value of interval, with shape of 0-D,
          dtype is float16, float32 or float64(for GPU).
        - **end** (Tensor) - End value of interval, with shape of 0-D,
          dtype is float16, float32 or float64(for GPU).

    Outputs:
        Tensor has the shape as (step, ). Its datatype is set by the attr 'dtype'.

    Raises:
        TypeError: If `input` is not a Tensor.
        TypeError: If `steps` is not an int.
        TypeError: If `base` is not an int.
        TypeError: If `dtype` is not mindspore.float16, mindspore.float32 or mindspore.float64(for GPU).
        ValueError: If `steps` is not a non-negative integer.
        ValueError: If `base` is not a non-negative integer.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> logspace = ops.LogSpace(steps = 10, base = 10, dtype=mindspore.float32)
        >>> start = Tensor(1, mindspore.float32)
        >>> end = Tensor(10, mindspore.float32)
        >>> output = logspace(start, end)
        >>> print(output)
        [1.e+01 1.e+02 1.e+03 1.e+04 1.e+05 1.e+06 1.e+07 1.e+08 1.e+09 1.e+10]
    """

    @prim_attr_register
    def __init__(self, steps=10, base=10, dtype=mstype.float32):
        """Initialize Logspace."""
        validator.check_value_type("steps", steps, [int], self.name)
        validator.check_value_type("base", base, [int], self.name)
        validator.check_non_negative_int(steps, "steps", self.name)
        validator.check_non_negative_int(base, "base", self.name)
        validator.check_value_type("dtype", dtype, [mstype.Type], self.name)
        valid_values = (mstype.float16, mstype.float32, mstype.float64)
        validator.check_type_name("dtype", dtype, valid_values, self.name)
        self.init_prim_io_names(inputs=['start', 'end'], outputs=['y'])


class NonZero(Primitive):
    """
    Return a tensor of the positions of all non-zero values.

    Refer to :func:`mindspore.ops.nonzero` for more detail.

    Supported Platforms:
       ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> import mindspore
        >>> import numpy as np
        >>> from mindspore import Tensor
        >>> from mindspore.ops.operations.array_ops import NonZero
        >>> x = Tensor(np.array([[[1,  0], [-5, 0]]]), mindspore.int32)
        >>> nonzero = NonZero()
        >>> output = nonzero(x)
        >>> print(output)
        [[0 0 0]
         [0 1 0]]
    """

    @prim_attr_register
    def __init__(self):
        self.init_prim_io_names(inputs=['x'], outputs=['y'])


class Tril(Primitive):
    """
    Returns the lower triangular part of the matrix (2-D tensor) or batch of matrices input,
    the other elements of the result tensor out are set to 0.
    The lower triangular part of the matrix is defined as the elements on and below the diagonal.

    Args:
        diagonal (int): An optional attribute indicates the diagonal to consider, default to 0.

    Inputs:
        - **x** (Tensor) - A Tensor with shape :math:`(x_1, x_2, ..., x_R)`. The rank must be at least 2.
          Supporting all number types including bool.

    Outputs:
        Tensor, the same shape and data type as the input.

    Raises:
        TypeError: If `x` is not a Tensor.
        TypeError: If `diagonal` is not an int.
        TypeError: If the type of `x` is neither number nor bool.
        ValueError: If the rank of `x` is less than 2.

    Supported Platforms:
        ``GPU`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> tril = P.Tril()
        >>> result = tril(x)
        >>> print(result)
        [[ 1  0  0  0]
         [ 5  6  0  0]
         [10 11 12  0]
         [14 15 16 17]]
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> tril = P.Tril(diagonal=1)
        >>> result = tril(x)
        >>> print(result)
        [[ 1  2  0  0]
         [ 5  6  7  0]
         [10 11 12 13]
         [14 15 16 17]]
        >>> x = Tensor(np.array([[ 1,  2,  3,  4],
        ...                      [ 5,  6,  7,  8],
        ...                      [10, 11, 12, 13],
        ...                      [14, 15, 16, 17]]))
        >>> tril = P.Tril(diagonal=-1)
        >>> result = tril(x)
        >>> print(result)
        [[ 0  0  0  0]
         [ 5  0  0  0]
         [10 11  0  0]
         [14 15 16  0]]
    """

    @prim_attr_register
    def __init__(self, diagonal=0):
        """Initialize Tril."""
        self.init_prim_io_names(inputs=["x"], outputs=["y"])
        validator.check_value_type("diagonal", diagonal, [int], self.name)


class IndexFill(Primitive):
    """
    Fills the elements under the dim dimension of the input Tensor with the input value
    by selecting the indices in the order given in index.

    Refer to :func:`mindspore.ops.index_fill` for more detail.
    Inputs:
        - **x** (Tensor) - Input tensor.
          The shape is :math:`(N,*)` where :math:`*` means, any number of additional dimensions.
        - **dim** (Union[int, Tensor]) - Dimension along which to fill the input tensor. Only supports
          a 0-D tensor or an int number.
        - **index** (Tensor) - Indices of the input tensor to fill in. Only supports a 0-D or 1-D tensor.
        - **value** (Tensor) - Value to fill the returned tensor. Only supports a 0-D tensor or a scalar.

    Outputs:
        Tensor, has the same type and shape as input tensor.

    Raises:
        TypeError: If `x` is not a Tensor.
        TypeError: If `dim` is neither a int number nor a tensor.
        TypeError: If `index` is not a Tensor.
        TypeError: If `value` is not a Tensor/Scalar.
        TypeError: If dtype of `index` is not int32.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> import mindspore
        >>> from mindspore.ops.operations.array_ops import IndexFill
        >>> index_fill = IndexFill()
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]).astype(np.float32))
        >>> index = Tensor([0, 2], mindspore.int32)
        >>> value = Tensor(-2.0, mindspore.float32)
        >>> y = index_fill(x, 1, index, value)
        >>> print(y)
        [[-2. 2. -2.]
         [-2. 5. -2.]
         [-2. 8. -2.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize IndexFill"""
        self.init_prim_io_names(inputs=['x', 'dim', 'index', 'value'], outputs=['y'])


class SegmentMax(Primitive):
    r"""
    Computes the maximum along segments of a tensor.

    Computes a tensor such that :math:`output_i=max_j(data_j)` where max is over :math:`j` such that
    :math:`segment\_ids[j] == i`. If the max is empty for a given segment ID :math:`i`, :math:`output[i] = 0`.

    Inputs:
        - **input_x** (Tensor) - The input tensor whose dtype is real number and whose rank is not less than 1.
        - **segment_ids** (Tensor) - A 1-D tensor whose dtype is int32 or int64. The size of tensor must be equal to
          the first dimension of the shape of `input_x`. Values must be sorted in ascending order and need not cover
          all values in the full range of valid values, but must be positive intege. Only constant values is allowed.

    Outputs:
        Tensor, whose dtype and the dimension of the shape is the same as `input_x`. The first dimension of the shape
        is equal to the value of the last element of `segment_ids` plus one, and the other dimensions are the same as
        those of `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `segment_ids` is not a Tensor.
        TypeError: If the dtype of `input_x` is invalid.
        TypeError: If the dtype of `segment_ids` is invalid.
        ValueError: If the rank of `input_x` is less than 1.
        ValueError: If the rank of `segment_ids` is not equal to 1.
        ValueError: If the size of `segment_ids` is not equal to the first dimension of the shape of `input_x`.
        ValueError: If the values of `segment_ids` are negative.
        ValueError: If the values of `segment_ids` are not sorted in ascending order.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], mstype.float64)
        >>> segment_ids = Tensor([0, 0, 2], mstype.int64)
        >>> op = ops.SegmentMax()
        >>> output = op(x, segment_ids)
        >>> print(output)
        [[4. 5. 6.]
         [0. 0. 0.]
         [7. 8. 9.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SegmentMax"""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['input_x', 'segment_ids'], outputs=['output'])


class SegmentMin(Primitive):
    r"""
    Computes the minimum along segments of a tensor.

    Computes a tensor such that :math:`output_i=min_j(data_j)` where :math:`min` is over :math:`j` such that
    :math:`segment\_ids[j] == i`. If the min is empty for a given segment ID :math:`i`, :math:`output[i] = 0`.

    Inputs:
        - **input_x** (Tensor) - The input tensor whose dtype is real number and whose rank is not less than 1.
        - **segment_ids** (Tensor) - A 1-D tensor whose dtype is int32 or int64. The size of tensor must be equal to
          the first dimension of the shape of `input_x`. Values must be sorted in ascending order and need not cover
          all values in the full range of valid values, but must be positive intege. Only constant values is allowed.

    Outputs:
        Tensor, whose dtype and the dimension of the shape is the same as `input_x`. The first dimension of the shape
        is equal to the value of the last element of `segment_ids` plus one, and the other dimensions are the same as
        those of `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `segment_ids` is not a Tensor.
        TypeError: If the dtype of `input_x` is invalid.
        TypeError: If the dtype of `segment_ids` is invalid.
        ValueError: If the rank of `input_x` is less than 1.
        ValueError: If the rank of `segment_ids` is not equal to 1.
        ValueError: If the size of `segment_ids` is not equal to the first dimension of the shape of `input_x`.
        ValueError: If the values of `segment_ids` are negative.
        ValueError: If the values of `segment_ids` are not sorted in ascending order.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], mstype.float64)
        >>> segment_ids = Tensor([0, 0, 2], mstype.int64)
        >>> op = ops.SegmentMin()
        >>> output = op(x, segment_ids)
        >>> print(output)
        [[1. 2. 3.]
         [0. 0. 0.]
         [7. 8. 9.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SegmentMin"""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['input_x', 'segment_ids'], outputs=['output'])


class SegmentSum(Primitive):
    r"""
    Computes the sum along segments of a tensor.

    Computes a tensor such that :math:`output_i = \sum_j data_j` where sum is over :math:`j` such that
    :math:`segment\_ids[j] == i`. If the sum is empty for a given segment ID :math:`i`, :math:`output[i] = 0`.

    .. warning::
        If the dtype of `input_x` is complex number, the gradient can not be calculated.

    Inputs:
        - **input_x** (Tensor) - The input tensor whose dtype is real number or complex number and whose rank is not
          less than 1.
        - **segment_ids** (Tensor) - A 1-D tensor whose dtype is int32 or int64. The size of tensor must be equal to
          the first dimension of the shape of `input_x`. Values must be sorted in ascending order and need not cover
          all values in the full range of valid values, but must be positive intege. Only constant values is allowed.

    Outputs:
        Tensor, whose dtype and the dimension of the shape is the same as `input_x`. The first dimension of the shape
        is equal to the value of the last element of `segment_ids` plus one, and the other dimensions are the same as
        those of `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `segment_ids` is not a Tensor.
        TypeError: If the dtype of `input_x` is invalid.
        TypeError: If the dtype of `segment_ids` is invalid.
        ValueError: If the rank of `input_x` is less than 1.
        ValueError: If the rank of `segment_ids` is not equal to 1.
        ValueError: If the size of `segment_ids` is not equal to the first dimension of the shape of `input_x`.
        ValueError: If the values of `segment_ids` are negative.
        ValueError: If the values of `segment_ids` are not sorted in ascending order.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], mstype.float64)
        >>> segment_ids = Tensor([0, 0, 2], mstype.int64)
        >>> op = ops.SegmentSum()
        >>> output = op(x, segment_ids)
        >>> print(output)
        [[5. 7. 9.]
         [0. 0. 0.]
         [7. 8. 9.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SegmentSum"""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['input_x', 'segment_ids'], outputs=['output'])


class LeftShift(Primitive):
    r"""
    Shift the value of each position of the tensor to the left several bits.
    The inputs are two tensors, dtypes of them must be consistent, and the
    shapes of them could be broadcast.
    The output does not support implicit type conversion.

    .. math::

        \begin{aligned}
        &out_{i} =x_{i} << y_{i}
        \end{aligned}

    Inputs:
        - **x1** (Tensor) - The target tensor whose dtype supports int8, int16, int32, int64,
        uint8, uint16, uint32, uint64, will be shifted to the left by x2 in element-wise.
        - **x2** (Tensor) - The tensor must have the same dtype as x1. And the tensor must have the same shape as x1
        or could be broadcast with x1.

    Outputs:
        - **output** (Tensor) - The output tensor, has the same dtype as x1.
        And the shape of the output tensor is the same shape as x1, or the same shape
        as x1 and x2 after broadcasting.

    Raises:
        TypeError: If `x1` or `x2` has wrong type.
        TypeError: If `x1` or `x2` is not tensor.
        TypeError: If `x1` and `x2` could not be broadcast.

    Supported Platforms:
        ``Ascend`` ``GPU`` ``CPU``

    Examples:
        >>> left_shift = ops.LeftShift()
        >>> x1 = Tensor(np.array([1, 2, 3]).astype(np.uint8))
        >>> x2 = Tensor(np.array([0, 1, -1]).astype(np.uint8))
        >>> output = left_shift(x1, x2)
        >>> print(output)
        [1 4 3]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize LeftShift"""
        self.init_prim_io_names(inputs=['x1', 'x2'], outputs=['y'])


class FillDiagonal(Primitive):
    """
    Fill the main diagonal of a tensor that has at least 2-dimensions.
    When dims>2, all dimensions of input must be of equal length.
    This function modifies the input tensor in-place, and returns the input tensor.

    Args:
        fill_value (float): The fill value.
        wrap (bool): the diagonal ‘wrapped’ after N columns for tall matrices. Default: False.

    Inputs:
        - **input_x** (Tensor) - The shape of tensor is :math:`(x_1, x_2, ..., x_R)`.
        The data type must be float32, int32 or int64.

    Outputs:
        - **y** (Tensor) - Tensor, has the same shape and data type as the input `x`.

    Raises:
        TypeError: If data type of `input_x` is not one of the following: float32, int32, int64.
        ValueError: If the dimension of `input_x` is not greater than 1.
        ValueError: If the size of each dimension is not equal, when the dimension is greater than 2.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor(np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]).astype(np.float32))
        >>> fill_value = 9.9
        >>> fill_diagonal = FillDiagonal(fill_value)
        >>> y = fill_diagonal(x)
        >>> print(y)
        [[9.9 2.  3. ]
         [4.  9.9 6. ]
         [7.  8.  9.9]]
        >>> x = Tensor(np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2], [3, 3, 3], [4, 4, 4], [5, 5, 5]]).astype(np.int32))
        >>> fill_value = 9.0
        >>> fill_diagonal = FillDiagonal(fill_value)
        >>> y = fill_diagonal(x)
        >>> print(y)
        [[9 0 0]
         [1 9 1]
         [2 2 9]
         [3 3 3]
         [4 4 4]
         [5 5 5]]
        >>> x = Tensor(np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2], [3, 3, 3],
        ...                      [4, 4, 4], [5, 5, 5], [6, 6, 6]]).astype(np.int64))
        >>> fill_value = 9.0
        >>> wrap = True
        >>> fill_diagonal = FillDiagonal(fill_value, wrap)
        >>> y = fill_diagonal(x)
        >>> print(y)
        [[9 0 0]
         [1 9 1]
         [2 2 9]
         [3 3 3]
         [9 4 4]
         [5 9 5]
         [6 6 9]]
    """

    @prim_attr_register
    def __init__(self, fill_value, wrap=False):
        """Initialize FillDiagonal"""
        validator.check_value_type('fill_value', fill_value, [float], self.name)
        self.fill_value = fill_value
        validator.check_value_type('wrap', wrap, [bool], self.name)
        self.init_prim_io_names(inputs=['input_x'], outputs=['y'])


class AffineGrid(Primitive):
    r"""
    Generates a 2D or 3D flow field (sampling grid), given a batch of affine matrices theta.

    Refer to :func:`mindspore.ops.affine_grid` for more detail.

    Supported Platforms:
        ``GPU``

    Examples:
        >>> affinegrid = AffineGrid(align_corners=False)
        >>> theta = Tensor([[[0.8, 0.5, 0],[-0.5, 0.8, 0]]], mindspore.float32)
        >>> out_size = (1, 3, 2, 3)
        >>> output = affinegrid(theta, out_size)
        >>> print(output)
        [[[[-0.78333336 -0.06666666]
        [-0.25       -0.4       ]
        [ 0.28333336 -0.73333335]]
        [[-0.28333336  0.73333335]
        [ 0.25        0.4       ]
        [ 0.78333336  0.06666666]]]]
    """

    @prim_attr_register
    def __init__(self, align_corners=False):
        """Initialize AffineGrid."""
        validator.check_value_type("align_corners", align_corners, [bool], self.name)
        self.init_prim_io_names(inputs=['theta', 'output_size'], outputs=['y'])


class SegmentMean(Primitive):
    r"""
    Computes the mean along segments of a tensor.

    Computes a tensor such that :math:`output_i = \mean_j data_j` where mean is over :math:`j` such that
    :math:`segment\_ids[j] == i`. If the mean is empty for a given segment ID :math:`i`, :math:`output[i] = 0`.

    .. warning::
        If the dtype of `input_x` is complex number, the gradient can not be calculated.

    Inputs:
        - **input_x** (Tensor) - The input tensor whose dtype is real number or complex number and whose rank is not
          less than 1.
        - **segment_ids** (Tensor) - A 1-D tensor whose dtype is int32 or int64. The size of tensor must be equal to
          the first dimension of the shape of `input_x`. Values must be sorted in ascending order and need not cover
          all values in the full range of valid values, but must be positive intege. Only constant values is allowed.

    Outputs:
        Tensor, whose dtype and the dimension of the shape is the same as `input_x`. The first dimension of the shape
        is equal to the value of the last element of `segment_ids` plus one, and the other dimensions are the same as
        those of `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `segment_ids` is not a Tensor.
        TypeError: If the dtype of `input_x` is invalid.
        TypeError: If the dtype of `segment_ids` is invalid.
        ValueError: If the rank of `input_x` is less than 1.
        ValueError: If the rank of `segment_ids` is not equal to 1.
        ValueError: If the size of `segment_ids` is not equal to the first dimension of the shape of `input_x`.
        ValueError: If the values of `segment_ids` are negative.
        ValueError: If the values of `segment_ids` are not sorted in ascending order.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor([[1, 2, 3], [1, 2, 3], [7, 8, 9]], mstype.float64)
        >>> segment_ids = Tensor([0, 0, 2], mstype.int64)
        >>> op = ops.SegmentMean()
        >>> output = op(x, segment_ids)
        >>> print(output)
        [[1. 2. 3.]
         [0. 0. 0.]
         [7. 8. 9.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SegmentMean"""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['input_x', 'segment_ids'], outputs=['output'])


class SegmentProd(Primitive):
    r"""
    Computes the prod along segments of a tensor.

    Computes a tensor such that :math:`output_i = \prod_j data_j` where prod is over :math:`j` such that
    :math:`segment\_ids[j] == i`. If the prod is empty for a given segment ID :math:`i`, :math:`output[i] = 0`.

    .. warning::
        If the dtype of `input_x` is complex number, the gradient can not be calculated.

    Inputs:
        - **input_x** (Tensor) - The input tensor whose dtype is real number or complex number and whose rank is not
          less than 1.
        - **segment_ids** (Tensor) - A 1-D tensor whose dtype is int32 or int64. The size of tensor must be equal to
          the first dimension of the shape of `input_x`. Values must be sorted in ascending order and need not cover
          all values in the full range of valid values, but must be positive intege. Only constant values is allowed.

    Outputs:
        Tensor, whose dtype and the dimension of the shape is the same as `input_x`. The first dimension of the shape
        is equal to the value of the last element of `segment_ids` plus one, and the other dimensions are the same as
        those of `input_x`.

    Raises:
        TypeError: If `input_x` is not a Tensor.
        TypeError: If `segment_ids` is not a Tensor.
        TypeError: If the dtype of `input_x` is invalid.
        TypeError: If the dtype of `segment_ids` is invalid.
        ValueError: If the rank of `input_x` is less than 1.
        ValueError: If the rank of `segment_ids` is not equal to 1.
        ValueError: If the size of `segment_ids` is not equal to the first dimension of the shape of `input_x`.
        ValueError: If the values of `segment_ids` are negative.
        ValueError: If the values of `segment_ids` are not sorted in ascending order.

    Supported Platforms:
        ``Ascend`` ``CPU``

    Examples:
        >>> x = Tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], mstype.float64)
        >>> segment_ids = Tensor([0, 0, 2], mstype.int64)
        >>> op = ops.SegmentProd()
        >>> output = op(x, segment_ids)
        >>> print(output)
        [[ 4. 10. 18.]
         [ 1.  1.  1.]
         [ 7.  8.  9.]]
    """

    @prim_attr_register
    def __init__(self):
        """Initialize SegmentProd"""
        self.add_prim_attr("max_length", 1000000)
        self.init_prim_io_names(inputs=['input_x', 'segment_ids'], outputs=['output'])

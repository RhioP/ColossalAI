from .op_wrapper import _COLOSSAL_OPS

import torch
from typing import Tuple, Optional, Callable
from numpy import product
from colossalai.core import global_context as gpc
from colossalai.nn.layer.utils import divide
from colossalai.tensor import TensorSpec, ComputePattern, ShardPattern
from colossalai.nn.layer.parallel_1d._utils import split_forward_gather_backward, gather_forward_split_backward
from enum import Enum


class TensorType(Enum):
    MODEL = 0
    NONMODEL = 1    # mainly activations


class ColoTensor(object):
    """ Data Structure for Tensor in Colossal-AI
    1. It contains a torch.Tensor as an attribute.
    2. It supports lazy init the tensor's payload.
    3. It can hijack the torch functions which using ColoTensors as args to our customized functions.
    4. It supports distributing the tensor's payload to the shards among processes. (TODO)
    """

    def __new__(cls, *args, **kwargs):
        return super(ColoTensor, cls).__new__(cls)

    def __init__(
            self,
            *size: Tuple[int],
            dtype=None,
            requires_grad=False,
            pin_memory=False,
            device=None,
            torch_tensor=torch.empty(0),
            shard_spec: TensorSpec = TensorSpec(),
            is_model_data: bool = False,
    ):
        self._size = size
        self._dtype = dtype
        self._requires_grad = requires_grad
        self._pin_memory = pin_memory
        self._device = device
        self._torch_tensor = torch_tensor
        self._shard_spec = shard_spec
        self._shard_pattern = ShardPattern.NA
        if is_model_data:
            self._type = TensorType.MODEL
        else:
            self._type = TensorType.NONMODEL

    def __getitem__(self, key):
        return ColoTensor.init_from_torch_tensor(self.torch_tensor()[key])

    @property
    def shard_spec(self) -> TensorSpec:
        return self._shard_spec

    @property
    def shard_pattern(self):
        return self._shard_pattern

    @property
    def data(self):
        return self._torch_tensor.data

    @property
    def grad(self):
        return self._torch_tensor.grad

    @property
    def size(self):
        return self._size

    @property
    def shape(self):
        return torch.Size(self._size)

    @property
    def device(self):
        return self._torch_tensor.device

    def size(self, dim=None):
        if dim is None:
            return self.shape
        return self._size[dim]

    def dim(self):
        return len(self._size)

    def normal_(self, mean=0., std=1.):
        torch_tensor = self.torch_tensor()
        return torch_tensor.normal_(mean=mean, std=std)

    def numel(self):
        return product(self._size)

    @staticmethod
    def init_from_torch_tensor(tensor: torch.Tensor, save_payload=True, is_model_data=False) -> 'ColoTensor':
        colo_t = ColoTensor(*tensor.size(),
                            dtype=tensor.dtype,
                            requires_grad=tensor.requires_grad,
                            pin_memory=tensor.is_pinned(),
                            device=tensor.device,
                            torch_tensor=tensor if save_payload else torch.empty(0),
                            is_model_data=is_model_data)
        return colo_t

    def del_torch_tensor(self, save_shape=False) -> None:
        """
        delete the payload of the torch tensor.

        Args:
            save_shape (bool, optional): if saving the shape of the torch_tensor. 
            If saving the shape, the size of self._torch_tensor is inconsist with the self._size.
            Defaults to False.
        """
        if not save_shape:
            self._size = (0,)
        self._torch_tensor = torch.empty((0,), device=self._device, dtype=self._dtype)

    def torch_tensor(self) -> torch.Tensor:
        if self._torch_tensor.numel() == 0:
            self._torch_tensor = torch.empty(*self._size,
                                             dtype=self._dtype,
                                             pin_memory=self._pin_memory,
                                             requires_grad=self._requires_grad,
                                             device=self._device)
        return self._torch_tensor

    def set_spec(self, spec: TensorSpec, shard: bool = True) -> None:
        self._shard_spec = spec
        if shard == True:
            self.shard()

    def set_shard_pattern(self, shard_pattern: ShardPattern):
        self._shard_pattern = shard_pattern

    def shard(self):
        assert self._shard_spec is not None, 'You should call set_spec() before _shard() ColoTensor.'
        if self._shard_pattern is not ShardPattern.NA:    # reshard
            self.gather()
        # Model Parameters
        if self._shard_spec.num_action == 1:
            parallel_action = self._shard_spec.get_action_by_compute_pattern(
                    self._shard_spec.compute_patterns[0])
            if parallel_action.compute_pattern in [ComputePattern.TP1DRow_Linear, \
                ComputePattern.TP1DCol_Embedding]:
                self._shard_1d(parallel_action=parallel_action, dim=-1)
                self._shard_pattern = ShardPattern.Col # We bind our ComputePattern on weight, which has to be transposed when linear().
            elif parallel_action.compute_pattern in [ComputePattern.TP1DCol_Linear, \
                ComputePattern.TP1DRow_Embedding]:
                self._shard_1d(parallel_action=parallel_action, dim=0)
                self._shard_pattern = ShardPattern.Row
            else:
                raise NotImplementedError

    def gather(self):
        assert self.is_activation(), 'Currently we only support gather Activation ColoTensor.'
        assert not self.is_gathered(), 'Only sharded ColoTensor can be gathered.'
        parallel_action = self._shard_spec.get_action_by_compute_pattern(ComputePattern.DP)
        if self._shard_pattern == ShardPattern.Row:
            dim = 0
        elif self._shard_pattern == ShardPattern.Col:
            dim = -1
        self._torch_tensor = gather_forward_split_backward(self._torch_tensor, parallel_action.parallel_mode, dim=dim)
        self._shard_pattern = ShardPattern.NA
        self._size = self._torch_tensor.size()

    def is_gathered(self) -> bool:
        return self._shard_pattern == ShardPattern.NA

    def has_spec(self) -> bool:
        return self._shard_spec is not None and self._shard_spec.num_action > 0

    def is_activation(self) -> bool:
        return self._type == TensorType.NONMODEL

    def _shard_1d(self, parallel_action, dim=-1):
        num_partition = gpc.get_world_size(parallel_action.parallel_mode)
        local_rank = gpc.get_local_rank(parallel_action.parallel_mode)
        chunk_size = divide(self._size[dim], num_partition)
        # Reshape to get shard for this rank and we don't want autograd
        # recording here for the narrow op and 'local_shard' should be a
        # leaf variable in the autograd graph.
        self._torch_tensor = self._torch_tensor.narrow(dim, local_rank * chunk_size, chunk_size).detach().contiguous(
        )    # TODO Shall we clone() here since detach() will point to the old tensor?
        self._torch_tensor.requires_grad = self._requires_grad
        self._size = self._torch_tensor.size()

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        global _COLOSSAL_OPS
        if func in _COLOSSAL_OPS:
            for arg in args:
                if isinstance(arg, ColoTensor):
                    return _COLOSSAL_OPS[func](types, args, kwargs, None)

            for kwarg in kwargs.values():
                if isinstance(kwarg, ColoTensor):
                    return _COLOSSAL_OPS[func](types, args, kwargs, None)
        else:
            # If we have not hijact the function, convert the ColoTensors in args and kwargs to torch tensors.
            args = [arg.torch_tensor() if isinstance(arg, ColoTensor) else arg for arg in args]
            if kwargs is None:
                kwargs = {}

            kwargs = {k: v.torch_tensor() if isinstance(v, ColoTensor) else v for k, v in kwargs.items()}
            return cls._filter_outputs_with_colo(func(*args, **kwargs))

    def backward(self, gradient: Optional[torch.Tensor] = None, retain_graph: bool = False):
        self._torch_tensor.backward(gradient=gradient, retain_graph=retain_graph)

    def __add__(self, o) -> "ColoTensor":
        if isinstance(o, ColoTensor):
            return ColoTensor.init_from_torch_tensor(self.torch_tensor() + o.torch_tensor())
        elif isinstance(o, torch.Tensor):
            return ColoTensor.init_from_torch_tensor(self.torch_tensor() + o)
        else:
            raise TypeError(f'{type(o)} is not supported in ColoTensor __add__')

    def __truediv__(self, o) -> "ColoTensor":
        return ColoTensor.init_from_torch_tensor(self.torch_tensor() / o)

    def __getattr__(self, name):

        def replace_tensor_with_colo(func):

            def execute_func(*args, **kwargs):
                # transform the ColoTensor args to torch Tensor.
                args = [arg.torch_tensor() if isinstance(arg, ColoTensor) else arg for arg in args]
                if kwargs is None:
                    kwargs = {}
                kwargs = {k: v.torch_tensor() if isinstance(v, ColoTensor) else v for k, v in kwargs.items()}
                return self._filter_outputs_with_colo(func(*args, **kwargs))

            return execute_func

        assert hasattr(self._torch_tensor, name), f"torch.Tensor has not attribute named as {name}. So is ColoTensor"
        attr = getattr(self._torch_tensor, name)

        if isinstance(attr, Callable):
            return replace_tensor_with_colo(attr)
        else:
            return attr

    @classmethod
    def _filter_outputs_with_colo(cls, outputs):
        if outputs is None:    # return None
            return None
        elif type(outputs) is not tuple:    # num of return val = 1
            return ColoTensor.init_from_torch_tensor(outputs) if type(outputs) is torch.Tensor else outputs
        else:    # num of return val > 1
            return tuple([
                ColoTensor.init_from_torch_tensor(output) if type(output) is torch.Tensor else output
                for output in outputs
            ])
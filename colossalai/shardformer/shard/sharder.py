import torch
import torch.nn as nn
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, Union, Callable
from .shardconfig import ShardConfig
from dataclasses import dataclass
from ..policies.basepolicy import Policy, Layer
from ..policies.autopolicy import get_autopolicy
from .slicer import Slicer
from ..utils.utils import hasattr_, setattr_, getattr_
import colossalai.nn as col_nn
from colossalai.logging import get_dist_logger
import os


logger = get_dist_logger()

class ModelSharder(object):
    """
    Shard the original huggingface model according to the policy

    Args:
        policy: The policy to shard the model
        model: The model to shard
        dist_setting: The setting of distributed model
    """
    def __init__(
            self,
            model: nn.Module,
            policy: Policy,
            shard_config: ShardConfig = None, # TODO
        ) -> None:
        self.model = model
        self.policy = get_autopolicy(self.model) if policy is None else policy
        self.slicer = Slicer(shard_config)
        self.shard_config = shard_config
        self.model_config = self.model.config
        self.binding_map = {}


    def shard(self) -> None:
        self.inject_model(self.model)
        self.replace_layer(self.model)
  
        
    def inject_model(
            self,
            model: nn.Module,
        ) -> None:
        """
        Replace the model to policy defined model
        Mainly modify the forward and backward to fit distributed model
        
        e.g.
            BertForMaskedLM.forward -> BertForMaskedLM_.forward
        """
        inject_policy = self.policy.inject_policy()

        org_model_cls = inject_policy[0]
        shard_model_cls = inject_policy[1]

        if model.__class__ == org_model_cls:
            for key in shard_model_cls.__dict__.keys():
                if hasattr(model.__class__, key):
                    setattr(
                        model.__class__,
                        key,
                        getattr(shard_model_cls,key),
                    )
        else:
            raise NotImplementedError(f"{model.__class__} is not implemented so far")


    def replace_layer(
            self,
            model: nn.Module,
        ) -> None:
        """
        Replace the layer according to the policy, and replace the layer one by one

        Args:
            layer: The layer to shard
        """
        argument_policies = self.policy.argument_policy(self.model_config, self.shard_config.world_size)
        for argument_policy in argument_policies.items():
            origin_layer_cls = argument_policy[0]
            attr_dict = argument_policy[1].attr_dict
            param_funcs = argument_policy[1].param_funcs
            binding_layers = argument_policy[1].binding_layers
            # if binding_layer is not None:
            #     self.binding_map[origin_layer_cls] = binding_layer
            self.reverse_replace_layer(model, origin_layer_cls, attr_dict, param_funcs, binding_layers)


    def reverse_replace_layer(
            self,
            layer: nn.Module,
            origin_cls: nn.Module,
            attr_dict: Dict[str, Any],
            param_funcs: List[Callable],
            binding_layers: List[nn.Module]
        ) -> None:
        """
        Reverse the replace layer operation

        Args:
            layer: The object of layer to shard
            origin_cls: The origin layer class
            attr_dict: The attribute dict to modify
            policy_cls: The policy class
        """
        for name, child in layer.named_children():
            if child.__class__ == origin_cls:
                # replac_layer = child
                for k, v in attr_dict.items():
                    setattr_(child, k, v, ignore=True)
                # print(f"Sharding {name} layer", replac_layer.attention.self.__dict__)
                # setattr_(layer, name, self.shard_one_layer(child, policy_cls))
                self.shard_one_layer(child, param_funcs, binding_layers)
                continue

            self.reverse_replace_layer(child, origin_cls, attr_dict, param_funcs, binding_layers)
        return layer


    def shard_one_layer(
            self, 
            org_layer: nn.Module, 
            param_funcs: List[Callable],
            binding_layers: List[nn.Module]
        ) -> None:
        """
        Shard one layer according to the policy, the layer should be the same class as the key in policy's argument_policy return dict

        Args:
            org_layer: The origin layer object to shard
            param_funcs: The function list to get shard information in policy class

        """
        # print(org_layer)
        for func in param_funcs:
            policy_layers = func()
            for policy_layer in policy_layers:
                weight = None
                bias = None
                weight_attr = policy_layer.weight
                bias_attr = policy_layer.bias
                replace_layer_cls = policy_layer.replace_layer
                ignore = policy_layer.ignore
                if policy_layer.__class__.__name__ == "Col_Layer":
                    gather_output = policy_layer.gather_output
                    print(gather_output)

                if weight_attr is not None:
                    if hasattr_(org_layer, weight_attr):
                        weight = getattr_(org_layer, weight_attr)
                    elif not ignore:
                        raise ValueError(f"Layer {org_layer.__class__.__qualname__} has no attribute {weight_attr}")

                if bias_attr is not None:
                    if hasattr_(org_layer, bias_attr):
                        bias = getattr_(org_layer, bias_attr)
                    elif not ignore:
                        raise ValueError(f"Layer {org_layer.__class__.__qualname__} has no attribute {bias_attr}")

                # dont have the attribute in policy, and ignore is true
                if weight is None and bias is None and ignore:
                    continue

                # set the sliced weight and bias to the new nn_col layer
                assert weight is not None or bias is not None
                layer_attr = (lambda x: x[:x.rfind(".")])(weight_attr or bias_attr)

                # slice weight and bias
                weight, bias = self.slicer.slice_weight_bias(weight, bias, policy_layer.__class__)
                print(os.environ['RANK'], policy_layer.__class__, weight.shape, bias.shape if bias is not None else None)
                # save the binding information
                for binding_layer in binding_layers:
                    self.binding_map[binding_layer] = dict(weight=weight, bias=bias)

                # create new object to replace the origin layer
                if replace_layer_cls is not None:
                    # print(f"RANK {os.environ['RANK']}: replace {getattr_(org_layer, layer_attr).__class__} to {replace_layer_cls}, shape is {weight.shape}")
                    if isinstance(getattr_(org_layer, layer_attr), nn.Linear):
                        if replace_layer_cls.__name__ == "Linear1D_Row":
                            replace_layer = replace_layer_cls(weight.shape[1], weight.shape[0], bias=False if bias is None else True)
                        elif replace_layer_cls.__name__ == "Linear1D_Col":
                            replace_layer = replace_layer_cls(weight.shape[0], weight.shape[1], bias=False if bias is None else True, gather_output=gather_output)
                        setattr_(org_layer, layer_attr, replace_layer, ignore=ignore)
                        self.set_param(replace_layer, weight, bias)
                    elif isinstance(getattr_(org_layer, layer_attr), nn.Embedding):    
                        replace_layer = replace_layer_cls(weight.shape[0], weight.shape[1], getattr_(org_layer, f"{layer_attr}.padding_idx", ignore=True))
                        setattr_(org_layer, layer_attr, replace_layer, ignore=ignore)
                        self.set_param(replace_layer, weight, bias)
                    else:
                        raise NotImplementedError(f"Replacing {getattr_(org_layer, layer_attr).__class__} is not implemented so far")
                # do not replace the layer object, just replace the weight and bias
                else:
                    self.set_param(org_layer, layer_attr, weight, bias)


    def set_param(
            self, 
            layer: Any, 
            layer_attr: str = "", 
            weight: torch.Tensor = None, 
            bias: torch.Tensor = None
        ) -> None:
        """
        Reset the weight and bias of the layer object

        Args:
            layer: The layer object
            layer_attr: The attribute name of the layer
            weight: The weight of the layer
            bias: The bias of the layer
        """
        assert weight is not None or bias is not None
        if weight is not None:
            setattr_(layer, "weight" if layer_attr == "" else layer_attr+".weight", nn.Parameter(weight))
            self.set_layer_size(layer, layer_attr, weight.shape)
        if bias is not None:
            setattr_(layer, "bias" if layer_attr == "" else layer_attr+".bias", nn.Parameter(bias))


    def set_layer_size(self, layer: nn.Module, layer_attr: str, size: torch.Size) -> None:
        """
        Set the layer attribute

        Args:
            layer: The layer object
            layer_attr: The attribute name of the layer
            size: Torch.size
        """
        # Tensor.shape[0] -> out_features, Tensor.shape[1] -> in_features
        attrs = ["out_features", "in_features"]
        for i, attr in enumerate(attrs):
            if hasattr_(layer, f"{layer_attr}.{attr}"):
                setattr_(layer, f"{layer_attr}.{attr}", size[i])    
import dataclasses
import typing
from typing import TYPE_CHECKING, Callable, Generic, Optional, Tuple, TypeVar

import equinox as eqx
import jax
import jmp
from jax import numpy as jnp
from jaxtyping import PRNGKeyArray, PyTree
from optax import GradientTransformation, OptState

from haliax.types import IntScalar, Scalar

from levanter.types import FilterTree
from levanter.utils.jax_utils import is_inexact_arrayish


M = TypeVar("M", bound=PyTree)
S = TypeVar("S")


if TYPE_CHECKING:
    from _typeshed import DataclassInstance  # type: ignore
else:
    DataclassInstance = typing.Any


def _ensure_int_is_array(x):
    # who tf decided that bools are ints
    if isinstance(x, int) and not isinstance(x, bool):
        return jnp.array(x)
    else:
        return x


class TrainerState(eqx.Module, Generic[M]):
    """
    This is the state of the trainer. It contains the model, optimizer state, and random key.
    It is an equinox Module because it is a PyTree that gets passed to the core `train_step` method
    of the Trainer. This unfortunately means that `step` is an Array and not an int, hence the IntScalar.

    It's designed to be extended by subclasses. Alternatively, you can implement your own trainer state
    that doesn't inherit from this class.
    """

    # I might be reinventing Flax.

    step: IntScalar = eqx.field(converter=_ensure_int_is_array)
    model: M
    optimizer: GradientTransformation = eqx.field(static=True)
    opt_state: OptState
    training_key: PRNGKeyArray

    is_trainable: FilterTree = eqx.field(static=True)
    mp: jmp.Policy = eqx.field(static=True)

    @property
    def int_step(self) -> int:
        """
        Returns the step as an int. On multinode, doing
        """
        return int(self.step)

    @property
    def trainable_model(self) -> M:
        return eqx.filter(self.model, self.is_trainable)

    @classmethod
    def init(
        cls,
        optimizer: GradientTransformation,
        model: M,
        *args,
        key: PRNGKeyArray,
        is_trainable: FilterTree = True,
        mp: Optional[jmp.Policy] = None,
        **kwargs,
    ) -> "TrainerState[M]":
        if mp is not None:
            model = cast_params_by_trainability(model, mp, is_trainable)
        else:
            mp = jmp.get_policy("f32")

        opt_state = init_optimizer_for_trainables(optimizer, model, is_trainable)
        return cls(0, model, optimizer, opt_state, key, is_trainable=is_trainable, mp=mp, *args, **kwargs)

    def take_step(self: S, grads: PyTree, obj_fun: Optional[Callable[[M], Scalar]] = None) -> S:
        assert isinstance(self, TrainerState)  # make mypy happy
        model, opt_state = take_train_step(
            self.optimizer,
            self.model,
            self.opt_state,
            grads,
            obj_fun=obj_fun,
            is_trainable=self.is_trainable,
        )
        return dataclasses.replace(self, model=model, opt_state=opt_state, step=self.step + 1)


def init_optimizer_for_trainables(optimizer, model, is_trainable):
    """
    Initializes the optimizer state for the trainable parameters of the model.
    """
    trainable = trainables_only(model, is_trainable)
    opt_state = optimizer.init(trainable)
    return opt_state


def _params_only(t):
    return eqx.filter(t, is_inexact_arrayish)


def _partition_trainable_params(model, filter):
    """
    Partitions the model into trainable and non-trainable parameters. This is used internally
    for the gradient calculation and checkpointing, but you can also use it to filter out params for logging
    or something.

    Returns:
        trainable, non-trainable
    """

    def trainable_and_diffable(pred):
        if callable(pred):
            return lambda x: pred(x) and is_inexact_arrayish(x)
        elif pred is True:
            return is_inexact_arrayish
        else:
            return pred

    combined_mask = jax.tree_util.tree_map(trainable_and_diffable, filter)
    return eqx.partition(model, combined_mask)


def trainables_only(model, filter):
    """
    Filters out non-trainable parameters from the model. This is used internally to
    for the optimizer state and to compute gradients, but you can also use it to filter out
    params for logging or something.
    """
    return _partition_trainable_params(model, filter)[0]


def cast_params_by_trainability(model, mp, is_trainable):
    """
    Casts the parameters of a model to the appropriate precision based on the is_trainable filter spec.
    Trainable parameters are cast to param precision, non-trainable parameters are cast to compute precision.
    """

    trainable, non_trainable = _partition_trainable_params(model, is_trainable)
    trainable = mp.cast_to_param(trainable)
    non_trainable = mp.cast_to_compute(non_trainable)
    model = eqx.combine(trainable, non_trainable)
    return model


def saveable_training_mask(trainer_state: S, is_trainable_param: FilterTree = True) -> FilterTree:
    """
    Returns a mask representing the saveable portion of a trainer state. This is used to filter out non-trainable
    parameters for checkpointing and for logging.
    """

    trainer_state = jax.tree_util.tree_map(lambda x: True, trainer_state)
    saveable_state = dataclasses.replace(trainer_state, model=is_trainable_param)  # type: ignore
    return saveable_state  # type: ignore


def take_train_step(
    optimizer,
    model: M,
    opt_state,
    grads,
    *,
    obj_fun: Optional[Callable[[M], Scalar]] = None,
    is_trainable: FilterTree = True,
) -> Tuple[M, OptState]:
    train_grads = trainables_only(grads, is_trainable)
    trainable_model = trainables_only(model, is_trainable)
    updates, opt_state = optimizer.update(train_grads, opt_state, params=trainable_model, obj_fn=obj_fun)
    model = eqx.apply_updates(model, updates)

    return model, opt_state
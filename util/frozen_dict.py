from __future__ import annotations
from typing import Any, Dict, Tuple, Iterator, Optional, Callable, Iterable, Union, Generic, TypeVar, overload

K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")

class FrozenDict(Generic[K, V]):
    """
    Immutable dict. Doesn't actually store the underlying dict as a field, instead its methods are closed over the
    underlying dict object.
    """

    __slots__ = ("___iter__", "___getitem__", "___len__", "___str__", "___repr__", "___eq__", "___ne__", "___or__",
        "___ror__", "___contains__", "_copy", "_get", "_items", "_keys", "_values")

    def __init__(self, *args: Any, **kwargs: Any):
        dct: Dict[K, V] = dict(*args, **kwargs)
        self.___iter__: Callable[[], Iterator[K]]
        self.___getitem__: Callable[[K], V]
        self.___len__: Callable[[], int]
        self.___str__: Callable[[], str]
        self.___repr__: Callable[[], str]
        self.___iter__ = lambda: dct.__iter__()
        self.___getitem__ = lambda index: dct.__getitem__(index)
        self.___len__ = lambda: dct.__len__()
        self.___str__ = lambda: "FrozenDict(" + dct.__str__() + ")"
        self.___repr__ = lambda: "FrozenDict(" + dct.__repr__() + ")"

        self.___eq__: Callable[[Any], bool]
        self.___ne__: Callable[[Any], bool]
        self.___eq__ = lambda other: other.___eq__(dct) if isinstance(other, FrozenDict) else dct.__eq__(other)
        self.___ne__ = lambda other: other.___ne__(dct) if isinstance(other, FrozenDict) else dct.__ne__(other)

        self.___or__: Callable[[Union[Dict[K, V], FrozenDict[K, V]]], FrozenDict[K, V]]
        self.___ror__: Callable[[Union[Dict[K, V], FrozenDict[K, V]]], FrozenDict[K, V]]
        self.___or__ = lambda other: FrozenDict(
            other.___ror__(dct) if isinstance(other, FrozenDict) else dct.__or__(other))
        self.___ror__ = lambda other: FrozenDict(
            other.___or__(dct) if isinstance(other, FrozenDict) else other.__ror__(dct)) # type: ignore
        # ^ typeshed doesn't know about dict.__ror__

        self.___contains__: Callable[[K], bool]
        self._copy: Callable[[], Dict[K, V]]
        self._get: Callable[[K, T], Union[V, T]]
        self._items: Callable[[], Iterable[Tuple[K, V]]]
        self._keys: Callable[[], Iterable[K]]
        self._values: Callable[[], Iterable[V]]
        self.___contains__ = lambda other: dct.__contains__(other)
        self._copy = lambda: dct.copy()
        self._get = lambda key, default: dct.get(key, default)
        self._items = lambda: dct.items()
        self._keys = lambda: dct.keys()
        self._values = lambda: dct.values()

    def __iter__(self) -> Iterator[K]: return self.___iter__()
    def __getitem__(self, index: K) -> V: return self.___getitem__(index)
    def __len__(self) -> int: return self.___len__()
    def __str__(self) -> str: return self.___str__()
    def __repr__(self) -> str: return self.___repr__()
    def __eq__(self, other: Any) -> bool: return self.___eq__(other)
    def __ne__(self, other: Any) -> bool: return self.___ne__(other)
    def __or__(self, other: Union[Dict[K, V], FrozenDict[K, V]]) -> FrozenDict[K, V]: return self.___or__(other)
    def __ror__(self, other: Union[Dict[K, V], FrozenDict[K, V]]) -> FrozenDict[K, V]: return self.___ror__(other)
    def __contains__(self, other: K) -> bool: return self.___contains__(other)
    def copy(self) -> Dict[K, V]: return self._copy()

    @overload
    def get(self, key: K, default: None = None, /) -> Optional[V]: ...
    @overload
    def get(self, key: K, default: T, /) -> Union[V, T]: ...
    def get(self, key: K, default: Any = None, /) -> Any: return self._get(key, default)

    def items(self) -> Iterable[Tuple[K, V]]: return self._items()
    def keys(self) -> Iterable[K]: return self._keys()
    def values(self) -> Iterable[V]: return self._values()

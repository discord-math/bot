from __future__ import annotations

from typing import Dict, Generic, Iterable, Iterator, Optional, Tuple, TypeVar, Union, overload

K = TypeVar("K")
V = TypeVar("V", covariant=True)
T = TypeVar("T")

class FrozenDict(Generic[K, V]):
    """
    Immutable dict. Doesn't actually store the underlying dict as a field, instead its methods are closed over the
    underlying dict object.
    """

    __slots__ = ("___iter__", "__getitem__", "__len__", "__str__", "__repr__", "__eq__", "__ne__", "__or__", "__ror__",
        "__contains__", "__reversed__", "copy", "get", "items", "keys", "values")

    def __init__(self, *args: object, **kwargs: object):
        dct: Dict[K, V] = dict(*args, **kwargs)
        def __iter__() -> Iterator[K]:
            return dct.__iter__()
        self.___iter__ = __iter__
        def __getitem__(key: K, /) -> V:
            return dct.__getitem__(key)
        self.__getitem__ = __getitem__
        def __len__() -> int:
            return dct.__len__()
        self.__len__ = __len__
        def __str__() -> str:
            return "FrozenDict({})".format(dct.__str__())
        self.__str__ = __str__
        def __repr__() -> str:
            return "FrozenDict({})".format(dct.__repr__())
        self.__repr__ = __repr__
        def __eq__(other: object, /) -> bool:
            return other.__eq__(dct) if isinstance(other, FrozenDict) else dct.__eq__(other)
        self.__eq__ = __eq__
        def __ne__(other: object, /) -> bool:
            return other.__ne__(dct) if isinstance(other, FrozenDict) else dct.__ne__(other)
        self.__ne__ = __ne__
        def __or__(other: Union[Dict[K, T], FrozenDict[K, T]], /) -> FrozenDict[K, Union[V, T]]:
            return other.__ror__(dct) if isinstance(other, FrozenDict) else FrozenDict(dct.__or__(other))
        self.__or__ = __or__
        def __ror__(other: Union[Dict[K, T], FrozenDict[K, T]], /) -> FrozenDict[K, Union[V, T]]:
            return other.__or__(dct) if isinstance(other, FrozenDict) else FrozenDict(dct.__ror__(other))
        self.__ror__ = __ror__
        def __contains__(key: object, /) -> bool:
            return dct.__contains__(key)
        self.__contains__ = __contains__
        def __reversed__() -> Iterator[K]:
            return dct.__reversed__()
        self.__reversed__ = __reversed__
        def copy() -> Dict[K, V]:
            return dct.copy()
        self.copy = copy
        @overload
        def get(key: K, /) -> Optional[V]: ...
        @overload
        def get(key: K, default: T, /) -> Union[V, T]: ...
        def get(key: K, default: Optional[T] = None) -> Optional[Union[V, T]]:
            return dct.get(key, default)
        self.get = get
        def items() -> Iterable[Tuple[K, V]]:
            return dct.items()
        self.items = items
        def keys() -> Iterable[K]:
            return dct.keys()
        self.keys = keys
        def values() -> Iterable[V]:
            return dct.values()
        self.values = values

    def __iter__(self) -> Iterator[K]:
        return self.___iter__()

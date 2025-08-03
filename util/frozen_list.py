from __future__ import annotations

from typing import Generic, Iterable, Iterator, List, Optional, SupportsIndex, TypeVar, Union, overload

import yaml


T = TypeVar("T", covariant=True)


class FrozenList(Generic[T]):
    """
    Immutable list. Doesn't actually store the underlying list as a field, instead its methods are closed over the
    underlying list object.
    """

    __slots__ = (
        "___iter__",
        "__getitem__",
        "___len__",
        "__str__",
        "__repr__",
        "__gt__",
        "__lt__",
        "__ge__",
        "__le__",
        "__eq__",
        "__ne__",
        "__mul__",
        "__rmul__",
        "__add__",
        "__radd__",
        "__contains__",
        "copy",
        "index",
        "count",
        "without",
    )

    def __init__(self, gen: Iterable[T] = (), /):
        lst = list(gen)

        def __iter__() -> Iterator[T]:
            return lst.__iter__()

        self.___iter__ = __iter__

        @overload
        def __getitem__(index: SupportsIndex, /) -> T: ...

        @overload
        def __getitem__(index: slice, /) -> FrozenList[T]: ...

        def __getitem__(index: Union[SupportsIndex, slice], /) -> Union[T, FrozenList[T]]:
            if isinstance(index, slice):
                return FrozenList(lst.__getitem__(index))
            else:
                return lst.__getitem__(index)

        self.__getitem__ = __getitem__

        def __len__() -> int:
            return lst.__len__()

        self.___len__ = __len__

        def __str__() -> str:
            return "FrozenList({})".format(lst.__str__())

        self.__str__ = __str__

        def __repr__() -> str:
            return "FrozenList({})".format(lst.__repr__())

        self.__repr__ = __repr__

        def __gt__(other: Union[List[T], FrozenList[T]], /) -> bool:
            return other.__lt__(lst) if isinstance(other, FrozenList) else lst.__gt__(other)

        self.__gt__ = __gt__

        def __lt__(other: Union[List[T], FrozenList[T]], /) -> bool:
            return other.__gt__(lst) if isinstance(other, FrozenList) else lst.__lt__(other)

        self.__lt__ = __lt__

        def __ge__(other: Union[List[T], FrozenList[T]], /) -> bool:
            return other.__le__(lst) if isinstance(other, FrozenList) else lst.__ge__(other)

        self.__ge__ = __ge__

        def __le__(other: Union[List[T], FrozenList[T]], /) -> bool:
            return other.__ge__(lst) if isinstance(other, FrozenList) else lst.__le__(other)

        self.__le__ = __le__

        def __eq__(other: object, /) -> bool:
            return other.__eq__(lst) if isinstance(other, FrozenList) else lst.__eq__(other)

        self.__eq__ = __eq__

        def __ne__(other: object, /) -> bool:
            return other.__ne__(lst) if isinstance(other, FrozenList) else lst.__ne__(other)

        self.__ne__ = __ne__

        def __mul__(other: SupportsIndex, /) -> FrozenList[T]:
            return FrozenList(lst.__mul__(other))

        self.__mul__ = __mul__

        def __rmul__(other: SupportsIndex, /) -> FrozenList[T]:
            return FrozenList(lst.__rmul__(other))

        self.__rmul__ = __rmul__

        def __add__(other: Union[List[T], FrozenList[T]], /) -> FrozenList[T]:
            return other.__radd__(lst) if isinstance(other, FrozenList) else FrozenList(lst.__add__(other))

        self.__add__ = __add__

        def __radd__(other: Union[List[T], FrozenList[T]], /) -> FrozenList[T]:
            return other.__add__(lst) if isinstance(other, FrozenList) else FrozenList(other.__add__(lst))

        self.__radd__ = __radd__

        def __contains__(other: object, /) -> bool:
            return lst.__contains__(other)

        self.__contains__ = __contains__

        def copy() -> List[T]:
            return lst.copy()

        self.copy = copy

        @overload
        def index(value: object, /) -> int: ...

        @overload
        def index(value: object, start: SupportsIndex, /) -> int: ...

        @overload
        def index(value: object, start: SupportsIndex, stop: SupportsIndex, /) -> int: ...

        def index(value: object, start: Optional[SupportsIndex] = None, stop: Optional[SupportsIndex] = None, /) -> int:
            if stop is None:
                if start is None:
                    return lst.index(value)  # type: ignore
                else:
                    return lst.index(value, start)  # type: ignore
            elif start is None:
                return lst.index(value, 0, stop)  # type: ignore
            else:
                return lst.index(value, start, stop)  # type: ignore

        self.index = index

        def count(other: object) -> int:
            return lst.count(other)  # type: ignore

        self.count = count

        def without(other: object) -> FrozenList[T]:
            return FrozenList(value for value in lst if value != other)

        self.without = without

    def __iter__(self) -> Iterator[T]:
        return self.___iter__()

    def __len__(self) -> int:
        return self.___len__()


yaml.add_representer(FrozenList, lambda dumper, data: dumper.represent_list(data))

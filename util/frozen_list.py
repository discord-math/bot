from __future__ import annotations
from typing import Any, List, Iterator, Callable, Iterable, Union, Generic, TypeVar

T = TypeVar("T")

class FrozenList(Generic[T]):
    """
    Immutable list. Doesn't actually store the underlying list as a field, instead its methods are closed over the
    underlying list object.
    """

    __slots__ = ("___iter__", "___getitem__", "___len__", "___str__", "___repr__", "___gt__", "___lt__", "___ge__",
        "___le__", "___eq__", "___ne__", "___mul__", "___rmul__", "___add__", "___radd__", "___contains__", "_copy",
        "_index", "_count")

    def __init__(self, gen: Iterable[T] =(), /):
        lst = list(gen)
        self.___iter__: Callable[[], Iterator[T]]
        self.___getitem__: Callable[[int], T]
        self.___len__: Callable[[], int]
        self.___str__: Callable[[], str]
        self.___repr__: Callable[[], str]
        self.___iter__ = lambda: lst.__iter__()
        self.___getitem__ = lambda index: lst.__getitem__(index)
        self.___len__ = lambda: lst.__len__()
        self.___str__ = lambda: "FrozenList(" + lst.__str__() + ")"
        self.___repr__ = lambda: "FrozenList(" + lst.__repr__() + ")"

        self.___gt__: Callable[[Union[List[T], FrozenList[T]]], bool]
        self.___lt__: Callable[[Union[List[T], FrozenList[T]]], bool]
        self.___ge__: Callable[[Union[List[T], FrozenList[T]]], bool]
        self.___le__: Callable[[Union[List[T], FrozenList[T]]], bool]
        self.___eq__: Callable[[Any], bool]
        self.___ne__: Callable[[Any], bool]
        self.___gt__ = lambda other: other.___lt__(lst) if isinstance(other, FrozenList) else lst.__gt__(other)
        self.___lt__ = lambda other: other.___gt__(lst) if isinstance(other, FrozenList) else lst.__lt__(other)
        self.___ge__ = lambda other: other.___le__(lst) if isinstance(other, FrozenList) else lst.__ge__(other)
        self.___le__ = lambda other: other.___ge__(lst) if isinstance(other, FrozenList) else lst.__le__(other)
        self.___eq__ = lambda other: other.___eq__(lst) if isinstance(other, FrozenList) else lst.__eq__(other)
        self.___ne__ = lambda other: other.___ne__(lst) if isinstance(other, FrozenList) else lst.__ne__(other)

        self.___mul__: Callable[[int], FrozenList[T]]
        self.___rmul__: Callable[[int], FrozenList[T]]
        self.___add__: Callable[[Union[List[T], FrozenList[T]]], FrozenList[T]]
        self.___radd__: Callable[[Union[List[T], FrozenList[T]]], FrozenList[T]]
        self.___mul__ = lambda other: FrozenList(lst.__mul__(other))
        self.___rmul__ = lambda other: FrozenList(lst.__rmul__(other))
        self.___add__ = lambda other: (
            other.___radd__(lst) if isinstance(other, FrozenList) else FrozenList(lst.__add__(other)))
        self.___radd__ = lambda other: (
            other.___add__(lst) if isinstance(other, FrozenList) else FrozenList(other.__add__(lst)))

        self.___contains__: Callable[[T], bool]
        self._copy: Callable[[], List[T]]
        self._index: Callable[..., int]
        self._count: Callable[[T], int]
        self.___contains__ = lambda other: lst.__contains__(other)
        self._copy = lambda: lst.copy()
        self._index = lambda *args: lst.index(*args)
        self._count = lambda x: lst.count(x)

    def __iter__(self) -> Iterator[T]: return self.___iter__()
    def __getitem__(self, index: int) -> T: return self.___getitem__(index)
    def __len__(self) -> int: return self.___len__()
    def __str__(self) -> str: return self.___str__()
    def __repr__(self) -> str: return self.___repr__()
    def __gt__(self, other: Union[List[T], FrozenList[T]]) -> bool: return self.___gt__(other)
    def __lt__(self, other: Union[List[T], FrozenList[T]]) -> bool: return self.___lt__(other)
    def __ge__(self, other: Union[List[T], FrozenList[T]]) -> bool: return self.___ge__(other)
    def __le__(self, other: Union[List[T], FrozenList[T]]) -> bool: return self.___le__(other)
    def __eq__(self, other: Any) -> bool: return self.___eq__(other)
    def __ne__(self, other: object) -> bool: return self.___ne__(other)
    def __mul__(self, other: int) -> FrozenList[T]: return self.___mul__(other)
    def __rmul__(self, other: int) -> FrozenList[T]: return self.___rmul__(other)
    def __add__(self, other: Union[List[T], FrozenList[T]]) -> FrozenList[T]: return self.___add__(other)
    def __radd__(self, other: Union[List[T], FrozenList[T]]) -> FrozenList[T]: return self.___radd__(other)
    def __contains__(self, other: T) -> bool: return self.___contains__(other)
    def copy(self) -> List[T]: return self._copy()
    def index(self, *args: Any) -> int: return self._index(*args)
    def count(self, x: T, /) -> int: return self._count(x)

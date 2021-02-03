class FrozenList():
    def __init__(self, gen=(), /):
        lst = list(gen)
        self.___iter__ = lambda: lst.__iter__()
        self.___getitem__ = lambda index: lst.__getitem__(index)
        self.___len__ = lambda: lst.__len__()
        self.___str__ = lambda: "FrozenList(" + lst.__str__() + ")"
        self.___repr__ = lambda: "FrozenList(" + lst.__repr__() + ")"
        self.___gt__ = lambda other: (
            other.___lt__(lst) if isinstance(other, FrozenList)
            else lst.__gt__(other))
        self.___lt__ = lambda other: (
            other.___gt__(lst) if isinstance(other, FrozenList)
            else lst.__lt__(other))
        self.___ge__ = lambda other: (
            other.___le__(lst) if isinstance(other, FrozenList)
            else lst.__ge__(other))
        self.___le__ = lambda other: (
            other.___ge__(lst) if isinstance(other, FrozenList)
            else lst.__le__(other))
        self.___eq__ = lambda other: (
            other.___eq__(lst) if isinstance(other, FrozenList)
            else lst.__eq__(other))
        self.___ne__ = lambda other: (
            other.___ne__(lst) if isinstance(other, FrozenList)
            else lst.__ne__(other))
        self.___mul__ = lambda other: FrozenList(lst.__mul__(other))
        self.___rmul__ = lambda other: FrozenList(lst.__rmul__(other))
        self.___add__ = lambda other: FrozenList(
            other.___radd__(lst) if isinstance(other, FrozenList)
            else lst.__add__(other))
        self.___radd__ = lambda other: FrozenList(
            other.___add__(lst) if isinstance(other, FrozenList)
            else other.__add__(lst))
        self.___contains__ = lambda other: lst.__contains__(other)
        self._copy = lambda: lst.copy()
        self._index = lambda *args: lst.index(*args)
        self._count = lambda x: lst.count(x)

    def __iter__(self): return self.___iter__()
    def __getitem__(self, index): return self.___getitem__(index)
    def __len__(self): return self.___len__()
    def __str__(self): return self.___str__()
    def __repr__(self): return self.___repr__()
    def __gt__(self, other): return self.___gt__(other)
    def __lt__(self, other): return self.___lt__(other)
    def __ge__(self, other): return self.___ge__(other)
    def __le__(self, other): return self.___le__(other)
    def __eq__(self, other): return self.___eq__(other)
    def __ne__(self, other): return self.___ne__(other)
    def __mul__(self, other): return self.___mul__(other)
    def __rmul__(self, other): return self.___rmul__(other)
    def __add__(self, other): return self.___add__(other)
    def __radd__(self, other): return self.___radd__(other)
    def __contains__(self, other): return self.___contains__(other)
    def copy(self): return self._copy()
    def index(self, *args): return self._index(*args)
    def count(self, x, /): return self._count(x)

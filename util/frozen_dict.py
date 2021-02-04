class FrozenDict:
    __slots__ = ("___iter__", "___getitem__", "___len__", "___str__",
        "___repr__", "___eq__", "___ne__", "___or__", "___ror__",
        "___contains__", "_copy", "_get", "_items", "_keys", "_values")

    def __init__(self, *args, **kwargs):
        dct = dict(*args, **kwargs)
        self.___iter__ = lambda: dct.__iter__()
        self.___getitem__ = lambda index: dct.__getitem__(index)
        self.___len__ = lambda: dct.__len__()
        self.___str__ = lambda: "FrozenDict(" + dct.__str__() + ")"
        self.___repr__ = lambda: "FrozenDict(" + dct.__repr__() + ")"
        self.___eq__ = lambda other: (
            other.___eq__(dct) if isinstance(other, FrozenDict)
            else dct.__eq__(other))
        self.___ne__ = lambda other: (
            other.___ne__(dct) if isinstance(other, FrozenDict)
            else dct.__ne__(other))
        self.___or__ = lambda other: FrozenDict(
            other.___ror__(dct) if isinstance(other, FrozenDict)
            else dct.__or__(other))
        self.___ror__ = lambda other: FrozenDict(
            other.___or__(dct) if isinstance(other, FrozenDict)
            else other.__ror__(dct))
        self.___contains__ = lambda other: dct.__contains__(other)
        self._copy = lambda: dct.copy()
        self._get = lambda key, default: dct.get(key, default)
        self._items = lambda: dct.items()
        self._keys = lambda: dct.keys()
        self._values = lambda: dct.values()

    def __iter__(self): return self.___iter__()
    def __getitem__(self, index): return self.___getitem__(index)
    def __len__(self): return self.___len__()
    def __str__(self): return self.___str__()
    def __repr__(self): return self.___repr__()
    def __eq__(self, other): return self.___eq__(other)
    def __ne__(self, other): return self.___ne__(other)
    def __or__(self, other): return self.___or__(other)
    def __ror__(self, other): return self.___ror__(other)
    def __contains__(self, other): return self.___contains__(other)
    def copy(self): return self._copy()
    def get(self, key, default=None, /): return self._get(key, default)
    def items(self): return self._items()
    def keys(self): return self._keys()
    def values(self): return self._values()

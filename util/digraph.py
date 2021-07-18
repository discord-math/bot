from __future__ import annotations
from typing import TypeVar, Generic, Dict, Set, Iterator

T = TypeVar("T")

class Digraph(Generic[T]):
    """A directed graph with no isolated vertices and no duplicate edges."""

    __slots__ = "fwd", "bck"
    fwd: Dict[T, Set[T]]
    bck: Dict[T, Set[T]]

    def __init__(self) -> None:
        """Create an empty graph."""
        self.fwd = {}
        self.bck = {}

    def add_edge(self, x: T, y: T) -> None:
        """Add an edge from x to y."""
        if x not in self.fwd:
            self.fwd[x] = set()
        self.fwd[x].add(y)
        if y not in self.bck:
            self.bck[y] = set()
        self.bck[y].add(x)

    def edges_to(self, x: T) -> Set[T]:
        """Return a (read-only) set of edges into x."""
        return self.bck[x] if x in self.bck else set()

    def edges_from(self, x: T) -> Set[T]:
        """Return a (read-only) set of edges from x."""
        return self.fwd[x] if x in self.fwd else set()

    def subgraph_paths_from(self, x: T) -> Digraph[T]:
        """
        Return an induced subgraph of exactly those vertices that can be reached from x via a path.
        """
        graph: Digraph[T] = Digraph()
        seen: Set[T] = set()
        def dfs(x: T) -> None:
            if x in seen:
                return
            seen.add(x)
            if x in self.fwd:
                for y in self.fwd[x]:
                    graph.add_edge(x, y)
                    dfs(y)
        dfs(x)
        return graph

    def subgraph_paths_to(self, x: T) -> Digraph[T]:
        """
        Return an induced subgraph of exactly those vertices that can reach x via a path.
        """
        graph: Digraph[T] = Digraph()
        seen: Set[T] = set()
        def dfs(x: T) -> None:
            if x in seen:
                return
            seen.add(x)
            if x in self.bck:
                for y in self.bck[x]:
                    graph.add_edge(y, x)
                    dfs(y)
        dfs(x)
        return graph

    def topo_sort_fwd(self, sources: Set[T] = set()) -> Iterator[T]:
        """
        Iterate through vertices in such a way that whenever there is an edge from x to y, x will come up earlier in
        iteration than y. The sources are forcibly included in the iteration.
        """
        seen: Set[T] = set()
        def dfs(x: T) -> Iterator[T]:
            if x in seen:
                return
            seen.add(x)
            if x in self.bck:
                for y in self.bck[x]:
                    yield from dfs(y)
            yield x
        for x in self.fwd:
            yield from dfs(x)
        for x in self.bck:
            yield from dfs(x)
        for x in sources:
            yield from dfs(x)

    def topo_sort_bck(self, sources: Set[T] = set()) -> Iterator[T]:
        """
        Iterate through vertices in such a way that whenever there is an edge from x to y, x will come up later in
        iteration than y. The sources are forcibly included in the iteration.
        """
        seen: Set[T] = set()
        def dfs(x: T) -> Iterator[T]:
            if x in seen:
                return
            seen.add(x)
            if x in self.fwd:
                for y in self.fwd[x]:
                    yield from dfs(y)
            yield x
        for x in self.bck:
            yield from dfs(x)
        for x in self.fwd:
            yield from dfs(x)
        for x in sources:
            yield from dfs(x)

    def del_edges_from(self, x: T) -> None:
        """
        Delete all edges from x.
        """
        if x in self.fwd:
            for y in self.fwd[x]:
                self.bck[y].discard(x)
                if not self.bck[y]:
                    del self.bck[y]
            del self.fwd[x]

    def del_edges_to(self, x: T) -> None:
        """
        Delete all edges into x.
        """
        if x in self.bck:
            for y in self.bck[x]:
                self.fwd[y].discard(x)
                if not self.fwd[y]:
                    del self.fwd[y]
            del self.bck[x]

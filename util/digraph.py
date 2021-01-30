class Digraph():
    def __init__(self):
        self.fwd = {}
        self.bck = {}

    def add_edge(self, x, y):
        if x not in self.fwd:
            self.fwd[x] = set()
        self.fwd[x].add(y)
        if y not in self.bck:
            self.bck[y] = set()
        self.bck[y].add(x)

    def edges_to(self, x):
        return self.bck[x] if x in self.bck else set()

    def edges_from(self, x):
        return self.fwd[x] if x in self.fwd else set()

    def subgraph_paths_to(self, x):
        graph = Digraph()
        seen = set()
        def dfs(x):
            if x in seen:
                return
            seen.add(x)
            if x in self.bck:
                for y in self.bck[x]:
                    graph.add_edge(y, x)
                    dfs(y)
        dfs(x)
        return graph

    def topo_sort_fwd(self):
        seen = set()
        def dfs(x):
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

    def topo_sort_bck(self):
        seen = set()
        def dfs(x):
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

    def del_edges_from(self, x):
        if x in self.fwd:
            for y in self.fwd[x]:
                self.bck[y].discard(x)
            del self.fwd[x]

    def del_edges_to(self, x):
        if x in self.bck:
            for y in self.bck[x]:
                self.fwd[y].discard(x)
            del self.bck[x]

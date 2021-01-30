import importlib.machinery
import importlib.util
import builtins
import types
import sys
import util.digraph

plugins_namespace = "plugins"
def is_plugin(name):
    return name.startswith(plugins_namespace + ".")

deps = util.digraph.Digraph()
import_stack = []

def trace_import(name, globals=None, locals=None, fromlist=(), level=0):
    last = import_stack[-1]
    name_parts = name.split(".")
    for i in range(1, len(name_parts) + 1):
        parent = ".".join(name_parts[:i])
        if is_plugin(parent):
            deps.add_edge(last, parent)
    return builtins.__import__(name, globals, locals, fromlist, level)

trace_builtins = types.ModuleType(builtins.__name__)
trace_builtins.__dict__.update(builtins.__dict__)
trace_builtins.__import__ = trace_import


class PluginLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, mod):
        mod.__builtins__ = trace_builtins
        import_stack.append(mod.__name__)
        try:
            importlib.machinery.SourceFileLoader.exec_module(self, mod)
        except:
            deps.del_edges_from(mod.__name__)
            raise
        finally:
            import_stack.pop()

class PluginFinder(importlib.machinery.PathFinder):
    @classmethod
    def find_spec(self, name, path=None, target=None):
        name_parts = name.split(".")
        if not is_plugin(name):
            return
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        if spec == None:
            return
        spec.loader = PluginLoader(spec.loader.name, spec.loader.path)
        return spec

for i in range(len(sys.meta_path)):
    if sys.meta_path[i] == importlib.machinery.PathFinder:
        sys.meta_path.insert(i, PluginFinder)

finalizers = {}

def finalizer(fin):
    if not len(import_stack):
        raise ValueError("not called during plugin initialization")
    name = import_stack[-1]
    if name not in finalizers:
        finalizers[name] = []
    finalizers[name].append(fin)
    return fin

def unsafe_unload(name):
    if not is_plugin(name):
        raise ValueError(name + " is not a plugin")
    gen = finalizers[name].__iter__() if name in finalizers else ()
    def cont_finalizers():
        try:
            for fin in gen:
                fin()
        except:
            cont_finalizers()
            raise
        del finalizers[name]
        del sys.modules[name]
        deps.del_edges_from(name)
    cont_finalizers()

def unload(name):
    gen = deps.subgraph_paths_to(name).topo_sort_fwd()
    def cont_unload():
        try:
            for dep in gen:
                if dep != name:
                    unsafe_unload(dep)
        except:
            cont_unload()
            raise
        unsafe_unload(name)
    cont_unload()

def reload(name):
    reloads = deps.subgraph_paths_to(name)
    unload_success = set()
    reload_success = set()
    unload_gen = reloads.topo_sort_fwd()
    reload_gen = reloads.topo_sort_bck()
    def cont_reload():
        try:
            for dep in reload_gen:
                if (dep in unload_success and
                    all(m in reload_success for m in reloads.edges_from(dep))):
                    importlib.import_module(dep)
                    reload_success.add(dep)
        except:
            cont_reload()
            raise
    def cont_unload():
        try:
            for dep in unload_gen:
                if dep != name:
                    unsafe_unload(dep)
                    unload_success.add(dep)
        except:
            cont_unload()
            raise
        try:
            unsafe_unload(name)
        except:
            cont_reload()
            raise
        try:
            ret = importlib.import_module(name)
            reload_success.add(name)
        except:
            cont_reload()
            raise
        cont_reload()
        return ret
    return cont_unload()

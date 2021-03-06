"""
This module makes all modules in a specified namespace (plugins_namespace) behave slightly differently.

We track whenever one plugin imports another, and keep a dependency graph around. We provide high level unload/reload
functions that will unload/reload dependent plugins as well. We also provide a way for a plugin to hook a "finalizer"
to be executed when the plugin is to be unloaded.
"""

import logging
import importlib.machinery
import importlib.util
import pkgutil
import builtins
import types
import sys
import atexit
from typing import Any, List, Dict, Optional, Union, Callable, Iterable, Sequence, Type, cast
import util.digraph

# Allow importing plugins from other directories on the path
__path__: List[str]
__path__  = pkgutil.extend_path(__path__, __name__)

logger: logging.Logger
logger = logging.getLogger(__name__)

plugins_namespace = "plugins"
def is_plugin(name: str) -> bool:
    return name.startswith(plugins_namespace + ".")

deps: util.digraph.Digraph[str] = util.digraph.Digraph()
import_stack: List[str] = []

def current_plugin() -> str:
    """
    In the lexical scope of the plugin, __name__ will refer to the plugin's name. This function can be used to get the
    name of the plugin in the *dynamic* scope of its initialization. In other words functions outside a plugin can get
    to know its name if they were called during plugin initialization.
    """
    if not len(import_stack):
        raise ValueError("not called during plugin initialization")
    return import_stack[-1]

def trace_import(name: str, globals: Optional[Dict[str, Any]] = None, locals: Optional[Dict[str, Any]] = None,
    fromlist: Sequence[str] = (), level: int = 0) -> Any:
    name_parts = name.split(".")
    for i in range(1, len(name_parts) + 1):
        parent = ".".join(name_parts[:i])
        if is_plugin(parent):
            deps.add_edge(current_plugin(), parent)
            logger.debug("{} depends on {}".format(current_plugin(), parent))
    return builtins.__import__(name, globals, locals, fromlist, level)

trace_builtins = types.ModuleType(builtins.__name__)
trace_builtins.__dict__.update(builtins.__dict__)
trace_builtins.__dict__["__import__"] = trace_import

finalizers: Dict[str, List[Callable[[], None]]] = {}

def finalizer(fin: Callable[[], None]) -> Callable[[], None]:
    """
    A decorator for registering a finalizer, which will be called during unloading/reloading of a plugin. E.g.:

        log = open("log", "w")
        @plugins.finalizer
        def close_log():
            log.close()

    If a module initialization fails to complete, the finalizers that managed to register will be called.
    """

    current = current_plugin()
    if current not in finalizers:
        finalizers[current] = []
    finalizers[current].append(fin)
    return fin

def finalize_module(name: str) -> None:
    logger.debug("Finalizing {}".format(name))
    if name not in finalizers:
        return
    gen = finalizers[name].__iter__()
    def cont_finalizers() -> None:
        try:
            for fin in gen:
                fin()
        except:
            logger.error("Error in finalizer of {}".format(name), exc_info=True)
            cont_finalizers()
            raise
        del finalizers[name]
    cont_finalizers()

class PluginLoader(importlib.machinery.SourceFileLoader):
    __slots__ = ()
    def exec_module(self, mod: types.ModuleType) -> None:
        name = mod.__name__
        mod.__dict__["__builtins__"] = trace_builtins
        import_stack.append(name)
        try:
            logger.debug("Executing {}".format(name))
            super().exec_module(mod)
        except:
            logger.error("Error during execution of {}".format(name), exc_info=True)
            try:
                finalize_module(name)
            finally:
                deps.del_edges_from(name)
            raise
        finally:
            import_stack.pop()

class PluginFinder(importlib.machinery.PathFinder):
    __slots__ = ()
    @classmethod
    def find_spec(self, name: str, path: Optional[Sequence[Union[bytes, str]]] = None,
        target: Optional[types.ModuleType] = None) -> Optional[importlib.machinery.ModuleSpec]:
        name_parts = name.split(".")
        if not is_plugin(name):
            return None
        spec = super().find_spec(name, path, target)
        if spec is None:
            return None
        spec.loader = PluginLoader(spec.loader.name, spec.loader.path) # type: ignore
        return spec

for i in range(len(sys.meta_path)):
    # typeshed for sys.meta_path is incorrect
    if sys.meta_path[i] == importlib.machinery.PathFinder: # type: ignore
        sys.meta_path.insert(i, PluginFinder) # type: ignore

def unsafe_unload(name: str) -> None:
    """
    Finalize and unload a single plugin. May break any plugins that depend on it. All finalizers will be executed even
    if some raise exceptions, if there were any they will all be reraised together.
    """
    if not is_plugin(name):
        raise ValueError(name + " is not a plugin")
    try:
        logger.debug("Unloading {}".format(name))
        finalize_module(name)
    finally:
        deps.del_edges_from(name)
        del sys.modules[name]

def unload(name: str) -> None:
    """
    Finalize and unload a plugin and any plugins that (transitively) depend on it. All finalizers will be executed even
    if some raise exceptions, if there were any they will all be reraised together.
    """
    logger.info("Unloading {} with dependencies: {}".format(name,
        ", ".join(dep for dep in deps.subgraph_paths_to(name).topo_sort_fwd() if dep != name)))
    gen = deps.subgraph_paths_to(name).topo_sort_fwd()
    def cont_unload() -> None:
        try:
            for dep in gen:
                if dep == name:
                    continue
                unsafe_unload(dep)
        except:
            cont_unload()
            raise
        unsafe_unload(name)
    cont_unload()

def unsafe_reload(name: str) -> types.ModuleType:
    """
    Finalize and reload a single plugin. This will run the new plugin code over the same module object, which may break
    any plugins that depend on it. All finalizers will be executed even if some raise exceptions. If there were any or
    if there was an exception during reinitialization, they will all be reraised together. If plugin initialization
    raises an exception the plugin remains loaded but may be in a half-updated state. Its finalizers aren't run
    immediately. Returns the module object if successful.
    """
    if not is_plugin(name):
        raise ValueError(name + " is not a plugin")
    try:
        logger.info("Reloading {} inplace".format(name))
        finalize_module(name)
    finally:
        deps.del_edges_from(name)
        ret = importlib.reload(sys.modules[name])
    return ret

def reload(name: str) -> types.ModuleType:
    """
    Finalize and reload a plugin and any plugins that (transitively) depend on it. We try to run all finalizers in
    dependency order, and only load plugins that were successfully unloaded, and whose dependencies have been
    successfully reloaded. If a plugin fails to initialize, we run any finalizers it managed to register, and the plugin
    is not loaded. Any exceptions raised will be reraised together. Returns the module object of the requested plugin if
    successful.
    """
    reloads = deps.subgraph_paths_to(name)
    logger.info("Reloading {} with dependencies: {}".format(name,
        ", ".join(dep for dep in reloads.topo_sort_fwd() if dep != name)))
    unload_success = set()
    reload_success = set()
    unload_gen = reloads.topo_sort_fwd()
    reload_gen = reloads.topo_sort_bck()
    def cont_reload() -> None:
        try:
            for dep in reload_gen:
                if dep == name:
                    continue
                elif dep not in unload_success:
                    logger.info("Not reloading {} because it was not unloaded properly".format(name))
                elif not all(m in reload_success
                    for m in reloads.edges_from(dep)):
                    logger.info("Not reloading {} because its dependencies were not reloaded properly".format(name))
                else:
                    importlib.import_module(dep)
                    reload_success.add(dep)
        except:
            cont_reload()
            raise
    def cont_unload() -> types.ModuleType:
        try:
            for dep in unload_gen:
                if dep == name:
                    continue
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
        finally:
            cont_reload()
        return ret
    return cont_unload()

def load(name: str) -> types.ModuleType:
    """
    Load a single plugin. If it's already loaded, nothing is changed. If there was an exception during initialization,
    the finalizers that managed to registers will be run. Returns the module object if successful.
    """
    if not is_plugin(name):
        raise ValueError(name + " is not a plugin")
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)

@atexit.register
def atexit_unload() -> None:
    unload_list = list(deps.topo_sort_fwd())
    unload_list.extend(name for name in finalizers if name not in unload_list)
    unload_gen = unload_list.__iter__()
    def cont_unload() -> None:
        try:
            for dep in unload_gen:
                unsafe_unload(dep)
        except:
            cont_unload()
            raise
    cont_unload()

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
import asyncio
import contextlib
import inspect
import types
import sys
import atexit
from typing import Any, List, Dict, Set, Optional, Union, Callable, Iterator, Awaitable, Sequence, TypeVar, cast
import util.digraph

# Allow importing plugins from other directories on the path
__path__: List[str]
__path__  = pkgutil.extend_path(__path__, __name__)

# We don't manipulate the dependency graph/module table concurrently, so this lock is taken in all public entry points
lock: asyncio.Lock = asyncio.Lock()

logger: logging.Logger = logging.getLogger(__name__)

plugins_namespace = "plugins"
def is_plugin(name: str) -> bool:
    return name.startswith(plugins_namespace + ".")

deps: util.digraph.Digraph[str] = util.digraph.Digraph()
import_stack: List[str] = []
# plugins that threw during execution and so might have registered initializers/finalizers but didn't get into sys.modules
dirty: Set[str] = set()

@contextlib.contextmanager
def push_plugin(name: str) -> Iterator[None]:
    import_stack.append(name)
    try:
        yield
    finally:
        import_stack.pop()

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

initializers: Dict[str, List[Callable[[], Awaitable[None]]]] = {}

T = TypeVar("T", bound=Union[Callable[[], None], Callable[[], Awaitable[None]]])

def init(init: T) -> T:
    """
    A decorator for registering an async initializer, which will be called after the module is loaded. Initializers are
    called in order, and if the initializer fails, subsequent initializers will not be called, and finalizers registered
    so far will be called.
    """
    if inspect.iscoroutinefunction(init):
        async_init = cast(Callable[[], Awaitable[None]], init)
    else:
        async def async_init() -> None:
            init()

    current = current_plugin()
    if current not in initializers:
        initializers[current] = []
    initializers[current].append(async_init)
    return init

finalizers: Dict[str, List[Callable[[], Awaitable[None]]]] = {}

def finalizer(fin: T) -> T:
    """
    A decorator for registering a finalizer, which will be called during unloading/reloading of a plugin. E.g.:

        log = open("log", "w")
        @plugins.finalizer
        def close_log():
            log.close()

    If a module initialization fails to complete, the finalizers that managed to register will be called. A finalizer
    can be an async function.
    """
    if inspect.iscoroutinefunction(fin):
        async_fin = cast(Callable[[], Awaitable[None]], fin)
    else:
        async def async_fin() -> None:
            fin()

    current = current_plugin()
    if current not in finalizers:
        finalizers[current] = []
    finalizers[current].append(async_fin)
    return fin

async def initialize_module(name: str) -> None:
    logger.debug("Initializing {}".format(name))
    if name not in initializers:
        return
    gen = iter(initializers[name])
    async def cont_initializers() -> None:
        try:
            for init in gen:
                await init()
        except:
            logger.error("Error in initializer of {}".format(name), exc_info=True)
            await cont_initializers()
            raise
        del initializers[name]
    with push_plugin(name):
        await cont_initializers()

async def finalize_module(name: str) -> None:
    logger.debug("Finalizing {}".format(name))
    if name not in finalizers:
        return
    gen = iter(finalizers[name])
    async def cont_finalizers() -> None:
        try:
            for fin in gen:
                await fin()
        except:
            logger.error("Error in finalizer of {}".format(name), exc_info=True)
            await cont_finalizers()
            raise
        del finalizers[name]
    await cont_finalizers()

class PluginLoader(importlib.machinery.SourceFileLoader):
    __slots__ = ()
    def exec_module(self, mod: types.ModuleType) -> None:
        name = mod.__name__
        mod.__dict__["__builtins__"] = trace_builtins
        try:
            with push_plugin(name):
                logger.debug("Executing {}".format(name))
                super().exec_module(mod)
        except:
            logger.error("Error during execution of {}".format(name), exc_info=True)
            dirty.add(name)
            raise

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

async def do_unload(name: str, is_dirty: bool = False) -> None:
    try:
        if is_dirty:
            logger.debug("Unloading {} (dirty)".format(name))
        else:
            logger.debug("Unloading {}".format(name))
        await finalize_module(name)
    finally:
        deps.del_edges_from(name)
        if dirty:
            dirty.remove(name)
        else:
            del sys.modules[name]

async def do_load(name: str) -> types.ModuleType:
    logger.debug("Loading {}".format(name))
    try:
        ret = importlib.import_module(name)
    finally:
        inits = deps.subgraph_paths_from(name)
        init_success = set()
        gen = inits.topo_sort_bck(sources={name})
        async def cont_init() -> None:
            try:
                for dep in gen:
                    if dep in dirty:
                        continue
                    elif not all(m in init_success for m in inits.edges_from(dep)):
                        logger.debug(
                            "Not initializing {} because its dependencies were not initialized properly".format(dep))
                        continue
                    await initialize_module(dep)
                    init_success.add(dep)
            except:
                await cont_init()
                raise
        try:
            await cont_init()
        finally:
            gen = inits.topo_sort_fwd(sources={name})
            async def cont_fin() -> None:
                try:
                    for dep in gen:
                        if dep in dirty:
                            await do_unload(dep, is_dirty=True)
                        elif not all(m in init_success for m in inits.edges_from(dep)):
                            logger.debug(
                                "Unloading {} because its dependencies were not initialized properly".format(dep))
                            await do_unload(dep)
                        elif dep not in init_success:
                            await do_unload(dep)
                except:
                    await cont_fin()
                    raise
            await cont_fin()
    return ret

async def unsafe_unload(name: str) -> None:
    """
    Finalize and unload a single plugin. May break any plugins that depend on it. All finalizers will be executed even
    if some raise exceptions, if there were any they will all be reraised together.
    """
    async with lock:
        if not is_plugin(name):
            raise ValueError(name + " is not a plugin")
        await do_unload(name)

async def unload(name: str) -> None:
    """
    Finalize and unload a plugin and any plugins that (transitively) depend on it. All finalizers will be executed even
    if some raise exceptions, if there were any they will all be reraised together.
    """
    async with lock:
        logger.debug("Unloading {} with dependencies: {}".format(name,
            ", ".join(dep for dep in deps.subgraph_paths_to(name).topo_sort_fwd() if dep != name)))
        gen = deps.subgraph_paths_to(name).topo_sort_fwd()
        async def cont_unload() -> None:
            try:
                for dep in gen:
                    if dep == name:
                        continue
                    await do_unload(dep)
            except:
                await cont_unload()
                raise
            await do_unload(name)
        await cont_unload()

async def unsafe_reload(name: str) -> types.ModuleType:
    """
    Finalize and reload a single plugin. This will run the new plugin code over the same module object, which may break
    any plugins that depend on it. All finalizers will be executed even if some raise exceptions. If there were any or
    if there was an exception during reinitialization, they will all be reraised together. If plugin initialization
    raises an exception the plugin remains loaded but may be in a half-updated state. Its finalizers aren't run
    immediately. Returns the module object if successful.
    """
    async with lock:
        if not is_plugin(name):
            raise ValueError(name + " is not a plugin")
        try:
            logger.debug("Reloading {} inplace".format(name))
            await finalize_module(name)
        finally:
            deps.del_edges_from(name)
            try:
                ret = importlib.reload(sys.modules[name])
                await initialize_module(name)
            except:
                await finalize_module(name)
                raise
        return ret

async def reload(name: str) -> types.ModuleType:
    """
    Finalize and reload a plugin and any plugins that (transitively) depend on it. We try to run all finalizers in
    dependency order, and only load plugins that were successfully unloaded, and whose dependencies have been
    successfully reloaded. If a plugin fails to initialize, we run any finalizers it managed to register, and the plugin
    is not loaded. Any exceptions raised will be reraised together. Returns the module object of the requested plugin if
    successful.
    """
    async with lock:
        reloads = deps.subgraph_paths_to(name)
        logger.debug("Reloading {} with dependencies: {}".format(name,
            ", ".join(dep for dep in reloads.topo_sort_fwd() if dep != name)))
        unload_success = set()
        reload_success = set()
        unload_gen = reloads.topo_sort_fwd()
        reload_gen = reloads.topo_sort_bck()
        async def cont_reload() -> None:
            try:
                for dep in reload_gen:
                    if dep == name:
                        continue
                    elif dep not in unload_success:
                        logger.debug("Not reloading {} because it was not unloaded properly".format(dep))
                    elif not all(m in reload_success for m in reloads.edges_from(dep)):
                        logger.debug("Not reloading {} because its dependencies were not reloaded properly".format(dep))
                    else:
                        await do_load(dep)
                        reload_success.add(dep)
            except:
                await cont_reload()
                raise
        async def cont_unload() -> types.ModuleType:
            try:
                for dep in unload_gen:
                    if dep == name:
                        continue
                    await do_unload(dep)
                    unload_success.add(dep)
            except:
                await cont_unload()
                raise
            try:
                await do_unload(name)
            except:
                await cont_reload()
                raise
            try:
                ret = await do_load(name)
                reload_success.add(name)
            finally:
                await cont_reload()
            return ret
        return await cont_unload()

async def load(name: str) -> types.ModuleType:
    """
    Load a single plugin. If it's already loaded, nothing is changed. If there was an exception during initialization,
    the finalizers that managed to registers will be run. Returns the module object if successful.
    """
    async with lock:
        if not is_plugin(name):
            raise ValueError(name + " is not a plugin")
        if name in sys.modules:
            return sys.modules[name]
        return await do_load(name)

@atexit.register
def atexit_unload() -> None:
    async def async_atexit_unload() -> None:
        async with lock:
            unload_list = list(deps.topo_sort_fwd(sources=set(finalizers)))
            unload_gen = iter(unload_list)
            async def cont_unload() -> None:
                try:
                    for dep in unload_gen:
                        await do_unload(dep)
                except:
                    await cont_unload()
                    raise
            await cont_unload()

    loop: Optional[asyncio.AbstractEventLoop]
    try:
        loop = asyncio.get_event_loop()
    except:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
    loop.run_until_complete(async_atexit_unload())

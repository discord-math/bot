from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
from enum import Enum, auto
import importlib
from importlib.machinery import ModuleSpec, PathFinder, SourceFileLoader
import inspect
import logging
import pkgutil
import sys
from types import ModuleType
from typing import Awaitable, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, TypeVar, Union

from util.digraph import Digraph

# This is technically a plugin, load it properly next time
del sys.modules["util.digraph"]
del sys.modules["util"]

# Allow importing plugins from other directories on the path
__spec__.submodule_search_locations = __path__ = pkgutil.extend_path(__path__, __name__)

class PluginException(Exception):
    """
    Any exception related to the management of plugins, including errors during imports, dependency errors,
    initializer/finalizer errors.
    """

class PluginState(Enum):
    NEW = auto() # Freshly registered in the plugins table
    IMPORTING = auto() # The contents of the plugin's module are currently being executed
    IMPORTED = auto() # The module object is constructed, but the initializers haven't been called yet
    INITIALIZING = auto() # The initializers of the plugin are currently being called
    INITIALIZED = auto() # Successfully loaded
    FINALIZING = auto() # Currently being unloaded, its finalizers are being called
    FINALIZED = auto() # Removed from the plugins table

A = TypeVar('A')

import_stack: List[Plugin] = []

def current_plugin() -> Plugin:
    """
    In the lexical scope of the plugin, __name__ will refer to the plugin's name. This function can be used to get the
    plugin in the *dynamic* scope of its initialization. In other words functions outside a plugin can get to know which
    plugin is currently being initialized.
    """
    if not len(import_stack):
        raise ValueError("not called during plugin initialization")
    return import_stack[-1]

class PluginManager:
    """
    A PluginManager makes all modules in its namespaces behave slightly differently.

    It tracks whenever one plugin imports another, and keeps a dependency graph around. It provides high level
    unload/reload functions that will unload/reload dependent plugins as well. It also provides a way for a plugin to
    hook an async "initializer" and a "finalizer" to be executed when the plugin is to be loaded/unloaded.
    """
    __slots__ = "logger", "namespaces", "plugins", "dependencies", "lock"
    logger: logging.Logger
    namespaces: List[str]
    plugins: Dict[str, Plugin]
    dependencies: Digraph[str] # The source is always in the plugins table
    lock: asyncio.Lock # We don't manipulate the dependency graph/module table concurrently, so this lock is taken in
    # all public entry points

    def __init__(self, namespaces: Iterable[str]) -> None:
        self.logger = logging.getLogger(__name__)
        self.namespaces = list(namespaces)
        self.plugins = {}
        self.dependencies = Digraph()
        self.lock = asyncio.Lock()

    def is_plugin(self, name: str) -> bool:
        """Does a given plugin name fall in our namespaces?"""
        return name != __name__ and any((name + ".").startswith(namespace + ".") for namespace in self.namespaces)

    def __str__(self) -> str:
        return "<PluginManager for {} at {:#x}>".format(
            ",".join(namespace + ".*" for namespace in self.namespaces), id(self))

    @staticmethod
    @contextmanager
    def push_plugin(plugin: Plugin) -> Iterator[None]:
        import_stack.append(plugin)
        try:
            yield
        finally:
            import_stack.pop()

    @staticmethod
    async def exc_foreach(fun: Callable[[A], Awaitable[object]], values: Iterable[A],
        map_exc: Callable[[Exception, A], Tuple[Exception, Optional[BaseException]]] = lambda e, _: (e, e.__cause__)
        ) -> None:
        gen = iter(values)
        async def continue_foreach() -> None:
            for value in gen:
                try:
                    await fun(value)
                except Exception as exc:
                    try:
                        exc, cause = map_exc(exc, value)
                        raise exc from cause
                    except Exception as exc:
                        await continue_foreach()
                        raise
        await continue_foreach()

    def register(self) -> None:
        for i in range(len(sys.meta_path)):
            if sys.meta_path[i] == PathFinder:
                sys.meta_path.insert(i, PluginFinder(self))

    @staticmethod
    def find_spec(name: str, path: List[str]) -> Optional[ModuleSpec]:
        if name in sys.modules:
            return sys.modules[name].__spec__
        for finder in sys.meta_path:
            if (spec := finder.find_spec(name, path)) is not None:
                return spec
        if "." in name:
            package = name.rsplit(".", 1)[0]
            if (package_spec := PluginManager.find_spec(package, path)) is not None:
                if package_spec.submodule_search_locations is not None:
                    if not all(loc in path for loc in package_spec.submodule_search_locations):
                        return PluginManager.find_spec(name, list(package_spec.submodule_search_locations) + path)

    @staticmethod
    def of(name: str) -> Optional[PluginManager]:
        if (spec := PluginManager.find_spec(name, sys.path)) is not None:
            return getattr(spec.loader, "manager", None)

    def add_dependency(self, source: str, target: str) -> None:
        assert source in self.plugins
        if source in self.dependencies.paths_from(target):
            self.logger.debug("Weak dependency: {} -> {}".format(source, target))
        else:
            self.logger.debug("Dependency: {} -> {}".format(source, target))
            self.dependencies.add_edge(source, target)

    async def do_unload(self, name: str) -> None:
        plugin = self.plugins[name]
        plugin.transition(PluginState.FINALIZING)
        try:
            await self.plugins[name].run_finalizers()
        finally:
            plugin.transition(PluginState.FINALIZED)
            self.dependencies.del_edges_from(name)
            if self.dependencies.edges_to(name):
                self.logger.info("Breaking reverse dependencies on {} for: {}".format(
                    name, ", ".join(self.dependencies.edges_to(name))))
                self.dependencies.del_edges_to(name)
            if name in sys.modules:
                del sys.modules[name]
            del self.plugins[name]

    async def do_load(self, name: str) -> ModuleType:
        self.logger.debug("Loading {}".format(name))
        try:
            ret = importlib.import_module(name)
        finally:
            if name in self.plugins:
                inits = self.dependencies.subgraph_paths_from(name)

                async def maybe_init_plugin(name: str) -> None:
                    plugin = self.plugins[name]
                    if plugin.state == PluginState.INITIALIZED:
                        return
                    elif plugin.state != PluginState.IMPORTED:
                        self.logger.debug(
                            "Not initializing {} because it was not imported properly".format(name))
                    elif any(self.plugins[dep].state != PluginState.INITIALIZED for dep in inits.edges_from(name)):
                        self.logger.debug(
                            "Not initializing {} because its dependencies were not initialized properly".format(name))
                    else:
                        with self.push_plugin(plugin):
                            plugin.transition(PluginState.INITIALIZING)
                            await plugin.run_initializers()
                            plugin.transition(PluginState.INITIALIZED)

                async def maybe_unload_plugin(name: str) -> None:
                    plugin = self.plugins[name]
                    if plugin.state != PluginState.INITIALIZED:
                        self.logger.debug("Unloading {} because it was not initialized properly".format(name))
                        await self.do_unload(name)
                    elif any(self.plugins[dep].state != PluginState.INITIALIZED for dep in inits.edges_from(name)):
                        self.logger.debug(
                            "Unloading {} because its dependencies were not initialized properly".format(name))
                        await self.do_unload(name)

                try:
                    await PluginManager.exc_foreach(maybe_init_plugin, inits.topo_sort_bck(sources={name}))
                finally:
                    await PluginManager.exc_foreach(maybe_unload_plugin, inits.topo_sort_fwd(sources={name}))
        return ret

    async def unsafe_unload(self, name: str) -> None:
        """
        Finalize and unload a single plugin. May break any plugins that depend on it. All finalizers will be executed
        even if some raise exceptions, if there were any they will all be reraised together.
        """
        async with self.lock:
            if name not in self.plugins:
                raise PluginException("{} is not a loaded plugin".format(name))
            await self.do_unload(name)

    async def unload(self, name: str) -> None:
        """
        Finalize and unload a plugin and any plugins that (transitively) depend on it. All finalizers will be executed
        even if some raise exceptions, if there were any they will all be reraised together.
        """
        async with self.lock:
            if name not in self.plugins:
                raise PluginException("{} is not a loaded plugin".format(name))
            unload_order = list(self.dependencies.subgraph_paths_to(name).topo_sort_fwd(sources={name}))
            self.logger.debug("Unloading {} with dependencies: {}".format(name, ", ".join(unload_order)))
            await PluginManager.exc_foreach(self.do_unload, unload_order)

    async def unsafe_reload(self, name: str) -> ModuleType:
        """
        Finalize and reload a single plugin. This will run the new plugin code over the same module object, which may
        break any plugins that depend on it. All finalizers will be executed even if some raise exceptions. If there
        were any or if there was an exception during reinitialization, they will all be reraised together. If plugin
        initialization raises an exception the plugin remains loaded but may be in a half-updated state. Its finalizers
        aren't run immediately. Returns the module object if successful.
        """
        async with self.lock:
            if name not in self.plugins:
                raise PluginException("{} is not a loaded plugin".format(name))
            plugin = self.plugins[name]
            try:
                self.logger.debug("Reloading {} inplace".format(name))
                plugin.transition(PluginState.FINALIZING)
                await plugin.run_finalizers()
            finally:
                self.dependencies.del_edges_from(name)
                try:
                    ret = importlib.reload(plugin.module)
                    with self.push_plugin(plugin):
                        plugin.transition(PluginState.INITIALIZING)
                        await plugin.run_initializers()
                finally:
                    plugin.transition(PluginState.INITIALIZED)
            return ret

    async def reload(self, name: str) -> ModuleType:
        """
        Finalize and reload a plugin and any plugins that (transitively) depend on it. We try to run all finalizers in
        dependency order, and only load plugins that were successfully unloaded, and whose dependencies have been
        successfully reloaded. If a plugin fails to initialize, we run any finalizers it managed to register, and the
        plugin is not loaded. Any exceptions raised will be reraised together. Returns the module object of the
        requested plugin if successful.
        """
        async with self.lock:
            if name not in self.plugins:
                raise PluginException("{} is not a loaded plugin".format(name))
            reloads = self.dependencies.subgraph_paths_to(name)
            unload_order = list(reloads.topo_sort_fwd(sources={name}))
            reload_order = list(reloads.topo_sort_bck(sources={name}))
            self.logger.debug("Reloading {} with dependencies: {}".format(name, ", ".join(reload_order)))
            unload_success = set()

            async def unload_plugin(name: str) -> None:
                await self.do_unload(name)
                unload_success.add(name)

            async def maybe_load_plugin(name: str) -> None:
                if name not in unload_success:
                    self.logger.debug("Not reloading {} because it was not unloaded properly".format(name))
                elif any(dep not in self.plugins or self.plugins[dep].state != PluginState.INITIALIZED
                    for dep in reloads.edges_from(name)):
                    self.logger.debug(
                        "Not reloading {} because its dependencies were not reloaded properly".format(name))
                else:
                    await self.do_load(name)

            try:
                await PluginManager.exc_foreach(unload_plugin, unload_order)
            finally:
                await PluginManager.exc_foreach(maybe_load_plugin, reload_order)
            return self.plugins[name].module

    async def load(self, name: str) -> ModuleType:
        """
        Load a single plugin. If it's already loaded, nothing is changed. If there was an exception during initialization,
        the finalizers that managed to register will be run. Returns the module object if successful.
        """
        async with self.lock:
            if not self.is_plugin(name):
                raise PluginException("{} is not a plugin".format(name))
            if name in sys.modules:
                return sys.modules[name]
            return await self.do_load(name)

    async def unload_all(self) -> None:
        async with self.lock:
            unload_order = list(self.dependencies.topo_sort_fwd(sources=self.plugins))
            await PluginManager.exc_foreach(self.do_unload, unload_order)

class Plugin:
    __slots__ = "name", "state", "module", "initializers", "finalizers", "logger"
    name: str
    state: PluginState
    module: ModuleType
    initializers: List[Callable[[], Awaitable[None]]]
    finalizers: List[Callable[[], Awaitable[None]]]
    logger: logging.Logger

    def __init__(self, name: str, module: ModuleType, logger: logging.Logger) -> None:
        self.name = name
        self.state = PluginState.NEW
        self.module = module
        self.initializers = []
        self.finalizers = []
        self.logger = logger

    @staticmethod
    def new(manager: PluginManager, module: ModuleType) -> Plugin:
        name = module.__name__
        if name in manager.plugins:
            raise PluginException("Plugin {} is already defined as {}".format(name, manager.plugins[name].module))
        plugin = Plugin(name, module, manager.logger)
        manager.logger.debug("Creating {} with {}".format(name, str(module)))
        manager.plugins[name] = plugin
        return plugin

    def transition(self, state: PluginState) -> None:
        self.logger.debug("{}: {} -> {}".format(self.name, self.state.name, state.name))
        self.state = state

    async def run_initializers(self) -> None:
        try:
            for init in self.initializers:
                try:
                    await init()
                except Exception as exc:
                    raise PluginException("Initializer {} of {} raised".format(init, self.name)) from exc
        finally:
            self.initializers.clear()

    async def run_finalizers(self) -> None:
        try:
            await PluginManager.exc_foreach(lambda fin: fin(), self.finalizers,
                lambda exc, fin: (PluginException("Finalizer {} of {} raised".format(fin, self.name)), exc))
        finally:
            self.finalizers.clear()

T = TypeVar("T", bound=Union[Callable[[], object], Callable[[], Awaitable[object]]])

def init(init: T) -> T:
    """
    A decorator for registering an async initializer, which will be called after the module is loaded. Initializers are
    called in order, and if the initializer fails, subsequent initializers will not be called, and finalizers registered
    so far will be called.
    """
    ret = init
    if inspect.iscoroutinefunction(init):
        ainit = init
    else:
        async def async_init() -> None:
            init()
        ainit = async_init
    current_plugin().initializers.append(ainit)
    return ret

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
    ret = fin
    if inspect.iscoroutinefunction(fin):
        afin = fin
    else:
        async def async_fin() -> None:
            fin()
        afin = async_fin
    current_plugin().finalizers.append(afin)
    return ret

def trace_import(name: str, globals: Optional[Dict[str, object]] = None, locals: Optional[Dict[str, object]] = None,
    fromlist: Sequence[str] = (), level: int = 0) -> object:
    current = current_plugin()
    current_manager = PluginManager.of(current.name)
    assert current_manager
    name_parts = name.split(".")
    for i in range(1, len(name_parts) + 1):
        parent = ".".join(name_parts[:i])
        if (other_manager := PluginManager.of(parent)) is not None:
            if other_manager != current_manager:
                raise PluginException("Import between managers: {} from {} imports {} from {}".format(
                    current.name, current_manager, parent, other_manager))
            else:
                current_manager.add_dependency(current.name, parent)
        importlib.import_module(parent)
    return builtins.__import__(name, globals, locals, fromlist, level)

trace_builtins = ModuleType(builtins.__name__)
trace_builtins.__dict__.update(builtins.__dict__)
trace_builtins.__dict__["__import__"] = trace_import
del trace_import

class PluginLoader(SourceFileLoader):
    __slots__ = "manager"
    def __init__(self, manager: PluginManager, fullname: str, path: Optional[Sequence[Union[bytes, str]]]) -> None:
        self.manager = manager
        super().__init__(fullname, path) # type: ignore

    def exec_module(self, module: ModuleType) -> None:
        plugin = Plugin.new(self.manager, module)
        module.__dict__["__builtins__"] = trace_builtins
        with self.manager.push_plugin(plugin):
            plugin.transition(PluginState.IMPORTING)
            super().exec_module(module)
            plugin.transition(PluginState.IMPORTED)

class PluginFinder(PathFinder):
    __slots__ = "manager"
    def __init__(self, manager: PluginManager) -> None:
        self.manager = manager
        super().__init__()

    def find_spec(self, fullname: str, path: Optional[Sequence[str]] = None # type: ignore
        , target: Optional[ModuleType] = None) -> Optional[ModuleSpec]:
        if not self.manager.is_plugin(fullname):
            return None
        spec = super().find_spec(fullname, path, target)
        if spec is None:
            return None
        if not spec.has_location:
            return spec
        spec.loader = PluginLoader(self.manager, spec.loader.name, spec.loader.path) # type: ignore
        return spec

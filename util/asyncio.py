import asyncio
import plugins
from typing import Any, Callable, Awaitable, TypeVar

R = TypeVar("R")

def __await__(fun: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    """Decorate a class's __await__ with this to be able to write it as an async def."""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fun(*args, **kwargs).__await__()
    return wrapper

def getloop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.get_event_loop()

def run_async(coro: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any) -> asyncio.Task[R]:
    """Schedule an asynchronous computation from synchronous code"""
    return getloop().create_task(coro(*args, **kwargs))

def concurrently(fun: Callable[..., R], *args: Any, **kwargs: Any) -> Awaitable[R]:
    """
    Run a synchronous blocking computation in a different python thread, avoiding blocking the current async thread.
    This function starts the computation and returns a future referring to its result. Beware of (actual) thread-safety.
    """
    return getloop().run_in_executor(None,
        lambda: fun(*args, **kwargs))

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

R = TypeVar("R")

def __await__(fun: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    """Decorate a class's __await__ with this to be able to write it as an async def."""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fun(*args, **kwargs).__await__()
    return wrapper

def concurrently(fun: Callable[..., R], *args: Any, **kwargs: Any) -> Awaitable[R]:
    """
    Run a synchronous blocking computation in a different python thread, avoiding blocking the current async thread.
    This function starts the computation and returns a future referring to its result. Beware of (actual) thread-safety.
    """
    return asyncio.get_running_loop().run_in_executor(None,
        lambda: fun(*args, **kwargs))

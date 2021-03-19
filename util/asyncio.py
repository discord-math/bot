import asyncio
import plugins

def getloop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.get_event_loop()

def run_async(coro):
    """Schedule an asynchronous computation from synchronous code"""
    getloop().create_task(coro())

def concurrently(fun, *args, **kwargs):
    """
    Run a synchronous blocking computation in a different python thread,
    avoiding blocking the current async thread. This function starts the
    computation and returns a future referring to its result. Beware of (actual)
    thread-safety.
    """
    return getloop().run_in_executor(None,
        lambda: fun(*args, **kwargs))

def init_async(coro):
    """
    Perform asynchronous initialization for a plugin. Can be used as a decorator
    around an async function. Cancels the initialization routine if the plugin
    is unloaded before it could complete.
    """
    task = getloop().create_task(coro())
    @plugins.finalizer
    def cancel_initialization():
        task.cancel()

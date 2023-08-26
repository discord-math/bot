"""
This module manages a registry of "main" tasks that are extending the runtime of the entire asyncio program. Once all
main tasks complete (by returning or raising an exception), the program terminates.
"""
import asyncio
import logging
from typing import Any, Coroutine, List, Optional, TypeVar

tasks: List[asyncio.Task[Any]]
try:
    # Keep the list of tasks if we're being reloaded
    tasks # type: ignore
except NameError:
    tasks = []
else:
    tasks = tasks # type: ignore

logger = logging.getLogger(__name__)

T = TypeVar("T")

def create_task(coro: Coroutine[Any, Any, T], *, name: Optional[str] = None) -> asyncio.Task[T]:
    """Register a task as a "main" task. If the task finishes or raises an exception, it is removed from the list"""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(tasks.remove)
    tasks.append(task)
    return task

def cancel() -> None:
    """Cancel all currently registered tasks"""
    for t in tasks:
        t.cancel()

async def wait() -> None:
    """Return when all registered tasks are done, or if any tasks raises an exception, raise that exception"""
    while tasks:
        logger.debug("Waiting for tasks: {}".format(tasks))
        await asyncio.gather(*tasks)

async def wait_all() -> None:
    """Return when all registered tasks are done, accumulating exceptions"""
    try:
        while tasks:
            logger.debug("Waiting for tasks: {}".format(tasks))
            await asyncio.gather(*tasks)
    except:
        logger.debug("Exception when waiting for main tasks", exc_info=True)
        await wait_all()
        raise

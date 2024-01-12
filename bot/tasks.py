import asyncio
import logging
from typing import Awaitable, Callable, Optional

import plugins


logger: logging.Logger = logging.getLogger(__name__)


class Task(asyncio.Task[None]):
    __slots__ = "cb", "timeout", "exc_backoff_base", "exc_backoff_multiplier", "queue"
    cb: Callable[[], Awaitable[object]]
    timeout: Optional[float]
    exc_backoff_base: Optional[float]
    exc_backoff_multiplier: int
    queue: asyncio.Queue[Optional[float]]

    def __init__(
        self,
        cb: Callable[[], Awaitable[object]],
        *,
        every: Optional[float] = None,
        exc_backoff_base: Optional[float] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(self.task_loop(), loop=asyncio.get_event_loop(), name=name)
        self.cb = cb
        self.timeout = every
        self.exc_backoff_base = exc_backoff_base
        self.exc_backoff_multiplier = 1
        self.queue = asyncio.Queue()

    def run_once(self) -> None:
        """
        Trigger the task to run once, and reset the "every" timer. The task will be run as many times as this function
        is called.
        """
        self.queue.put_nowait(None)

    def run_coalesced(self, timeout: float) -> None:
        """
        Trigger the task to run once, unless another run_coalesced or run_once happens within "timeout" seconds, in
        which case the two requests are coalesced and the task runs once. Multiple run_coalesced invocations will join
        into one, but a run_once always ends the chain.
        """
        self.queue.put_nowait(timeout)

    async def task_loop(self) -> None:
        while True:
            try:
                try:
                    timeout = self.timeout
                    while True:
                        timeout = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                        if timeout is None:
                            break
                        elif self.timeout is not None and timeout > self.timeout:
                            timeout = self.timeout
                except asyncio.TimeoutError:
                    pass
                await self.cb()
                self.exc_backoff_multiplier = 1
            except asyncio.CancelledError:
                raise
            except:
                logger.error("Exception in {}".format(self.get_name()), exc_info=True)
                if self.exc_backoff_base is not None:
                    await asyncio.sleep(self.exc_backoff_base * self.exc_backoff_multiplier)
                    self.exc_backoff_multiplier *= 2


def task(
    *, every: Optional[float] = None, exc_backoff_base: Optional[float] = None, name: Optional[str] = None
) -> Callable[[Callable[[], Awaitable[object]]], Task]:
    """
    A decorator that registers the function as a task that is called periodically or upon request. The task is cancelled
    on plugin unload. The "every" parameter causes the task to wait at most that many seconds between executions. A task
    can be started sooner by invoking .run_once or .run_coalesced on it. The "exc_backoff_base" parameter causes an
    additional delay in case the task throws an exception, and the delay increases exponentially if the task keeps
    throwing exceptions.
    """

    def register_task(cb: Callable[[], Awaitable[object]]) -> Task:
        task = Task(cb, every=every, exc_backoff_base=exc_backoff_base, name=name)
        plugins.finalizer(task.cancel)
        return task

    return register_task

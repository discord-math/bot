"""
Some common utilities for interacting with discord.
"""

import asyncio
import discord
import logging
import discord_client
import plugins

logger = logging.getLogger(__name__)

def unsafe_hook_event(name, fun):
    if not asyncio.iscoroutinefunction(fun):
        raise TypeError("expected coroutine function")

    method_name = "on_" + name
    client = discord_client.client

    if not hasattr(client, method_name):
        # Insert a hook that is actually a bound method of "of" a list. The list
        # contains hooks that are to be executed.
        async def event_hook(hooks, *args, **kwargs):
            for hook in list(hooks):
                try:
                    await hook(*args, **kwargs)
                except:
                    logger.error(
                        "Exception in {} hook {}".format(method_name, hook),
                        exc_info=True)
        event_hook.__name__ = method_name
        client.event(event_hook.__get__([]))

    getattr(client, method_name).__self__.append(fun)

def unsafe_unhook_event(name, fun):
    method_name = "on_" + name
    client = discord_client.client
    if hasattr(client, method_name):
        getattr(client, method_name).__self__.remove(fun)

def event(name):
    """
    discord.py doesn't allow multiple functions to register for the same event.
    This decorator fixes that. Takes the event name without "on_" Example usage:

        @event("message")
        def func(msg):

    This function registers a finalizer that removes the registered function,
    and hence should only be called during plugin initialization.
    """
    def decorator(fun):
        unsafe_hook_event(name, fun)
        @plugins.finalizer
        def finalizer():
            unsafe_unhook_event(name, fun)
        return fun
    return decorator

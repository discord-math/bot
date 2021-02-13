"""
Some common utilities for interacting with discord.
"""

import asyncio
import discord
import discord.abc
import string
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

class CodeBlock:
    __slots__ = "text", "language"

    def __init__(self, text, language=None):
        self.text = text
        self.language = language

    def __str__(self):
        text = self.text.replace("``", "`\u200D`")
        return "```{}\n".format(self.language or "") + text + "```"

class Inline:
    __slots__ = "text"

    def __init__(self, text):
        self.text = text

    def __str__(self):
        text = self.text
        if "`" in text:
            if "``" in text:
                text = text.replace("`", "`\u200D")
            if text.startswith("`"):
                text = " " + text
            if text.endswith("`"):
                text = text + " "
            return "``" + text + "``"
        return "`" + text + "`"

class Formatter(string.Formatter):
    """
    A formatter class designed for discord messages. The following conversions
    are understood:

        {!i} -- turn into inline code
        {!b} -- turn into a code block
        {!b:lang} -- turn into a code block in the specified language
        {!m} -- turn into mention
        {!M} -- turn into role mention
        {!c} -- turn into channel link
    """

    __slots__ = ()

    def convert_field(self, value, conversion):
        if conversion == "i":
            return str(Inline(str(value)))
        elif conversion == "b":
            return CodeBlock(str(value))
        elif conversion == "m":
            if isinstance(value, discord.Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, discord.abc.User):
                return "<@{}>".format(value.id)
            elif isinstance(value, int):
                return "<@{}>".format(value)
        elif conversion == "M":
            if isinstance(value, discord.Role):
                return "<@&{}>".format(value.id)
            elif isinstance(value, int):
                return "<@&{}>".format(value)
        elif conversion == "c":
            if isinstance(value, discord.abc.Messageable):
                return "<#{}>".format(value.id)
            elif isinstance(value, discord.CategoryChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, int):
                return "<#{}>".format(value)
        return super().convert_field(value, conversion)

    def format_field(self, value, fmt):
        if isinstance(value, CodeBlock):
            if fmt:
                value.language = fmt
            return str(value)
        return super().format_field(value, fmt)

formatter = Formatter()
format = formatter.format

class UserError(Exception):
    __slots__ = "text"

    def __init__(self, text, *args, **kwargs):
        if args or kwargs:
            text = format(text, *args, **kwargs)
        super().__init__(text)
        self.text = text

def smart_find(name_or_id, iterable):
    """
    Find an object by its name or id. We try an exact id match, then the
    shortest prefix match, if unique among prefix matches of that length, then
    an infix match, if unique.
    """
    try:
        int_id = int(name_or_id)
    except ValueError:
        int_id = None
    prefix_match = None
    prefix_matches = []
    infix_matches = []
    for x in iterable:
        if x.id == int_id:
            return x
        if x.name.startswith(name_or_id):
            if prefix_matches and len(x.name) < len(prefix_matches[0]):
                prefix_matches = []
            prefix_matches.append(x.name)
            prefix_match = x
        elif getattr(x, "nick", None) != None and x.nick.startswith(name_or_id):
            if prefix_matches and len(x.nick) < len(prefix_matches[0]):
                prefix_matches = []
            prefix_matches.append(x.nick)
            prefix_match = x
        elif name_or_id in x.name:
            infix_matches.append(x)
        elif getattr(x, "nick", None) != None and name_or_id in x.nick:
            infix_matches.append(x)
    if len(prefix_matches) == 1:
        return prefix_match
    if len(infix_matches) == 1:
        return infix_matches[0]
    return None

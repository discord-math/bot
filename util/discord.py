"""
Some common utilities for interacting with discord.
"""

import asyncio
import discord
import discord.abc
import string
import logging
from typing import Any, List, Callable, Iterable, Optional, Union, Coroutine, TypeVar, Protocol, AsyncContextManager
import discord_client
import plugins

logger: logging.Logger = logging.getLogger(__name__)

def unsafe_hook_event(name: str, fun: Callable[..., Coroutine[Any, Any, None]]) -> None:
    if not asyncio.iscoroutinefunction(fun):
        raise TypeError("expected coroutine function")
    method_name = "on_" + name
    discord_client.client.add_listener(fun, name=method_name)

def unsafe_unhook_event(name: str, fun: Callable[..., Coroutine[Any, Any, None]]) -> None:
    method_name = "on_" + name
    discord_client.client.remove_listener(fun, name=method_name)

def event(name: str) -> Callable[
        [Callable[..., Coroutine[Any, Any, None]]],
        Callable[..., Coroutine[Any, Any, None]]]:
    """
    discord.py doesn't allow multiple functions to register for the same event.
    This decorator fixes that. Takes the event name without "on_" Example usage:

        @event("message")
        def func(msg):

    This function registers a finalizer that removes the registered function,
    and hence should only be called during plugin initialization.
    """
    def decorator(fun: Callable[..., Coroutine[Any, Any, None]]
        ) -> Callable[..., Coroutine[Any, Any, None]]:
        unsafe_hook_event(name, fun)
        @plugins.finalizer
        def finalizer() -> None:
            unsafe_unhook_event(name, fun)
        return fun
    return decorator

class CodeBlock:
    __slots__ = "text", "language"
    text: str
    language: Optional[str]

    def __init__(self, text: str, language: Optional[str] = None):
        self.text = text
        self.language = language

    def __str__(self) -> str:
        text = self.text.replace("``", "`\u200D`")
        return "```{}\n".format(self.language or "") + text + "```"

class Inline:
    __slots__ = "text"
    text: str

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
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

    def convert_field(self, value: Any, conversion: str) -> Any:
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
            if isinstance(value, discord.TextChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, discord.CategoryChannel):
                return "<#{}>".format(value.id)
            elif isinstance(value, int):
                return "<#{}>".format(value)
        return super().convert_field(value, conversion)

    def format_field(self, value: Any, fmt: str) -> Any:
        if isinstance(value, CodeBlock):
            if fmt:
                value.language = fmt
            return str(value)
        return super().format_field(value, fmt)

formatter: string.Formatter = Formatter()
format = formatter.format

class UserError(Exception):
    __slots__ = "text"

    def __init__(self, text: str, *args: Any, **kwargs: Any):
        if args or kwargs:
            text = format(text, *args, **kwargs)
        super().__init__(text)
        self.text = text

class NamedType(Protocol):
    id: int
    name: str

class NicknamedType(Protocol):
    id: int
    name: str
    nick: str

T = TypeVar("T", bound=Union[NamedType, NicknamedType])

def smart_find(name_or_id: str, iterable: Iterable[T]) -> Optional[T]:
    """
    Find an object by its name or id. We try an exact id match, then the
    shortest prefix match, if unique among prefix matches of that length, then
    an infix match, if unique.
    """
    int_id: Optional[int]
    try:
        int_id = int(name_or_id)
    except ValueError:
        int_id = None
    prefix_match: Optional[T] = None
    prefix_matches: List[str] = []
    infix_matches: List[T] = []
    for x in iterable:
        if x.id == int_id:
            return x
        if x.name.startswith(name_or_id):
            if prefix_matches and len(x.name) < len(prefix_matches[0]):
                prefix_matches = []
            prefix_matches.append(x.name)
            prefix_match = x
        else:
            nick = getattr(x, "nick", None)
            if nick is not None and nick.startswith(name_or_id):
                if prefix_matches and len(nick) < len(prefix_matches[0]):
                    prefix_matches = []
                prefix_matches.append(nick)
                prefix_match = x
            elif name_or_id in x.name:
                infix_matches.append(x)
            elif nick is not None and name_or_id in nick:
                infix_matches.append(x)
    if len(prefix_matches) == 1:
        return prefix_match
    if len(infix_matches) == 1:
        return infix_matches[0]
    return None

class TempMessage(AsyncContextManager[discord.Message]):
    __slots__ = "sendable", "args", "kwargs", "message"
    sendable: discord.abc.Messageable
    args: Any
    kwargs: Any
    message: Optional[discord.Message]

    def __init__(self, sendable: discord.abc.Messageable,
        *args: Any, **kwargs: Any):
        self.sendable = sendable
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self) -> discord.Message:
        self.message = await self.sendable.send(*self.args, **self.kwargs)
        return self.message

    async def __aexit__(self, exc_type, exc_val, tb) -> None: # type: ignore
        try:
            if self.message is not None:
                await self.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

class ChannelById(discord.abc.Messageable):
    __slots__ = "id", "_state"
    id: int
    _state: discord.state.ConnectionState

    def __init__(self, client: discord.Client, id: int):
        self.id = id
        self._state = client._connection # type: ignore

    async def _get_channel(self) -> discord.abc.Messageable:
        return self

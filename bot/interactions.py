import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union
from typing_extensions import Concatenate, ParamSpec

from discord import Interaction, Member, Message, User
import discord.app_commands
from discord.app_commands import AppCommandError, CheckFailure, Command, ContextMenu, Group
from discord.ui import View

from bot.client import client
import plugins
from util.discord import format

logger = logging.getLogger(__name__)

old_on_error = client.tree.on_error
@client.tree.error
async def on_error(interaction: Interaction, exc: AppCommandError) -> None:
    if isinstance(exc, CheckFailure):
        message = "Error: {}".format(str(exc))
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return
    else:
        logger.error(format("Error in command {!r} {!r} from {!m} in {!c}: {}", interaction.command,
            interaction.data, interaction.user, interaction.channel_id, str(exc)), exc_info=exc.__cause__)
        return
@plugins.finalizer
def restore_on_error() -> None:
    client.tree.error(old_on_error)

sync_required = asyncio.Event()

async def sync_commands() -> None:
    await client.wait_until_ready()

    while True:
        try:
            try:
                await asyncio.wait_for(sync_required.wait(), timeout=None)
                while True:
                    sync_required.clear()
                    await asyncio.wait_for(sync_required.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

            logger.debug("Syncing command tree")
            await client.tree.sync()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in command synching task", exc_info=True)
            await asyncio.sleep(60)

P = ParamSpec("P")
T = TypeVar("T")

def command(name: str, description: Optional[str] = None) -> Callable[
    [Callable[Concatenate[Interaction, P], Coroutine[Any, Any, T]]], Command[Any, P, T]]:
    """Decorator for a slash command that is added/removed together with the plugin."""
    def decorator(fun: Callable[Concatenate[Interaction, P], Coroutine[Any, Any, T]]) -> Command[Any, P, T]:
        if description is None:
            cmd = discord.app_commands.command(name=name)(fun)
        else:
            cmd = discord.app_commands.command(name=name, description=description)(fun)

        client.tree.add_command(cmd)
        sync_required.set()
        def finalizer():
            client.tree.remove_command(cmd.name)
            sync_required.set()
        plugins.finalizer(finalizer)

        return cmd
    return decorator

def group(name: str, *, description: str, **kwargs: Any) -> Group:
    """Decorator for a slash command group that is added/removed together with the plugin."""
    cmd = Group(name=name, description=description, **kwargs)

    client.tree.add_command(cmd)
    sync_required.set()
    def finalizer():
        client.tree.remove_command(cmd.name)
        sync_required.set()
    plugins.finalizer(finalizer)

    return cmd

def context_menu(name: str) -> Callable[[Union[
        Callable[[Interaction, Member], Coroutine[Any, Any, Any]],
        Callable[[Interaction, User], Coroutine[Any, Any, Any]],
        Callable[[Interaction, Message], Coroutine[Any, Any, Any]],
        Callable[[Interaction, Union[Member, User]], Coroutine[Any, Any, Any]]
    ]], ContextMenu]:
    """Decorator for a context menu command that is added/removed together with the plugin."""
    def decorator(fun: Union[
        Callable[[Interaction, Member], Coroutine[Any, Any, Any]],
        Callable[[Interaction, User], Coroutine[Any, Any, Any]],
        Callable[[Interaction, Message], Coroutine[Any, Any, Any]],
        Callable[[Interaction, Union[Member, User]], Coroutine[Any, Any, Any]]]
        ) -> ContextMenu:
        cmd = discord.app_commands.context_menu(name=name)(fun)

        client.tree.add_command(cmd)
        sync_required.set()
        def finalizer():
            client.tree.remove_command(cmd.name)
            sync_required.set()
        plugins.finalizer(finalizer)

        return cmd
    return decorator

V = TypeVar("V", bound=View)

def persistent_view(view: V) -> V:
    """Declare a given view as persistent (for as long as the plugin is loaded)."""
    assert view.is_persistent()
    client.add_view(view)
    def finalizer():
        view.stop()
    plugins.finalizer(finalizer)

    return view

@plugins.init
async def init():
    global sync_task
    sync_task = asyncio.create_task(sync_commands())
    plugins.finalizer(sync_task.cancel)
    sync_required.set()

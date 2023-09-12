import logging
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union
from typing_extensions import Concatenate, ParamSpec

from discord import Interaction, Member, Message, User
import discord.app_commands
from discord.app_commands import AppCommandError, CheckFailure, Command, ContextMenu, Group
from discord.ui import View

from bot.client import client
from bot.tasks import task
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
    client.tree.error(old_on_error) # type: ignore

@task(name="Command tree sync task", exc_backoff_base=1)
async def sync_task() -> None:
    await client.wait_until_ready()
    logger.debug("Syncing command tree")
    await client.tree.sync()

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
        sync_task.run_coalesced(5)
        def finalizer():
            client.tree.remove_command(cmd.name)
            sync_task.run_coalesced(5)
        plugins.finalizer(finalizer)

        return cmd
    return decorator

def group(name: str, *, description: str, **kwargs: Any) -> Group:
    """Decorator for a slash command group that is added/removed together with the plugin."""
    cmd = Group(name=name, description=description, **kwargs)

    client.tree.add_command(cmd)
    sync_task.run_coalesced(5)
    def finalizer():
        client.tree.remove_command(cmd.name)
        sync_task.run_coalesced(5)
    plugins.finalizer(finalizer)

    return cmd

def context_menu(name: str) -> Callable[[Union[
        Callable[[Interaction, Member], Coroutine[Any, Any, object]],
        Callable[[Interaction, User], Coroutine[Any, Any, object]],
        Callable[[Interaction, Message], Coroutine[Any, Any, object]],
        Callable[[Interaction, Union[Member, User]], Coroutine[Any, Any, object]]
    ]], ContextMenu]:
    """Decorator for a context menu command that is added/removed together with the plugin."""
    def decorator(fun: Union[
        Callable[[Interaction, Member], Coroutine[Any, Any, object]],
        Callable[[Interaction, User], Coroutine[Any, Any, object]],
        Callable[[Interaction, Message], Coroutine[Any, Any, object]],
        Callable[[Interaction, Union[Member, User]], Coroutine[Any, Any, object]]]
        ) -> ContextMenu:
        cmd = discord.app_commands.context_menu(name=name)(fun)

        client.tree.add_command(cmd)
        sync_task.run_coalesced(5)
        def finalizer():
            client.tree.remove_command(cmd.name)
            sync_task.run_coalesced(5)
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

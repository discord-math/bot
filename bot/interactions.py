import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union
from typing_extensions import Concatenate, ParamSpec

import discord
import discord.app_commands
import discord.ext.commands
import discord.ui

import bot.client
import plugins
import util.discord

logger = logging.getLogger(__name__)

old_on_error = bot.client.client.tree.on_error
@bot.client.client.tree.error
async def on_error(interaction: discord.Interaction, exc: discord.app_commands.AppCommandError) -> None:
    if isinstance(exc, discord.app_commands.CheckFailure):
        message = "Error: {}".format(str(exc))
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return
    else:
        logger.error(util.discord.format("Error in command {!r} {!r} from {!m} in {!c}: {}", interaction.command,
            interaction.data, interaction.user, interaction.channel_id, str(exc)), exc_info=exc.__cause__)
        return
@plugins.finalizer
def restore_on_error() -> None:
    bot.client.client.tree.error(old_on_error)

sync_required = asyncio.Event()

async def sync_commands() -> None:
    await bot.client.client.wait_until_ready()

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
            await bot.client.client.tree.sync()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in command synching task", exc_info=True)
            await asyncio.sleep(60)

P = ParamSpec("P")
T = TypeVar("T")

def command(name: str, description: Optional[str] = None) -> Callable[
    [Callable[Concatenate[discord.Interaction, P], Coroutine[Any, Any, T]]],
    discord.app_commands.Command[Any, P, T]]:
    def decorator(fun: Callable[Concatenate[discord.Interaction, P], Coroutine[Any, Any, T]]
        ) -> discord.app_commands.Command[Any, P, T]:
        if description is None:
            cmd = discord.app_commands.command(name=name)(fun)
        else:
            cmd = discord.app_commands.command(name=name, description=description)(fun)

        bot.client.client.tree.add_command(cmd)
        sync_required.set()
        def finalizer():
            bot.client.client.tree.remove_command(cmd.name)
            sync_required.set()
        plugins.finalizer(finalizer)

        return cmd
    return decorator

def group(name: str, *, description: str, **kwargs: Any) -> discord.app_commands.Group:
    cmd = discord.app_commands.Group(name=name, description=description, **kwargs)

    bot.client.client.tree.add_command(cmd)
    sync_required.set()
    def finalizer():
        bot.client.client.tree.remove_command(cmd.name)
        sync_required.set()
    plugins.finalizer(finalizer)

    return cmd

def context_menu(name: str) -> Callable[[Union[
        Callable[[discord.Interaction, discord.Member], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, discord.User], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, discord.Message], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, Union[discord.Member, discord.User]], Coroutine[Any, Any, Any]]
    ]], discord.app_commands.ContextMenu]:
    def decorator(fun: Union[
        Callable[[discord.Interaction, discord.Member], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, discord.User], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, discord.Message], Coroutine[Any, Any, Any]],
        Callable[[discord.Interaction, Union[discord.Member, discord.User]], Coroutine[Any, Any, Any]]]
        ) -> discord.app_commands.ContextMenu:
        cmd = discord.app_commands.context_menu(name=name)(fun)

        bot.client.client.tree.add_command(cmd)
        sync_required.set()
        def finalizer():
            bot.client.client.tree.remove_command(cmd.name)
            sync_required.set()
        plugins.finalizer(finalizer)

        return cmd
    return decorator

V = TypeVar("V", bound=discord.ui.View)

def persistent_view(view: V) -> V:
    assert view.is_persistent()
    bot.client.client.add_view(view)
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

import sys
import traceback
import importlib
import discord
import discord.ext.commands
from typing import Optional
import plugins
import plugins.autoload
import plugins.commands
import plugins.privileges
import util.discord
import util.restart

@plugins.commands.command_ext("restart")
@plugins.privileges.priv_ext("admin")
async def restart_command(ctx: discord.ext.commands.Context) -> None:
    """Restart the bot process"""
    await ctx.send("Restarting...")
    util.restart.restart()

class PluginConverter(str):
    @classmethod
    async def convert(cls, ctx: discord.ext.commands.Context, arg: str) -> str:
        if not arg.startswith(plugins.plugins_namespace + "."):
            arg = plugins.plugins_namespace + "." + arg
        return arg

async def reply_exception(ctx: discord.ext.commands.Context) -> None:
    _, exc, tb = sys.exc_info()
    text = util.discord.format("{!b:py}", "{}\n{}".format("".join(traceback.format_tb(tb)), repr(exc)))
    del tb
    await ctx.send(text)

@plugins.commands.command_ext("load")
@plugins.privileges.priv_ext("admin")
async def load_command(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Load a plugin"""
    try:
        await plugins.load(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("reload")
@plugins.privileges.priv_ext("admin")
async def reload_command(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin"""
    try:
        await plugins.reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("unsafereload")
@plugins.privileges.priv_ext("admin")
async def unsafe_reload_command(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin without its dependents"""
    try:
        await plugins.unsafe_reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("unload")
@plugins.privileges.priv_ext("admin")
async def unload_command(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin"""
    try:
        await plugins.unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("unsafeunload")
@plugins.privileges.priv_ext("admin")
async def unsafe_unload_command(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin without its dependents"""
    try:
        await plugins.unsafe_unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("reloadmod")
@plugins.privileges.priv_ext("admin")
async def reloadmod_command(ctx: discord.ext.commands.Context, module: str) -> None:
    """Reload a module"""
    try:
        importlib.reload(sys.modules[module])
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@plugins.commands.command_ext("autoload", cls=discord.ext.commands.Group, invoke_without_command=True)
@plugins.privileges.priv_ext("admin")
async def autoload_command(ctx: discord.ext.commands.Context) -> None:
    """Manage plugins loaded at startup"""
    await ctx.send(", ".join(util.discord.format("{!i}", name) for name in plugins.autoload.get_autoload()))

@autoload_command.command("add")
@plugins.privileges.priv_ext("admin")
async def autoload_add(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Add a plugin to be loaded at startup"""
    await plugins.autoload.set_autoload(plugin, True)
    await ctx.send("\u2705")

@autoload_command.command("remove")
@plugins.privileges.priv_ext("admin")
async def autoload_remove(ctx: discord.ext.commands.Context, plugin: PluginConverter) -> None:
    """Remove a plugin from startup loading list"""
    await plugins.autoload.set_autoload(plugin, False)
    await ctx.send("\u2705")

@plugins.commands.command_ext("plugins")
@plugins.privileges.priv_ext("mod")
async def plugins_command(ctx: discord.ext.commands.Context) -> None:
    """List loaded plugins"""
    await ctx.send(", ".join(util.discord.format("{!i}", name)
        for name in sys.modules if plugins.is_plugin(name)))

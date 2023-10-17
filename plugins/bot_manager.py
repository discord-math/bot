from collections import defaultdict
import importlib
import sys
import traceback

from bot.acl import privileged
import bot.autoload
from bot.commands import Context, cleanup, command, group
import plugins
from util.discord import CodeItem, Typing, chunk_messages, format
import util.restart

manager = plugins.PluginManager.of(__name__)
assert manager

@command("restart")
@privileged
async def restart_command(ctx: Context) -> None:
    """Restart the bot process."""
    await ctx.send("Restarting...")
    util.restart.restart()

class PluginConverter(str):
    @classmethod
    async def convert(cls, ctx: Context, arg: str) -> str:
        if "." not in arg:
            arg = "plugins." + arg
        return arg

async def reply_exception(ctx: Context) -> None:
    _, exc, tb = sys.exc_info()
    for content, files in chunk_messages((
        CodeItem("".join(traceback.format_exception(None, exc, tb)), language="py", filename="error.txt"),)):
        await ctx.send(content, files=files)
    del tb

@cleanup
@command("load")
@privileged
async def load_command(ctx: Context, plugin: PluginConverter) -> None:
    """Load a plugin."""
    try:
        async with Typing(ctx):
            await manager.load(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@command("reload")
@privileged
async def reload_command(ctx: Context, plugin: PluginConverter) -> None:
    """Reload a plugin."""
    try:
        async with Typing(ctx):
            await manager.reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@command("unsafereload")
@privileged
async def unsafe_reload_command(ctx: Context, plugin: PluginConverter) -> None:
    """Reload a plugin without its dependents."""
    try:
        async with Typing(ctx):
            await manager.unsafe_reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@command("unload")
@privileged
async def unload_command(ctx: Context, plugin: PluginConverter) -> None:
    """Unload a plugin."""
    try:
        async with Typing(ctx):
            await manager.unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@command("unsafeunload")
@privileged
async def unsafe_unload_command(ctx: Context, plugin: PluginConverter) -> None:
    """Unload a plugin without its dependents."""
    try:
        async with Typing(ctx):
            await manager.unsafe_unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@command("reloadmod")
@privileged
async def reloadmod_command(ctx: Context, module: str) -> None:
    """Reload a module."""
    try:
        importlib.reload(sys.modules[module])
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@cleanup
@group("autoload", invoke_without_command=True)
@privileged
async def autoload_command(ctx: Context) -> None:
    """Manage plugins loaded at startup."""
    await ctx.send(", ".join(format("{!i}", name) for name in bot.autoload.get_autoload()))

@autoload_command.command("add")
@privileged
async def autoload_add(ctx: Context, plugin: PluginConverter) -> None:
    """Add a plugin to be loaded at startup."""
    await bot.autoload.set_autoload(plugin, True)
    await ctx.send("\u2705")

@autoload_command.command("remove")
@privileged
async def autoload_remove(ctx: Context, plugin: PluginConverter) -> None:
    """Remove a plugin from startup loading list."""
    await bot.autoload.set_autoload(plugin, False)
    await ctx.send("\u2705")

@cleanup
@command("plugins")
@privileged
async def plugins_command(ctx: Context) -> None:
    """List loaded plugins."""
    output = defaultdict(list)
    for name in sys.modules:
        if manager.is_plugin(name):
            try:
                key = manager.plugins[name].state.name
            except KeyError:
                key = "???"
            output[key].append(name)
    await ctx.send("\n".join(format("- {!i}: {}", key, ", ".join(format("{!i}", name) for name in sorted(plugins)))
        for key, plugins in output.items()))

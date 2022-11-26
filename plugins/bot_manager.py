import collections
import importlib
import sys
import traceback

import bot.autoload
import bot.commands
import bot.privileges
import plugins
import util.discord
import util.restart

manager = plugins.PluginManager.of(__name__)
assert manager

@bot.commands.command("restart")
@bot.privileges.priv("admin")
async def restart_command(ctx: bot.commands.Context) -> None:
    """Restart the bot process."""
    await ctx.send("Restarting...")
    util.restart.restart()

class PluginConverter(str):
    @classmethod
    async def convert(cls, ctx: bot.commands.Context, arg: str) -> str:
        if "." not in arg:
            arg = "plugins." + arg
        return arg

async def reply_exception(ctx: bot.commands.Context) -> None:
    _, exc, tb = sys.exc_info()
    text = util.discord.format("{!b:py}", "".join(traceback.format_exception(None, exc, tb)))
    del tb
    await ctx.send(text)

@bot.commands.cleanup
@bot.commands.command("load")
@bot.privileges.priv("admin")
async def load_command(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Load a plugin."""
    try:
        await manager.load(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("reload")
@bot.privileges.priv("admin")
async def reload_command(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin."""
    try:
        await manager.reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("unsafereload")
@bot.privileges.priv("admin")
async def unsafe_reload_command(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Reload a plugin without its dependents."""
    try:
        await manager.unsafe_reload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("unload")
@bot.privileges.priv("admin")
async def unload_command(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin."""
    try:
        await manager.unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("unsafeunload")
@bot.privileges.priv("admin")
async def unsafe_unload_command(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Unload a plugin without its dependents."""
    try:
        await manager.unsafe_unload(plugin)
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("reloadmod")
@bot.privileges.priv("admin")
async def reloadmod_command(ctx: bot.commands.Context, module: str) -> None:
    """Reload a module."""
    try:
        importlib.reload(sys.modules[module])
    except:
        await reply_exception(ctx)
    else:
        await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.group("autoload", invoke_without_command=True)
@bot.privileges.priv("admin")
async def autoload_command(ctx: bot.commands.Context) -> None:
    """Manage plugins loaded at startup."""
    await ctx.send(", ".join(util.discord.format("{!i}", name) for name in bot.autoload.get_autoload()))

@autoload_command.command("add")
@bot.privileges.priv("admin")
async def autoload_add(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Add a plugin to be loaded at startup."""
    await bot.autoload.set_autoload(plugin, True)
    await ctx.send("\u2705")

@autoload_command.command("remove")
@bot.privileges.priv("admin")
async def autoload_remove(ctx: bot.commands.Context, plugin: PluginConverter) -> None:
    """Remove a plugin from startup loading list."""
    await bot.autoload.set_autoload(plugin, False)
    await ctx.send("\u2705")

@bot.commands.cleanup
@bot.commands.command("plugins")
@bot.privileges.priv("mod")
async def plugins_command(ctx: bot.commands.Context) -> None:
    """List loaded plugins."""
    output = collections.defaultdict(list)
    for name in sys.modules:
        if manager.is_plugin(name):
            try:
                key = manager.plugins[name].state.name
            except KeyError:
                key = "???"
            output[key].append(name)
    await ctx.send("\n".join(
        util.discord.format("{!i}: {}", key, ", ".join(util.discord.format("{!i}", name) for name in plugins))
        for key, plugins in output.items()))

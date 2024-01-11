import json
from typing import Any, Iterator, Optional, Sequence, TypeVar, Union

from discord.ext.commands import Command, group

from bot.acl import privileged
from bot.commands import Context, cleanup, plugin_command
import plugins
import util.db
import util.db.kv
from util.discord import CodeBlock, CodeItem, Inline, PlainItem, Quoted, chunk_messages, format

@plugin_command
@cleanup
@group("config", invoke_without_command=True)
@privileged
async def config_command(ctx: Context, namespace: Optional[str], key: Optional[str],
    value: Optional[Union[CodeBlock, Inline, Quoted]]) -> None:
    """Edit the key-value configs."""
    if namespace is None:
        def namespace_items(nsps: Sequence[str]) -> Iterator[PlainItem]:
            first = True
            for nsp in nsps:
                if first:
                    first = False
                else:
                    yield PlainItem(", ")
                yield PlainItem(format("{!i}", nsp))
        for content, _ in chunk_messages(namespace_items(await util.db.kv.get_namespaces())):
            await ctx.send(content)
        return

    conf = await util.db.kv.load(namespace)

    if key is None:
        def keys_items() -> Iterator[PlainItem]:
            first = True
            for keys in conf:
                if first:
                    first = False
                else:
                    yield PlainItem("; ")
                yield PlainItem(",".join(format("{!i}", key) for key in keys))
        for content, _ in chunk_messages(keys_items()):
            await ctx.send(content)
        return

    keys = key.split(",")

    if value is None:
        for content, files in chunk_messages((
            CodeItem(util.db.kv.json_encode(conf[keys]) or "", language="json", filename="{}.json".format(key)),)):
            await ctx.send(content, files=files)
        return

    conf[keys] = json.loads(value.text)
    await conf
    await ctx.send("\u2705")

@config_command.command("--delete")
@privileged
async def config_delete(ctx: Context, namespace: str, key: str) -> None:
    """Delete the provided key from the config."""
    conf = await util.db.kv.load(namespace)
    keys = key.split(",")
    conf[keys] = None
    await conf
    await ctx.send("\u2705")

CommandT = TypeVar("CommandT", bound=Command[Any, Any, Any])

def plugin_config_command(cmd: CommandT) -> CommandT:
    """Register a subcommand of the config command to be added/removed together with the plugin."""
    config_command.add_command(cmd)
    plugins.finalizer(lambda: config_command.remove_command(cmd.name))
    return cmd

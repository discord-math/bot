import json
from typing import Iterator, List, Optional, Sequence, Union

import asyncpg
from discord.ext.commands import Greedy

from bot.commands import Context, cleanup, command, group
from bot.privileges import priv
from bot.reactions import get_reaction
import util.db
import util.db.kv
from util.discord import CodeBlock, CodeItem, Inline, PlainItem, Quoted, Typing, chunk_messages, format

@cleanup
@group("config", invoke_without_command=True)
@priv("shell")
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
@priv("shell")
async def config_delete(ctx: Context, namespace: str, key: str) -> None:
    """Delete the provided key from the config."""
    conf = await util.db.kv.load(namespace)
    keys = key.split(",")
    conf[keys] = None
    await conf
    await ctx.send("\u2705")

@cleanup
@command("sql")
@priv("shell")
async def sql_command(ctx: Context, args: Greedy[Union[CodeBlock, Inline, str]]) -> None:
    """Execute arbitrary SQL statements in the database."""
    data_outputs: List[List[str]] = []
    outputs: List[Union[str, List[str]]] = []
    async with util.db.connection() as conn:
        async with Typing(ctx):
            tx = conn.transaction()
            await tx.start()
            for arg in args:
                if isinstance(arg, (CodeBlock, Inline)):
                    try:
                        stmt = await conn.prepare(arg.text)
                        results = (await stmt.fetch())[:1000]
                    except asyncpg.PostgresError as e:
                        outputs.append(format("{!b}", e))
                    else:
                        outputs.append(stmt.get_statusmsg())
                        if results:
                            data = [" ".join(results[0].keys())]
                            data.extend(" ".join(repr(col) for col in result) for result in results)
                            if len(results) == 1000:
                                data.append("...")
                            data_outputs.append(data)
                            outputs.append(data)

        def output_len(output: List[str]) -> int:
            return sum(len(row) + 1 for row in output)

        total_len = sum(4 + output_len(output) + 4
            if isinstance(output, list) else len(output) + 1
            for output in outputs)

        while total_len > 2000 and any(data_outputs):
            lst = max(data_outputs, key=output_len)
            if lst[-1] == "...":
                removed = lst.pop(-2)
            else:
                removed = lst.pop()
                lst.append("...")
                total_len += 4
            total_len -= len(removed) + 1

        text = "\n".join(format("{!b}", "\n".join(output))
            if isinstance(output, list) else output for output in outputs)[:2000]

        reply = await ctx.send(text)

        # If we've been assigned a transaction ID, means we've changed
        # something. Prompt the user to commit.
        has_tx = False
        try:
            if await conn.fetchval("SELECT txid_current_if_assigned()"):
                has_tx = True
        except asyncpg.PostgresError:
            pass
        if not has_tx:
            return

        if await get_reaction(reply, ctx.author, {"\u21A9": False, "\u2705": True}, timeout=60):
            await tx.commit()
        else:
            await tx.rollback()

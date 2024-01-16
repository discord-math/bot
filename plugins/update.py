import asyncio
import asyncio.subprocess
from typing import TYPE_CHECKING, Optional

from discord.ext.commands import command, group
from sqlalchemy import TEXT, select
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

from bot.acl import privileged
from bot.commands import Context, cleanup, plugin_command
from bot.config import plugin_config_command
import plugins
import util.db.kv
from util.discord import CodeItem, Typing, chunk_messages, format


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine)


@registry.mapped
class GitDirectory:
    __tablename__ = "update_git_directories"

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    directory: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, name: str, directory: str) -> None:
            ...


@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for key in [key for key, in conf]:
            session.add(GitDirectory(name=key, directory=str(conf[key])))
            conf[key] = None
        await session.commit()
        await conf


@plugin_command
@cleanup
@command("update")
@privileged
async def update_command(ctx: Context, bot_directory: Optional[str]) -> None:
    """Pull changes from git remote."""
    async with sessionmaker() as session:
        cwd = None
        if bot_directory is not None:
            if conf := await session.get(GitDirectory, bot_directory):
                cwd = conf.directory

    git_pull = await asyncio.create_subprocess_exec(
        "git",
        "pull",
        "--ff-only",
        "--recurse-submodules",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async with Typing(ctx):
        try:
            assert git_pull.stdout
            output = (await git_pull.stdout.read()).decode("utf", "replace")
        finally:
            await git_pull.wait()

    for content, files in chunk_messages((CodeItem(output, filename="update.txt"),)):
        await ctx.send(content, files=files)


@plugin_config_command
@group("update", invoke_without_command=True)
async def config(ctx: Context) -> None:
    async with sessionmaker() as session:
        stmt = select(GitDirectory)
        dirs = (await session.execute(stmt)).scalars()
        await ctx.send(
            "\n".join(format("- {!i}: {!i}", conf.name, conf.directory) for conf in dirs)
            or "No repositories registered"
        )


@config.command("add")
async def config_add(ctx: Context, name: str, directory: str) -> None:
    async with sessionmaker() as session:
        session.add(GitDirectory(name=name, directory=directory))
        await session.commit()
        await ctx.send("\u2705")


@config.command("remove")
async def config_remove(ctx: Context, name: str) -> None:
    async with sessionmaker() as session:
        await session.delete(await session.get(GitDirectory, name))
        await session.commit()
        await ctx.send("\u2705")

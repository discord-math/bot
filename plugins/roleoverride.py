from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Set, cast
from sqlalchemy import BigInteger, select

from sqlalchemy.ext.asyncio import async_sessionmaker
from bot.commands import Context
from bot.config import plugin_config_command

from discord import AllowedMentions, Member
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

from bot.cogs import Cog, cog
from discord.ext.commands import group
import plugins
import util.db.kv
from util.discord import PartialRoleConverter, retry, format

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine)

@registry.mapped
class Override:
    __tablename__ = "roleoverrides"

    retained_role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    excluded_role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    if TYPE_CHECKING:
        def __init__(self, *, retained_role_id: int, excluded_role_id: int) -> None: ...

@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for key, in conf:
            for role in cast(List[int], conf[key]):
                session.add(Override(retained_role_id=int(key), excluded_role_id=role))
        await session.commit()
        for key in [key for key, in conf]:
            conf[key] = None
        await conf

@cog
class RoleOverride(Cog):
    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member) -> None:
        removed = []
        async with sessionmaker() as session:
            stmt = select(Override.excluded_role_id).distinct().where(
                Override.retained_role_id.in_([role.id for role in after.roles]))
            excluded = set((await session.execute(stmt)).scalars())
            for role in after.roles:
                if role.id in excluded:
                    removed.append(role)
        if len(removed):
            await retry(lambda: after.remove_roles(*removed))

@plugin_config_command
@group("roleoverride", invoke_without_command=True)
async def config(ctx: Context) -> None:
    async with sessionmaker() as session:
        overrides: Dict[int, Set[int]] = defaultdict(set)
        stmt = select(Override)
        for override in (await session.execute(stmt)).scalars():
            overrides[override.retained_role_id].add(override.excluded_role_id)

    await ctx.send("\n".join(format("- having {!M} removes {}", retained,
                ", ".join(format("{!M}", excluded) for excluded in excludeds))
            for retained, excludeds in overrides.items())
        or "No roles registered", allowed_mentions=AllowedMentions.none())

@config.command("add")
async def config_add(ctx: Context, retained_role: PartialRoleConverter, excluded_role: PartialRoleConverter) -> None:
    async with sessionmaker() as session:
        session.add(Override(retained_role_id=retained_role.id, excluded_role_id=excluded_role.id))
        await session.commit()
        await ctx.send("\u2705")

@config.command("remove")
async def config_remove(ctx: Context, retained_role: PartialRoleConverter, excluded_role: PartialRoleConverter) -> None:
    async with sessionmaker() as session:
        await session.delete(await session.get(Override, (retained_role.id, excluded_role.id)))
        await session.commit()
        await ctx.send("\u2705")

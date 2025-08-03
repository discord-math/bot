from typing import TYPE_CHECKING, List, Set, cast

from discord import AllowedMentions, Member
from discord.ext.commands import group
from sqlalchemy import BigInteger, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.acl import privileged
from bot.cogs import Cog, cog
from bot.commands import Context
from bot.config import plugin_config_command
import plugins
import util.db
import util.db.kv
from util.discord import PartialRoleConverter, format, retry


registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

sessionmaker = async_sessionmaker(util.db.engine)


@registry.mapped
class PersistedRole:
    __tablename__ = "roles"
    __table_args__ = {"schema": "persistence"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, *, id: int) -> None: ...


@registry.mapped
class MemberRole:
    __tablename__ = "member_roles"
    __table_args__ = {"schema": "persistence"}

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


persisted_roles: Set[int]


async def rehash_roles(session: AsyncSession) -> None:
    global persisted_roles
    stmt = select(PersistedRole.id)
    persisted_roles = set((await session.execute(stmt)).scalars())


@plugins.init
async def init() -> None:
    global persisted_roles
    await util.db.init(util.db.get_ddl(CreateSchema("persistence"), registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        if conf.roles is not None:
            for id in cast(List[int], conf.roles):
                session.add(PersistedRole(id=id))
            await session.commit()
            conf.roles = None
            await conf

        await rehash_roles(session)


@cog
class Persistence(Cog):
    """Role persistence."""

    @Cog.listener()
    async def on_member_remove(self, member: Member) -> None:
        role_ids = set(role.id for role in member.roles if role.id in persisted_roles)
        if len(role_ids) == 0:
            return
        async with sessionmaker() as session:
            stmt = (
                insert(MemberRole)
                .values([{"user_id": member.id, "role_id": role_id} for role_id in role_ids])
                .on_conflict_do_nothing(index_elements=["user_id", "role_id"])
            )
            await session.execute(stmt)
            await session.commit()

    @Cog.listener()
    async def on_member_join(self, member: Member) -> None:
        async with sessionmaker() as session:
            stmt = delete(MemberRole).where(MemberRole.user_id == member.id).returning(MemberRole.role_id)
            roles = []
            for (role_id,) in await session.execute(stmt):
                if (role := member.guild.get_role(role_id)) is not None:
                    roles.append(role)
            if len(roles) == 0:
                return
            await retry(lambda: member.add_roles(*roles, reason="Role persistence", atomic=False))
            await session.commit()


async def drop_persistent_role(*, user_id: int, role_id: int) -> None:
    async with sessionmaker() as session:
        stmt = delete(MemberRole).where(MemberRole.user_id == user_id, MemberRole.role_id == role_id)
        await session.execute(stmt)
        await session.commit()


@plugin_config_command
@group("persistence", invoke_without_command=True)
@privileged
async def config(ctx: Context) -> None:
    async with sessionmaker() as session:
        stmt = select(PersistedRole.id)
        roles = (await session.execute(stmt)).scalars()
        await ctx.send(
            ", ".join(format("{!M}", id) for id in roles) or "No roles registered",
            allowed_mentions=AllowedMentions.none(),
        )


@config.command("add")
@privileged
async def config_add(ctx: Context, role: PartialRoleConverter) -> None:
    async with sessionmaker() as session:
        session.add(PersistedRole(id=role.id))
        await session.commit()
        await rehash_roles(session)
        await ctx.send("\u2705")


@config.command("remove")
@privileged
async def config_remove(ctx: Context, role: PartialRoleConverter) -> None:
    async with sessionmaker() as session:
        await session.delete(await session.get(PersistedRole, role.id))
        await session.commit()
        await rehash_roles(session)
        await ctx.send("\u2705")

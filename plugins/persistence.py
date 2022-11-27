from typing import Protocol, cast

from discord import Member
from sqlalchemy import BigInteger, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.cogs import Cog, cog
import plugins
import util.db
import util.db.kv
from util.frozen_list import FrozenList

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True)

@registry.mapped
class MemberRole:
    __tablename__ = "member_roles"
    __table_args__ = {"schema": "persistence"}

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

class PersistenceConf(Protocol):
    roles: FrozenList[int]

conf: PersistenceConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(PersistenceConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        CreateSchema("persistence"),
        registry.metadata.create_all))

@cog
class Persistence(Cog):
    """Role persistence."""
    @Cog.listener()
    async def on_member_remove(self, member: Member) -> None:
        role_ids = set(role.id for role in member.roles if role.id in conf.roles)
        if len(role_ids) == 0: return
        async with sessionmaker() as session:
            stmt = (insert(MemberRole)
                .values([{"user_id": member.id, "role_id": role_id} for role_id in role_ids])
                .on_conflict_do_nothing(index_elements=["user_id", "role_id"]))
            await session.execute(stmt)
            await session.commit()

    @Cog.listener()
    async def on_member_join(self, member: Member) -> None:
        async with sessionmaker() as session:
            stmt = delete(MemberRole).where(MemberRole.user_id == member.id).returning(MemberRole.role_id)
            roles = []
            for role_id, in await session.execute(stmt):
                if (role := member.guild.get_role(role_id)) is not None:
                    roles.append(role)
            if len(roles) == 0: return
            await member.add_roles(*roles, reason="Role persistence", atomic=False)
            await session.commit()

async def drop_persistent_role(*, user_id: int, role_id: int) -> None:
    async with sessionmaker() as session:
        stmt = delete(MemberRole).where(MemberRole.user_id == user_id, MemberRole.role_id == role_id)
        await session.execute(stmt)
        await session.commit()

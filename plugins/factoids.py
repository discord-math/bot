from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING, Mapping, Optional, Protocol, TypedDict, Union, cast, overload
from typing_extensions import NotRequired

from discord import AllowedMentions, Embed, Message, MessageReference, Thread
from discord.abc import GuildChannel
from sqlalchemy import TEXT, TIMESTAMP, BigInteger, ForeignKey, Integer, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, aliased, mapped_column, raiseload, relationship
from sqlalchemy.schema import CreateSchema

from bot.acl import EvalResult, evaluate_acl, evaluate_ctx, privileged, register_action
from bot.cogs import Cog, cog, group
from bot.commands import Context, cleanup
from bot.reactions import get_input, get_reaction
import plugins
import util.db
import util.db.kv
from util.discord import CodeBlock, Inline, InvocationError, Quoted, UserError, format

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True)

class Flags(TypedDict):
    mentions: NotRequired[bool]
    acl: NotRequired[str]

@registry.mapped
class Factoid:
    __tablename__ = "factoids"
    __table_args__ = {"schema": "factoids"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_text: Mapped[Optional[str]] = mapped_column(TEXT)
    embed_data: Mapped[Optional[Mapping[str, object]]] = mapped_column(JSONB)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    uses: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    flags: Mapped[Optional[Flags]] = mapped_column(JSONB)

    if TYPE_CHECKING:
        def __init__(self, /, author_id: int, created_at: datetime, uses: int, id: Optional[int] = ...,
            message_text: Optional[str] = ..., embed_data: Optional[Mapping[str, object]] = ...,
            used_at: Optional[datetime] = ..., flags: Optional[Mapping[str, object]] = ...) -> None: ...

@registry.mapped
class Alias:
    __tablename__ = "aliases"
    __table_args__ = {"schema": "factoids"}

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, ForeignKey(Factoid.id), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    uses: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    factoid: Mapped[Factoid] = relationship(Factoid, lazy="joined")

    if TYPE_CHECKING:
        @overload
        def __init__(self, *, name: str, author_id: int, created_at: datetime, uses: int,
            factoid: Factoid, used_at: Optional[datetime] = ...) -> None: ...
        @overload
        def __init__(self, *, name: str, author_id: int, created_at: datetime, uses: int,
            id: int, used_at: Optional[datetime] = ...) -> None: ...
        def __init__(self, *, name: str, author_id: int, created_at: datetime, uses: int,
            factoid: Optional[Factoid] = ..., id: Optional[int] = ..., used_at: Optional[datetime] = ...
            ) -> None: ...

class FactoidsConf(Protocol):
    prefix: str

conf: FactoidsConf

use_tags = register_action("use_tags")
manage_tag_flags = register_action("manage_tag_flags")

@plugins.init
async def init() -> None:
    global conf
    conf = cast(FactoidsConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        CreateSchema("factoids"),
        registry.metadata.create_all))

@cog
class Factoids(Cog):
    """Manage factoids."""
    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if msg.author.bot: return
        if not isinstance(msg.channel, (GuildChannel, Thread)): return
        if not msg.content.startswith(conf.prefix): return
        if use_tags.evaluate(msg.author, msg.channel) != EvalResult.TRUE: return
        text = " ".join(msg.content[len(conf.prefix):].split()).lower()
        if not len(text): return
        async with sessionmaker() as session:
            stmt = (select(Alias)
                .where(Alias.name == func.substring(text, 1, func.length(Alias.name)))
                .order_by(func.length(Alias.name).desc())
                .limit(1))
            if (alias := (await session.execute(stmt)).scalar()) is None: return

            mentions = AllowedMentions.none()
            if (flags := alias.factoid.flags) is not None:
                if "acl" in flags and evaluate_acl(flags["acl"], msg.author, msg.channel) != EvalResult.TRUE:
                    return
                if "mentions" in flags and flags["mentions"]:
                    mentions = AllowedMentions(roles=True, users=True)

            embed = Embed.from_dict(alias.factoid.embed_data) if alias.factoid.embed_data is not None else None
            if msg.reference is not None and msg.reference.message_id is not None:
                reference = MessageReference(guild_id=msg.reference.guild_id,
                    channel_id=msg.reference.channel_id, message_id=msg.reference.message_id,
                    fail_if_not_exists=False)
                if embed is not None:
                    await msg.channel.send(alias.factoid.message_text, embed=embed, reference=reference,
                        allowed_mentions=mentions)
                else:
                    await msg.channel.send(alias.factoid.message_text, reference=reference,
                        allowed_mentions=mentions)
            else:
                if embed is not None:
                    await msg.channel.send(alias.factoid.message_text, embed=embed,
                        allowed_mentions=mentions)
                else:
                    await msg.channel.send(alias.factoid.message_text,
                        allowed_mentions=mentions)

            alias.factoid.uses += 1
            alias.factoid.used_at = datetime.utcnow()
            alias.uses += 1
            alias.used_at = datetime.utcnow()
            await session.commit()

    @cleanup
    @group("tag")
    @privileged
    async def tag_command(self, ctx: Context) -> None:
        """Manage factoids."""
        pass

    @privileged
    @tag_command.command("add")
    async def tag_add(self, ctx: Context, *, name: str) -> None:
        """Add a factoid. You will be prompted to enter the contents as a separate message."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if await session.get(Alias, name, options=(raiseload(Alias.factoid),)) is not None:
                raise UserError(format("The factoid {!i} already exists", conf.prefix + name))

            content = await prompt_contents(ctx)
            if not content: return

            session.add(Alias(
                name=name,
                author_id=ctx.author.id,
                created_at=datetime.utcnow(),
                uses=0,
                factoid=Factoid(
                    message_text=content if isinstance(content, str) else None,
                    embed_data=content.to_dict() if not isinstance(content, str) else None,
                    author_id=ctx.author.id,
                    created_at=datetime.utcnow(),
                    uses=0)))
            await session.commit()
        await ctx.send(format("Factoid created, use with {!i}", conf.prefix + name))

    @privileged
    @tag_command.command("alias")
    async def tag_alias(self, ctx: Context, name: str, *, newname: str) -> None:
        """
        Alias a factoid. Both names will lead to the same output.
        If the original factoid contains spaces, it would need to be quoted.
        """
        name = validate_name(name)
        newname = " ".join(newname.split()).lower()
        async with sessionmaker() as session:
            if await session.get(Alias, newname, options=(raiseload(Alias.factoid),)) is not None:
                raise UserError(
                    format("The factoid {!i} already exists", conf.prefix + newname))
            if (alias := await session.get(Alias, name, options=(raiseload(Alias.factoid),))) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))

            session.add(Alias(
                name=newname,
                author_id=ctx.author.id,
                created_at=datetime.utcnow(),
                uses=0,
                id=alias.id))
            await session.commit()
        await ctx.send(format("Aliased {!i} to {!i}", conf.prefix + newname, conf.prefix + name))

    @privileged
    @tag_command.command("edit")
    async def tag_edit(self, ctx: Context, *, name: str) -> None:
        """
        Edit a factoid (and all factoids aliased to it).
        You will be prompted to enter the contents as a separate message.
        """
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))

            if alias.factoid.flags is not None and manage_tag_flags.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                raise UserError(format(
                    "This factoid can only be edited by admins because it has special behaviors"))

            content = await prompt_contents(ctx)
            if not content: return

            alias.factoid.message_text = content if isinstance(content, str) else None
            alias.factoid.embed_data = content.to_dict() if not isinstance(content, str) else None
            alias.factoid.author_id = ctx.author.id
            await session.commit()
        await ctx.send(format("Factoid updated, use with {!i}", conf.prefix + name))

    @privileged
    @tag_command.command("unalias")
    async def tag_unalias(self, ctx: Context, *, name: str) -> None:
        """
        Remove an alias for a factoid. The last name for a factoid cannot be removed (use delete instead).
        """
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))
            stmt = select(True).where(Alias.id == alias.id, Alias.name != alias.name).limit(1)
            if not (await session.execute(stmt)).scalar():
                raise UserError("Cannot remove the last alias")

            await session.delete(alias)
            await session.commit()
        await ctx.send(format("Alias removed"))

    @privileged
    @tag_command.command("delete")
    async def tag_delete(self, ctx: Context, *, name: str) -> None:
        """Delete a factoid and all its aliases."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))

            stmt = delete(Alias).where(Alias.id == alias.id)
            await session.execute(stmt)
            stmt = delete(Factoid).where(Factoid.id == alias.id)
            await session.execute(stmt)
            await session.commit()
        await ctx.send(format("Factoid deleted"))

    @privileged
    @tag_command.command("info")
    async def tag_info(self, ctx: Context, *, name: str) -> None:
        """Show information about a factoid."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))

            stmt = select(Alias).where(Alias.id == alias.id).order_by(Alias.uses.desc())
            aliases = (await session.execute(stmt)).scalars()

            created_at = int(alias.factoid.created_at.replace(tzinfo=timezone.utc).timestamp())
            used_at = None
            if alias.factoid.used_at is not None:
                used_at = int(alias.factoid.used_at.replace(tzinfo=timezone.utc).timestamp())
            await ctx.send(format(
                    "Created by {!m} on <t:{}:F> (<t:{}:R>). Used {} times{}.{}\nAliases: {}",
                    alias.factoid.author_id, created_at, created_at, alias.factoid.uses,
                    "" if used_at is None else ", last on <t:{}:F> (<t:{}:R>)".format(used_at, used_at),
                    "" if alias.factoid.flags is None else " Has flags.",
                    ", ".join(format("{!i} ({} uses)",
                        conf.prefix + alias.name, alias.uses) for alias in aliases)),
                allowed_mentions=AllowedMentions.none())

    @privileged
    @tag_command.command("top")
    async def tag_top(self, ctx: Context) -> None:
        """Show most used factoids."""
        async with sessionmaker() as session:
            aliases = aliased(Alias)
            stmt = (select(Alias.name, Factoid.uses)
                .join(Alias.factoid)
                .where(Alias.name == (select(aliases.name)
                    .where(aliases.id == Factoid.id)
                    .order_by(aliases.uses.desc())
                    .limit(1)
                    .scalar_subquery()))
                .order_by(Factoid.uses.desc())
                .limit(20))
            results = list(await session.execute(stmt))
            await ctx.send("\n".join(format("{!i}: {} uses", conf.prefix + name, uses)
                for name, uses in results))

    @privileged
    @tag_command.command("flags")
    async def tag_flags(self, ctx: Context, name: str,
        flags: Optional[Union[CodeBlock, Inline, Quoted]]) -> None:
        """
        Configure admin-only flags for a factoid. The flags are a JSON dictionary with the following keys:
        - "mentions": a boolean, if true, makes the factoid invocation ping the roles and users it involves
        - "acl": a string referring to an ACL (configurable with `acl`) required to use the factoid
        """
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise UserError(format("The factoid {!i} does not exist", conf.prefix + name))

            if flags is None:
                await ctx.send(format("{!i}", json.dumps(alias.factoid.flags)))
            else:
                alias.factoid.flags = json.loads(flags.text)
                await session.commit()
                await ctx.send("\u2705")

async def prompt_contents(ctx: Context) -> Optional[Union[str, Embed]]:
    prompt = await ctx.send("Please enter the factoid contents:")
    response = await get_input(prompt, ctx.author, {"\u274C": None}, timeout=300)
    if response is None: return None

    try:
        embed_data = json.loads(response.content)
    except:
        pass
    else:
        if manage_tag_flags.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
            raise UserError("Creating factoids with embeds is only available for moderators")
        try:
            embed = Embed.from_dict(embed_data)
        except Exception as exc:
            raise InvocationError("Could not parse embed data: {!r}".format(exc))

        prompt = await ctx.channel.send("Embed preview:", embed=embed)
        if not await get_reaction(prompt, ctx.author, {"\u2705": True, "\u274C": False}):
            await ctx.send("Cancelled.")
            return None
        return embed
    return response.content

def validate_name(name: str) -> str:
    name = " ".join(name.split()).lower()
    if len(name) == 0:
        raise InvocationError("Factoid name must be nonempty")
    else:
        return name

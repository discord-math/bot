import sqlalchemy
import sqlalchemy.schema
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
import datetime
import json
import discord
import discord.ext.commands
from typing import Optional, Union, Any, Protocol, cast, TYPE_CHECKING, overload
import util.discord
import util.db
import util.db.kv
import plugins
import plugins.commands
import plugins.locations
import plugins.privileges
import plugins.cogs
import plugins.reactions

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = sqlalchemy.ext.asyncio.async_sessionmaker(engine, future=True)

@registry.mapped
class Factoid:
    __tablename__ = "factoids"
    __table_args__ = {"schema": "factoids"}

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer, primary_key=True)
    message_text: sqlalchemy.orm.Mapped[Optional[str]] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT)
    embed_data: sqlalchemy.orm.Mapped[Optional[Any]] = sqlalchemy.orm.mapped_column(
        sqlalchemy.dialects.postgresql.JSONB)
    author_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(sqlalchemy.TIMESTAMP,
        nullable=False)
    uses: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    used_at: sqlalchemy.orm.Mapped[Optional[datetime.datetime]] = sqlalchemy.orm.mapped_column(sqlalchemy.TIMESTAMP)

    if TYPE_CHECKING:
        def __init__(self, /, author_id: int, created_at: datetime.datetime, uses: int, id: Optional[int] = ...,
            message_text: Optional[str] = ..., embed_data: Optional[Any] = ...,
            used_at: Optional[datetime.datetime] = ...) -> None: ...

@registry.mapped
class Alias:
    __tablename__ = "aliases"
    __table_args__ = {"schema": "factoids"}

    name: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, primary_key=True)
    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer,
        sqlalchemy.ForeignKey(Factoid.id), nullable=False) # type: ignore
    author_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(sqlalchemy.TIMESTAMP,
        nullable=False)
    uses: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    used_at: sqlalchemy.orm.Mapped[Optional[datetime.datetime]] = sqlalchemy.orm.mapped_column(sqlalchemy.TIMESTAMP)

    factoid: sqlalchemy.orm.Mapped[Factoid] = sqlalchemy.orm.relationship(Factoid, lazy="joined")

    if TYPE_CHECKING:
        @overload
        def __init__(self, *, name: str, author_id: int, created_at: datetime.datetime, uses: int,
            factoid: Factoid, used_at: Optional[datetime.datetime] = ...) -> None: ...
        @overload
        def __init__(self, *, name: str, author_id: int, created_at: datetime.datetime, uses: int,
            id: int, used_at: Optional[datetime.datetime] = ...) -> None: ...
        def __init__(self, *, name: str, author_id: int, created_at: datetime.datetime, uses: int,
            factoid: Optional[Factoid] = ..., id: Optional[int] = ..., used_at: Optional[datetime.datetime] = ...
            ) -> None: ...

class FactoidsConf(Protocol):
    prefix: str

conf: FactoidsConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(FactoidsConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        sqlalchemy.schema.CreateSchema("factoids"),
        registry.metadata.create_all))


@plugins.cogs.cog
class Factoids(discord.ext.commands.Cog):
    """Manage factoids."""
    @discord.ext.commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot: return
        if not isinstance(msg.channel, (discord.abc.GuildChannel, discord.Thread)): return
        if not msg.content.startswith(conf.prefix): return
        if not plugins.locations.in_location("factoids", msg.channel): return
        text = " ".join(msg.content[len(conf.prefix):].split()).lower()
        if not len(text): return
        async with sessionmaker() as session:
            stmt = (sqlalchemy.select(Alias)
                .where(Alias.name == sqlalchemy.func.substring(text, 1, sqlalchemy.func.length(Alias.name)))
                .order_by(sqlalchemy.func.length(Alias.name).desc())
                .limit(1))
            if (alias := (await session.execute(stmt)).scalar()) is None: return
            embed = discord.Embed.from_dict(alias.factoid.embed_data) if alias.factoid.embed_data is not None else None
            if msg.reference is not None and msg.reference.message_id is not None:
                reference = discord.MessageReference(guild_id=msg.reference.guild_id,
                    channel_id=msg.reference.channel_id, message_id=msg.reference.message_id,
                    fail_if_not_exists=False)
                if embed is not None:
                    await msg.channel.send(alias.factoid.message_text, embed=embed, reference=reference,
                        allowed_mentions=discord.AllowedMentions.none())
                else:
                    await msg.channel.send(alias.factoid.message_text, reference=reference,
                        allowed_mentions=discord.AllowedMentions.none())
            else:
                if embed is not None:
                    await msg.channel.send(alias.factoid.message_text, embed=embed,
                        allowed_mentions=discord.AllowedMentions.none())
                else:
                    await msg.channel.send(alias.factoid.message_text,
                        allowed_mentions=discord.AllowedMentions.none())

            alias.factoid.uses += 1
            alias.factoid.used_at = datetime.datetime.utcnow()
            alias.uses += 1
            alias.used_at = datetime.datetime.utcnow()
            await session.commit()

    @plugins.commands.cleanup
    @discord.ext.commands.group("tag")
    async def tag_command(self, ctx: discord.ext.commands.Context) -> None:
        """Manage factoids."""
        pass

    @plugins.privileges.priv("factoids")
    @tag_command.command("add")
    async def tag_add(self, ctx: discord.ext.commands.Context, *, name: str) -> None:
        """Add a factoid. You will be prompted to enter the contents as a separate message."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if await session.get(Alias, name, options=(sqlalchemy.orm.raiseload(Alias.factoid),)) is not None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} already exists", conf.prefix + name))

            content = await prompt_contents(ctx)
            if content is None: return

            session.add(Alias(
                name=name,
                author_id=ctx.author.id,
                created_at=datetime.datetime.utcnow(),
                uses=0,
                factoid=Factoid(
                    message_text=content if isinstance(content, str) else None,
                    embed_data=content.to_dict() if not isinstance(content, str) else None,
                    author_id=ctx.author.id,
                    created_at=datetime.datetime.utcnow(),
                    uses=0)))
            await session.commit()
        await ctx.send(util.discord.format("Factoid created, use with {!i}", conf.prefix + name))

    @plugins.privileges.priv("factoids")
    @tag_command.command("alias")
    async def tag_alias(self, ctx: discord.ext.commands.Context, name: str, *, newname: str) -> None:
        """
        Alias a factoid. Both names will lead to the same output.
        If the original factoid contains spaces, it would need to be quoted.
        """
        name = validate_name(name)
        newname = " ".join(newname.split()).lower()
        async with sessionmaker() as session:
            if await session.get(Alias, newname, options=(sqlalchemy.orm.raiseload(Alias.factoid),)) is not None:
                raise util.discord.UserError(
                    util.discord.format("The factoid {!i} already exists", conf.prefix + newname))
            if (alias := await session.get(Alias, name, options=(sqlalchemy.orm.raiseload(Alias.factoid),))) is None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))

            session.add(Alias(
                name=newname,
                author_id=ctx.author.id,
                created_at=datetime.datetime.utcnow(),
                uses=0,
                id=alias.id))
            await session.commit()
        await ctx.send(util.discord.format("Aliased {!i} to {!i}", conf.prefix + newname, conf.prefix + name))

    @plugins.privileges.priv("factoids")
    @tag_command.command("edit")
    async def tag_edit(self, ctx: discord.ext.commands.Context, *, name: str) -> None:
        """
        Edit a factoid (and all factoids aliased to it).
        You will be prompted to enter the contents as a separate message.
        """
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))

            content = await prompt_contents(ctx)
            if content is None: return

            alias.factoid.message_text = content if isinstance(content, str) else None
            alias.factoid.embed_data = content.to_dict() if not isinstance(content, str) else None
            alias.factoid.author_id = ctx.author.id
            await session.commit()
        await ctx.send(util.discord.format("Factoid updated, use with {!i}", conf.prefix + name))

    @plugins.privileges.priv("factoids")
    @tag_command.command("unalias")
    async def tag_unalias(self, ctx: discord.ext.commands.Context, *, name: str) -> None:
        """
        Remove an alias for a factoid. The last name for a factoid cannot be removed (use delete instead).
        """
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))
            stmt = (sqlalchemy.select(True)
                .where(Alias.id == alias.id, Alias.name != alias.name)
                .limit(1))
            if not (await session.execute(stmt)).scalar():
                raise util.discord.UserError("Cannot remove the last alias")

            await session.delete(alias)
            await session.commit()
        await ctx.send(util.discord.format("Alias removed"))

    @plugins.privileges.priv("factoids")
    @tag_command.command("delete")
    async def tag_delete(self, ctx: discord.ext.commands.Context, *, name: str) -> None:
        """Delete a factoid and all its aliases."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))

            stmt = (sqlalchemy.delete(Alias)
                .where(Alias.id == alias.id))
            await session.execute(stmt)
            stmt = (sqlalchemy.delete(Factoid)
                .where(Factoid.id == alias.id))
            await session.execute(stmt)
            await session.commit()
        await ctx.send(util.discord.format("Factoid deleted"))

    @tag_command.command("info")
    async def tag_info(self, ctx: discord.ext.commands.Context, *, name: str) -> None:
        """Show information about a factoid."""
        name = validate_name(name)
        async with sessionmaker() as session:
            if (alias := await session.get(Alias, name)) is None:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))

            stmt = (sqlalchemy.select(Alias)
                .where(Alias.id == alias.id)
                .order_by(Alias.uses.desc()))
            aliases = (await session.execute(stmt)).scalars()

            created_at = int(alias.factoid.created_at.replace(tzinfo=datetime.timezone.utc).timestamp())
            used_at = None
            if alias.factoid.used_at is not None:
                used_at = int(alias.factoid.used_at.replace(tzinfo=datetime.timezone.utc).timestamp())
            await ctx.send(util.discord.format(
                    "Created by {!m} on <t:{}:F> (<t:{}:R>). Used {} times{}.\nAliases: {}",
                    alias.factoid.author_id, created_at, created_at, alias.factoid.uses,
                    "" if used_at is None else ", last on <t:{}:F> (<t:{}:R>)".format(used_at, used_at),
                    ", ".join(util.discord.format("{!i} ({} uses)",
                        conf.prefix + alias.name, alias.uses) for alias in aliases)),
                allowed_mentions=discord.AllowedMentions.none())

    @tag_command.command("top")
    async def tag_top(sef, ctx: discord.ext.commands.Context) -> None:
        """Show most used factoids."""
        async with sessionmaker() as session:
            aliases = sqlalchemy.orm.aliased(Alias)
            stmt = (sqlalchemy.select(Alias.name, Factoid.uses)
                .join(Alias.factoid)
                .where(Alias.name == (sqlalchemy.select(aliases.name)
                    .where(aliases.id == Factoid.id)
                    .order_by(aliases.uses.desc())
                    .limit(1)
                    .scalar_subquery()))
                .order_by(Factoid.uses.desc())
                .limit(20))
            results = list(await session.execute(stmt))
            await ctx.send("\n".join(util.discord.format("{!i}: {} uses", conf.prefix + name, uses)
                for name, uses in results))

async def prompt_contents(ctx: discord.ext.commands.Context) -> Optional[Union[str, discord.Embed]]:
    prompt = await ctx.send("Please enter the factoid contents:")
    response = await plugins.reactions.get_input(prompt, ctx.author, {"\u274C": None}, timeout=300)
    if response is None: return None

    try:
        embed_data = json.loads(response.content)
    except:
        pass
    else:
        if not plugins.privileges.PrivCheck("admin")(ctx):
            raise util.discord.UserError("Creating factoids with embeds is only available for admins")
        try:
            embed = discord.Embed.from_dict(embed_data)
        except Exception as exc:
            raise util.discord.InvocationError("Could not parse embed data: {!r}".format(exc))

        prompt = await ctx.channel.send("Embed preview:", embed=embed)
        if not await plugins.reactions.get_reaction(prompt, ctx.author, {"\u2705": True, "\u274C": False}):
            await ctx.send("Cancelled.")
            return None
        return embed
    return response.content

def validate_name(name: str) -> str:
    name = " ".join(name.split()).lower()
    if len(name) == 0:
        raise util.discord.InvocationError("Factoid name must be nonempty")
    else:
        return name

import asyncio
import sqlalchemy
import sqlalchemy.schema
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
import datetime
import json
import discord
import discord.ext.commands
from typing import Optional, Any, Protocol, cast
import util.db
import util.db.kv
import plugins
import plugins.commands
import plugins.locations
import plugins.privileges
import plugins.cogs
import plugins.reactions
import discord_client

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

@registry.mapped
class Factoid:
    __tablename__ = "factoids"
    __table_args__ = {"schema": "factoids"}

    name: str = sqlalchemy.Column(sqlalchemy.TEXT, primary_key=True)
    message_text: Optional[str] = sqlalchemy.Column(sqlalchemy.TEXT)
    embed_data: Optional[Any] = sqlalchemy.Column(sqlalchemy.dialects.postgresql.JSONB)
    author_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    created_at: datetime.datetime = sqlalchemy.Column(sqlalchemy.TIMESTAMP, nullable=False)
    uses: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    used_at: Optional[datetime.datetime] = sqlalchemy.Column(sqlalchemy.TIMESTAMP)

class FactoidsConf(Protocol):
    prefix: str

conf: FactoidsConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(FactoidsConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        sqlalchemy.schema.CreateSchema("factoids").execute,
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
        words = msg.content[len(conf.prefix):].split(maxsplit=1)
        if len(words) == 0: return
        name = words[0].lower()
        async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
            print(name)
            if (factoid := await session.get(Factoid, name)) is None: return
            print(factoid)
            embed = discord.Embed.from_dict(factoid.embed_data) if factoid.embed_data is not None else None
            reference = None
            if msg.reference is not None and msg.reference.message_id is not None:
                reference = discord.MessageReference(guild_id=msg.reference.guild_id,
                    channel_id=msg.reference.channel_id, message_id=msg.reference.message_id,
                    fail_if_not_exists=False)
            await msg.channel.send(factoid.message_text, embed=embed, reference=reference,
                allowed_mentions=discord.AllowedMentions.none())

            factoid.uses += 1
            factoid.used_at = datetime.datetime.utcnow()
            await session.commit()

    @plugins.commands.cleanup
    @discord.ext.commands.group("tag")
    async def tag_command(self, ctx: discord.ext.commands.Context) -> None:
        """Manage factoids."""
        pass

    @plugins.privileges.priv("factoids")
    @tag_command.command("add")
    async def tag_add(self, ctx: discord.ext.commands.Context, name: str) -> None:
        """Add a factoid. You will be prompted to enter the contents as a separate message."""
        await create_tag(ctx, name, False)

    @plugins.privileges.priv("factoids")
    @tag_command.command("edit")
    async def tag_edit(self, ctx: discord.ext.commands.Context, name: str) -> None:
        """Edit a factoid. You will be prompted to enter the contents as a separate message."""
        await create_tag(ctx, name, True)

    @tag_command.command("top")
    async def tag_top(sef, ctx: discord.ext.commands.Context) -> None:
        """Show most used factoids."""
        async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
            stmt = (sqlalchemy.select(Factoid.name, Factoid.uses)
                .order_by(Factoid.uses.desc())
                .limit(20))
            results = list(await session.execute(stmt))
            await ctx.send("\n".join(util.discord.format("{!i}: {} uses", conf.prefix + name, uses)
                for name, uses in results))

async def create_tag(ctx: discord.ext.commands.Context, name: str, update: bool) -> None:
    name = name.lower()
    if any(c.isspace() for c in name):
        raise util.discord.InvocationError("No spaces allowed in factoid name")
    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        if (factoid := await session.get(Factoid, name)) is not None:
            if update:
                await session.delete(factoid)
            else:
                raise util.discord.UserError(util.discord.format("The factoid {!i} already exists", conf.prefix + name))
        else:
            if update:
                raise util.discord.UserError(util.discord.format("The factoid {!i} does not exist", conf.prefix + name))

        content = None
        prompt = await ctx.send("Please enter the factoid contents:")
        del_reaction = '\u274C'
        await prompt.add_reaction(del_reaction)
        with plugins.reactions.ReactionMonitor(channel_id=ctx.channel.id, message_id=prompt.id,
            author_id=ctx.author.id, event="add", filter=lambda _, p: p.emoji.name == del_reaction) as mon:
            msg_task = asyncio.create_task(
                discord_client.client.wait_for('message',
                    check=lambda msg: msg.channel == ctx.channel and msg.author == ctx.author))
            reaction_task = asyncio.ensure_future(mon)
            try:
                done, pending = await asyncio.wait((msg_task, reaction_task),
                    timeout=300, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.TimeoutError:
                await ctx.send("Prompt timed out.")

            if msg_task in done:
                content = msg_task.result().content
            elif reaction_task in done:
                await ctx.send("Prompt cancelled.")
            msg_task.cancel()
            reaction_task.cancel()

        if content is None: return

        embed = None
        try:
            embed_data = json.loads(content)
        except:
            pass
        else:
            if not plugins.privileges.PrivCheck("admin")(ctx):
                raise util.discord.UserError("Creating factoids with embeds is only available for admins")
            try:
                embed = discord.Embed.from_dict(embed_data)
            except Exception as exc:
                raise util.discord.InvocationError("Could not parse embed data: {!r}".format(exc))

            prompt = await ctx.channel.send("Embed preview", embed=embed)
            ok_reaction = '\u2705'
            del_reaction = '\u274C'
            await prompt.add_reaction(ok_reaction)
            await prompt.add_reaction(del_reaction)
            confirmed = False
            with plugins.reactions.ReactionMonitor(channel_id=ctx.channel.id, message_id=prompt.id,
                author_id=ctx.author.id, event="add",
                filter=lambda _, p: p.emoji.name in [ok_reaction, del_reaction], timeout_each=300) as mon:
                try:
                    confirmed = (await mon)[1].emoji.name == ok_reaction
                except asyncio.TimeoutError:
                    await ctx.send("Timed out.")
                else:
                    if not confirmed:
                        await ctx.send("Cancelled.")
            if not confirmed: return

        session.add(Factoid(
            name=name,
            message_text=content if embed is None else None,
            embed_data=embed.to_dict() if embed is not None else None, # type: ignore
            author_id=ctx.author.id,
            created_at=datetime.datetime.utcnow(),
            uses=factoid.uses if factoid is not None else 0))
        await session.commit()
        if update:
            await ctx.send(util.discord.format("Factoid updated, use with {!i}", conf.prefix + name))
        else:
            await ctx.send(util.discord.format("Factoid created, use with {!i}", conf.prefix + name))

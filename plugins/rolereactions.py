from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union, cast

import discord
from discord import (AllowedMentions, Emoji, Guild, Message, MessageReference, Object, PartialEmoji,
    RawReactionActionEvent)
from discord.abc import Snowflake
import discord.utils
from sqlalchemy import TEXT, BigInteger, ForeignKey, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column, raiseload, relationship
from sqlalchemy.schema import CreateSchema

from bot.acl import privileged
from bot.client import client
from bot.cogs import Cog, cog, group
from bot.commands import Context, cleanup
import plugins
import util.db.kv
from util.discord import (InvocationError, PartialRoleConverter, ReplyConverter, UserError, format, partial_from_reply,
    retry)

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine)

@registry.mapped
class ReactionMessage:
    __tablename__ = "messages"
    __table_args__ = {"schema": "role_reactions"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    reactions: Mapped[List["Reaction"]] = relationship("Reaction", lazy="joined")

    def reference(self) -> MessageReference:
        return MessageReference(guild_id=self.guild_id, channel_id=self.channel_id, message_id=self.id)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, guild_id: int, channel_id: int) -> None: ...

@registry.mapped
class Reaction:
    __tablename__ = "reactions"
    __table_args__ = {"schema": "role_reactions"}

    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(ReactionMessage.id), primary_key=True)
    # either a unicode emoji, or a discord emoji's ID converted to string
    emoji: Mapped[str] = mapped_column(TEXT, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, message_id: int, emoji: str, role_id: int) -> None: ...

reaction_messages: Set[int]

@plugins.init
async def init() -> None:
    global reaction_messages
    await util.db.init(util.db.get_ddl(
        CreateSchema("role_reactions"),
        registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for msg_id_str, in conf:
            msg_id = int(msg_id_str)
            obj = cast(Dict[str, Any], conf[msg_id])
            session.add(ReactionMessage(id=msg_id, guild_id=obj["guild"], channel_id=obj["channel"]))
            for emoji, role_id in obj["rolereacts"].items():
                session.add(Reaction(message_id=msg_id, emoji=emoji, role_id=role_id))
        await session.commit()
        for msg_id_str in [msg_id_str for msg_id_str, in conf]:
            conf[msg_id_str] = None
        await conf

        stmt = select(ReactionMessage.id)
        reaction_messages = set((await session.execute(stmt)).scalars())

async def find_message(channel_id: int, msg_id: int) -> Optional[Message]:
    channel = client.get_partial_messageable(channel_id)
    if channel is None: return None
    try:
        return await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.Forbidden):
        return None

def format_role(guild: Optional[Guild], role_id: int) -> str:
    role = discord.utils.find(lambda r: r.id == role_id, guild.roles if guild else ())
    if role is None:
        return format("{!i}", role_id)
    else:
        return format("{!M}({!i} {!i})", role, role.name, role.id)

def format_emoji(emoji_str: str) -> str:
    if emoji_str.isdigit():
        emoji = client.get_emoji(int(emoji_str))
        if emoji is not None and emoji.is_usable():
            return str(emoji) + format("({!i})", emoji)
    return format("{!i}", emoji_str)

def make_discord_emoji(emoji_str: str) -> Union[str, Emoji, None]:
    if emoji_str.isdigit():
        emoji = client.get_emoji(int(emoji_str))
        if emoji is not None and emoji.is_usable():
            return emoji
        return None
    else:
        return emoji_str

async def react_initial(channel_id: int, msg_id: int, emoji_str: str) -> None:
    react_msg = await find_message(channel_id, msg_id)
    if react_msg is None: return
    react_emoji = make_discord_emoji(emoji_str)
    if react_emoji is None: return
    try:
        await react_msg.add_reaction(react_emoji)
    except (discord.Forbidden, discord.NotFound):
        pass
    except discord.HTTPException as exc:
        if exc.text != "Unknown Emoji":
            raise

async def get_payload_role(session: AsyncSession, guild: Guild, payload: RawReactionActionEvent) -> Optional[Snowflake]:
    if payload.emoji.id is not None:
        emoji = str(payload.emoji.id)
    else:
        if payload.emoji.name is None: return None
        emoji = payload.emoji.name
    if obj := await session.get(Reaction, (payload.message_id, emoji)):
        return Object(obj.role_id)
    else:
        return None

@cog
class RoleReactions(Cog):
    """Manage role reactions."""
    @Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if (member := payload.member) is None: return
        if member.bot: return
        if payload.message_id not in reaction_messages: return
        async with sessionmaker() as session:
            if (role := await get_payload_role(session, member.guild, payload)) is None:
                return
        await retry(lambda: member.add_roles(role, reason="Role reactions on {}".format(payload.message_id)))

    @Cog.listener()
    async def on_raw_reaction_remove(self, payload: RawReactionActionEvent) -> None:
        if payload.guild_id is None: return
        if payload.message_id not in reaction_messages: return
        if (guild := client.get_guild(payload.guild_id)) is None: return
        if (member := guild.get_member(payload.user_id)) is None: return
        if member.bot: return
        async with sessionmaker() as session:
            if (role := await get_payload_role(session, member.guild, payload)) is None:
                return
        await retry(lambda: member.remove_roles(role, reason="Role reactions on {}".format(payload.message_id)))

    @cleanup
    @group("rolereact")
    @privileged
    async def rolereact_command(self, ctx: Context) -> None:
        """Manage role reactions."""
        pass

    @rolereact_command.command("new")
    @privileged
    async def rolereact_new(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """Make the given message a role react message."""
        msg = partial_from_reply(message, ctx)
        async with sessionmaker() as session:
            if await session.get(ReactionMessage, msg.id, options=[raiseload(ReactionMessage.reactions)]):
                raise UserError("Role reactions already exist on {}".format(msg.jump_url))

            if msg.guild is None:
                raise InvocationError("The message must be in a guild")

            session.add(ReactionMessage(id=msg.id, guild_id=msg.guild.id, channel_id=msg.channel.id))
            await session.commit()

        await ctx.send("Created role reactions on {}".format(msg.jump_url))

    @rolereact_command.command("delete")
    @privileged
    async def rolereact_delete(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """Make the given message not a role react message."""
        msg = partial_from_reply(message, ctx)
        async with sessionmaker() as session:
            if obj := await session.get(ReactionMessage, msg.id, options=[raiseload(ReactionMessage.reactions)]):
                stmt = delete(Reaction).where(Reaction.message_id == obj.id)
                await session.execute(stmt)
                await session.delete(obj)
                await session.commit()
            else:
                raise UserError("Role reactions do not exist on {}".format(msg.jump_url))

        await ctx.send("Removed role reactions on {}".format(msg.jump_url))

    @rolereact_command.command("list")
    @privileged
    async def rolereact_list(self, ctx: Context) -> None:
        """List role react messages."""
        async with sessionmaker() as session:
            stmt = select(ReactionMessage).options(raiseload(ReactionMessage.reactions))
            await ctx.send("Role reactions exist on:\n{}".format(
                "\n".join(obj.reference().jump_url for obj in (await session.execute(stmt)).scalars())))

    @rolereact_command.command("show")
    @privileged
    async def rolereact_show(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """List roles on a role react message."""
        msg = partial_from_reply(message, ctx)
        async with sessionmaker() as session:
            if obj := await session.get(ReactionMessage, msg.id):
                await ctx.send("Role reactions on {} include: {}".format(msg.jump_url, "; ".join(
                        "{} for {}".format(format_emoji(reaction.emoji), format_role(msg.guild, reaction.role_id))
                            for reaction in obj.reactions)),
                    allowed_mentions=AllowedMentions.none())

            else:
                raise UserError("Role reactions do not exist on {}".format(msg.jump_url))

    @rolereact_command.command("add")
    @privileged
    async def rolereact_add(self, ctx: Context, message: ReplyConverter, emoji: Union[PartialEmoji, str],
        role: PartialRoleConverter) -> None:
        """Add an emoji/role to a role react message."""
        async with sessionmaker() as session:
            if not (obj := await session.get(ReactionMessage, message.id)):
                raise UserError("Role reactions do not exist on {}".format(message.jump_url))

            emoji_str = str(emoji.id) if isinstance(emoji, PartialEmoji) else emoji
            if reaction := await session.get(Reaction, (message.id, emoji_str)):
                await ctx.send("Emoji {} already sets role {}".format(
                    format_emoji(emoji_str), format_role(message.guild, reaction.role_id)),
                    allowed_mentions=AllowedMentions.none())
                return

            obj.reactions.append(Reaction(message_id=message.id, emoji=emoji_str, role_id=role.id))
            await session.commit()

            await react_initial(message.channel.id, message.id, emoji_str)
            await ctx.send("Reacting with {} on message {} now sets {}".format(
                    format_emoji(emoji_str), message.jump_url, format_role(message.guild, role.id)),
                allowed_mentions=AllowedMentions.none())

    @rolereact_command.command("remove")
    @privileged
    async def rolereact_remove(self, ctx: Context, message: ReplyConverter, emoji: Union[PartialEmoji, str]) -> None:
        """Remove an emoji from a role react message."""
        async with sessionmaker() as session:
            if not await session.get(ReactionMessage, message.id):
                raise UserError("Role reactions do not exist on {}".format(message.jump_url))

            emoji_str = str(emoji.id) if isinstance(emoji, PartialEmoji) else emoji
            if not (reaction := await session.get(Reaction, (message.id, emoji_str))):
                await ctx.send("Role reactions for {} do not exist on {}".format(
                    format_emoji(emoji_str), message.jump_url))
                return

            await session.delete(reaction)
            await session.commit()

            await ctx.send("Reacting with {} on message {} no longer sets roles".format(
                    format_emoji(emoji_str), message.jump_url),
                allowed_mentions=AllowedMentions.none())

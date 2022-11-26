from typing import Awaitable, Iterator, Optional, Protocol, Tuple, TypedDict, Union, cast

import discord
from discord import AllowedMentions, Emoji, Guild, Message, Object, PartialEmoji, PartialMessage, RawReactionActionEvent
from discord.abc import Snowflake
from discord.ext.commands import Cog, group
import discord.utils

from bot.client import client
from bot.cogs import cog
from bot.commands import Context, cleanup
from bot.privileges import priv
import plugins
import util.db.kv
from util.discord import InvocationError, PartialRoleConverter, ReplyConverter, UserError, format, partial_from_reply
from util.frozen_dict import FrozenDict

class MessageReactions(TypedDict):
    guild: int
    channel: int
    rolereacts: FrozenDict[str, int]

class RoleReactionsConf(Awaitable[None], Protocol):
    def __getitem__(self, msg_id: int) -> Optional[MessageReactions]: ...
    def __setitem__(self, msg_id: int, obj: Optional[MessageReactions]) -> None: ...
    def __iter__(self) -> Iterator[Tuple[str]]: ...

conf: RoleReactionsConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RoleReactionsConf, await util.db.kv.load(__name__))

    for msg_id_str, in conf:
        obj = conf[int(msg_id_str)]
        assert obj is not None
        conf[int(msg_id_str)] = {"guild": int(obj["guild"]), "channel": int(obj["channel"]),
            "rolereacts": FrozenDict({emoji: int(role_id_str) for emoji, role_id_str in obj["rolereacts"].items()})}
    await conf

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

def format_msg(guild_id: int, channel_id: int, msg_id: int) -> str:
    return "https://discord.com/channels/{}/{}/{}".format(guild_id, channel_id, msg_id)

def format_partial_msg(msg: PartialMessage) -> str:
    assert msg.guild
    return format_msg(msg.guild.id, msg.channel.id, msg.id)

def retrieve_msg_link(msg_id: int) -> str:
    obj = conf[msg_id]
    assert obj is not None
    return format_msg(obj['guild'], obj['channel'], msg_id)

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

def get_payload_role(guild: Guild, payload: RawReactionActionEvent) -> Optional[Snowflake]:
    obj = conf[payload.message_id]
    if obj is None: return None
    if payload.emoji.id is not None:
        emoji = str(payload.emoji.id)
    else:
        if payload.emoji.name is None: return None
        emoji = payload.emoji.name
    if (role_id := obj['rolereacts'].get(emoji)) is None: return None
    return Object(role_id)

@cog
class RoleReactions(Cog):
    """Manage role reactions."""
    @Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if payload.member is None: return
        if payload.member.bot: return
        role = get_payload_role(payload.member.guild, payload)
        if role is None: return
        await payload.member.add_roles(role, reason="Role reactions on {}".format(payload.message_id))

    @Cog.listener()
    async def on_raw_reaction_remove(self, payload: RawReactionActionEvent) -> None:
        if payload.guild_id is None: return
        guild = client.get_guild(payload.guild_id)
        if guild is None: return
        member = guild.get_member(payload.user_id)
        if member is None: return
        if member.bot: return
        role = get_payload_role(guild, payload)
        if role is None: return
        await member.remove_roles(role, reason="Role reactions on {}".format(
            payload.message_id))

    @cleanup
    @group("rolereact")
    @priv("admin")
    async def rolereact_command(self, ctx: Context) -> None:
        """Manage role reactions."""
        pass

    @rolereact_command.command("new")
    async def rolereact_new(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """Make the given message a role react message."""
        msg = partial_from_reply(message, ctx)
        if conf[msg.id] is not None:
            raise UserError("Role reactions already exist on {}".format(format_partial_msg(msg)))
        if msg.guild is None:
            raise InvocationError("The message must be in a guild")
        conf[msg.id] = {"guild": msg.guild.id, "channel": msg.channel.id, "rolereacts": FrozenDict()}
        await conf
        await ctx.send("Created role reactions on {}".format(format_partial_msg(msg)))

    @rolereact_command.command("delete")
    async def rolereact_delete(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """Make the given message not a role react message."""
        msg = partial_from_reply(message, ctx)
        if conf[msg.id] is None:
            raise UserError("Role reactions do not exist on {}".format(format_partial_msg(msg)))
        conf[msg.id] = None
        await conf
        await ctx.send("Removed role reactions on {}".format(format_partial_msg(msg)))

    @rolereact_command.command("list")
    async def rolereact_list(self, ctx: Context) -> None:
        """List role react messages."""
        await ctx.send("Role reactions exist on:\n{}".format("\n".join(retrieve_msg_link(int(id)) for id, in conf)))

    @rolereact_command.command("show")
    async def rolereact_show(self, ctx: Context, message: Optional[ReplyConverter]) -> None:
        """List roles on a role react message."""
        msg = partial_from_reply(message, ctx)
        if (obj := conf[msg.id]) is None:
            raise UserError("Role reactions do not exist on {}".format(format_partial_msg(msg)))
        await ctx.send("Role reactions on {} include: {}".format(format_partial_msg(msg),
                "; ".join(("{} for {}".format(format_emoji(emoji), format_role(msg.guild, role))
                    for emoji, role in obj['rolereacts'].items()))),
            allowed_mentions=AllowedMentions.none())

    @rolereact_command.command("add")
    async def rolereact_add(self, ctx: Context, message: ReplyConverter, emoji: Union[PartialEmoji, str],
        role: PartialRoleConverter) -> None:
        """Add an emoji/role to a role react message."""
        if (obj := conf[message.id]) is None:
            raise UserError("Role reactions do not exist on {}".format(format_partial_msg(message)))
        emoji_str = str(emoji.id) if isinstance(emoji, PartialEmoji) else emoji
        if emoji_str in obj['rolereacts']:
            await ctx.send("Emoji {} already sets role {}".format(
                format_emoji(emoji_str), format_role(message.guild, obj['rolereacts'][emoji_str])),
                allowed_mentions=AllowedMentions.none())
            return
        obj = obj.copy()
        obj["rolereacts"] |= {emoji_str: role.id}
        conf[message.id] = obj
        await conf
        await react_initial(obj['channel'], message.id, emoji_str)
        await ctx.send("Reacting with {} on message {} now sets {}".format(
                format_emoji(emoji_str), format_partial_msg(message), format_role(message.guild, role.id)),
            allowed_mentions=AllowedMentions.none())

    @rolereact_command.command("remove")
    async def rolereact_remove(self, ctx: Context, message: ReplyConverter, emoji: Union[PartialEmoji, str]) -> None:
        """Remove an emoji from a role react message."""
        if (obj := conf[message.id]) is None:
            raise UserError("Role reactions do not exist on {}".format(format_partial_msg(message)))
        emoji_str = str(emoji.id) if isinstance(emoji, PartialEmoji) else emoji
        if emoji_str not in obj['rolereacts']:
            await ctx.send("Role reactions for {} do not exist on {}".format(
                format_emoji(emoji_str), format_partial_msg(message)))
            return
        obj = obj.copy()
        reacts = obj["rolereacts"].copy()
        del reacts[emoji_str]
        obj["rolereacts"] = FrozenDict(reacts)
        conf[message.id] = obj
        await ctx.send("Reacting with {} on message {} no longer sets roles".format(
                format_emoji(emoji_str), format_partial_msg(message)),
            allowed_mentions=AllowedMentions.none())

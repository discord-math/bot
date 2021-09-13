import re
import discord
import discord.utils
import discord_client
from typing import Tuple, Optional, Iterator, Union, TypedDict, Protocol, cast
import util.db.kv
import util.discord
import util.frozen_dict
import plugins.commands
import plugins.privileges
import discord.ext.commands
from util.discord import PartialEmojiConverter, PartialRoleConverter, PartialMessageConverter
import plugins.cogs

class MessageReactions(TypedDict):
    jump_url: str
    channel: str
    rolereacts: util.frozen_dict.FrozenDict[str, str]

class RoleReactionsConf(Protocol):
    def __getitem__(self, msg_id: str) -> Optional[MessageReactions]: ...
    def __setitem__(self, msg_id: str, obj: Optional[MessageReactions]) -> None: ...
    def __iter__(self) -> Iterator[str]: ...

conf: RoleReactionsConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RoleReactionsConf, await util.db.kv.load(__name__))

def format_role(guild: Optional[discord.Guild], role_id: str) -> str:
    role = discord.utils.find(lambda r: str(r.id) == role_id, guild.roles if guild else ())
    if role is None:
        return util.discord.format("{!i}", role_id)
    else:
        return util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)

def format_emoji(ctx: discord.ext.commands.Context, emoji_str: str) -> str:
    if emoji_str.isdigit():
        emoji = ctx.bot.get_emoji(int(emoji_str))
        if emoji is not None and emoji.is_usable():
            return str(emoji) + util.discord.format("({!i})", emoji)
    return util.discord.format("{!i}", emoji_str)

def get_reference(ctx: discord.ext.commands.Context,
    optional_msg: Optional[discord.PartialMessage]) -> discord.PartialMessage:
    if optional_msg is not None: return optional_msg
    if ctx.message.reference is not None:
                if (not isinstance(ctx.channel, discord.abc.GuildChannel)
                    or ctx.channel.id != ctx.message.reference.channel_id or ctx.message.reference.message_id is None):
                    raise discord.ext.commands.BadArgument("An invalid message reply was provided")
                return ctx.channel.get_partial_message(ctx.message.reference.message_id)
    raise discord.ext.commands.BadArgument("A message reply or argument is required")

def make_db_emoji(emoji: discord.PartialEmoji) -> str:
    assert emoji.name is not None
    if emoji.id is not None:
        return str(emoji.id)
    return emoji.name

async def react_initial(react_msg: discord.PartialMessage, react_emoji: discord.PartialEmoji) -> None:
    try:
        await react_msg.add_reaction(react_emoji)
    except (discord.Forbidden, discord.NotFound):
        pass
    except discord.HTTPException as exc:
        if exc.text != "Unknown Emoji":
            raise

def get_payload_role(guild: discord.Guild, payload: discord.RawReactionActionEvent) -> Optional[discord.Role]:
    obj = conf[str(payload.message_id)]
    if obj is None: return None
    if payload.emoji.id is not None:
        emoji = str(payload.emoji.id)
    else:
        if payload.emoji.name is None: return None
        emoji = payload.emoji.name
    if (emoji_id := obj['rolereacts'].get(emoji)) is None: return None
    return discord.utils.find(lambda r: str(r.id) == emoji_id, guild.roles)

@plugins.cogs.cog
class RoleReactions(discord.ext.typed_commands.Cog[discord.ext.commands.Context]):
    """Listen to reactions on messages that add or remove roles"""
    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member is None: return
        if payload.member.bot: return
        role = get_payload_role(payload.member.guild, payload)
        if role is None: return
        await payload.member.add_roles(role, reason="Role reactions on {}".format(payload.message_id))

    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None: return
        guild = discord_client.client.get_guild(payload.guild_id)
        if guild is None: return
        member = guild.get_member(payload.user_id)
        if member is None: return
        if member.bot: return
        role = get_payload_role(guild, payload)
        if role is None: return
        await member.remove_roles(role, reason="Role reactions on {}".format(payload.message_id))

@plugins.commands.command_ext("rolereact", cls=discord.ext.commands.Group)
@plugins.privileges.priv_ext("admin")
async def rolereact_command(ctx: discord.ext.commands.Context) -> None:
    """Manage messages that control roles based on reactions"""
    pass

@rolereact_command.command("new")
async def rolereact_new(ctx: discord.ext.commands.Context,
    optional_listener_msg: Optional[PartialMessageConverter]) -> None:
    """Create a reaction listener on a message"""
    listener_msg = get_reference(ctx, optional_listener_msg)
    listener_msg_index = str(listener_msg.id)
    obj = conf[listener_msg_index]
    if obj is not None:
        await ctx.send("Role reactions already exist on {}".format(obj['jump_url']))
        return
    conf[listener_msg_index] = {"jump_url": listener_msg.jump_url, "channel": str(listener_msg.channel.id),
        "rolereacts": util.frozen_dict.FrozenDict()}
    await ctx.send("Created role reactions on {}".format(listener_msg.jump_url))

@rolereact_command.command("delete")
async def rolereact_delete(ctx: discord.ext.commands.Context,
    optional_listener_msg: Optional[PartialMessageConverter]) -> None:
    """Delete a reaction listener on a message"""
    listener_msg = get_reference(ctx, optional_listener_msg)
    listener_msg_index = str(listener_msg.id)
    obj = conf[listener_msg_index]
    if obj is None:
        await ctx.send("Role reactions do not exist on {}".format(listener_msg.jump_url))
        return
    await ctx.send("Removed role reactions on {}".format(obj['jump_url']))
    conf[listener_msg_index] = None

@rolereact_command.command("list")
async def rolereact_list(ctx: discord.ext.commands.Context) -> None:
    """List all reaction listeners"""
    await ctx.send("Role reactions exist on: {}".format("\n".join(obj['jump_url'] for id in conf if (obj := conf[id]))))

@rolereact_command.command("show")
async def rolereact_show(ctx: discord.ext.commands.Context,
    optional_listener_msg: Optional[PartialMessageConverter]) -> None:
    """Show the emojis associated with roles on a message"""
    listener_msg = get_reference(ctx, optional_listener_msg)
    listener_msg_index = str(listener_msg.id)
    obj = conf[listener_msg_index]
    if obj is None:
        await ctx.send("Role reactions do not exist on {}".format(listener_msg.jump_url))
        return
    await ctx.send("Role reactions on {} include: {}".format(obj['jump_url'],
            "; ".join(("{} for {}".format(format_emoji(ctx, emoji), format_role(ctx.guild, role))
                for emoji, role in obj['rolereacts'].items()))),
        allowed_mentions=discord.AllowedMentions.none())

@rolereact_command.command("add")
async def rolereact_add(ctx: discord.ext.commands.Context,
    optional_listener_msg: Optional[PartialMessageConverter],
    react_emoji: PartialEmojiConverter, role: PartialRoleConverter) -> None:
    """Set the role an emoji sets on a reaction message"""
    listener_msg = get_reference(ctx, optional_listener_msg)
    listener_msg_index = str(listener_msg.id)
    emoji_db = make_db_emoji(react_emoji)
    obj = conf[listener_msg_index]
    if obj is None:
        await ctx.send("Role reactions do not exist on {}".format(listener_msg.jump_url))
        return
    if emoji_db in obj['rolereacts']:
        await ctx.send("Emoji {} already sets role {}".format(
                format_emoji(ctx, emoji_db), format_role(ctx.guild, obj['rolereacts'][emoji_db])),
            allowed_mentions=discord.AllowedMentions.none())
        return
    obj = obj.copy()
    obj["rolereacts"] |= {emoji_db: str(role.id)}
    conf[listener_msg_index] = obj
    await react_initial(listener_msg, react_emoji)
    await ctx.send(
        "Reacting with emoji {} on message {} now sets {}".format(
        format_emoji(ctx, emoji_db), obj['jump_url'],
        format_role(ctx.guild, str(role.id))),
        allowed_mentions=discord.AllowedMentions.none())

@rolereact_command.command("remove")
async def rolereact_remove(ctx: discord.ext.commands.Context,
    optional_listener_msg: Optional[PartialMessageConverter],
    react_emoji: PartialEmojiConverter) -> None:
    """Stops an emoji from controlling roles on a reaction message"""
    listener_msg = get_reference(ctx, optional_listener_msg)
    listener_msg_index = str(listener_msg.id)
    emoji_db = make_db_emoji(react_emoji)
    obj = conf[listener_msg_index]
    if obj is None:
        await ctx.send("Role reactions do not exist on {}".format(listener_msg.jump_url))
        return
    if emoji_db not in obj['rolereacts']:
        await ctx.send("Role reactions for emoji {} do not exist on {}".format(
            format_emoji(ctx, emoji_db), obj['jump_url']))
        return
    obj = obj.copy()
    reacts = obj["rolereacts"].copy()
    del reacts[emoji_db]
    obj['rolereacts'] = util.frozen_dict.FrozenDict(reacts)
    conf[listener_msg_index] = obj
    await ctx.send("Reacting with emoji {} on message {} no longer sets roles" .format(
            format_emoji(ctx, emoji_db), obj['jump_url']),
        allowed_mentions=discord.AllowedMentions.none())

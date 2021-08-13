import re
import discord
import discord.utils
from typing import Tuple, Optional, Iterator, Union, TypedDict, Protocol, cast
import discord_client
import util.db.kv
import util.discord
import util.frozen_dict
import plugins.commands
import plugins.privileges

class MessageReactions(TypedDict):
    guild: str
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

msg_id_re = re.compile(r"https?://(?:\w*\.)?(?:discord.com|discordapp.com)/channels/(\d+)/(\d+)/(\d+)")

async def find_message(channel_id: Union[str, int], msg_id: Union[str, int]) -> Optional[discord.Message]:
    channel = discord_client.client.get_channel(int(channel_id))
    if channel is None: return None
    if not isinstance(channel, discord.TextChannel): return None
    try:
        return await channel.fetch_message(int(msg_id))
    except (discord.NotFound, discord.Forbidden):
        return None

def find_role_id(guild: Optional[discord.Guild], role_text: str) -> int:
    role = util.discord.smart_find(role_text, guild.roles if guild else ())
    if role is None:
        raise util.discord.UserError("Multiple or no results for role {!i}", role_text)
    return role.id

def find_emoji_id(emoji_text: str) -> int:
    emoji = util.discord.smart_find(emoji_text, discord_client.client.emojis)
    if emoji is None:
        raise util.discord.UserError("Multiple or no results for emoji {!i}", emoji_text)
    return emoji.id

def format_role(guild: Optional[discord.Guild], role_id: str) -> str:
    role = discord.utils.find(lambda r: str(r.id) == role_id, guild.roles if guild else ())
    if role is None:
        return util.discord.format("{!i}", role_id)
    else:
        return util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)

def format_emoji(emoji_str: str) -> str:
    if emoji_str.isdigit():
        emoji = discord_client.client.get_emoji(int(emoji_str))
        if emoji is not None and emoji.is_usable():
            return str(emoji) + util.discord.format("({!i})", emoji)
    return util.discord.format("{!i}", emoji_str)

def format_msg(guild_id: str, channel_id: str, msg_id: str) -> str:
    return "https://discord.com/channels/{}/{}/{}".format(guild_id, channel_id, msg_id)

# retrieve the original message link used
def retrieve_msg_link(msg_id: str) -> str:
    obj = conf[msg_id]
    assert obj is not None
    return format_msg(obj['guild'], obj['channel'], msg_id)

def get_emoji(args: plugins.commands.ArgParser) -> Optional[str]:
    emoji_arg = args.next_arg(chunk_emoji=True)
    if isinstance(emoji_arg, plugins.commands.EmojiArg):
        return str(emoji_arg.id)
    elif isinstance(emoji_arg, plugins.commands.InlineCodeArg):
        return str(find_emoji_id(emoji_arg.text))
    elif isinstance(emoji_arg, plugins.commands.StringArg):
        return emoji_arg.text
    return None

def get_role(guild: Optional[discord.Guild], args: plugins.commands.ArgParser) -> Optional[str]:
    role_arg = args.next_arg()
    if isinstance(role_arg, plugins.commands.RoleMentionArg):
        return str(role_arg.id)
    elif isinstance(role_arg, plugins.commands.StringArg):
        return str(find_role_id(guild, role_arg.text))
    return None

def get_msg_ref(msg: discord.Message, args: plugins.commands.ArgParser) -> Optional[Tuple[str, str, str]]:
    if msg.reference is not None:
        if msg.reference.guild_id is None: return None
        if msg.reference.message_id is None: return None
        if msg.reference.channel_id != msg.channel.id: return None
        return (str(msg.reference.guild_id), str(msg.reference.channel_id), str(msg.reference.message_id))
    else:
        arg = args.next_arg()
        if not isinstance(arg, plugins.commands.StringArg): return None
        if (match := msg_id_re.match(arg.text)) is None: return None
        return match[1], match[2], match[3]

def make_discord_emoji(emoji_str: str) -> Union[str, discord.Emoji, None]:
    if emoji_str.isdigit():
        emoji = discord_client.client.get_emoji(int(emoji_str))
        if emoji is not None and emoji.is_usable():
            return emoji
        return None
    else:
        return emoji_str

async def react_initial(channel_id: str, msg_id: str, emoji_str: str) -> None:
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

@util.discord.event("raw_reaction_add")
async def rolereact_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.member is None: return
    if payload.member.bot: return
    role = get_payload_role(payload.member.guild, payload)
    if role is None: return
    await payload.member.add_roles(role, reason="Role reactions on {}".format(payload.message_id))

@util.discord.event("raw_reaction_remove")
async def rolereact_remove(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None: return
    guild = discord_client.client.get_guild(payload.guild_id)
    if guild is None: return
    member = guild.get_member(payload.user_id)
    if member is None: return
    if member.bot: return
    role = get_payload_role(guild, payload)
    if role is None: return
    await member.remove_roles(role, reason="Role reactions on {}".format(
        payload.message_id))

@plugins.commands.command("rolereact")
@plugins.privileges.priv("admin")
async def rolereact_command(msg: discord.Message, args: plugins.commands.ArgParser) -> None:
    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        role_msg_guild, role_msg_chan, role_msg = role_msg_info
        if conf[role_msg] is not None:
            await msg.channel.send("Role reactions already exist on {}".format(retrieve_msg_link(role_msg)))
            return
        conf[role_msg] = {"guild": role_msg_guild, "channel": role_msg_chan,
            "rolereacts": util.frozen_dict.FrozenDict()}
        await msg.channel.send("Created role reactions on {}".format(format_msg(*role_msg_info)))

    elif cmd.text.lower() == "delete":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        _, _, role_msg = role_msg_info
        if conf[role_msg] is None:
            await msg.channel.send("Role reactions do not exist on {}".format(format_msg(*role_msg_info)))
            return
        await msg.channel.send("Removed role reactions on {}".format(retrieve_msg_link(role_msg)))
        conf[role_msg] = None

    elif cmd.text.lower() == "list":
        await msg.channel.send("Role reactions exist on: {}".format("\n".join(retrieve_msg_link(id) for id in conf)))

    elif cmd.text.lower() == "show":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        _, _, role_msg = role_msg_info
        obj = conf[role_msg]
        if obj is None:
            await msg.channel.send("Role reactions do not exist on {}".format(format_msg(*role_msg_info)))
            return
        await msg.channel.send("Role reactions on {} include: {}".format(retrieve_msg_link(role_msg),
                "; ".join(("{} for {}".format(format_emoji(emoji), format_role(msg.guild, role))
                    for emoji, role in obj['rolereacts'].items()))),
            allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "add":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        role_msg_guild, role_msg_chan, role_msg = role_msg_info
        emoji = get_emoji(args)
        if emoji is None: return
        obj = conf[role_msg]
        if obj is None:
            await msg.channel.send("Role reactions do not exist on {}".format(format_msg(*role_msg_info)))
            return
        if emoji in obj['rolereacts']:
            await msg.channel.send("Emoji {} already sets role {}".format(
                    format_emoji(emoji), format_role(msg.guild, obj['rolereacts'][emoji])),
                allowed_mentions=discord.AllowedMentions.none())
            return
        role = get_role(msg.guild, args)
        if role is None: return
        obj = obj.copy()
        obj["rolereacts"] |= {emoji: role}
        conf[role_msg] = obj
        await react_initial(obj['channel'], role_msg, emoji)
        await msg.channel.send(
            "Reacting with emoji {} on message {} now sets {}".format(
            format_emoji(emoji), retrieve_msg_link(role_msg),
            format_role(msg.guild, role)),
            allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "remove":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        _, _, role_msg = role_msg_info
        emoji = get_emoji(args)
        if emoji is None: return
        obj = conf[role_msg]
        if obj is None:
            await msg.channel.send("Role reactions do not exist on {}"
                .format(format_msg(*role_msg_info)))
            return
        if emoji not in obj['rolereacts']:
            await msg.channel.send("Role reactions for emoji {} do not exist on {}".format(
                format_emoji(emoji), retrieve_msg_link(role_msg)))
            return
        obj = obj.copy()
        reacts = obj["rolereacts"].copy()
        del reacts[emoji]
        obj['rolereacts'] = util.frozen_dict.FrozenDict(reacts)
        conf[role_msg] = obj
        await msg.channel.send("Reacting with emoji {} on message {} no longer sets roles" .format(
                format_emoji(emoji), retrieve_msg_link(role_msg)),
            allowed_mentions=discord.AllowedMentions.none())

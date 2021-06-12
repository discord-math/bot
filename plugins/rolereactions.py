import re
import discord
import discord_client
import discord.utils
import util.db.kv
import util.discord
import plugins.commands
import plugins.privileges

conf = util.db.kv.Config(__name__)
msg_id_re = re.compile(
    r"https?://(?:\w*\.)?(?:discord.com|discordapp.com)"
    r"/channels/(\d+)/(\d+)/(\d+)"
)

async def find_message(channel_id, msg_id):
    channel = discord_client.client.get_channel(int(channel_id))
    if channel is None: return
    if not isinstance(channel, discord.TextChannel): return
    try:
        return await channel.fetch_message(int(msg_id))
    except (discord.NotFound, discord.Forbidden):
        pass

def find_role_id(guild, role_text):
    role = util.discord.smart_find(role_text, guild.roles if guild else ())
    if role is None:
        raise util.discord.UserError(
            "Multiple or no results for role {!i}", role_text)
    return role.id

def find_emoji_id(emoji_text):
    emoji = util.discord.smart_find(emoji_text, discord_client.client.emojis)
    if emoji is None:
        raise util.discord.UserError(
            "Multiple or no results for emoji {!i}", emoji_text)
    return emoji.id

def format_role(guild, role_id):
    role = discord.utils.find(
        lambda r: str(r.id) == role_id, guild.roles if guild else ())
    return (
        util.discord.format("{!M}({!i} {!i})", role, role.name, role.id)
        if role else util.discord.format("{!i}", role_id))

def format_emoji(emoji_str):
    if emoji_str.isdigit():
        emoji = discord_client.client.get_emoji(int(emoji_str))
        if (emoji is not None) and emoji.is_usable():
            return str(emoji) + util.discord.format("({!i})", emoji)
    return util.discord.format("{!i}", emoji_str)

def format_msg(guild_id, channel_id, msg_id):
    return ("https://discord.com/channels/{}/{}/{}"
        .format(guild_id, channel_id, msg_id))

# retrieve the original message link used
def retrieve_msg_link(msg_id):
    obj = conf[msg_id]
    return format_msg(obj['guild'], obj['channel'], msg_id)

def get_emoji(args):
    emoji_arg = args.next_arg(chunk_emoji=True)
    if isinstance(emoji_arg, plugins.commands.EmojiArg):
        return str(emoji_arg.id)
    elif isinstance(emoji_arg, plugins.commands.InlineCodeArg):
        return str(find_emoji_id(emoji_arg.text))
    elif isinstance(emoji_arg, plugins.commands.StringArg):
        return emoji_arg.text

def get_role(guild, args):
    role_arg = args.next_arg()
    if isinstance(role_arg, plugins.commands.RoleMentionArg):
        return str(role_arg.id)
    elif isinstance(role_arg, plugins.commands.StringArg):
        return str(find_role_id(guild, role_arg.text))

def get_msg_ref(msg, args):
    if msg.reference is not None:
        if msg.reference.guild_id is None: return
        if msg.reference.message_id is None: return
        if msg.reference.channel_id != msg.channel.id: return
        return (str(msg.reference.guild_id), str(msg.reference.channel_id),
            str(msg.reference.message_id))
    else:
        arg = args.next_arg()
        if not isinstance(arg, plugins.commands.StringArg): return
        if (match := msg_id_re.match(arg.text)) is None: return
        return match[1], match[2], match[3]

def make_discord_emoji(emoji_str):
    if emoji_str.isdigit():
        emoji = discord_client.client.get_emoji(int(emoji_str))
        if (emoji is not None) and emoji.is_usable():
            return emoji
    else:
        return emoji_str

async def react_initial(channel_id, msg_id, emoji_str):
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

def get_payload_role(guild, payload):
    obj = conf[str(payload.message_id)]
    if obj is None: return
    if payload.emoji.id is not None:
        emoji = str(payload.emoji.id)
    else:
        emoji = payload.emoji.name
        if emoji is None: return
    if emoji not in obj['rolereacts']: return
    return discord.utils.find(
        lambda r: str(r.id) == obj['rolereacts'][emoji], guild.roles)

@util.discord.event("raw_reaction_add")
async def rolereact_add(payload):
    if payload.member is None: return
    if payload.member.bot: return
    role = get_payload_role(payload.member.guild, payload)
    if role is None: return
    await payload.member.add_roles(role, reason="Role reactions on {}".format(
        payload.message_id))

@util.discord.event("raw_reaction_remove")
async def rolereact_remove(payload):
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
async def rolereact_command(msg, args):

    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        role_msg_guild, role_msg_chan, role_msg = role_msg_info
        if conf[role_msg] is not None:
            return await msg.channel.send("Role reactions already exist on {}"
                .format(retrieve_msg_link(role_msg)))
        conf[role_msg] = {"guild": role_msg_guild, "channel": role_msg_chan,
            "rolereacts": {}}
        await msg.channel.send("Created role reactions on {}"
            .format(format_msg(*role_msg_info)))

    elif cmd.text.lower() == "delete":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        _, _, role_msg = role_msg_info
        if conf[role_msg] is None:
            return await msg.channel.send("Role reactions do not exist on {}"
                .format(format_msg(*role_msg_info)))
        await msg.channel.send("Removed role reactions on {}"
            .format(retrieve_msg_link(role_msg)))
        conf[role_msg] = None

    elif cmd.text.lower() == "list":
        await msg.channel.send("Role reactions exist on: {}".format(
            "\n".join(retrieve_msg_link(id) for id in conf)))

    elif cmd.text.lower() == "show":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        _, _, role_msg = role_msg_info
        obj = conf[role_msg]
        if obj is None:
            return await msg.channel.send("Role reactions do not exist on {}"
                .format(format_msg(*role_msg_info)))
        await msg.channel.send("Role reactions on {} include: {}".format(
            retrieve_msg_link(role_msg),
            "; ".join(("{} for {}"
                .format(format_emoji(emoji), format_role(msg.guild, role))
                for emoji, role in obj['rolereacts'].items()
            ))), allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "add":
        role_msg_info = get_msg_ref(msg, args)
        if role_msg_info is None: return
        role_msg_guild, role_msg_chan, role_msg = role_msg_info
        emoji = get_emoji(args)
        if emoji is None: return
        obj = conf[role_msg]
        if obj is None:
            return await msg.channel.send("Role reactions do not exist on {}"
                .format(format_msg(*role_msg_info)))
        if emoji in obj['rolereacts']:
            return await msg.channel.send("Emoji {} already sets role {}"
                .format(
                format_emoji(emoji),
                format_role(msg.guild, obj['rolereacts'][emoji])),
                allowed_mentions=discord.AllowedMentions.none())
        role = get_role(msg.guild, args)
        if role is None: return
        obj = dict(obj)
        obj['rolereacts'] = dict(obj['rolereacts'])
        obj['rolereacts'][emoji] = role
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
            return await msg.channel.send("Role reactions do not exist on {}"
                .format(format_msg(*role_msg_info)))
        if emoji not in obj['rolereacts']:
            return await msg.channel.send(
                "Role reactions for emoji {} do not exist on {}".format(
                format_emoji(emoji), retrieve_msg_link(role_msg)))
        obj = dict(obj)
        obj['rolereacts'] = dict(obj['rolereacts'])
        obj['rolereacts'].pop(emoji)
        conf[role_msg] = obj
        await msg.channel.send(
            "Reacting with emoji {} on message {} no longer sets roles"
            .format(format_emoji(emoji), retrieve_msg_link(role_msg),
            allowed_mentions=discord.AllowedMentions.none()))

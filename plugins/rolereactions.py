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
    r"(?:https?://(?:\w*\.)?(?:discord.com|discordapp.com)"
    r"/channels/\d+/\d+/)?(\d+)"
)

async def find_message(guild, msg_id):
    for channel in guild.channels if guild else ():
        if not isinstance(channel, discord.TextChannel): continue
        try:
            return await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden):
            pass

def find_role_id(guild, role_text):
    role = util.discord.smart_find(role_text, guild.roles if guild else ())
    if role is None:
        raise util.discord.UserError(
            "Multiple or no results for role {!i}", role_text)
    return role.id

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

def get_emoji(args):
    emoji_arg = args.next_arg(chunk_emoji=True)
    if isinstance(emoji_arg, plugins.commands.EmojiArg):
        return str(emoji_arg.id)
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
        if msg.reference.channel_id != msg.channel.id: return
        return str(msg.reference.message_id)
    else:
        arg = args.next_arg()
        if not isinstance(arg, plugins.commands.StringArg): return
        if (match := msg_id_re.match(arg.text)) is None: return
        return match[1]

def make_discord_emoji(emoji_str):
    if emoji_str.isdigit():
        emoji = discord_client.client.get_emoji(int(emoji_str))
        if (emoji is not None) and emoji.is_usable():
            return emoji
    else:
        return emoji_str

async def react_initial(guild, msg_id, emoji_str):
    react_msg = await find_message(guild, msg_id)
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
    if emoji not in obj: return
    return discord.utils.find(lambda r: str(r.id) == obj[emoji], guild.roles)

@util.discord.event("raw_reaction_add")
async def rolereact_add(payload):
    if payload.member is None: return
    if payload.member.bot: return
    role = get_payload_role(payload.member.guild, payload)
    if role is None: return
    await payload.member.add_roles(role, reason=util.discord.format(
        "Role reactions on {!i}", payload.message_id))

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
    await member.remove_roles(role, reason=util.discord.format(
        "Role reactions on {!i}", payload.message_id))


@plugins.commands.command("rolereact")
@plugins.privileges.priv("admin")
async def rolereact_command(msg, args):

    cmd = args.next_arg()
    if not isinstance(cmd, plugins.commands.StringArg): return

    if cmd.text.lower() == "new":
        role_msg = get_msg_ref(msg, args)
        if role_msg is None: return
        if conf[role_msg] is not None:
            return await msg.channel.send(util.discord.format(
                "Role reactions already exist on {!i}", role_msg))
        conf[role_msg] = {}
        await msg.channel.send(util.discord.format(
            "Created role reactions on {!i}", role_msg))

    elif cmd.text.lower() == "delete":
        role_msg = get_msg_ref(msg, args)
        if role_msg is None: return
        if conf[role_msg] is None:
            return await msg.channel.send(util.discord.format(
                "Role reactions do not exist on {!i}", role_msg))
        conf[role_msg] = None
        await msg.channel.send(util.discord.format(
            "Removed role reactions on {!i}", role_msg))

    elif cmd.text.lower() == "list":
        await msg.channel.send("Role reactions exist on: {}".format(
            "; ".join((util.discord.format("{!i}", id) for id in conf))))

    elif cmd.text.lower() == "show":
        role_msg = get_msg_ref(msg, args)
        if role_msg is None: return
        obj = conf[role_msg]
        if obj is None:
            return await msg.channel.send(util.discord.format(
                "Role reactions do not exist on {!i}", role_msg))
        await msg.channel.send(util.discord.format(
            "Role reactions on {!i} include: {}", role_msg,
            "; ".join(("{} for {}"
                .format(format_emoji(emoji), format_role(msg.guild, role))
                for emoji, role in obj.items()
            ))), allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "add":
        role_msg = get_msg_ref(msg, args)
        if role_msg is None: return
        emoji = get_emoji(args)
        if emoji is None: return
        obj = conf[role_msg]
        if obj is None:
            return await msg.channel.send(util.discord.format(
                "Role reactions do not exist on {!i}", role_msg))
        if emoji in obj:
            return await msg.channel.send("Emoji {} already sets role {}"
                .format(
                format_emoji(emoji), format_role(msg.guild, obj[emoji])),
                allowed_mentions=discord.AllowedMentions.none())
        role = get_role(msg.guild, args)
        if role is None: return
        await react_initial(msg.guild, role_msg, emoji)
        obj = dict(obj)
        obj[emoji] = role
        conf[role_msg] = obj
        await msg.channel.send(util.discord.format(
            "Reacting with emoji {} on message {!i} now sets {}",
            format_emoji(emoji), role_msg, format_role(msg.guild, role)),
            allowed_mentions=discord.AllowedMentions.none())

    elif cmd.text.lower() == "remove":
        role_msg = get_msg_ref(msg, args)
        if role_msg is None: return
        emoji = get_emoji(args)
        if emoji is None: return
        obj = conf[role_msg]
        if obj is None:
            return await msg.channel.send(util.discord.format(
                "Role reactions do not exist on {!i}", role_msg))
        if emoji not in obj:
            return await msg.channel.send(util.discord.format(
                "Role reactions for emoji {} do not exist on {!i}",
                format_emoji(emoji), role_msg))
        obj = dict(obj)
        obj.pop(emoji)
        conf[role_msg] = obj
        await msg.channel.send(util.discord.format(
            "Reacting with emoji {} on message {!i} no longer sets roles",
            format_emoji(emoji), role_msg),
            allowed_mentions=discord.AllowedMentions.none())

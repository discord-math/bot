import io
import csv
import collections
import discord
import discord.ext.commands
from typing import List, Dict, Set, Tuple, Callable, Awaitable, Union, Optional, Any
import util.discord
import plugins.commands
import plugins.privileges
import plugins.reactions

def channel_sort_key(channel: discord.abc.GuildChannel) -> Tuple[int, bool, int]:
    if isinstance(channel, discord.CategoryChannel):
        return (channel.position, False, -1)
    else:
        return (channel.category.position if channel.category is not None else -1,
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel)),
            channel.position)

def overwrite_sort_key(pair: Tuple[Union[discord.Role, discord.Member, discord.Object], discord.PermissionOverwrite]
    ) -> int:
    if isinstance(pair[0], discord.Role):
        try:
            return pair[0].guild.roles.index(pair[0])
        except ValueError:
            return -1
    else:
        return -1

def disambiguated_name(channel: discord.abc.GuildChannel) -> str:
    chans: List[discord.abc.GuildChannel] = [chan for chan in channel.guild.channels if chan.name == channel.name]
    if len(chans) < 2:
        return channel.name
    chans.sort(key=lambda chan: chan.id)
    return "{} ({})".format(channel.name, 1 + chans.index(channel))

@plugins.privileges.priv("mod")
@plugins.commands.command("exportperms")
async def exportperms(ctx: plugins.commands.Context) -> None:
    """Export all role and channel permission settings into CSV."""
    if ctx.guild is None:
        raise util.discord.InvocationError("This can only be used in a guild.")

    file = io.StringIO()
    writer = csv.writer(file)
    writer.writerow(["Category", "Channel", "Role/User"] + [flag for flag, _ in discord.Permissions()])

    for role in ctx.guild.roles:
        writer.writerow(["", "", "Role " + role.name] + ["+" if value else "-" for _, value in role.permissions])

    for channel in sorted(ctx.guild.channels, key=channel_sort_key):
        if isinstance(channel, discord.CategoryChannel):
            header = [disambiguated_name(channel), ""]
        else:
            header = [disambiguated_name(channel.category) if channel.category is not None else "",
                disambiguated_name(channel)]
        writer.writerow(header + ["(synced)" if channel.permissions_synced else ""])
        if channel.permissions_synced: continue
        for target, overwrite in sorted(channel.overwrites.items(), key=overwrite_sort_key):
            if isinstance(target, discord.Role):
                name = "Role {}".format(target.name)
            elif isinstance(target, discord.Member):
                name = "User {} {}".format(target.id, target.name)
            elif target.type == discord.Role:
                name = "Role {}".format(target.id)
            else:
                name = "User {}".format(target.id)
            writer.writerow(header + [name] + ["+" if allow else "-" if deny else "/"
                for (_, allow), (_, deny) in zip(*overwrite.pair())])

    await ctx.send(file=discord.File(io.BytesIO(file.getvalue().encode()), "perms.csv"))

def tweak_permissions(permissions: discord.Permissions, add_mask: int, remove_mask: int) -> discord.Permissions:
    return discord.Permissions(permissions.value & ~remove_mask | add_mask)

def tweak_overwrite(overwrite: discord.PermissionOverwrite,
    add_mask: int, remove_mask: int, reset_mask: int) -> discord.PermissionOverwrite:
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(
        tweak_permissions(allow, add_mask, reset_mask),
        tweak_permissions(deny, remove_mask, reset_mask))

def overwrites_for(channel: discord.abc.GuildChannel, target: Union[discord.Role, discord.Member]
    ) -> discord.PermissionOverwrite:
    for t, overwrite in channel.overwrites.items():
        if t.id == target.id:
            return overwrite
    return discord.PermissionOverwrite()

SubChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]
GuildChannel = Union[discord.CategoryChannel, SubChannel]

def edit_role_perms(role: discord.Role, add_mask: int, remove_mask: int
    ) -> Callable[[], Awaitable[Optional[discord.Role]]]:
    return lambda: role.edit(permissions=discord.Permissions(
        role.permissions.value & ~remove_mask | add_mask))

def edit_channel_category(channel: SubChannel, category: Optional[discord.CategoryChannel]
    ) -> Callable[[], Awaitable[Any]]:
    return lambda: channel.edit(category=category)

def edit_channel_overwrites(channel: GuildChannel,
    overwrites: Dict[Union[discord.Role, discord.Member], Tuple[int, int, int]]) -> Callable[[], Awaitable[Any]]:
    if isinstance(channel, (discord.VoiceChannel, discord.CategoryChannel, discord.StageChannel)):
        return lambda: channel.edit(overwrites={target:
            tweak_overwrite(overwrites_for(channel, target), add_mask, remove_mask, reset_mask)
            for target, (add_mask, remove_mask, reset_mask) in overwrites.items()})
    else:
        return lambda: channel.edit(overwrites={target:
            tweak_overwrite(overwrites_for(channel, target), add_mask, remove_mask, reset_mask)
            for target, (add_mask, remove_mask, reset_mask) in overwrites.items()})

def sync_channel(channel: SubChannel) -> Callable[[], Awaitable[Any]]:
    return lambda: channel.edit(sync_permissions=True)

@plugins.privileges.priv("admin")
@plugins.commands.command("importperms")
async def importperms(ctx: plugins.commands.Context) -> None:
    """Import all role and channel permission settings from an attached CSV file."""
    if ctx.guild is None:
        raise util.discord.InvocationError("This can only be used in a guild.")
    if len(ctx.message.attachments) != 1:
        raise util.discord.InvocationError("Expected 1 attachment.")
    file = io.StringIO((await ctx.message.attachments[0].read()).decode())
    reader = csv.reader(file)

    channels = {disambiguated_name(channel): channel for channel in ctx.guild.channels}
    roles = {role.name: role for role in ctx.guild.roles}

    header = next(reader)
    if len(header) < 3 or header[0] != "Category" or header[1] != "Channel" or header[2] != "Role/User":
        raise util.discord.UserError("Invalid header.")

    flags: List[Tuple[Any, str]] = []
    for perm in header[3:]:
        try:
            flags.append((getattr(discord.Permissions, perm), perm))
        except AttributeError:
            raise util.discord.UserError("Unknown permission: {!r}".format(perm))

    actions: List[Callable[[], Awaitable[Any]]] = []
    output: List[str] = []
    new_overwrites: Dict[GuildChannel, Dict[Union[discord.Role, discord.Member], Tuple[int, int, int]]]
    new_overwrites = collections.defaultdict(dict)
    overwrites_changed: Set[GuildChannel] = set()
    want_sync: Set[SubChannel] = set()
    seen_moved: Dict[SubChannel, Optional[discord.CategoryChannel]] = {}

    for row in reader:
        if len(row) < 3:
            raise util.discord.UserError("Line {}: invalid row.".format(reader.line_num))
        if row[0] == "" and row[1] == "":
            if not row[2].startswith("Role "):
                raise util.discord.UserError("Line {}: expected a role.".format(reader.line_num))
            role_name = row[2].removeprefix("Role ")
            if role_name not in roles:
                raise util.discord.UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
            role = roles[role_name]
            changes = []
            add_mask = 0
            remove_mask = 0
            for (flag, perm), sign in zip(flags, row[3:]):
                if sign == "+" and not role.permissions.value & flag.flag:
                    changes.append("\u2705" + perm)
                    add_mask |= flag.flag
                if sign == "-" and role.permissions.value & flag.flag:
                    changes.append("\u274C" + perm)
                    remove_mask |= flag.flag
            if changes:
                output.append(util.discord.format("{!M}: {}", role, ", ".join(changes)))
            if add_mask != 0 or remove_mask != 0:
                actions.append(edit_role_perms(role, add_mask, remove_mask))
        else:
            category: Optional[discord.CategoryChannel]
            channel: discord.abc.GuildChannel
            if row[1] == "":
                category = None
                if row[0] not in channels:
                    raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                channel = channels[row[0]]
                if not isinstance(channel, discord.CategoryChannel):
                    raise util.discord.UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
            else:
                if row[0] == "":
                    category = None
                else:
                    if row[0] not in channels:
                        raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                    cat = channels[row[0]]
                    if not isinstance(cat, discord.CategoryChannel):
                        raise util.discord.UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
                    category = cat
                if row[1] not in channels:
                    raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[1]))
                channel = channels[row[1]]
                if isinstance(channel, discord.CategoryChannel):
                    raise util.discord.UserError("Line {}: {!r} is a category.".format(reader.line_num, row[1]))

            if not isinstance(channel, discord.CategoryChannel) and channel.category != category:
                if not channel in seen_moved:
                    seen_moved[channel] = category
                    output.append(util.discord.format("Move {!c} to {!c}", channel, category))
                    actions.append(edit_channel_category(channel, category))

            if row[2] == "(synced)" and not isinstance(channel, discord.CategoryChannel):
                want_sync.add(channel)
            elif row[2] != "":
                if row[2].startswith("Role "):
                    role_name = row[2].removeprefix("Role ")
                    if role_name not in roles:
                        raise util.discord.UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
                    target = roles[role_name]
                elif row[2].startswith("User "):
                    try:
                        user_id = int(row[2].removeprefix("User ").split(maxsplit=1)[0])
                    except ValueError:
                        raise util.discord.UserError("Line {}: expected user ID".format(reader.line_num))
                    if (member := ctx.guild.get_member(user_id)) is None:
                        raise util.discord.UserError("Line {}: no such member {}.".format(reader.line_num, user_id))
                    target = member
                else:
                    raise util.discord.UserError("Line {}: expected a role or user.".format(reader.line_num))
                allow, deny = overwrites_for(channel, target).pair()
                changes = []
                add_mask = 0
                remove_mask = 0
                reset_mask = 0
                for (flag, perm), sign in zip(flags, row[3:]):
                    if sign == "+" and not allow.value & flag.flag:
                        changes.append("\u2705" + perm)
                        add_mask |= flag.flag
                    if sign == "-" and not deny.value & flag.flag:
                        changes.append("\u274C" + perm)
                        remove_mask |= flag.flag
                    if sign == "/" and (allow.value & flag.flag or deny.value & flag.flag):
                        changes.append("\U0001F533" + perm)
                        reset_mask |= flag.flag
                if changes:
                    output.append(util.discord.format(
                        "{!c} {!M}: {}" if isinstance(target, discord.Role) else "{!c} {!m}: {}",
                        channel, target, ", ".join(changes)))
                new_overwrites[channel][target] = (add_mask, remove_mask, reset_mask)
            else:
                new_overwrites[channel]
    for channel in new_overwrites:
        if channel in want_sync: continue
        for add_mask, remove_mask, reset_mask in new_overwrites[channel].values():
            if add_mask != 0 or remove_mask != 0 or reset_mask != 0:
                overwrites_changed.add(channel)
                break
        for target in channel.overwrites:
            if target not in new_overwrites[channel]:
                output.append(util.discord.format(
                    "{!c} remove {!M}" if isinstance(target, discord.Role) else "{!c} remove {!m}",
                    channel, target))
                overwrites_changed.add(channel)
        actions.append(edit_channel_overwrites(channel, new_overwrites[channel]))
    for channel in want_sync:
        new_category = seen_moved.get(channel, channel.category)
        if new_category is None:
            raise util.discord.UserError(util.discord.format("Cannot sync channel {!c} with no category", channel))
        if not channel.permissions_synced or channel in seen_moved or new_category in overwrites_changed:
            output.append(util.discord.format("Sync {!c} with {!c}", channel, new_category))
            actions.append(sync_channel(channel))

    if not output:
        await ctx.send("No changes.")
        return

    text = ""
    for out in output:
        if len(text) + 1 + len(out) > 2000:
            await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
            text = out
        else:
            text += "\n" + out
    msg = await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    if await plugins.reactions.get_reaction(msg, ctx.author, {"\u274C": False, "\u2705": True}, timeout=300):
        for action in actions:
            await action()
        await ctx.send("\u2705")

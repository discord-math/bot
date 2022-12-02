from collections import defaultdict
import csv
from io import BytesIO, StringIO
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, Union

from discord import (AllowedMentions, CategoryChannel, File, ForumChannel, Member, Object, PermissionOverwrite,
    Permissions, Role, StageChannel, TextChannel, VoiceChannel)
import discord.abc

from bot.commands import Context, command
from bot.privileges import priv
from bot.reactions import get_reaction
from util.discord import InvocationError, PlainItem, UserError, chunk_messages, format

def channel_sort_key(channel: discord.abc.GuildChannel) -> Tuple[int, bool, int]:
    if isinstance(channel, CategoryChannel):
        return (channel.position, False, -1)
    else:
        return (channel.category.position if channel.category is not None else -1,
            isinstance(channel, (VoiceChannel, StageChannel)),
            channel.position)

def overwrite_sort_key(pair: Tuple[Union[Role, Member, Object], PermissionOverwrite]) -> int:
    if isinstance(pair[0], Role):
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

@priv("mod")
@command("exportperms")
async def exportperms(ctx: Context) -> None:
    """Export all role and channel permission settings into CSV."""
    if ctx.guild is None:
        raise InvocationError("This can only be used in a guild.")

    file = StringIO()
    writer = csv.writer(file)
    writer.writerow(["Category", "Channel", "Role/User"] + [flag for flag, _ in Permissions()])

    for role in ctx.guild.roles:
        writer.writerow(["", "", "Role " + role.name] + ["+" if value else "-" for _, value in role.permissions])

    for channel in sorted(ctx.guild.channels, key=channel_sort_key):
        if isinstance(channel, CategoryChannel):
            header = [disambiguated_name(channel), ""]
        else:
            header = [disambiguated_name(channel.category) if channel.category is not None else "",
                disambiguated_name(channel)]
        writer.writerow(header + ["(synced)" if channel.permissions_synced else ""])
        if channel.permissions_synced: continue
        for target, overwrite in sorted(channel.overwrites.items(), key=overwrite_sort_key):
            if isinstance(target, Role):
                name = "Role {}".format(target.name)
            elif isinstance(target, Member):
                name = "User {} {}".format(target.id, target.name)
            elif target.type == Role:
                name = "Role {}".format(target.id)
            else:
                name = "User {}".format(target.id)
            writer.writerow(header + [name] + ["+" if allow else "-" if deny else "/"
                for (_, allow), (_, deny) in zip(*overwrite.pair())])

    await ctx.send(file=File(BytesIO(file.getvalue().encode()), "perms.csv"))

def tweak_permissions(permissions: Permissions, add_mask: int, remove_mask: int) -> Permissions:
    return Permissions(permissions.value & ~remove_mask | add_mask)

def tweak_overwrite(overwrite: PermissionOverwrite,
    add_mask: int, remove_mask: int, reset_mask: int) -> PermissionOverwrite:
    allow, deny = overwrite.pair()
    return PermissionOverwrite.from_pair(
        tweak_permissions(allow, add_mask, reset_mask),
        tweak_permissions(deny, remove_mask, reset_mask))

def overwrites_for(channel: discord.abc.GuildChannel, target: Union[Role, Member]
    ) -> PermissionOverwrite:
    for t, overwrite in channel.overwrites.items():
        if t.id == target.id:
            return overwrite
    return PermissionOverwrite()

SubChannel = Union[TextChannel, VoiceChannel, StageChannel, ForumChannel]
GuildChannel = Union[CategoryChannel, SubChannel]

def edit_role_perms(role: Role, add_mask: int, remove_mask: int
    ) -> Callable[[], Awaitable[Optional[Role]]]:
    return lambda: role.edit(permissions=Permissions(
        role.permissions.value & ~remove_mask | add_mask))

def edit_channel_category(channel: SubChannel, category: Optional[CategoryChannel]
    ) -> Callable[[], Awaitable[Any]]:
    return lambda: channel.edit(category=category)

def edit_channel_overwrites(channel: GuildChannel,
    overwrites: Dict[Union[Role, Member], Tuple[int, int, int]]) -> Callable[[], Awaitable[Any]]:
    if isinstance(channel, (VoiceChannel, CategoryChannel, StageChannel)):
        return lambda: channel.edit(overwrites={target:
            tweak_overwrite(overwrites_for(channel, target), add_mask, remove_mask, reset_mask)
            for target, (add_mask, remove_mask, reset_mask) in overwrites.items()})
    else:
        return lambda: channel.edit(overwrites={target:
            tweak_overwrite(overwrites_for(channel, target), add_mask, remove_mask, reset_mask)
            for target, (add_mask, remove_mask, reset_mask) in overwrites.items()})

def sync_channel(channel: SubChannel) -> Callable[[], Awaitable[Any]]:
    return lambda: channel.edit(sync_permissions=True)

@priv("admin")
@command("importperms")
async def importperms(ctx: Context) -> None:
    """Import all role and channel permission settings from an attached CSV file."""
    if ctx.guild is None:
        raise InvocationError("This can only be used in a guild.")
    if len(ctx.message.attachments) != 1:
        raise InvocationError("Expected 1 attachment.")
    file = StringIO((await ctx.message.attachments[0].read()).decode())
    reader = csv.reader(file)

    channels = {disambiguated_name(channel): channel for channel in ctx.guild.channels}
    roles = {role.name: role for role in ctx.guild.roles}

    header = next(reader)
    if len(header) < 3 or header[0] != "Category" or header[1] != "Channel" or header[2] != "Role/User":
        raise UserError("Invalid header.")

    flags: List[Tuple[Any, str]] = []
    for perm in header[3:]:
        try:
            flags.append((getattr(Permissions, perm), perm))
        except AttributeError:
            raise UserError("Unknown permission: {!r}".format(perm))

    actions: List[Callable[[], Awaitable[Any]]] = []
    output: List[str] = []
    new_overwrites: Dict[GuildChannel, Dict[Union[Role, Member], Tuple[int, int, int]]]
    new_overwrites = defaultdict(dict)
    overwrites_changed: Set[GuildChannel] = set()
    want_sync: Set[SubChannel] = set()
    seen_moved: Dict[SubChannel, Optional[CategoryChannel]] = {}

    for row in reader:
        if len(row) < 3:
            raise UserError("Line {}: invalid row.".format(reader.line_num))
        if row[0] == "" and row[1] == "":
            if not row[2].startswith("Role "):
                raise UserError("Line {}: expected a role.".format(reader.line_num))
            role_name = row[2].removeprefix("Role ")
            if role_name not in roles:
                raise UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
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
                output.append(format("{!M}: {}", role, ", ".join(changes)))
            if add_mask != 0 or remove_mask != 0:
                actions.append(edit_role_perms(role, add_mask, remove_mask))
        else:
            category: Optional[CategoryChannel]
            channel: GuildChannel
            if row[1] == "":
                category = None
                if row[0] not in channels:
                    raise UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                channel = channels[row[0]]
                if not isinstance(channel, CategoryChannel):
                    raise UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
            else:
                if row[0] == "":
                    category = None
                else:
                    if row[0] not in channels:
                        raise UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                    cat = channels[row[0]]
                    if not isinstance(cat, CategoryChannel):
                        raise UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
                    category = cat
                if row[1] not in channels:
                    raise UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[1]))
                channel = channels[row[1]]
                if isinstance(channel, CategoryChannel):
                    raise UserError("Line {}: {!r} is a category.".format(reader.line_num, row[1]))

            if not isinstance(channel, CategoryChannel) and channel.category != category:
                if not channel in seen_moved:
                    seen_moved[channel] = category
                    output.append(format("Move {!c} to {!c}", channel, category))
                    actions.append(edit_channel_category(channel, category))

            if row[2] == "(synced)" and not isinstance(channel, CategoryChannel):
                want_sync.add(channel)
            elif row[2] != "":
                if row[2].startswith("Role "):
                    role_name = row[2].removeprefix("Role ")
                    if role_name not in roles:
                        raise UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
                    target = roles[role_name]
                elif row[2].startswith("User "):
                    try:
                        user_id = int(row[2].removeprefix("User ").split(maxsplit=1)[0])
                    except ValueError:
                        raise UserError("Line {}: expected user ID".format(reader.line_num))
                    if (member := ctx.guild.get_member(user_id)) is None:
                        raise UserError("Line {}: no such member {}.".format(reader.line_num, user_id))
                    target = member
                else:
                    raise UserError("Line {}: expected a role or user.".format(reader.line_num))
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
                    output.append(format(
                        "{!c} {!M}: {}" if isinstance(target, Role) else "{!c} {!m}: {}",
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
                output.append(format(
                    "{!c} remove {!M}" if isinstance(target, Role) else "{!c} remove {!m}",
                    channel, target))
                overwrites_changed.add(channel)
        actions.append(edit_channel_overwrites(channel, new_overwrites[channel]))
    for channel in want_sync:
        new_category = seen_moved.get(channel, channel.category)
        if new_category is None:
            raise UserError(format("Cannot sync channel {!c} with no category", channel))
        if not channel.permissions_synced or channel in seen_moved or new_category in overwrites_changed:
            output.append(format("Sync {!c} with {!c}", channel, new_category))
            actions.append(sync_channel(channel))

    if not output:
        await ctx.send("No changes.")
        return

    msg = None
    for content, _ in chunk_messages(PlainItem(text + "\n") for text in output):
        msg = await ctx.send(content, allowed_mentions=AllowedMentions.none())
    assert msg

    if await get_reaction(msg, ctx.author, {"\u274C": False, "\u2705": True}, timeout=300):
        for action in actions:
            await action()
        await ctx.send("\u2705")

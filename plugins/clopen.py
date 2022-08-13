import asyncio
import time
import collections
import logging
import discord
import discord.ext.commands
from typing import Dict, List, Tuple, Optional, Union, Any, Literal, Awaitable, Protocol, overload, cast
import util.discord
import util.frozen_list
import util.db.kv
import discord_client
import plugins
import plugins.cogs
import plugins.commands
import plugins.privileges

def available_embed() -> discord.Embed:
    checkmark_url = "https://cdn.discordapp.com/emojis/901284681633370153.png?size=256"
    helpers = 286206848099549185
    help_chan = 488120190538743810
    return discord.Embed(color=0x7CB342,
        description=util.discord.format(
            "Send your question here to claim the channel.\n\n"
            "Remember:\n"
            "• **Ask** your math question in a clear, concise manner.\n"
            "• **Show** your work, and if possible, explain where you are stuck.\n"
            "• **After 15 minutes**, feel free to ping {!M}.\n"
            "• Type the command {!i} to free the channel when you're done.\n"
            "• Be polite and have a nice day!\n\n"
            "Read {!c} for further information on how to ask a good question, "
            "and about conduct in the question channels.", helpers, plugins.commands.conf.prefix + "close", help_chan)
        ).set_author(name="Available help channel!", icon_url=checkmark_url)

def closed_embed(reason: str, reopen: bool) -> discord.Embed:
    if reopen:
        reason += util.discord.format("\n\nUse {!i} if this was a mistake.", plugins.commands.conf.prefix + "reopen")
    return discord.Embed(color=0x000000, title="Channel closed", description=reason)

def limit_embed() -> discord.Embed:
    return discord.Embed(color=0xB37C42, description="Please don't occupy multiple help channels.")

def prompt_message(mention: int) -> str:
    return util.discord.format("{!m} Has your question been resolved?", mention)

class ClopenConf(Awaitable[None], Protocol):
    channels: util.frozen_list.FrozenList[int]
    available_category: int
    used_category: int
    hidden_category: int
    owner_timeout: int
    timeout: int
    min_avail: int
    max_avail: int
    max_channels: int
    limit: int
    limit_role: int

    @overload
    def __getitem__(self, k: Tuple[int, Literal["state"]]
        ) -> Optional[Literal["available", "used", "pending", "closed", "hidden"]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["owner", "prompt_id", "op_id", "extension"]]) -> Optional[int]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["expiry"]]) -> Optional[float]: ...

    @overload
    def __setitem__(self, k: Tuple[int, Literal["state"]],
        v: Literal["available", "used", "pending", "closed", "hidden"]) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["owner", "prompt_id", "op_id", "extension"]], v: Optional[int]
        ) -> None: ...
    @overload
    def __setitem__(self, k: Tuple[int, Literal["expiry"]], v: Optional[float]) -> None: ...

conf: ClopenConf
logger = logging.getLogger(__name__)

channel_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

scheduler_event = asyncio.Event()
scheduler_event.set()

def scheduler_updated() -> None:
    scheduler_event.set()

async def scheduler() -> None:
    await discord_client.client.wait_until_ready()
    while True:
        try:
            if sum(conf[id, "state"] == "available" for id in conf.channels) < conf.min_avail:
                for id in conf.channels:
                    if conf[id, "state"] == "hidden":
                        await make_available(id)
                        break
                else:
                    if (new_id := await create_channel()) is not None:
                        await make_available(new_id)

            min_next = None
            for id in conf.channels:
                async with channel_locks[id]:
                    expiry = conf[id, "expiry"]
                    if conf[id, "state"] == "used" and expiry is not None:
                        if expiry < time.time():
                            await make_pending(id)
                        elif min_next is None or expiry < min_next:
                            min_next = expiry
                    elif conf[id, "state"] == "pending" and expiry is not None:
                        if expiry < time.time():
                            await close(id, "Closed due to timeout")
                        elif min_next is None or expiry < min_next:
                            min_next = expiry
                    elif conf[id, "state"] in ["closed", None]:
                        if expiry is None or expiry < time.time():
                            if sum(conf[id, "state"] == "available" for id in conf.channels) >= conf.max_avail:
                                await make_hidden(id)
                            else:
                                await make_available(id)
                        elif min_next is None or expiry < min_next:
                            min_next = expiry
            try:
                await asyncio.wait_for(scheduler_event.wait(), min_next - time.time() if min_next is not None else None)
            except asyncio.TimeoutError:
                pass
            scheduler_event.clear()
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in scheduler", exc_info=True)
            await asyncio.sleep(300)

scheduler_task: asyncio.Task[None]

@plugins.init
async def init() -> None:
    global conf, scheduler_task
    conf = cast(ClopenConf, await util.db.kv.load(__name__))
    scheduler_task = asyncio.create_task(scheduler())
    plugins.finalizer(scheduler_task.cancel)

rename_tasks: Dict[int, asyncio.Task[Any]] = {}
last_rename: Dict[int, float] = {}

def request_rename(chan: discord.TextChannel, name: str) -> None:
    if chan.id in rename_tasks and not rename_tasks[chan.id].done():
        rename_tasks[chan.id].cancel()
    async def do_rename(chan: discord.TextChannel, name: str) -> None:
        try:
            await chan.edit(name=name)
        except asyncio.CancelledError:
            raise
        except:
            last_rename[chan.id] = time.time()
        else:
            last_rename[chan.id] = time.time()
    rename_tasks[chan.id] = asyncio.create_task(do_rename(chan, name))

async def insert_chan(cat_id: int, chan: discord.TextChannel, *, beginning: bool = False) -> None:
    channels = conf.channels
    assert chan.id in channels
    cat = await discord_client.client.fetch_channel(cat_id)
    assert isinstance(cat, discord.CategoryChannel)
    max_chan = None
    if not beginning:
        for other in cat.channels:
            if other.id in channels and channels.index(other.id) >= channels.index(chan.id):
                break
            max_chan = other
    if max_chan is None:
        await chan.move(category=cat, sync_permissions=True, beginning=True)
    else:
        await chan.move(category=cat, sync_permissions=True, after=max_chan)

async def update_owner_limit(user_id: int) -> bool:
    assert isinstance(cat := discord_client.client.get_channel(conf.used_category), discord.abc.GuildChannel)
    user = cat.guild.get_member(user_id)
    if user is None:
        return False
    role_id = conf.limit_role
    has_role = any(role.id == role_id for role in user.roles)
    reached_limit = sum(conf[id, "owner"] == user_id and conf[id, "state"] in ["used", "pending"]
        for id in conf.channels) >= conf.limit
    try:
        if reached_limit and not has_role:
            logger.debug("Adding limiting role for {}".format(user_id))
            await user.add_roles(discord.Object(role_id))
        elif not reached_limit and has_role:
            logger.debug("Removing limiting role for {}".format(user_id))
            await user.remove_roles(discord.Object(role_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    return reached_limit

async def occupy(id: int, msg_id: int, author: Union[discord.User, discord.Member]) -> None:
    logger.debug("Occupying {}, author {}, OP {}".format(id, author.id, msg_id))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] == "available"
    conf[id, "state"] = "used"
    conf[id, "owner"] = author.id
    old_op_id = conf[id, "op_id"]
    conf[id, "op_id"] = msg_id
    conf[id, "extension"] = 1
    conf[id, "expiry"] = time.time() + conf.owner_timeout
    await enact_occupied(channel, author, op_id=msg_id, old_op_id=old_op_id)
    await conf
    scheduler_updated()

async def enact_occupied(channel: discord.TextChannel, owner: Union[discord.User, discord.Member], *,
    op_id: Optional[int], old_op_id: Optional[int]) -> None:
    reached_limit = await update_owner_limit(owner.id)
    try:
        if old_op_id is not None:
            await discord.PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if op_id is not None:
            await discord.PartialMessage(channel=channel, id=op_id).pin()
    except (discord.NotFound, discord.Forbidden):
        pass
    try:
        await insert_chan(conf.used_category, channel, beginning=True)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix + "\uFF5C" + owner.display_name)
    except discord.Forbidden:
        pass
    if reached_limit:
        await channel.send(embed=limit_embed(), allowed_mentions=discord.AllowedMentions.none())

async def keep_occupied(id: int, msg_author_id: int) -> None:
    logger.debug("Bumping {} by {}".format(id, msg_author_id))
    assert conf[id, "state"] == "used"
    assert (extension := conf[id, "extension"]) is not None
    if msg_author_id == conf[id, "owner"]:
        new_expiry = time.time() + conf.owner_timeout * extension
    else:
        new_expiry = time.time() + conf.timeout * extension
    if (old_expiry := conf[id, "expiry"]) is None or old_expiry < new_expiry:
        conf[id, "expiry"] = new_expiry

async def close(id: int, reason: str, *, reopen: bool = True) -> None:
    logger.debug("Closing {}, reason {!r}, reopen={!r}".format(id, reason, reopen))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] in ["used", "pending"]
    conf[id, "state"] = "closed"
    now = time.time()
    conf[id, "expiry"] = max(now + 60, last_rename.get(id, now) + 600) # channel rename ratelimit
    old_op_id = conf[id, "op_id"]
    old_owner_id = conf[id, "owner"]
    if not reopen:
        conf[id, "owner"] = None
        conf[id, "op_id"] = None
    if old_owner_id is not None:
        await update_owner_limit(old_owner_id)
    try:
        if not reopen and old_op_id is not None:
            await discord.PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if (prompt_id := conf[id, "prompt_id"]) is not None:
            assert discord_client.client.user is not None
            await discord.PartialMessage(channel=channel, id=prompt_id).remove_reaction("\u274C",
                discord_client.client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await channel.send(embed=closed_embed(reason, reopen), allowed_mentions=discord.AllowedMentions.none())
    await conf
    scheduler_updated()

async def make_available(id: int) -> None:
    logger.debug("Making {} available".format(id))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] in ["closed", "hidden", None]
    conf[id, "state"] = "available"
    conf[id, "expiry"] = None
    conf[id, "prompt_id"] = await enact_available(channel)
    await conf
    scheduler_updated()

async def enact_available(channel: discord.TextChannel) -> int:
    try:
        await insert_chan(conf.available_category, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix)
    except discord.Forbidden:
        pass
    return (await channel.send(embed=available_embed(), allowed_mentions=discord.AllowedMentions.none())).id

async def make_hidden(id: int) -> None:
    logger.debug("Making {} hidden".format(id))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] in ["available", "closed", None]
    conf[id, "state"] = "hidden"
    conf[id, "expiry"] = None
    conf[id, "prompt_id"] = None
    await enact_hidden(channel)
    await conf
    scheduler_updated()

async def enact_hidden(channel: discord.TextChannel) -> None:
    try:
        await insert_chan(conf.hidden_category, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix)
    except discord.Forbidden:
        pass

async def create_channel() -> Optional[int]:
    if len(conf.channels) >= conf.max_channels:
        return None
    logger.debug("Creating a new channel")
    cat = discord_client.client.get_channel(conf.used_category)
    assert isinstance(cat, discord.CategoryChannel)
    try:
        chan = await cat.create_text_channel(name="help-{}".format(len(conf.channels)))
        logger.debug("Created a new channel: {}".format(chan.id))
        conf.channels = conf.channels + [chan.id]
        return chan.id
    except discord.Forbidden:
        return None

async def extend(id: int) -> None:
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] == "pending"
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    extension *= 2
    logger.debug("Extending {} to {}x".format(id, extension))
    conf[id, "extension"] = extension
    conf[id, "expiry"] = time.time() + conf.owner_timeout * extension
    conf[id, "state"] = "used"
    try:
        if (prompt_id := conf[id, "prompt_id"]) is not None:
            assert discord_client.client.user is not None
            await discord.PartialMessage(channel=channel, id=prompt_id).remove_reaction("\u2705",
                discord_client.client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await conf
    scheduler_updated()

async def make_pending(id: int) -> None:
    logger.debug("Prompting {} for closure".format(id))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] == "used"
    assert (owner := conf[id, "owner"]) is not None
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    conf[id, "expiry"] = time.time() + conf.owner_timeout * extension
    prompt = await channel.send(prompt_message(owner))
    await prompt.add_reaction("\u2705")
    await prompt.add_reaction("\u274C")
    conf[id, "prompt_id"] = prompt.id
    conf[id, "state"] = "pending"
    await conf
    scheduler_updated()

async def reopen(id: int) -> None:
    logger.debug("Reopening {}".format(id))
    assert isinstance(channel := discord_client.client.get_channel(id), discord.TextChannel)
    assert conf[id, "state"] in ["available", "closed"]
    assert (owner := conf[id, "owner"]) is not None
    prompt_id = conf[id, "prompt_id"] if conf[id, "state"] == "available" else None
    conf[id, "state"] = "used"
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    conf[id, "expiry"] = time.time() + conf.owner_timeout * extension
    await update_owner_limit(owner)
    try:
        if prompt_id is not None:
            await discord.PartialMessage(channel=channel, id=prompt_id).delete()
    except (discord.NotFound, discord.Forbidden):
        pass
    try:
        await insert_chan(conf.used_category, channel, beginning=True)
        prefix = channel.name.split("\uFF5C", 1)[0]
        author = await channel.guild.fetch_member(owner)
        request_rename(channel, prefix + "\uFF5C" + author.display_name)
    except (discord.NotFound, discord.Forbidden):
        pass
    await conf
    scheduler_updated()

async def synchronize_channels() -> List[str]:
    output = []
    available_category = conf.available_category
    used_category = conf.used_category
    hidden_category = conf.hidden_category
    assert isinstance(cat := discord_client.client.get_channel(used_category), discord.abc.GuildChannel)
    for id in conf.channels:
        channel = discord_client.client.get_channel(id)
        if not isinstance(channel, discord.TextChannel):
            output.append(util.discord.format("{!c} is not a text channel", id))
            continue
        state = conf[id, "state"]
        if state == "available":
            if channel.category is None or channel.category.id != available_category:
                output.append(util.discord.format("{!c} moved to the available category", id))
                await enact_available(channel)
            else:
                valid_prompt = conf[id, "prompt_id"]
                if valid_prompt is not None:
                    try:
                        if not (await channel.fetch_message(valid_prompt)).embeds:
                            valid_prompt = None
                    except (discord.NotFound, discord.Forbidden):
                        valid_prompt = None
                if valid_prompt is None:
                    output.append(util.discord.format("Posted available message in {!c}", id))
                    msg = await channel.send(embed=available_embed(), allowed_mentions=discord.AllowedMentions.none())
                    conf[id, "prompt_id"] = msg.id
                    await conf
                else:
                    async for msg in channel.history(limit=None, after=discord.Object(valid_prompt)):
                        if not msg.author.bot:
                            output.append(util.discord.format("{!c} assigned to {!m}", id, msg.author))
                            await occupy(id, msg.id, msg.author)
                            break
        elif state == "used" or state == "pending":
            op_id = conf[id, "op_id"]
            owner_id = conf[id, "owner"]
            owner = cat.guild.get_member(owner_id) if owner_id is not None else None
            if owner is None:
                output.append(util.discord.format("{!c} has no owner, closed", id))
                await close(id, "The owner is missing!", reopen=False)
            elif op_id is None:
                output.append(util.discord.format("{!c} has no OP message, closed", id))
                await close(id, "The original message is missing!", reopen=False)
            elif channel.category is None or channel.category.id != used_category:
                output.append(util.discord.format("{!c} moved to the used category", id))
                await enact_occupied(channel, owner, op_id=op_id, old_op_id=None)
        elif state == "closed":
            if channel.category is None or channel.category.id != used_category:
                output.append(util.discord.format("{!c} moved to the used category", id))
                await insert_chan(used_category, channel, beginning=True)
        elif state == "hidden":
            if channel.category is None or channel.category.id != hidden_category:
                output.append(util.discord.format("{!c} moved to the hidden category", id))
                await insert_chan(hidden_category, channel, beginning=True)
    if (role := cat.guild.get_role(conf.limit_role)) is not None:
        for user in role.members:
            if not await update_owner_limit(user.id):
                output.append(util.discord.format("Removed limiting role from {!m}", user))
    for id in conf.channels:
        channel = discord_client.client.get_channel(id)
        if not isinstance(channel, discord.TextChannel):
            continue
        for msg in await channel.pins():
            if conf[id, "state"] not in ["available", "used", "pending", "closed"] or msg.id != conf[id, "op_id"]:
                output.append(util.discord.format("Removed extraneous pin from {!c}", id))
                await msg.unpin()
    return output

@plugins.cogs.cog
class ClopenCog(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_ready(self) -> None:
        output = await synchronize_channels()
        if output:
            logger.error("\n".join(output))

    @discord.ext.commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if not msg.author.bot and msg.channel.id in conf.channels:
            async with channel_locks[msg.channel.id]:
                if conf[msg.channel.id, "state"] == "used":
                    await keep_occupied(msg.channel.id, msg.author.id)
                elif conf[msg.channel.id, "state"] == "available":
                    if not msg.content.startswith(plugins.commands.conf.prefix):
                        await occupy(msg.channel.id, msg.id, msg.author)

    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.channel_id in conf.channels and payload.message_id == conf[payload.channel_id, "prompt_id"]:
            if payload.user_id == conf[payload.channel_id, "owner"]:
                async with channel_locks[payload.channel_id]:
                    if conf[payload.channel_id, "state"] == "pending":
                        if payload.emoji.name == "\u2705":
                            await close(payload.channel_id, util.discord.format("Closed by {!m}", payload.user_id))
                        elif payload.emoji.name == "\u274C":
                            await extend(payload.channel_id)

    @discord.ext.commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.channel_id in conf.channels and payload.message_id == conf[payload.channel_id, "op_id"]:
            async with channel_locks[payload.channel_id]:
                if conf[payload.channel_id, "state"] in ["used", "pending"]:
                    await close(payload.channel_id, "Closed due to the original message being deleted", reopen=False)
                else:
                    conf[payload.channel_id, "owner"] = None

    @discord.ext.commands.command("close")
    async def close_command(self, ctx: plugins.commands.Context) -> None:
        """For use in help channels. Close a channel."""
        if ctx.channel.id not in conf.channels:
            return
        if ctx.author.id != conf[ctx.channel.id, "owner"] and not plugins.privileges.PrivCheck("helper")(ctx):
            return
        async with channel_locks[ctx.channel.id]:
            if conf[ctx.channel.id, "state"] in ["used", "pending"]:
                await close(ctx.channel.id, util.discord.format("Closed by {!m}", ctx.author))

    @discord.ext.commands.command("reopen")
    async def reopen_command(self, ctx: plugins.commands.Context) -> None:
        """For use in help channels. Reopen a recently closed channel."""
        if ctx.channel.id not in conf.channels:
            return
        if ctx.author.id != conf[ctx.channel.id, "owner"] and not plugins.privileges.PrivCheck("helper")(ctx):
            return
        async with channel_locks[ctx.channel.id]:
            if conf[ctx.channel.id, "state"] in ["closed", "available"] and conf[ctx.channel.id, "owner"] is not None:
                await reopen(ctx.channel.id)
                await ctx.send("\u2705")

    @plugins.privileges.priv("mod")
    @discord.ext.commands.command("clopen_sync")
    async def clopen_sync_command(self, ctx: plugins.commands.Context) -> None:
        """Try and synchronize the state of clopen channels with Discord in case of errors or outages."""
        output = await synchronize_channels()
        text = ""
        for out in output:
            if len(text) + 1 + len(out) > 2000:
                await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
                text = out
            else:
                text += "\n" + out
        await ctx.send(text or "\u2705", allowed_mentions=discord.AllowedMentions.none())

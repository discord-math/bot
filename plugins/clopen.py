import asyncio
import time
import collections
import logging
import discord
import discord.ext.commands.cog
from typing import List, Dict, Tuple, Optional, Union, Any, Literal, Awaitable, Protocol, overload, cast
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

def prompt_message(mention: int) -> str:
    return util.discord.format("{!m} Has your question been resolved?", mention)

class ClopenConf(Protocol, Awaitable[None]):
    channels: util.frozen_list.FrozenList[int]
    available_category: int
    used_category: int
    owner_timeout: int
    timeout: int

    @overload
    def __getitem__(self, k: Tuple[int, Literal["state"]]
        ) -> Optional[Literal["available", "used", "pending", "closed"]]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["owner", "prompt_id", "op_id", "extension"]]) -> Optional[int]: ...
    @overload
    def __getitem__(self, k: Tuple[int, Literal["expiry"]]) -> Optional[float]: ...

    @overload
    def __setitem__(self, k: Tuple[int, Literal["state"]], v: Literal["available", "used", "pending", "closed"]
        ) -> None: ...
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

async def insert_chan(cat: discord.CategoryChannel, chan: discord.TextChannel) -> None:
    channels = conf.channels
    assert chan.id in channels
    max_chan = None
    for other in cat.channels:
        if other.id in channels and channels.index(other.id) >= channels.index(chan.id):
            break
        max_chan = other
    if max_chan is None:
        await chan.move(category=cat, sync_permissions=True, beginning=True)
    else:
        await chan.move(category=cat, sync_permissions=True, after=max_chan)

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
    try:
        if old_op_id is not None:
            await discord.PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        await discord.PartialMessage(channel=channel, id=msg_id).pin()
    except (discord.NotFound, discord.Forbidden):
        pass
    try:
        cat = discord_client.client.get_channel(conf.used_category)
        assert isinstance(cat, discord.CategoryChannel)
        await insert_chan(cat, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix + "\uFF5C" + author.display_name)
    except discord.Forbidden:
        pass
    await conf
    scheduler_updated()

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
    if not reopen:
        conf[id, "owner"] = None
        conf[id, "op_id"] = None
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
    assert conf[id, "state"] in ["closed", None]
    conf[id, "state"] = "available"
    conf[id, "expiry"] = None
    try:
        cat = discord_client.client.get_channel(conf.available_category)
        assert isinstance(cat, discord.CategoryChannel)
        await insert_chan(cat, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix)
    except discord.Forbidden:
        pass
    prompt = await channel.send(embed=available_embed(), allowed_mentions=discord.AllowedMentions.none())
    conf[id, "prompt_id"] = prompt.id
    await conf
    scheduler_updated()

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
    try:
        if prompt_id is not None:
            await discord.PartialMessage(channel=channel, id=prompt_id).delete()
    except (discord.NotFound, discord.Forbidden):
        pass
    try:
        cat = discord_client.client.get_channel(conf.used_category)
        assert isinstance(cat, discord.CategoryChannel)
        await insert_chan(cat, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        author = await channel.guild.fetch_member(owner)
        request_rename(channel, prefix + "\uFF5C" + author.display_name)
    except (discord.NotFound, discord.Forbidden):
        pass
    await conf
    scheduler_updated()

@plugins.cogs.cog
class ClopenCog(discord.ext.commands.Cog):
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
                await close(payload.channel_id, "Closed due to the original message being deleted", reopen=False)

    @discord.ext.commands.command("close")
    async def close_command(self, ctx: discord.ext.commands.Context) -> None:
        if ctx.channel.id not in conf.channels:
            return
        if ctx.author.id != conf[ctx.channel.id, "owner"] and not plugins.privileges.PrivCheck("helper")(ctx):
            return
        async with channel_locks[ctx.channel.id]:
            if conf[ctx.channel.id, "state"] in ["used", "pending"]:
                await close(ctx.channel.id, util.discord.format("Closed by {!m}", ctx.author))

    @discord.ext.commands.command("reopen")
    async def reopen_command(self, ctx: discord.ext.commands.Context) -> None:
        if ctx.channel.id not in conf.channels:
            return
        if ctx.author.id != conf[ctx.channel.id, "owner"] and not plugins.privileges.PrivCheck("helper")(ctx):
            return
        async with channel_locks[ctx.channel.id]:
            if conf[ctx.channel.id, "state"] in ["closed", "available"] and conf[ctx.channel.id, "owner"] is not None:
                await reopen(ctx.channel.id)
                await ctx.send("\u2705")

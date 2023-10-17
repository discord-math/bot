import asyncio
from collections import defaultdict
import logging
from time import time
from typing import (TYPE_CHECKING, Awaitable, Dict, Iterable, List, Literal, Optional, Protocol, Tuple, Union, cast,
    overload)

import discord
from discord import (AllowedMentions, ButtonStyle, CategoryChannel, Embed, ForumChannel, ForumTag, Interaction,
    InteractionType, Member, Message, Object, PartialMessage, RawMessageDeleteEvent, RawReactionActionEvent,
    SelectOption, TextChannel, TextStyle, Thread, User)
from discord.abc import GuildChannel

if TYPE_CHECKING:
    import discord.types.interactions
from discord.ui import Button, Modal, Select, TextInput, View

from bot.acl import EvalResult, evaluate_ctx, evaluate_interaction, register_action
from bot.client import client
from bot.cogs import Cog, cog, command
import bot.commands
from bot.commands import Context
import bot.message_tracker
from bot.privileges import priv
from bot.tasks import task
import plugins
import util.db.kv
from util.discord import PlainItem, Typing, chunk_messages, format
from util.frozen_list import FrozenList

def available_embed() -> Embed:
    checkmark_url = "https://cdn.discordapp.com/emojis/901284681633370153.png?size=256"
    helpers = 286206848099549185
    help_chan = 488120190538743810
    return Embed(color=0x7CB342,
        description=format(
            "Send your question here to claim the channel.\n\n"
            "Remember:\n"
            "• **Ask** your math question in a clear, concise manner.\n"
            "• **Show** your work, and if possible, explain where you are stuck.\n"
            "• **After 15 minutes**, feel free to ping {!M}.\n"
            "• Type the command {!i} to free the channel when you're done.\n"
            "• Be polite and have a nice day!\n\n"
            "Read {!c} for further information on how to ask a good question, "
            "and about conduct in the question channels.", helpers, bot.commands.conf.prefix + "close", help_chan)
        ).set_author(name="Available help channel!", icon_url=checkmark_url)

def closed_embed(reason: str, reopen: bool) -> Embed:
    if reopen:
        reason += format("\n\nUse {!i} if this was a mistake.", bot.commands.conf.prefix + "reopen")
    return Embed(color=0x000000, title="Channel closed", description=reason)

def solved_embed(reason: str) -> Embed:
    checkmark_url = "https://cdn.discordapp.com/emojis/1021392975449825322.webp?size=256&quality=lossless"
    return Embed(color=0x7CB342, description=format(
            "Post marked as solved {}.\n\nUse {!i} if this was a mistake.", reason,
            bot.commands.conf.prefix + "unsolved")
        ).set_author(name="Solved", icon_url=checkmark_url)

def unsolved_embed(reason: str) -> Embed:
    ping_url = "https://cdn.discordapp.com/emojis/1021392792783683655.webp?size=256&quality=lossless"
    return Embed(color=0x7CB342, description=format(
            "Post marked as unsolved {}.\n\nUse {!i} to mark as solved.", reason,
            bot.commands.conf.prefix + "solved")
        ).set_author(name="Unsolved", icon_url=ping_url)

def limit_embed() -> Embed:
    return Embed(color=0xB37C42, description="Please don't occupy multiple help channels.")

def prompt_message(mention: int) -> str:
    return format("{!m} Has your question been resolved?", mention)

class ClopenConf(Awaitable[None], Protocol):
    channels: FrozenList[int]
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
    forum: int
    pinned_posts: FrozenList[int]
    solved_tag: int
    unsolved_tag: int

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

manage_clopen = register_action("manage_clopen")

channel_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

@task(name="Clopen scheduler task", exc_backoff_base=10)
async def scheduler_task() -> None:
    await client.wait_until_ready()

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
                if expiry < time():
                    await make_pending(id)
                elif min_next is None or expiry < min_next:
                    min_next = expiry
            elif conf[id, "state"] == "pending" and expiry is not None:
                if expiry < time():
                    await close(id, "Closed due to timeout")
                elif min_next is None or expiry < min_next:
                    min_next = expiry
            elif conf[id, "state"] in ["closed", None]:
                if expiry is None or expiry < time():
                    if sum(conf[id, "state"] == "available" for id in conf.channels) >= conf.max_avail:
                        await make_hidden(id)
                    else:
                        await make_available(id)
                elif min_next is None or expiry < min_next:
                    min_next = expiry

    if min_next is not None:
        scheduler_task.run_coalesced(min_next - time())

@plugins.init
async def init() -> None:
    global conf, scheduler_task
    conf = cast(ClopenConf, await util.db.kv.load(__name__))
    scheduler_task.run_coalesced(0)

    await bot.message_tracker.subscribe(__name__, None, process_messages, missing=True, retroactive=False)
    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)
    plugins.finalizer(unsubscribe)

rename_tasks: Dict[int, asyncio.Task[object]] = {}
last_rename: Dict[int, float] = {}

def request_rename(chan: TextChannel, name: str) -> None:
    if chan.id in rename_tasks and not rename_tasks[chan.id].done():
        rename_tasks[chan.id].cancel()
    async def do_rename(chan: TextChannel, name: str) -> None:
        try:
            await chan.edit(name=name)
        except asyncio.CancelledError:
            raise
        except:
            last_rename[chan.id] = time()
        else:
            last_rename[chan.id] = time()
    rename_tasks[chan.id] = asyncio.create_task(do_rename(chan, name))

async def insert_chan(cat_id: int, chan: TextChannel, *, beginning: bool = False) -> None:
    channels = conf.channels
    assert chan.id in channels
    cat = await client.fetch_channel(cat_id)
    assert isinstance(cat, CategoryChannel)
    max_chan = None
    if not beginning:
        for other in sorted(cat.channels, key=lambda chan: chan.position):
            if other.id in channels and channels.index(other.id) >= channels.index(chan.id):
                break
            max_chan = other
    if max_chan is None:
        await chan.move(category=cat, sync_permissions=True, beginning=True)
    else:
        await chan.move(category=cat, sync_permissions=True, after=max_chan)

async def update_owner_limit(user_id: int) -> bool:
    assert isinstance(cat := client.get_channel(conf.used_category), GuildChannel)
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
            await user.add_roles(Object(role_id))
        elif not reached_limit and has_role:
            logger.debug("Removing limiting role for {}".format(user_id))
            await user.remove_roles(Object(role_id))
    except (discord.NotFound, discord.Forbidden):
        pass
    return reached_limit

async def occupy(id: int, msg_id: int, author: Union[User, Member]) -> None:
    logger.debug("Occupying {}, author {}, OP {}".format(id, author.id, msg_id))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] == "available"
    conf[id, "state"] = "used"
    conf[id, "owner"] = author.id
    old_op_id = conf[id, "op_id"]
    conf[id, "op_id"] = msg_id
    conf[id, "extension"] = 1
    conf[id, "expiry"] = time() + conf.owner_timeout
    await enact_occupied(channel, author, op_id=msg_id, old_op_id=old_op_id)
    await conf
    scheduler_task.run_coalesced(0)

async def enact_occupied(channel: TextChannel, owner: Union[User, Member], *,
    op_id: Optional[int], old_op_id: Optional[int]) -> None:
    reached_limit = await update_owner_limit(owner.id)
    try:
        if old_op_id is not None:
            await PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if op_id is not None:
            await PartialMessage(channel=channel, id=op_id).pin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        await insert_chan(conf.used_category, channel, beginning=True)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix + "\uFF5C" + owner.display_name)
    except discord.Forbidden:
        pass
    if reached_limit:
        await channel.send(embed=limit_embed(), allowed_mentions=AllowedMentions.none())

async def keep_occupied(id: int, msg_author_id: int) -> None:
    logger.debug("Bumping {} by {}".format(id, msg_author_id))
    assert conf[id, "state"] == "used"
    assert (extension := conf[id, "extension"]) is not None
    if msg_author_id == conf[id, "owner"]:
        new_expiry = time() + conf.owner_timeout * extension
    else:
        new_expiry = time() + conf.timeout * extension
    if (old_expiry := conf[id, "expiry"]) is None or old_expiry < new_expiry:
        conf[id, "expiry"] = new_expiry

async def close(id: int, reason: str, *, reopen: bool = True) -> None:
    logger.debug("Closing {}, reason {!r}, reopen={!r}".format(id, reason, reopen))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] in ["used", "pending"]
    conf[id, "state"] = "closed"
    now = time()
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
            await PartialMessage(channel=channel, id=old_op_id).unpin()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        if (prompt_id := conf[id, "prompt_id"]) is not None:
            assert client.user is not None
            await PartialMessage(channel=channel, id=prompt_id).remove_reaction("\u274C",
                client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await channel.send(embed=closed_embed(reason, reopen), allowed_mentions=AllowedMentions.none())
    await conf
    scheduler_task.run_coalesced(0)

async def make_available(id: int) -> None:
    logger.debug("Making {} available".format(id))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] in ["closed", "hidden", None]
    conf[id, "state"] = "available"
    conf[id, "expiry"] = None
    conf[id, "prompt_id"] = await enact_available(channel)
    await conf
    scheduler_task.run_coalesced(0)

async def enact_available(channel: TextChannel) -> int:
    try:
        await insert_chan(conf.available_category, channel)
        prefix = channel.name.split("\uFF5C", 1)[0]
        request_rename(channel, prefix)
    except discord.Forbidden:
        pass
    return (await channel.send(embed=available_embed(), allowed_mentions=AllowedMentions.none())).id

async def make_hidden(id: int) -> None:
    logger.debug("Making {} hidden".format(id))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] in ["available", "closed", None]
    conf[id, "state"] = "hidden"
    conf[id, "expiry"] = None
    conf[id, "prompt_id"] = None
    await enact_hidden(channel)
    await conf
    scheduler_task.run_coalesced(0)

async def enact_hidden(channel: TextChannel) -> None:
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
    cat = client.get_channel(conf.used_category)
    assert isinstance(cat, CategoryChannel)
    try:
        chan = await cat.create_text_channel(name="help-{}".format(len(conf.channels)))
        logger.debug("Created a new channel: {}".format(chan.id))
        conf.channels = conf.channels + [chan.id]
        return chan.id
    except discord.Forbidden:
        return None

async def extend(id: int) -> None:
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] == "pending"
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    extension *= 2
    logger.debug("Extending {} to {}x".format(id, extension))
    conf[id, "extension"] = extension
    conf[id, "expiry"] = time() + conf.owner_timeout * extension
    conf[id, "state"] = "used"
    try:
        if (prompt_id := conf[id, "prompt_id"]) is not None:
            assert client.user is not None
            await PartialMessage(channel=channel, id=prompt_id).remove_reaction("\u2705",
                client.user)
    except (discord.NotFound, discord.Forbidden):
        pass
    await conf
    scheduler_task.run_coalesced(0)

async def make_pending(id: int) -> None:
    logger.debug("Prompting {} for closure".format(id))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] == "used"
    assert (owner := conf[id, "owner"]) is not None
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    conf[id, "expiry"] = time() + conf.owner_timeout * extension
    prompt = await channel.send(prompt_message(owner))
    await prompt.add_reaction("\u2705")
    await prompt.add_reaction("\u274C")
    conf[id, "prompt_id"] = prompt.id
    conf[id, "state"] = "pending"
    await conf
    scheduler_task.run_coalesced(0)

async def reopen(id: int) -> None:
    logger.debug("Reopening {}".format(id))
    assert isinstance(channel := client.get_channel(id), TextChannel)
    assert conf[id, "state"] in ["available", "closed"]
    assert (owner := conf[id, "owner"]) is not None
    prompt_id = conf[id, "prompt_id"] if conf[id, "state"] == "available" else None
    conf[id, "state"] = "used"
    extension = conf[id, "extension"]
    if extension is None:
        extension = 1
    conf[id, "expiry"] = time() + conf.owner_timeout * extension
    await update_owner_limit(owner)
    try:
        if prompt_id is not None:
            await PartialMessage(channel=channel, id=prompt_id).delete()
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
    scheduler_task.run_coalesced(0)

async def synchronize_channels() -> List[str]:
    output = []
    available_category = conf.available_category
    used_category = conf.used_category
    hidden_category = conf.hidden_category
    assert isinstance(cat := client.get_channel(used_category), GuildChannel)
    for id in conf.channels:
        channel = client.get_channel(id)
        if not isinstance(channel, TextChannel):
            output.append(format("{!c} is not a text channel", id))
            continue
        state = conf[id, "state"]
        if state == "available":
            if channel.category is None or channel.category.id != available_category:
                output.append(format("{!c} moved to the available category", id))
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
                    output.append(format("Posted available message in {!c}", id))
                    msg = await channel.send(embed=available_embed(), allowed_mentions=AllowedMentions.none())
                    conf[id, "prompt_id"] = msg.id
                    await conf
                else:
                    async for msg in channel.history(limit=None, after=Object(valid_prompt)):
                        if not msg.author.bot:
                            output.append(format("{!c} assigned to {!m}", id, msg.author))
                            await occupy(id, msg.id, msg.author)
                            break
        elif state == "used" or state == "pending":
            op_id = conf[id, "op_id"]
            owner_id = conf[id, "owner"]
            owner = cat.guild.get_member(owner_id) if owner_id is not None else None
            if owner is None:
                output.append(format("{!c} has no owner, closed", id))
                await close(id, "The owner is missing!", reopen=False)
            elif op_id is None:
                output.append(format("{!c} has no OP message, closed", id))
                await close(id, "The original message is missing!", reopen=False)
            elif channel.category is None or channel.category.id != used_category:
                output.append(format("{!c} moved to the used category", id))
                await enact_occupied(channel, owner, op_id=op_id, old_op_id=None)
        elif state == "closed":
            if channel.category is None or channel.category.id != used_category:
                output.append(format("{!c} moved to the used category", id))
                await insert_chan(used_category, channel, beginning=True)
        elif state == "hidden":
            if channel.category is None or channel.category.id != hidden_category:
                output.append(format("{!c} moved to the hidden category", id))
                await insert_chan(hidden_category, channel, beginning=True)
    if (role := cat.guild.get_role(conf.limit_role)) is not None:
        for user in role.members:
            if not await update_owner_limit(user.id):
                output.append(format("Removed limiting role from {!m}", user))
    for id in conf.channels:
        channel = client.get_channel(id)
        if not isinstance(channel, TextChannel):
            continue
        for msg in await channel.pins():
            if conf[id, "state"] not in ["available", "used", "pending", "closed"] or msg.id != conf[id, "op_id"]:
                output.append(format("Removed extraneous pin from {!c}", id))
                await msg.unpin()
    return output

async def set_solved_tags(post: Thread, new_tags: Iterable[int], reason: str) -> None:
    solved_tags = [conf.solved_tag, conf.unsolved_tag]
    tags = [tag for tag in post.applied_tags if tag.id not in solved_tags]
    tags += [cast(ForumTag, Object(id)) for id in new_tags]
    try:
        await post.edit(applied_tags=tags, reason=reason)
    except discord.HTTPException:
        logger.error(format("Could not set solved tags on {!c}", post), exc_info=True)

async def solved(post: Thread, reason: str) -> None:
    if any(tag.id == conf.solved_tag for tag in post.applied_tags): return
    await set_solved_tags(post, [conf.solved_tag], reason)
    await post.send(embed=solved_embed(reason), allowed_mentions=AllowedMentions.none())

async def unsolved(post: Thread, reason: str) -> None:
    if not any(tag.id == conf.solved_tag for tag in post.applied_tags): return
    await set_solved_tags(post, [conf.unsolved_tag], reason)
    await post.send(embed=unsolved_embed(reason), allowed_mentions=AllowedMentions.none())

async def wait_close_post(post: Thread, reason: str) -> None:
    await asyncio.sleep(300) # TODO: what if the post is reopened in the meantime?
    await post.edit(archived=True, reason=reason)

class PostTagsView(View):
    def __init__(self, post: Thread) -> None:
        assert isinstance(post.parent, ForumChannel)
        super().__init__(timeout=None)

        options = [SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji,
            default=tag in post.applied_tags) for tag in post.parent.available_tags if not tag.moderated]

        self.add_item(Select(placeholder="Select tags for this post...",
            min_values=0, max_values=min(4, len(options)), # 5 sans 1 for solved/unsolved
            options=options, custom_id="{}:tags:{}".format(__name__, post.id)))

        self.add_item(Button(style=ButtonStyle.secondary, label="Rename post",
            custom_id="{}:title:{}".format(__name__, post.id)))

class PostTitleModal(Modal):
    def __init__(self, post: Thread) -> None:
        super().__init__(title="Edit post title", timeout=600)
        self.thread = post
        self.name = TextInput(style=TextStyle.short, placeholder="Enter post title...",
            label="Post title", default=post.name, required=True, max_length=100)
        self.add_item(self.name)

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            await self.thread.edit(name=str(self.name), reason=format("By {!m}", interaction.user))
        except discord.HTTPException:
            return
        await interaction.response.send_message("\u2705", ephemeral=True, delete_after=60)

async def manage_title(interaction: Interaction, thread_id: int) -> None:
    try:
        thread = await interaction.client.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if not isinstance(thread, Thread): return
    if not isinstance(thread.parent, ForumChannel): return
    if not interaction.message: return

    if thread.owner_id != interaction.user.id:
        if manage_clopen.evaluate(*evaluate_interaction(interaction)) != EvalResult.TRUE:
            await interaction.response.send_message("You cannot edit the title on this post", ephemeral=True,
                delete_after=60)
            return

    await interaction.response.send_modal(PostTitleModal(thread))

async def manage_tags(interaction: Interaction, thread_id: int, values: List[str]) -> None:
    try:
        thread = await interaction.client.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if not isinstance(thread, Thread): return
    if not isinstance(thread.parent, ForumChannel): return
    if not interaction.message: return

    if thread.owner_id != interaction.user.id:
        if manage_clopen.evaluate(*evaluate_interaction(interaction)) != EvalResult.TRUE:
            await interaction.response.send_message("You cannot edit tags on this post", ephemeral=True,
                delete_after=60)
            return

    id_values = []
    for v in values:
        try:
            id_values.append(int(v))
        except ValueError:
            continue
    solved_tags = [conf.solved_tag, conf.unsolved_tag]
    tags = [tag for tag in thread.applied_tags if tag.id in solved_tags]
    tags += [tag for tag in thread.parent.available_tags if not tag.moderated and tag.id in id_values]

    try:
        new_thread = await thread.edit(applied_tags=tags, reason=format("By {!m}", interaction.user))
    except discord.HTTPException:
        return
    await interaction.message.edit(view=PostTagsView(new_thread))
    await interaction.response.send_message("\u2705", ephemeral=True, delete_after=60)

async def process_messages(msgs: Iterable[Message]) -> None:
    for msg in msgs:
        if msg.author.bot: continue

        if msg.channel.id in conf.pinned_posts:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

        if isinstance(msg.channel, Thread) and msg.channel.parent_id == conf.forum:
            if msg.id == msg.channel.id: # starter post in a thread
                await set_solved_tags(msg.channel, [conf.unsolved_tag], "new post")
                await msg.channel.send(view=PostTagsView(msg.channel))

@cog
class ClopenCog(Cog):
    @Cog.listener()
    async def on_ready(self) -> None:
        output = await synchronize_channels()
        if output:
            logger.error("\n".join(output))

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if not msg.author.bot and msg.channel.id in conf.channels:
            async with channel_locks[msg.channel.id]:
                if conf[msg.channel.id, "state"] == "used":
                    await keep_occupied(msg.channel.id, msg.author.id)
                elif conf[msg.channel.id, "state"] == "available":
                    if not msg.content.startswith(bot.commands.conf.prefix):
                        await occupy(msg.channel.id, msg.id, msg.author)

    @Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if payload.channel_id in conf.channels and payload.message_id == conf[payload.channel_id, "prompt_id"]:
            if payload.user_id == conf[payload.channel_id, "owner"]:
                async with channel_locks[payload.channel_id]:
                    if conf[payload.channel_id, "state"] == "pending":
                        if payload.emoji.name == "\u2705":
                            await close(payload.channel_id, format("Closed by {!m}", payload.user_id))
                        elif payload.emoji.name == "\u274C":
                            await extend(payload.channel_id)

    @Cog.listener()
    async def on_raw_message_delete(self, payload: RawMessageDeleteEvent) -> None:
        if payload.channel_id in conf.channels and payload.message_id == conf[payload.channel_id, "op_id"]:
            async with channel_locks[payload.channel_id]:
                if conf[payload.channel_id, "state"] in ["used", "pending"]:
                    await close(payload.channel_id,
                        "Channel closed due to the original message being deleted. \n"
                        "If you did not intend to do this, please **open a new help channel**, \n"
                        "as this action is irreversible, and this channel may abruptly lock.",
                        reopen=False)
                else:
                    conf[payload.channel_id, "owner"] = None

    @Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        if interaction.type != InteractionType.component or interaction.data is None:
            return
        data = cast("discord.types.interactions.MessageComponentInteractionData", interaction.data)
        if data["component_type"] == 3:
            if ":" not in data["custom_id"]:
                return
            mod, rest = data["custom_id"].split(":", 1)
            if mod != __name__ or ":" not in rest:
                return
            action, thread_id = rest.split(":", 1)
            try:
                thread_id = int(thread_id)
            except ValueError:
                return
            if action == "tags":
                await manage_tags(interaction, thread_id, data["values"])
        elif data["component_type"] == 2:
            if ":" not in data["custom_id"]:
                return
            mod, rest = data["custom_id"].split(":", 1)
            if mod != __name__ or ":" not in rest:
                return
            action, thread_id = rest.split(":", 1)
            try:
                thread_id = int(thread_id)
            except ValueError:
                return
            if action == "title":
                await manage_title(interaction, thread_id)

    @command("close")
    async def close_command(self, ctx: Context) -> None:
        """For use in help channels and help forum posts. Close a channel and/or mark the post as solved."""
        if ctx.channel.id in conf.channels:
            if ctx.author.id != conf[ctx.channel.id, "owner"]:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            async with channel_locks[ctx.channel.id]:
                if conf[ctx.channel.id, "state"] in ["used", "pending"]:
                    await close(ctx.channel.id, format("Closed by {!m}", ctx.author))
        elif isinstance(ctx.channel, Thread) and ctx.channel.parent_id == conf.forum:
            if ctx.author.id != ctx.channel.owner_id:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            await solved(ctx.channel, format("by {!m}", ctx.author))
            asyncio.create_task(wait_close_post(ctx.channel, format("Closed by {!m}", ctx.author)))

    @command("reopen")
    async def reopen_command(self, ctx: Context) -> None:
        """For use in help channels and help forum posts. Reopen a recently closed channel and/or mark the post as
        unsolved."""
        if ctx.channel.id in conf.channels:
            if ctx.author.id != conf[ctx.channel.id, "owner"]:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            async with channel_locks[ctx.channel.id]:
                if conf[ctx.channel.id, "state"] in ["closed", "available"]:
                    if conf[ctx.channel.id, "owner"] is not None:
                        await reopen(ctx.channel.id)
                        await ctx.send("\u2705")
        elif isinstance(ctx.channel, Thread) and ctx.channel.parent_id == conf.forum:
            if ctx.author.id != ctx.channel.owner_id:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            await unsolved(ctx.channel, format("by {!m}", ctx.author))

    @priv("minimod")
    @command("clopen_sync")
    async def clopen_sync_command(self, ctx: Context) -> None:
        """Try and synchronize the state of clopen channels with Discord in case of errors or outages."""
        async with Typing(ctx):
            output = await synchronize_channels()
        if output:
            for content, _ in chunk_messages(PlainItem(text + "\n") for text in output):
                await ctx.send(content, allowed_mentions=AllowedMentions.none())
        else:
            await ctx.send("\u2705", allowed_mentions=AllowedMentions.none())

    @command("solved")
    async def solved_command(self, ctx: Context) -> None:
        """For use in help forum posts. Mark the post as solved."""
        if isinstance(ctx.channel, Thread) and ctx.channel.parent_id == conf.forum:
            if ctx.author.id != ctx.channel.owner_id:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            await solved(ctx.channel, format("by {!m}", ctx.author))

    @command("unsolved")
    async def unsolved_command(self, ctx: Context) -> None:
        """For use in help forum posts. Mark the post as unsolved."""
        if isinstance(ctx.channel, Thread) and ctx.channel.parent_id == conf.forum:
            if ctx.author.id != ctx.channel.owner_id:
                if manage_clopen.evaluate(*evaluate_ctx(ctx)) != EvalResult.TRUE:
                    return
            await unsolved(ctx.channel, format("by {!m}", ctx.author))

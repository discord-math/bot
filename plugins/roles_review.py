import enum
import logging
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Literal, Optional, Set, Tuple, Union, cast

import discord
from discord import (
    AllowedMentions,
    ButtonStyle,
    Guild,
    Interaction,
    InteractionType,
    Member,
    Message,
    Object,
    Role,
    TextChannel,
    TextStyle,
    Thread,
    User,
)
from discord.abc import Messageable
from discord.ext.commands import group
from discord.ui import Button, Modal, TextInput, View
from sqlalchemy import ARRAY, BOOLEAN, TEXT, BigInteger, ForeignKey, Integer, PrimaryKeyConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.acl import EvalResult, evaluate_interaction, privileged, register_action
from bot.cogs import Cog, cog, command
from bot.commands import Context, cleanup
from bot.config import plugin_config_command
import plugins
import util.db
import util.db.kv
from util.discord import (
    CodeBlock,
    Inline,
    PartialChannelConverter,
    PartialRoleConverter,
    PartialUserConverter,
    PlainItem,
    Quoted,
    UserError,
    chunk_messages,
    format,
    retry,
)


if TYPE_CHECKING:
    import discord.types.interactions


logger = logging.getLogger(__name__)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)

action_can_vote = register_action("review_can_vote")
action_can_veto = register_action("review_can_veto")


@registry.mapped
class ReviewedRole:
    __tablename__ = "roles"
    __table_args__ = {"schema": "roles_review"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    review_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    upvote_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    downvote_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_role_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    denied_role_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    prompt: Mapped[List[str]] = mapped_column(ARRAY(TEXT), nullable=False)
    invitation: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            id: int,
            review_channel_id: int,
            upvote_limit: int,
            downvote_limit: int,
            prompt: List[str],
            invitation: str,
            pending_role_id: Optional[int] = ...,
            denied_role_id: Optional[int] = ...,
        ) -> None: ...


@registry.mapped
class Application:
    __tablename__ = "applications"
    __table_args__ = {"schema": "roles_review"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    voting_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[Optional[bool]] = mapped_column(BOOLEAN)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            listing_id: int,
            user_id: int,
            role_id: int,
            id: Optional[int] = None,
            voting_id: Optional[int] = None,
            decision: Optional[bool] = None,
        ) -> None: ...


@registry.mapped
class Vote:
    __tablename__ = "votes"

    application_id: Mapped[int] = mapped_column(Integer, ForeignKey(Application.id, ondelete="CASCADE"), nullable=False)
    voter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vote: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)
    veto: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)

    __table_args__ = (PrimaryKeyConstraint(application_id, voter_id, veto), {"schema": "roles_review"})

    if TYPE_CHECKING:

        def __init__(self, *, application_id: int, voter_id: int, vote: bool, veto: bool) -> None: ...


reviewed_roles: Set[int]
pending_roles: Set[int]


@plugins.init
async def init() -> None:
    global reviewed_roles, pending_roles
    await util.db.init(util.db.get_ddl(CreateSchema("roles_review"), registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        for (role_id_str,) in conf:
            role_id = int(role_id_str)
            obj = cast(Dict[str, Any], conf[role_id])
            session.add(
                ReviewedRole(
                    id=role_id,
                    review_channel_id=obj["review_channel"],
                    upvote_limit=obj["upvote_limit"],
                    downvote_limit=obj["downvote_limit"],
                    pending_role_id=obj.get("pending_role"),
                    denied_role_id=obj.get("denied_role"),
                    prompt=list(obj["prompt"]),
                    invitation=obj["invitation"],
                )
            )
        await session.commit()
        for role_id_str in [role_id_str for role_id_str, in conf]:
            conf[role_id_str] = None
        await conf

        stmt = select(ReviewedRole.id)
        reviewed_roles = set((await session.execute(stmt)).scalars())
        stmt = select(ReviewedRole.pending_role_id).where(ReviewedRole.pending_role_id != None)
        pending_roles = cast(Set[int], set((await session.execute(stmt)).scalars()))


class RolePromptModal(Modal):
    def __init__(
        self, guild: Guild, prompt_roles: List[Tuple[Role, ReviewedRole]], message: Optional[Message] = None
    ) -> None:
        super().__init__(title="Additional information", timeout=1200)
        self.guild = guild
        self.message = message
        self.inputs = {}
        for role, review in prompt_roles:
            self.inputs[role] = []
            for prompt in review.prompt:
                if "\n" in prompt:
                    prompt, placeholder = prompt.split("\n", 1)
                    placeholder = placeholder[:100]
                else:
                    placeholder = None
                prompt = prompt[:45]
                input = TextInput(label=prompt, style=TextStyle.paragraph, max_length=600, placeholder=placeholder)
                self.inputs[role].append(input)
                self.add_item(input)

    async def on_submit(self, interaction: Interaction) -> None:
        if isinstance(interaction.user, Member):
            member = interaction.user
        else:
            if (member := self.guild.get_member(interaction.user.id)) is None:
                await interaction.response.send_message("You have left the server.")
                if self.message:
                    await self.message.delete()
                return

        await interaction.response.defer(ephemeral=True)

        outputs = []
        for role, inputs in self.inputs.items():
            output = await apply(member, role, [(input.label, str(input)) for input in inputs])
            if output == ApplicationStatus.APPROVED:
                outputs.append("{} assigned.".format(role.name))
            elif output is None:
                outputs.append("Your application for {} has been submitted for review.".format(role.name))
            else:
                outputs.append("You have already applied for {}.".format(role.name))
        await interaction.followup.send("\n".join(outputs), ephemeral=True)
        if self.message:
            await self.message.delete()


class ApproveRoleView(View):
    def __init__(self, msg_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(
            Button(style=ButtonStyle.success, label="Approve", custom_id="{}:{}:Approve".format(__name__, msg_id))
        )
        self.add_item(Button(style=ButtonStyle.danger, label="Deny", custom_id="{}:{}:Deny".format(__name__, msg_id)))
        self.add_item(
            Button(style=ButtonStyle.secondary, label="Retract", custom_id="{}:{}:Retract".format(__name__, msg_id))
        )
        self.add_item(
            Button(style=ButtonStyle.secondary, label="Veto", custom_id="{}:{}:Veto".format(__name__, msg_id))
        )


def voting_decision(app_id: int, review: ReviewedRole, votes: List[Vote]) -> Optional[bool]:
    logger.debug("Calculating votes for {}".format(app_id))
    up_total = 0
    down_total = 0
    for vote in votes:
        if vote.veto:
            logger.debug(format("Veto {} by {!m} decides vote on {}", vote.vote, vote.voter_id, app_id))
            return vote.vote
        if vote.vote:
            up_total += 1
        else:
            down_total += 1
        if up_total >= review.upvote_limit:
            logger.debug(format("Vote on {} decided by reaching {} upvotes", app_id, up_total))
            return True
        elif down_total >= review.downvote_limit:
            logger.debug(format("Vote on {} decided by reaching {} downvotes", app_id, down_total))
            return False
    logger.debug("Vote on {} undecided".format(app_id))
    return None


async def cast_vote(interaction: Interaction, msg_id: int, dir: Optional[bool], veto: Optional[str] = None) -> None:
    assert interaction.message
    assert interaction.guild
    assert isinstance(interaction.channel, Messageable)
    assert dir is not None or veto is None
    async with sessionmaker() as session:
        stmt = select(Application).where(Application.listing_id == msg_id).limit(1)
        app = (await session.execute(stmt)).scalars().first()

        if app is None:
            await interaction.response.send_message("No such application.", ephemeral=True)
            return
        review = await session.get(ReviewedRole, app.role_id)
        assert review

        channel = interaction.guild.get_channel(review.review_channel_id)
        assert isinstance(channel, Messageable)

        logger.debug(
            format(
                "Vote {!r} from {!m} for {!m} {!M} veto={!r}",
                dir,
                interaction.user,
                app.user_id,
                app.role_id,
                bool(veto),
            )
        )

        if app.decision is not None:
            await interaction.response.send_message("This application is already resolved.", ephemeral=True)
            return

        can_vote = action_can_vote.evaluate(*evaluate_interaction(interaction)) == EvalResult.TRUE
        can_veto = action_can_veto.evaluate(*evaluate_interaction(interaction)) == EvalResult.TRUE

        if veto is None:
            if not (can_vote or can_veto):
                await interaction.response.send_message(
                    "You are not allowed to vote on this application.", ephemeral=True
                )
                return
        else:
            if not can_veto:
                await interaction.response.send_message("You are not allowed to veto this application.", ephemeral=True)
                return

        stmt = select(Vote).where(Vote.application_id == app.id)
        votes = list((await session.execute(stmt)).scalars())

        if dir is None:
            for vote in votes:
                if vote.voter_id == interaction.user.id:
                    await session.delete(vote)
                    break
            else:
                await interaction.response.send_message("You have not voted on this application.", ephemeral=True)
                return
        else:
            if veto is None and any(vote.voter_id == interaction.user.id for vote in votes):
                await interaction.response.send_message("You have already voted on this application.", ephemeral=True)
                return

            vote = Vote(application_id=app.id, voter_id=interaction.user.id, vote=dir, veto=veto is not None)
            session.add(vote)
            votes.append(vote)

        await interaction.response.defer(ephemeral=True)
        await session.commit()

        decision = voting_decision(app.id, review, votes)

        if dir is None:
            comment = "\u21A9"
        elif dir:
            comment = "\u2705"
        else:
            comment = "\u274C"
        if veto is not None:
            comment = "veto {}: {}".format(comment, veto)

        voting_id = interaction.message.id
        thread = interaction.channel

        async def update_messages():
            voting = await thread.fetch_message(voting_id)
            await voting.edit(
                content=format("{}\n{!m}: {}", voting.content, interaction.user, comment),
                view=ApproveRoleView(msg_id) if decision is None else None,
                allowed_mentions=AllowedMentions.none(),
            )
            if decision is not None:
                listing = await channel.fetch_message(app.listing_id)
                await listing.edit(
                    content=listing.content + "\nDecision: " + ("\u2705" if decision else "\u274C"),
                    allowed_mentions=AllowedMentions.none(),
                )

        try:
            await retry(update_messages, attempts=10)
        finally:
            if decision is not None:
                app.decision = decision
                await session.commit()

            if decision is not None and (user := interaction.guild.get_member(app.user_id)) is not None:
                if review.pending_role_id is not None:
                    if p_role := interaction.guild.get_role(review.pending_role_id):
                        await retry(lambda: user.remove_roles(p_role, reason=format("Granted {!m}", app.role_id)))
                if decision == True:
                    if (role := interaction.guild.get_role(app.role_id)) is not None:
                        await retry(lambda: user.add_roles(role, reason="By vote"))
                elif review.denied_role_id is not None:
                    if d_role := interaction.guild.get_role(review.denied_role_id):
                        await retry(lambda: user.add_roles(d_role, reason=format("Denied {!m}", app.role_id)))


class VetoModal(Modal):
    reason = TextInput(style=TextStyle.paragraph, required=False, label="Reason for the veto")
    # Selects disallowed in modals for now:
    # decision = Select(placeholder="Decision", options=[
    #    SelectOption(label="Approve", emoji="\u2705"), SelectOption(label="Deny", emoji="\u274C")])
    decision = TextInput(max_length=1, label="[Y]es\u2705 / [N]o\u274C")

    def __init__(self, msg_id: int) -> None:
        self.msg_id = msg_id
        super().__init__(title="Veto", timeout=600)

    async def on_submit(self, interaction: Interaction) -> None:
        # if self.decision.values == ["Approve"]:
        if str(self.decision)[:1].upper() == "Y":
            await cast_vote(interaction, self.msg_id, True, veto=str(self.reason))
        # elif self.decision.values == ["Deny"]:
        elif str(self.decision)[:1].upper() == "N":
            await cast_vote(interaction, self.msg_id, False, veto=str(self.reason))


class PromptRoleView(View):
    def __init__(self, guild_id: int, role_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(
            Button(
                style=ButtonStyle.success,
                label="Get started",
                custom_id="{}:{}:Prompt:{}".format(__name__, guild_id, role_id),
            )
        )


async def prompt_role(interaction: Interaction, guild_id: int, role_id: int, message: Message) -> None:
    assert (guild := interaction.client.get_guild(guild_id))
    assert (role := guild.get_role(role_id))

    async with sessionmaker() as session:
        review = await session.get(ReviewedRole, role_id)
        assert review

    pre = await pre_apply(interaction.user, role)
    if isinstance(pre, ApplicationStatus):
        await interaction.response.send_message("You have already applied for this role.", ephemeral=True)
        await message.delete()
    else:
        await interaction.response.send_modal(RolePromptModal(guild, [(role, review)], message))


@cog
class RolesReviewCog(Cog):
    @Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        if interaction.type != InteractionType.component or interaction.data is None:
            return
        data = cast("discord.types.interactions.MessageComponentInteractionData", interaction.data)
        if data["component_type"] != 2:
            return
        if ":" not in data["custom_id"]:
            return
        mod, rest = data["custom_id"].split(":", 1)
        if mod != __name__ or ":" not in rest:
            return
        id, action = rest.split(":", 1)
        try:
            id = int(id)
        except ValueError:
            return
        if action == "Approve":
            await cast_vote(interaction, id, True)
        elif action == "Deny":
            await cast_vote(interaction, id, False)
        elif action == "Retract":
            await cast_vote(interaction, id, None)
        elif action == "Veto":
            await interaction.response.send_modal(VetoModal(id))
        elif action.startswith("Prompt:"):
            _, role_id = action.split(":", 1)
            try:
                role_id = int(role_id)
            except ValueError:
                return
            if not interaction.message:
                return
            await prompt_role(interaction, id, role_id, interaction.message)

    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member) -> None:
        async with sessionmaker() as session:
            for pending_role in set(after.roles) - set(before.roles):
                if pending_role.id in pending_roles:
                    stmt = select(ReviewedRole).where(ReviewedRole.pending_role_id == pending_role.id)
                    for review in (await session.execute(stmt)).scalars():
                        role = after.guild.get_role(review.id)
                        assert role
                        pre = await pre_apply(after, role)
                        if pre == ApplicationStatus.APPROVED:
                            await after.remove_roles(pending_role)
                            await after.add_roles(role)
                        elif pre == ApplicationStatus.DENIED:
                            if review.denied_role_id is not None:
                                await after.add_roles(Object(review.denied_role_id))
                            await after.remove_roles(pending_role)
                        elif isinstance(pre, ReviewedRole):
                            try:
                                await after.send(pre.invitation, view=PromptRoleView(after.guild.id, pre.id))
                            except discord.Forbidden:
                                pass

    @cleanup
    @command("review_queue")
    @privileged
    async def review_queue(self, ctx: Context, whose: Literal["any", "mine"] = "mine") -> None:
        """List unresolved applications."""
        async with sessionmaker() as session:
            if whose == "any":
                stmt = select(Application).where(Application.decision == None).order_by(Application.listing_id)
            else:
                stmt = (
                    select(Application)
                    .outerjoin(Vote, (Application.id == Vote.application_id) & (Vote.voter_id == ctx.author.id))
                    .where(Vote.application_id == None)
                    .where(Application.decision == None)
                    .order_by(Application.listing_id)
                )

            apps = (await session.execute(stmt)).scalars()

            def generate_links() -> Iterator[PlainItem]:
                yield PlainItem("Applications:\n")
                for app in apps:
                    if app.voting_id is None:
                        continue
                    chan = ctx.bot.get_partial_messageable(app.listing_id, guild_id=ctx.guild.id if ctx.guild else None)
                    msg = chan.get_partial_message(app.voting_id)
                    yield PlainItem("{}\n".format(msg.jump_url))

            for content, _ in chunk_messages(generate_links()):
                await ctx.send(content)

    @cleanup
    @command("review_reset")
    @privileged
    async def review_reset(self, ctx: Context, user: PartialUserConverter, role: PartialRoleConverter) -> None:
        """
        If a user's application for a particular role has been denied, this command will allow them to apply again.
        """
        async with sessionmaker() as session:
            review = await session.get(ReviewedRole, role.id)
            assert review

            stmt = (
                delete(Application)
                .where(Application.user_id == user.id, Application.role_id == role.id)
                .returning(Application.listing_id, Application.voting_id)
            )
            seen = False
            chan = ctx.bot.get_partial_messageable(review.review_channel_id)
            for listing_id, voting_id in await session.execute(stmt):
                seen = True
                try:
                    if voting_id is not None:
                        await chan.get_partial_message(voting_id).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                try:
                    await chan.get_partial_message(listing_id).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                try:
                    thread = await ctx.bot.fetch_channel(listing_id)
                    if isinstance(thread, Thread):
                        await thread.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            if seen:
                await session.commit()
                await ctx.send(
                    format("Reset {!m}'s application status for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none(),
                )
            else:
                await ctx.send(
                    format("{!m} has no resolved applications for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none(),
                )


class ApplicationStatus(enum.Enum):
    APPROVED = 0
    PENDING = 1
    DENIED = 2


async def check_applications(
    member: Union[User, Member], role: Role, session: AsyncSession
) -> Optional[Union[ApplicationStatus, ReviewedRole]]:
    """
    If the user wants the role, depending on whether they have previous applications:
    - the role can be just given to them, returning APPROVED,
    - if they have a pending application, we can do nothing, returning PENDING
    - we can deny it, returning DENIED,
    - if they had no previous applications, we can direct them to answer the questions, returning the ReviewedRole
    """
    stmt = select(Application.decision).where(Application.user_id == member.id, Application.role_id == role.id).limit(1)
    for decision in (await session.execute(stmt)).scalars():
        logger.debug(format("Found old application {!m} {!M} with decision={!r}", member, role, decision))
        if decision is None:
            return ApplicationStatus.PENDING
        elif decision:
            return ApplicationStatus.APPROVED
        else:
            return ApplicationStatus.DENIED
    return await session.get(ReviewedRole, role.id)


async def pre_apply(member: Union[User, Member], role: Role) -> Union[ApplicationStatus, ReviewedRole]:
    if role.id not in reviewed_roles:
        return ApplicationStatus.APPROVED
    async with sessionmaker() as session:
        pre = await check_applications(member, role, session)
        logger.debug(format("Pre-application {!m} {!M}: {!r}", member, role, pre))
        return ApplicationStatus.APPROVED if pre is None else pre


async def apply(member: Member, role: Role, inputs: List[Tuple[str, str]]) -> Optional[ApplicationStatus]:
    """
    If somehow the previous application status has changed while they were filling out the answers:
    - we can just give them the role (actually do it), returning APPROVED,
    - we can deny it, returning FALSE or PENDING,
    - we can submit it for review, returning None.
    """
    async with sessionmaker() as session:
        if role.id not in reviewed_roles:
            logger.debug(format("Application from {!m} for a non-reviewed role {!M}", member, role))
            await retry(lambda: member.add_roles(role, reason="Application for non-reviewed role"))
            return ApplicationStatus.APPROVED

        pre = await check_applications(member, role, session)
        logger.debug(format("Application {!m} {!M}: {!r}", member, role, pre))

        if pre is None:
            logger.debug(format("Application from {!m} for a non-reviewed role {!M}", member, role))
            await retry(lambda: member.add_roles(role, reason="Application for non-reviewed role"))
            return ApplicationStatus.APPROVED

        if pre == ApplicationStatus.APPROVED:
            await retry(lambda: member.add_roles(role, reason="Previously approved"))

        if isinstance(pre, ApplicationStatus):
            return pre

        review = pre

        channel = member.guild.get_channel(review.review_channel_id)
        assert isinstance(channel, TextChannel)

        async def post_application() -> None:
            listing = None
            thread = None
            voting = None
            try:
                listing = await channel.send(
                    format(
                        "{!m} ({}) requested {!M}:\n\n{}",
                        member,
                        member.display_name,
                        role,
                        "\n\n".join("**{}**: {}".format(question, answer) for question, answer in inputs),
                    ),
                    allowed_mentions=AllowedMentions.none(),
                )
                thread = await listing.create_thread(name=member.display_name)
                voting = await thread.send(
                    "Votes:", view=ApproveRoleView(listing.id), allowed_mentions=AllowedMentions.none()
                )
                app = Application(listing_id=listing.id, voting_id=voting.id, user_id=member.id, role_id=role.id)
                session.add(app)
                await session.commit()
            except:
                if voting is not None:
                    await retry(voting.delete)
                if thread is not None:
                    await retry(thread.delete)
                if listing is not None:
                    await retry(listing.delete)
                raise

        await retry(post_application)

        if review.pending_role_id is not None:
            if (repl := member.guild.get_role(review.pending_role_id)) is not None:
                await retry(lambda: member.add_roles(repl, reason="Awaiting {}".format(role.name)))


class RoleContext(Context):
    role_id: int


@plugin_config_command
@group("roles_review")
async def config(ctx: RoleContext, role: PartialRoleConverter) -> None:
    ctx.role_id = role.id


@config.command("new")
async def config_new(
    ctx: RoleContext,
    review_channel: PartialChannelConverter,
    upvote_limit: int,
    downvote_limit: int,
    invitation: Union[CodeBlock, Inline, Quoted],
    *prompt: Union[CodeBlock, Inline, Quoted],
) -> None:
    async with sessionmaker() as session:
        session.add(
            ReviewedRole(
                id=ctx.role_id,
                review_channel_id=review_channel.id,
                upvote_limit=upvote_limit,
                downvote_limit=downvote_limit,
                invitation=invitation.text,
                prompt=[q.text for q in prompt],
            )
        )
        await session.commit()
        await ctx.send("\u2705")


async def get_review(session: AsyncSession, ctx: RoleContext) -> ReviewedRole:
    if (review := await session.get(ReviewedRole, ctx.role_id)) is None:
        raise UserError("No config for {}".format(ctx.role_id))
    return review


@config.command("review_channel")
async def config_review_channel(ctx: RoleContext, channel: Optional[PartialChannelConverter]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if channel is None:
            await ctx.send(format("{!c}", review.review_channel_id))
        else:
            review.review_channel_id = channel.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("upvote_limit")
async def config_upvote_limit(ctx: RoleContext, limit: Optional[int]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if limit is None:
            await ctx.send(str(review.upvote_limit))
        else:
            review.upvote_limit = limit
            await session.commit()
            await ctx.send("\u2705")


@config.command("downvote_limit")
async def config_downvote_limit(ctx: RoleContext, limit: Optional[int]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if limit is None:
            await ctx.send(str(review.downvote_limit))
        else:
            review.downvote_limit = limit
            await session.commit()
            await ctx.send("\u2705")


@config.command("invitation")
async def config_invitation(ctx: RoleContext, text: Optional[Union[CodeBlock, Inline, Quoted]]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if text is None:
            await ctx.send(format("{!b}", review.invitation))
        else:
            review.invitation = text.text
            await session.commit()
            await ctx.send("\u2705")


@config.command("prompt")
async def config_prompt(ctx: RoleContext, *text: Union[CodeBlock, Inline, Quoted]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if not text:
            await ctx.send("\n".join(format("{!b}", prompt) for prompt in review.prompt))
        else:
            review.prompt = [q.text for q in text]
            await session.commit()
            await ctx.send("\u2705")


@config.command("pending_role")
async def config_pending_role(ctx: RoleContext, role: Optional[Union[Literal["None"], PartialRoleConverter]]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if role is None:
            await ctx.send(format("{!M}", review.pending_role_id), allowed_mentions=AllowedMentions.none())
        else:
            review.pending_role_id = None if role == "None" else role.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("denied_role")
async def config_denied_role(ctx: RoleContext, role: Optional[Union[Literal["None"], PartialRoleConverter]]) -> None:
    async with sessionmaker() as session:
        review = await get_review(session, ctx)
        if role is None:
            await ctx.send(format("{!M}", review.denied_role_id), allowed_mentions=AllowedMentions.none())
        else:
            review.denied_role_id = None if role == "None" else role.id
            await session.commit()
            await ctx.send("\u2705")

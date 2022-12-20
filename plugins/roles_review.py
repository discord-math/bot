import logging
from typing import TYPE_CHECKING, List, Optional, Protocol, Tuple, TypedDict, cast
from typing_extensions import NotRequired

import discord
from discord import AllowedMentions, ButtonStyle, Interaction, InteractionType, Member, Role, TextChannel, TextStyle
if TYPE_CHECKING:
    import discord.types.interactions
from discord.ui import Button, Modal, TextInput, View
from sqlalchemy import BOOLEAN, BigInteger, ForeignKey, Integer, PrimaryKeyConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.cogs import Cog, cog, command
from bot.commands import Context, cleanup
from bot.privileges import priv
import plugins
import util.db
import util.db.kv
from util.discord import PartialRoleConverter, PartialUserConverter, format
from util.frozen_list import FrozenList

logger = logging.getLogger(__name__)

class ReviewedRole(TypedDict):
    prompt: FrozenList[str]
    review_channel: int
    review_role: NotRequired[int]
    veto_role: NotRequired[int]
    upvote_limit: int
    downvote_limit: int
    pending_role: NotRequired[int]
    denied_role: NotRequired[int]

class RolesReviewConf(Protocol):
    def __getitem__(self, index: int) -> Optional[ReviewedRole]: ...

conf: RolesReviewConf

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

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
        def __init__(self, *, listing_id: int, user_id: int, role_id: int, id: Optional[int] = None,
            voting_id: Optional[int] = None, decision: Optional[bool] = None) -> None: ...

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

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RolesReviewConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        CreateSchema("roles_review"),
        registry.metadata.create_all))

class RolePromptModal(Modal):
    def __init__(self, prompt_roles: List[Role]) -> None:
        super().__init__(title="Additional information", timeout=1200)
        self.inputs = {}
        for role in prompt_roles:
            self.inputs[role] = []
            if (review := conf[role.id]) is None:
                continue
            for prompt in review["prompt"]:
                if "\n" in prompt:
                    prompt, placeholder = prompt.split("\n", 1)
                    placeholder = placeholder[:100]
                else:
                    placeholder = None
                prompt = prompt[:45]
                input = TextInput(label=prompt, style=TextStyle.paragraph, max_length=600,
                    placeholder=placeholder)
                self.inputs[role].append(input)
                self.add_item(input)

    async def on_submit(self, interaction: Interaction) -> None:
        if not isinstance(interaction.user, Member):
            await interaction.response.send_message("This can only be done in a server.", ephemeral=True,
                delete_after=60)
            return

        outputs = []
        for role, inputs in self.inputs.items():
            output = await apply(interaction.user, role, [(input.label, str(input)) for input in inputs])
            if output is True:
                outputs.append(format("{!M} assigned.", role))
            elif output is False:
                outputs.append(format("You have already applied for {!M}.", role))
            elif output is None:
                outputs.append(format("Your application for {!M} has been submitted for review.", role))

        await interaction.response.send_message("\n".join(outputs), ephemeral=True)

class ApproveRoleView(View):
    def __init__(self, msg_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(Button(style=ButtonStyle.success, label="Approve",
            custom_id="{}:{}:Approve".format(__name__, msg_id)))
        self.add_item(Button(style=ButtonStyle.danger, label="Deny",
            custom_id="{}:{}:Deny".format(__name__, msg_id)))
        self.add_item(Button(style=ButtonStyle.secondary, label="Retract",
            custom_id="{}:{}:Retract".format(__name__, msg_id)))
        self.add_item(Button(style=ButtonStyle.secondary, label="Veto",
            custom_id="{}:{}:Veto".format(__name__, msg_id)))

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
        if up_total >= review["upvote_limit"]:
            logger.debug(format("Vote on {} decided by reaching {} upvotes", app_id, up_total))
            return True
        elif down_total >= review["downvote_limit"]:
            logger.debug(format("Vote on {} decided by reaching {} downvotes", app_id, down_total))
            return False
    logger.debug("Vote on {} undecided".format(app_id))
    return None

async def cast_vote(interaction: Interaction, msg_id: int, dir: Optional[bool], veto: Optional[str] = None) -> None:
    assert interaction.message
    assert interaction.guild
    assert dir is not None or veto is None
    async with sessionmaker() as session:
        stmt = select(Application).where(Application.listing_id == msg_id).limit(1)
        app = (await session.execute(stmt)).scalars().first()

        if app is None:
            await interaction.response.send_message("No such application.", ephemeral=True)
            return
        review = conf[app.role_id]
        assert review

        logger.debug(format("Vote {!r} from {!m} for {!m} {!M} veto={!r}", dir, interaction.user, app.user_id,
            app.role_id, bool(veto)))

        if app.decision is not None:
            await interaction.response.send_message("This application is already resolved.", ephemeral=True)
            return

        can_vote = True
        can_veto = False
        if "review_role" in review:
            can_veto = can_vote = isinstance(interaction.user, Member) and any(role.id == review["review_role"]
                for role in interaction.user.roles)
        if "veto_role" in review:
            can_veto = isinstance(interaction.user, Member) and any(role.id == review["veto_role"]
                for role in interaction.user.roles)

        if veto is None:
            if not (can_vote or can_veto):
                await interaction.response.send_message("You are not allowed to vote on this application.",
                    ephemeral=True)
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
        await interaction.message.edit(
            content=format("{}\n{!m}: {}{}", interaction.message.content, interaction.user, comment,
                "" if decision is None else "\nDecision: \u2705" if decision else "\nDecision: \u274C"),
            view=ApproveRoleView(msg_id) if decision is None else None,
            allowed_mentions=AllowedMentions.none())

        if decision is not None:
            app.decision = decision
            await session.commit()

        await interaction.response.send_message("Vote recorded.", ephemeral=True, delete_after=60)

        if decision is not None and (user := interaction.guild.get_member(app.user_id)) is not None:
            if "pending_role" in review:
                if (role := interaction.guild.get_role(review["pending_role"])):
                    await user.remove_roles(role, reason=format("Granted {!m}", app.role_id))
            if decision == True:
                if (role := interaction.guild.get_role(app.role_id)) is not None:
                    await user.add_roles(role, reason="By vote")
            elif "denied_role" in review:
                if (role := interaction.guild.get_role(review["denied_role"])):
                    await user.add_roles(role, reason=format("Denied {!m}", app.role_id))

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
        #if self.decision.values == ["Approve"]:
        if str(self.decision)[:1].upper() == "Y":
            await cast_vote(interaction, self.msg_id, True, veto=str(self.reason))
        #elif self.decision.values == ["Deny"]:
        elif str(self.decision)[:1].upper() == "N":
            await cast_vote(interaction, self.msg_id, False, veto=str(self.reason))

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
        msg_id, action = rest.split(":", 1)
        try:
            msg_id = int(msg_id)
        except ValueError:
            return
        if action == "Approve":
            await cast_vote(interaction, msg_id, True)
        elif action == "Deny":
            await cast_vote(interaction, msg_id, False)
        elif action == "Retract":
            await cast_vote(interaction, msg_id, None)
        elif action == "Veto":
            await interaction.response.send_modal(VetoModal(msg_id))

    @cleanup
    @command("review_reset")
    @priv("mod")
    async def review_reset(self, ctx: Context, user: PartialUserConverter, role: PartialRoleConverter) -> None:
        """
        If a user's application for a particular role has been denied, this command will allow them to apply again.
        """
        review = conf[role.id]
        assert review
        async with sessionmaker() as session:
            stmt = (delete(Application)
                .where(Application.user_id == user.id, Application.role_id == role.id)
                .returning(Application.listing_id, Application.voting_id))
            seen = False
            chan = ctx.bot.get_partial_messageable(review["review_channel"])
            for listing_id, voting_id in await session.execute(stmt):
                seen = True
                try:
                    await chan.get_partial_message(listing_id).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                try:
                    if voting_id is not None:
                        await chan.get_partial_message(voting_id).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            if seen:
                await session.commit()
                await ctx.send(format("Reset {!m}'s application status for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none())
            else:
                await ctx.send(format("{!m} has no resolved applications for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none())

async def check_applications(member: Member, role: Role, session: AsyncSession) -> Optional[bool]:
    """
    If the user wants the role, depending on whether they have previous applications:
    - the role can be just given to them, returning True,
    - we can deny it, returning False,
    - if they had no previous applications, we can direct them to answer the questions, returning None
    """
    stmt = (select(Application.decision)
        .where(Application.user_id == member.id, Application.role_id == role.id)
        .limit(1))
    for decision in (await session.execute(stmt)).scalars():
        logger.debug(format("Found old application {!m} {!M} with decision={!r}", member, role, decision))
        return bool(decision)
    return None


async def pre_apply(member: Member, role: Role) -> Optional[bool]:
    if conf[role.id] is None:
        logger.debug(format("Not a reviewed role: {!M}", role))
        return True
    async with sessionmaker() as session:
        pre = await check_applications(member, role, session)
        logger.debug(format("Pre-application {!m} {!M}: {}", member, role,
            "grant" if pre else "prompt" if pre is None else "deny"))
        return pre

async def apply(member: Member, role: Role, inputs: List[Tuple[str, str]]) -> Optional[bool]:
    """
    If somehow the previous application status has changed while they were filling out the answers:
    - we can just give them the role (actually do it), returning True,
    - we can deny it, returning False,
    - we can submit it for review, returning None.
    """
    if (review := conf[role.id]) is None:
        logger.debug(format("Application from {!m} for a non-reviewed role {!M}", member, role))
        await member.add_roles(role, reason="Application for non-reviewed role")
        return True
    async with sessionmaker() as session:
        pre = await check_applications(member, role, session)
        logger.debug(format("Application {!m} {!M}: {}", member, role,
            "give" if pre else "review" if pre is None else "deny"))
        if pre:
            await member.add_roles(role, reason="Previously approved")
        if pre is not None:
            return pre

        channel = member.guild.get_channel(review["review_channel"])
        assert isinstance(channel, TextChannel)

        msg = await channel.send(format("{!m} ({}#{} {}) requested {!M}:\n\n{}", member, member.name,
                member.discriminator, member.id, role,
                "\n\n".join("**{}**: {}".format(question, answer) for question, answer in inputs)),
            allowed_mentions=AllowedMentions.none())
        app = Application(listing_id=msg.id, user_id=member.id, role_id=role.id)
        session.add(app)
        await session.commit()
        voting = await channel.send("Votes:", view=ApproveRoleView(msg.id), allowed_mentions=AllowedMentions.none())
        app.voting_id = voting.id
        await session.commit()

        if "pending_role" in review:
            if (repl := member.guild.get_role(review["pending_role"])) is not None:
                await member.add_roles(repl, reason="Awaiting {}".format(role.name))

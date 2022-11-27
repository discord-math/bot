from typing import TYPE_CHECKING, List, Literal, Optional, Protocol, Tuple, cast

from discord import AllowedMentions, ButtonStyle, Interaction, InteractionType, Member, Role, TextChannel, TextStyle
from discord.ext.commands import Cog, command
if TYPE_CHECKING:
    import discord.types.interactions
from discord.ui import Button, Modal, TextInput, View
from sqlalchemy import BOOLEAN, BigInteger, ForeignKey, Integer, PrimaryKeyConstraint, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.cogs import cog
from bot.commands import Context, cleanup
from bot.privileges import priv
import plugins
import util.db
import util.db.kv
from util.discord import PartialRoleConverter, PartialUserConverter, format

class RolesReviewConf(Protocol):
    review_channel: int
    upvote_limit: int
    downvote_limit: int
    veto_role: int

    def __getitem__(self, index: Tuple[int, Literal["role", "replace"]]) -> Optional[int]: ...

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
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, listing_id: int, user_id: int, role_id: int, resolved: bool, id: Optional[int] = None
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

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RolesReviewConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        CreateSchema("roles_review"),
        registry.metadata.create_all))

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

def voting_decision(votes: List[Vote]) -> Optional[bool]:
    up_total = 0
    down_total = 0
    for vote in votes:
        if vote.veto:
            return vote.vote
        if vote.vote:
            up_total += 1
        else:
            down_total += 1
        if up_total >= conf.upvote_limit:
            return True
        elif down_total >= conf.downvote_limit:
            return False
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

        if app.resolved:
            await interaction.response.send_message("This application is already resolved.", ephemeral=True)
            return

        if (role_id := conf[app.role_id, "role"]) is not None:
            if not isinstance(interaction.user, Member) or not any(role.id == role_id for role in interaction.user.roles):
                await interaction.response.send_message("You are not allowed to vote on this application.", ephemeral=True)
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

        decision = voting_decision(votes)

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
            if decision == True:
                stmt = delete(Application).where(Application.id == app.id)
                await session.execute(stmt)
            else:
                app.resolved = True
            await session.commit()

        await interaction.response.send_message("Vote recorded.", ephemeral=True)

        if decision == True:
            if (user := interaction.guild.get_member(app.user_id)) is not None:
                if (role := interaction.guild.get_role(app.role_id)) is not None:
                    await user.add_roles(role, reason="By vote")

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
            if (isinstance(interaction.user, Member)
                and any(role.id == conf.veto_role for role in interaction.user.roles)):
                await interaction.response.send_modal(VetoModal(msg_id))
            else:
                await interaction.response.send_message("You are not allowed to veto.", ephemeral=True)

    @cleanup
    @command("review_reset")
    @priv("mod")
    async def review_reset(self, ctx: Context, user: PartialUserConverter, role: PartialRoleConverter) -> None:
        """
        If a user's application for a particular role has been denied, this command will allow them to apply again.
        """
        async with sessionmaker() as session:
            stmt = (delete(Application)
                .where(Application.user_id == user.id, Application.role_id == role.id, Application.resolved == True)
                .returning(True))
            if (await session.execute(stmt)).first():
                await ctx.send(format("Reset {!m}'s application status for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none())
                await session.commit()
            else:
                await ctx.send(format("{!m} has no resolved applications for {!M}.", user, role),
                    allowed_mentions=AllowedMentions.none())

async def apply(member: Member, role: Role, inputs: List[Tuple[str, str]]) -> Optional[str]:
    async with sessionmaker() as session:
        stmt = (select(Application.id)
            .where(Application.user_id == member.id, Application.role_id == role.id)
            .limit(1))
        if (await session.execute(stmt)).first() is not None:
            return "You have already applied for {}.".format(role.name)

        channel = member.guild.get_channel(conf.review_channel)
        assert isinstance(channel, TextChannel)

        msg = await channel.send(format("{!m} ({}#{} {}) requested {!M}:\n\n{}", member, member.name,
                member.discriminator, member.id, role,
                "\n\n".join("**{}**: {}".format(question, answer) for question, answer in inputs)),
            allowed_mentions=AllowedMentions.none())
        session.add(Application(listing_id=msg.id, user_id=member.id, role_id=role.id, resolved=False))
        await session.commit()
        await channel.send("Votes:", view=ApproveRoleView(msg.id), allowed_mentions=AllowedMentions.none())

        if (repl_id := conf[role.id, "replace"]) is not None:
            if (repl := member.guild.get_role(repl_id)) is not None:
                await member.add_roles(repl, reason="Awaiting {}".format(role.name))

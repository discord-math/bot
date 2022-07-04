import discord
import discord.ui
import discord.app_commands
import discord.ext.commands
import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
from typing import List, Tuple, Literal, Optional, Protocol, cast, TYPE_CHECKING
import plugins
import plugins.commands
import plugins.privileges
import plugins.interactions
import plugins.cogs
import util.db
import util.db.kv
import util.discord
import util.frozen_list
if TYPE_CHECKING:
    import discord.types.interactions

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

sessionmaker = sqlalchemy.ext.asyncio.async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class Application:
    __tablename__ = "applications"
    __table_args__ = {"schema": "roles_review"}

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer, primary_key=True)
    listing_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    user_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    role_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    resolved: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(sqlalchemy.BOOLEAN, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, listing_id: int, user_id: int, role_id: int, resolved: bool, id: Optional[int] = None
            ) -> None: ...

@registry.mapped
class Vote:
    __tablename__ = "votes"

    application_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.Integer,
        sqlalchemy.ForeignKey(Application.id, ondelete="CASCADE"), nullable=False) # type: ignore
    voter_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    vote: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(sqlalchemy.BOOLEAN, nullable=False)
    veto: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(sqlalchemy.BOOLEAN, nullable=False)

    __table_args__ = (sqlalchemy.PrimaryKeyConstraint(application_id, voter_id, veto), # type: ignore
        {"schema": "roles_review"})

    if TYPE_CHECKING:
        def __init__(self, *, application_id: int, voter_id: int, vote: bool, veto: bool) -> None: ...

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RolesReviewConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        sqlalchemy.schema.CreateSchema("roles_review"),
        registry.metadata.create_all))

class ApproveRoleView(discord.ui.View):
    def __init__(self, msg_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.success, label="Approve",
            custom_id="{}:{}:Approve".format(__name__, msg_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label="Deny",
            custom_id="{}:{}:Deny".format(__name__, msg_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="Retract",
            custom_id="{}:{}:Retract".format(__name__, msg_id)))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="Veto",
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

async def cast_vote(interaction: discord.Interaction, msg_id: int, dir: Optional[bool], veto: Optional[str] = None) -> None:
    assert interaction.message
    assert interaction.guild
    assert dir is not None or veto is None
    async with sessionmaker() as session:
        stmt = sqlalchemy.select(Application).where(Application.listing_id == msg_id).limit(1)
        app = (await session.execute(stmt)).scalars().first()

        if app is None:
            await interaction.response.send_message("No such application.", ephemeral=True)
            return

        if app.resolved:
            await interaction.response.send_message("This application is already resolved.", ephemeral=True)
            return

        if (role_id := conf[app.role_id, "role"]) is not None:
            if not isinstance(interaction.user, discord.Member) or not any(role.id == role_id for role in interaction.user.roles):
                await interaction.response.send_message("You are not allowed to vote on this application.", ephemeral=True)
                return

        stmt = sqlalchemy.select(Vote).where(Vote.application_id == app.id)
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
            content=util.discord.format("{}\n{!m}: {}{}", interaction.message.content, interaction.user, comment,
                "" if decision is None else "\nDecision: \u2705" if decision else "\nDecision: \u274C"),
            view=ApproveRoleView(msg_id) if decision is None else None,
            allowed_mentions=discord.AllowedMentions.none())

        if decision is not None:
            if decision == True:
                stmt = sqlalchemy.delete(Application).where(Application.id == app.id)
                await session.execute(stmt)
            else:
                app.resolved = True
            await session.commit()

        await interaction.response.send_message("Vote recorded.", ephemeral=True)

        if decision == True:
            if (user := interaction.guild.get_member(app.user_id)) is not None:
                if (role := interaction.guild.get_role(app.role_id)) is not None:
                    await user.add_roles(role, reason="By vote")

class VetoModal(discord.ui.Modal):
    reason = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, label="Reason for the veto")
    decision = discord.ui.Select(placeholder="Decision", options=[
        discord.SelectOption(label="Approve", emoji="\u2705"), discord.SelectOption(label="Deny", emoji="\u274C")])

    def __init__(self, msg_id: int) -> None:
        self.msg_id = msg_id
        super().__init__(title="Veto", timeout=600)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.decision.values == ["Approve"]:
            await cast_vote(interaction, self.msg_id, True, veto=str(self.reason))
        elif self.decision.values == ["Deny"]:
            await cast_vote(interaction, self.msg_id, False, veto=str(self.reason))

@plugins.cogs.cog
class RolesReviewCog(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component or interaction.data is None:
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
            if (isinstance(interaction.user, discord.Member)
                and any(role.id == conf.veto_role for role in interaction.user.roles)):
                await interaction.response.send_modal(VetoModal(msg_id))
            else:
                await interaction.response.send_message("You are not allowed to veto.", ephemeral=True)

    @plugins.commands.cleanup
    @discord.ext.commands.command("review_reset")
    @plugins.privileges.priv("mod")
    async def review_reset(self, ctx: plugins.commands.Context, user: util.discord.PartialUserConverter,
        role: util.discord.PartialRoleConverter) -> None:
        """
        If a user's application for a particular role has been denied, this command will allow them to apply again.
        """
        async with sessionmaker() as session:
            stmt = (sqlalchemy.delete(Application)
                .where(Application.user_id == user.id, Application.role_id == role.id, Application.resolved == True)
                .returning(True))
            if (await session.execute(stmt)).first():
                await ctx.send(util.discord.format("Reset {!m}'s application status for {!M}.", user, role),
                    allowed_mentions=discord.AllowedMentions.none())
                await session.commit()
            else:
                await ctx.send(util.discord.format("{!m} has no resolved applications for {!M}.", user, role),
                    allowed_mentions=discord.AllowedMentions.none())

async def apply(member: discord.Member, role: discord.Role, inputs: List[Tuple[str, str]]) -> Optional[str]:
    async with sessionmaker() as session:
        stmt = (sqlalchemy.select(Application.id)
            .where(Application.user_id == member.id, Application.role_id == role.id)
            .limit(1))
        if (await session.execute(stmt)).first() is not None:
            return "You have already applied for {}.".format(role.name)

        channel = member.guild.get_channel(conf.review_channel)
        assert isinstance(channel, discord.TextChannel)

        msg = await channel.send(util.discord.format("{!m} ({}#{} {}) requested {!M}:\n\n{}", member, member.name,
                member.discriminator, member.id, role,
                "\n\n".join("**{}**: {}".format(question, answer) for question, answer in inputs)),
            allowed_mentions=discord.AllowedMentions.none())
        session.add(Application(listing_id=msg.id, user_id=member.id, role_id=role.id, resolved=False))
        await session.commit()
        await channel.send("Votes:", view=ApproveRoleView(msg.id), allowed_mentions=discord.AllowedMentions.none())

        if (repl_id := conf[role.id, "replace"]) is not None:
            if (repl := member.guild.get_role(repl_id)) is not None:
                await member.add_roles(repl, reason="Awaiting {}".format(role.name))

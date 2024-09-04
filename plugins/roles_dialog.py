from typing import TYPE_CHECKING, Dict, Iterable, List, Literal, Optional, Tuple, Union, cast

from discord import AllowedMentions, ButtonStyle, Interaction, Member, Role, SelectOption
from discord.abc import Messageable
from discord.ext.commands import group
from discord.ui import Button, Select, View
from sqlalchemy import BOOLEAN, TEXT, BigInteger, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column, raiseload, relationship
from sqlalchemy.schema import CreateSchema

from bot.commands import Context
from bot.config import plugin_config_command
from bot.interactions import command as app_command, persistent_view
import plugins
import plugins.roles_review
import util.db.kv
from util.discord import CodeBlock, Inline, PartialRoleConverter, Quoted, format, retry


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class SelectField:
    __tablename__ = "selects"
    __table_args__ = {"schema": "roles_dialog"}

    index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    boolean: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)

    items: Mapped[List["SelectItem"]] = relationship("SelectItem", lazy="joined", order_by="SelectItem.id")

    if TYPE_CHECKING:

        def __init__(self, index: int, boolean: bool) -> None: ...


@registry.mapped
class SelectItem:
    __tablename__ = "items"
    __table_args__ = {"schema": "roles_dialog"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    index: Mapped[int] = mapped_column(BigInteger, ForeignKey(SelectField.index), nullable=False)
    role_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    label: Mapped[Optional[str]] = mapped_column(TEXT)
    description: Mapped[Optional[str]] = mapped_column(TEXT)

    if TYPE_CHECKING:

        def __init__(
            self,
            index: int,
            id: int = ...,
            role_id: Optional[int] = ...,
            label: Optional[str] = ...,
            description: Optional[str] = ...,
        ) -> None: ...


selects: List[SelectField]


async def rehash(session: AsyncSession) -> None:
    global selects
    stmt = select(SelectField).order_by(SelectField.index)
    selects = list((await session.execute(stmt)).scalars().unique())


@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(CreateSchema("roles_dialog"), registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)

        def mk_item(index: int, item: Union[int, str]) -> SelectItem:
            if isinstance(item, int):
                return SelectItem(index=index, role_id=item, description=cast(Optional[str], conf[item, "desc"]))
            else:
                return SelectItem(index=index, label=item)

        if conf.roles is not None:
            index = 0
            booleans: bool = False
            for lst in cast(List[List[Union[int, str]]], conf.roles):
                if len(lst) == 1:
                    session.add(mk_item(index, lst[0]))
                    booleans = True
                else:
                    if booleans:
                        session.add(SelectField(index=index, boolean=True))
                        index += 1
                        booleans = False
                    for l in lst:
                        session.add(mk_item(index, l))
                    session.add(SelectField(index=index, boolean=False))
                    index += 1
            if booleans:
                session.add(SelectField(index=index, boolean=True))
                index += 1

            await session.commit()
            conf.roles = None
            await conf

        await rehash(session)


class RoleSelect(Select["RolesView"]):
    def __init__(
        self, boolean: bool, role_items: Iterable[SelectItem], member: Member, row: Optional[int] = None
    ) -> None:
        self.roles: Dict[str, Role] = {}
        index = 0
        options = []

        for item in role_items:
            if item.role_id is not None:
                if (role := member.guild.get_role(item.role_id)) is not None:
                    options.append(
                        SelectOption(
                            label=(role.name if item.label is None else item.label)[:100],
                            value=str(index),
                            description=(item.description or "")[:100],
                            default=role in member.roles,
                        )
                    )
                    self.roles[str(index)] = role
                    index += 1
            elif item.label is not None:
                options.append(
                    SelectOption(label=item.label[:100], value="_", description=(item.description or "")[:100])
                )

        if not boolean and sum(option.default for option in options) > 1:
            for option in options:
                option.default = False

        super().__init__(
            placeholder="Select roles..." if boolean else "Select a role...",
            min_values=0 if boolean else 1,
            max_values=len(options) if boolean else 1,
            options=options,
        )

    async def callback(self, interaction: Interaction) -> None:
        if not isinstance(interaction.user, Member):
            await interaction.response.send_message(
                "This can only be done in a server.", ephemeral=True, delete_after=60
            )
            return
        member = interaction.user

        selected_roles = set()
        for index in self.values:
            if index in self.roles:
                selected_roles.add(self.roles[index])
        add_roles = set()
        remove_roles = set()
        prompt_roles: List[Tuple[Role, plugins.roles_review.ReviewedRole]] = []
        for role in self.roles.values():
            if role in member.roles and role not in selected_roles:
                remove_roles.add(role)
            if role not in member.roles and role in selected_roles:
                pre = await plugins.roles_review.pre_apply(member, role)
                if pre == plugins.roles_review.ApplicationStatus.APPROVED:
                    add_roles.add(role)
                elif isinstance(pre, plugins.roles_review.ReviewedRole):
                    prompt_roles.append((role, pre))
                # TODO: tell them if False?

        if prompt_roles:
            await interaction.response.send_modal(plugins.roles_review.RolePromptModal(member.guild, prompt_roles))
        else:
            await interaction.response.defer(ephemeral=True)

        if add_roles:
            await retry(lambda: member.add_roles(*add_roles, reason="Role dialog"))
        if remove_roles:
            await retry(lambda: member.remove_roles(*remove_roles, reason="Role dialog"))

        if not prompt_roles:
            await interaction.followup.send(
                "\u2705 Updated roles." if add_roles or remove_roles else "Roles not changed.", ephemeral=True
            )


class RolesView(View):
    def __init__(self, member: Member) -> None:
        super().__init__(timeout=600)

        for select in selects:
            self.add_item(RoleSelect(select.boolean, select.items, member))


async def send_roles_view(interaction: Interaction) -> None:
    if not isinstance(interaction.user, Member):
        await interaction.response.send_message("This can only be done in a server.", ephemeral=True, delete_after=60)
        return
    await interaction.response.send_message("Select your roles:", view=RolesView(interaction.user), ephemeral=True)


class ManageRolesButton(Button["ManageRolesView"]):
    def __init__(self) -> None:
        super().__init__(style=ButtonStyle.primary, label="Manage roles", custom_id="{}:manage".format(__name__))

    async def callback(self, interaction: Interaction) -> None:
        await send_roles_view(interaction)


class ManageRolesView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ManageRolesButton())


persistent_view(ManageRolesView())


@app_command("roles", description="Manage self-assigned roles.")
async def roles_command(interaction: Interaction) -> None:
    await send_roles_view(interaction)


@plugin_config_command
@group("roles_dialog", invoke_without_command=True)
async def config(ctx: Context) -> None:
    async with sessionmaker() as session:
        stmt = select(SelectField).order_by(SelectField.index)
        selects = (await session.execute(stmt)).scalars().unique()

    await ctx.send(
        "\n".join(
            format(
                "- index {!i}: {} {}",
                select.index,
                "multi" if select.boolean else "choice",
                ", ".join(format("ID {!i}", item.id) for item in select.items),
            )
            for select in selects
        )
    )


@config.command("new")
async def config_new(ctx: Context, index: int) -> None:
    async with sessionmaker() as session:
        if not await session.get(SelectField, index, options=[raiseload(SelectField.items)]):
            session.add(SelectField(index=index, boolean=True))
        item = SelectItem(index=index)
        session.add(item)
        await session.commit()
        await rehash(session)
        await ctx.send(format("Created: ID {!i}", item.id))


@config.command("remove")
async def config_remove(ctx: Context, id: int) -> None:
    async with sessionmaker() as session:
        item = await session.get(SelectItem, id)
        assert item
        await session.delete(item)
        select = await session.get(SelectField, item.index)
        assert select
        if not select.items:
            await session.delete(select)
        await session.commit()
        await rehash(session)
        await ctx.send(
            format("Removed ID {!i} and index {!i}", item.id, select.index)
            if not select.items
            else format("Removed ID {!i}", item.id)
        )


@config.command("mode")
async def config_mode(ctx: Context, index: int, mode: Optional[Literal["choice", "multi"]]) -> None:
    async with sessionmaker() as session:
        select = await session.get(SelectField, index, options=[raiseload(SelectField.items)])
        assert select
        if mode is None:
            await ctx.send("multi" if select.boolean else "choice")
        else:
            select.boolean = mode == "choice"
            await session.commit()
            await rehash(session)
            await ctx.send("\u2705")


@config.command("role")
async def config_role(ctx: Context, id: int, role: Optional[Union[Literal["None"], PartialRoleConverter]]) -> None:
    async with sessionmaker() as session:
        item = await session.get(SelectItem, id)
        assert item
        if role is None:
            await ctx.send(
                "None" if item.role_id is None else format("{!M}", item.role_id),
                allowed_mentions=AllowedMentions.none(),
            )
        else:
            item.role_id = None if role == "None" else role.id
            await session.commit()
            await rehash(session)
            await ctx.send("\u2705")


@config.command("label")
async def config_label(
    ctx: Context, id: int, label: Optional[Union[Literal["None"], CodeBlock, Inline, Quoted]]
) -> None:
    async with sessionmaker() as session:
        item = await session.get(SelectItem, id)
        assert item
        if label is None:
            await ctx.send("None" if item.label is None else format("{!b}", item.label))
        else:
            item.label = None if label == "None" else label.text
            await session.commit()
            await rehash(session)
            await ctx.send("\u2705")


@config.command("description")
async def config_description(
    ctx: Context, id: int, description: Optional[Union[Literal["None"], CodeBlock, Inline, Quoted]]
) -> None:
    async with sessionmaker() as session:
        item = await session.get(SelectItem, id)
        assert item
        if description is None:
            await ctx.send("None" if item.description is None else format("{!b}", item.description))
        else:
            item.description = None if description == "None" else description.text
            await session.commit()
            await rehash(session)
            await ctx.send("\u2705")


async def setup(target: Messageable) -> None:
    await target.send(view=ManageRolesView())

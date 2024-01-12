from typing import Iterable, List, Literal, Optional, Protocol, Tuple, Union, cast

from discord import ButtonStyle, Interaction, Member, SelectOption
from discord.abc import Messageable
from discord.ui import Button, Select, View

from bot.interactions import command, persistent_view
import plugins
import plugins.roles_review
import util.db.kv
from util.discord import retry
from util.frozen_list import FrozenList


class RolesDialogConf(Protocol):
    roles: FrozenList[FrozenList[Union[int, str]]]

    def __getitem__(self, index: Tuple[int, Literal["desc"]]) -> Optional[str]:
        ...


conf: RolesDialogConf


@plugins.init
async def init() -> None:
    global conf
    conf = cast(RolesDialogConf, await util.db.kv.load(__name__))


class RoleSelect(Select["RolesView"]):
    def __init__(
        self, boolean: bool, role_items: Iterable[Union[int, str]], member: Member, row: Optional[int] = None
    ) -> None:
        self.roles = {}
        index = 0
        options = []

        for item in role_items:
            if isinstance(item, int):
                if (role := member.guild.get_role(item)) is not None:
                    options.append(
                        SelectOption(
                            label=role.name,
                            value=str(index),
                            description=(conf[role.id, "desc"] or "")[:100],
                            default=role in member.roles,
                        )
                    )
                    self.roles[str(index)] = role
                    index += 1
            else:
                options.append(SelectOption(label=item, value="_"))
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
        prompt_roles = []
        for role in self.roles.values():
            if role in member.roles and role not in selected_roles:
                remove_roles.add(role)
            if role not in member.roles and role in selected_roles:
                pre = await plugins.roles_review.pre_apply(member, role)
                if pre == plugins.roles_review.ApplicationStatus.APPROVED:
                    add_roles.add(role)
                elif pre is None:
                    prompt_roles.append(role)
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

        booleans: List[Union[int, str]] = []
        for lst in conf.roles:
            if len(lst) == 1:
                booleans.append(lst[0])
            else:
                if booleans:
                    self.add_item(RoleSelect(True, booleans, member))
                    booleans = []
                self.add_item(RoleSelect(False, lst, member))
        if booleans:
            self.add_item(RoleSelect(True, booleans, member))


async def send_roles_view(interaction: Interaction) -> None:
    if not isinstance(interaction.user, Member):
        await interaction.response.send_message("This can only be done in a server.", ephemeral=True, delete_after=60)
        return
    await interaction.response.send_message("Select your roles:", view=RolesView(interaction.user), ephemeral=True)


class ManageRolesButton(Button["ManageRolesView"]):
    def __init__(self) -> None:
        super().__init__(style=ButtonStyle.primary, label="Manage roles", custom_id=__name__ + ":" + "manage")

    async def callback(self, interaction: Interaction) -> None:
        await send_roles_view(interaction)


class ManageRolesView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ManageRolesButton())


persistent_view(ManageRolesView())


@command("roles", description="Manage self-assigned roles.")
async def roles_command(interaction: Interaction) -> None:
    await send_roles_view(interaction)


async def setup(target: Messageable) -> None:
    await target.send(view=ManageRolesView())

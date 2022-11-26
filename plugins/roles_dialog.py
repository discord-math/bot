from typing import Iterable, List, Literal, Optional, Protocol, Tuple, Union, cast, overload

from discord import ButtonStyle, Interaction, Member, Role, SelectOption, TextStyle
from discord.abc import Messageable
from discord.ui import Button, Modal, Select, TextInput, View

from bot.interactions import command, persistent_view
import plugins
import plugins.roles_review
import util.db.kv
from util.frozen_list import FrozenList

class RolesDialogConf(Protocol):
    roles: FrozenList[FrozenList[Union[int, str]]]

    @overload
    def __getitem__(self, index: Tuple[int, Literal["desc"]]) -> Optional[str]: ...
    @overload
    def __getitem__(self, index: Tuple[int, Literal["prompt"]]) -> Optional[FrozenList[str]]: ...

conf: RolesDialogConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RolesDialogConf, await util.db.kv.load(__name__))

class RolePromptModal(Modal):
    def __init__(self, prompt_roles: List[Role]) -> None:
        super().__init__(title="Additional information", timeout=1200)
        self.inputs = {}
        for role in prompt_roles:
            self.inputs[role] = []
            for prompt in conf[role.id, "prompt"] or ():
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
            await interaction.response.send_message("This can only be done in a server.", ephemeral=True)
            return

        outputs = []
        for role, inputs in self.inputs.items():
            output = await plugins.roles_review.apply(interaction.user, role,
                [(input.label, str(input)) for input in inputs])
            if output is not None:
                outputs.append(output)

        await interaction.response.send_message(
            "\n".join(outputs) if outputs else "Your input has been submitted for review.", ephemeral=True)

class RoleSelect(Select["RolesView"]):
    def __init__(self, boolean: bool, role_items: Iterable[Union[int, str]], member: Member, row: Optional[int] = None
        ) -> None:
        self.roles = {}
        index = 0
        options = []

        for item in role_items:
            if isinstance(item, int):
                if (role := member.guild.get_role(item)) is not None:
                    options.append(SelectOption(label=role.name, value=str(index),
                        description=(conf[role.id, "desc"] or "")[:100], default=role in member.roles))
                    self.roles[str(index)] = role
                    index += 1
            else:
                options.append(SelectOption(label=item, value="_"))
        if not boolean and sum(option.default for option in options) > 1:
            for option in options:
                option.default = False

        super().__init__(placeholder="Select roles..." if boolean else "Select a role...",
            min_values=0 if boolean else 1,
            max_values=len(options) if boolean else 1,
            options=options)

    async def callback(self, interaction: Interaction) -> None:
        if not isinstance(interaction.user, Member):
            await interaction.response.send_message("This can only be done in a server.", ephemeral=True)
            return

        selected_roles = set()
        for index in self.values:
            if index in self.roles:
                selected_roles.add(self.roles[index])
        add_roles = set()
        remove_roles = set()
        prompt_roles = []
        for role in self.roles.values():
            if role in interaction.user.roles and role not in selected_roles:
                remove_roles.add(role)
            if role not in interaction.user.roles and role in selected_roles:
                if conf[role.id, "prompt"] is not None:
                    prompt_roles.append(role)
                else:
                    add_roles.add(role)

        if add_roles:
            await interaction.user.add_roles(*add_roles, reason="Role dialog")
        if remove_roles:
            await interaction.user.remove_roles(*remove_roles, reason="Role dialog")

        if prompt_roles:
            await interaction.response.send_modal(RolePromptModal(prompt_roles))
        else:
            await interaction.response.send_message(
                "\u2705 Updated roles." if add_roles or remove_roles else "Roles not changed.", ephemeral=True)

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
        await interaction.response.send_message("This can only be done in a server.", ephemeral=True)
        return
    await interaction.response.send_message("Select your roles:", view=RolesView(interaction.user), ephemeral=True)

class ManageRolesButton(Button["ManageRolesView"]):
    def __init__(self) -> None:
        super().__init__(style=ButtonStyle.primary, label="Manage roles",
            custom_id=__name__ + ":" + "manage")

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

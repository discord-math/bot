from typing import Optional, Protocol, cast

import discord
import discord.ext.commands

import bot.cogs
import plugins
import util.db.kv
import util.discord
import util.frozen_list

class RoleOverrideConf(Protocol):
    def __getitem__(self, id: int) -> Optional[util.frozen_list.FrozenList[int]]: ...
    def __contains__(self, id: int) -> bool: ...

conf: RoleOverrideConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RoleOverrideConf, await util.db.kv.load(__name__))

@bot.cogs.cog
class RoleOverride(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        removed = set()
        for role in after.roles:
            if (masked := conf[role.id]) is not None:
                for r in after.roles:
                    if r.id in masked:
                        removed.add(r)
        if len(removed):
            await after.remove_roles(*removed)

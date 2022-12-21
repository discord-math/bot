from typing import Optional, Protocol, cast

from discord import Member

from bot.cogs import Cog, cog
import plugins
import util.db.kv
from util.discord import retry
from util.frozen_list import FrozenList

class RoleOverrideConf(Protocol):
    def __getitem__(self, id: int) -> Optional[FrozenList[int]]: ...
    def __contains__(self, id: int) -> bool: ...

conf: RoleOverrideConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(RoleOverrideConf, await util.db.kv.load(__name__))

@cog
class RoleOverride(Cog):
    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member) -> None:
        removed = set()
        for role in after.roles:
            if (masked := conf[role.id]) is not None:
                for r in after.roles:
                    if r.id in masked:
                        removed.add(r)
        if len(removed):
            await retry(lambda: after.remove_roles(*removed))

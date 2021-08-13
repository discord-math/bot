from typing import Protocol, cast
import plugins
import discord
import util.db.kv
import util.discord

class TalksRoleConf(Protocol):
    def __getitem__(self, id: str) -> util.frozen_list.FrozenList[str]: ...
    def __contains__(self, id: str) -> bool: ...

conf: TalksRoleConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(TalksRoleConf, await util.db.kv.load(__name__))

@util.discord.event("member_update")
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    removed = set()
    for role in after.roles:
        if str(role.id) in conf:
            masked = conf[str(role.id)]
            for r in after.roles:
                if str(r.id) in masked:
                    removed.add(r)
    if len(removed):
        await after.remove_roles(*removed)

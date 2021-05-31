import util.db.kv
import util.discord

conf = util.db.kv.Config(__name__)

@util.discord.event("member_update")
async def on_member_update(before, after):
    removed = set()
    for role in after.roles:
        if str(role.id) in conf:
            masked = conf[str(role.id)]
            for r in after.roles:
                if str(r.id) in masked:
                    removed.add(r)
    if len(removed):
        await after.remove_roles(*removed)

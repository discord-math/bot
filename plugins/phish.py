import asyncio
import aiohttp
import json
import re
import logging
import discord.ext.commands
from typing import List, Set, Optional, Union, Iterable, Awaitable, Protocol, cast
import util.db.kv
import util.discord
import util.frozen_list
import plugins
import plugins.commands

class PhishConf(Protocol, Awaitable[None]):
    api: str
    identity: str
    resolve_domains: util.frozen_list.FrozenList[str]
    local_blacklist: util.frozen_list.FrozenList[str]
    local_whitelist: util.frozen_list.FrozenList[str]

conf: PhishConf
logger = logging.getLogger(__name__)
session: aiohttp.ClientSession
ws_task: asyncio.Task[None]

async def get_all_domains() -> List[str]:
    async with session.request("GET", conf.api + "/v2/all", headers={"X-Identity": conf.identity}) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json"
        data = json.loads(await response.text())
        assert isinstance(data, list)
        for domain in data:
            assert isinstance(domain, str)
        return data

domains: Set[str] = set()
local_blacklist: Set[str] = set()
local_whitelist: Set[str] = set()

async def watch_websocket() -> None:
    global domains, local_blacklist, local_whitelist
    while True:
        try:
            ws = await session.ws_connect(conf.api + "/feed", headers={"X-Identity": conf.identity})
            logger.info("Websocket connected: {!r}".format(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        logger.debug("Got payload: {}".format(msg.data))
                        payload = json.loads(msg.data)
                        new_domains = set(payload["domains"])
                        update_conf = False
                        if payload["type"] == "add":
                            domains |= new_domains
                            if local_blacklist & new_domains:
                                local_blacklist -= new_domains
                                update_conf = True
                        elif payload["type"] == "delete":
                            domains -= new_domains
                            if local_whitelist & new_domains:
                                local_whitelist -= new_domains
                                update_conf = True
                        if update_conf:
                            conf.local_blacklist = util.frozen_list.FrozenList(local_blacklist)
                            conf.local_whitelist = util.frozen_list.FrozenList(local_whitelist)
                            await conf
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break
            finally:
                await ws.close()
                logger.info("Websocket closed, restarting")
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in phish websocket", exc_info=True)
        await asyncio.sleep(60)

def should_resolve_domain(domain: str) -> bool:
    return domain.lower() in conf.resolve_domains

def domain_checks(domain: str) -> Iterable[str]:
    checks = [domain]
    if domain.startswith("www."):
        checks.append(domain.removeprefix("www."))
    else:
        checks.append("www." + domain)
    return checks

def is_bad_domain(domain: str) -> bool:
    checks = domain_checks(domain)
    if any(domain in local_whitelist for domain in checks):
        return False
    if any(domain in local_blacklist for domain in checks):
        return True
    if any(domain in domains for domain in checks):
        return True
    return False

async def resolve_link(link: str) -> Optional[str]:
    try:
        logger.debug("Looking up {!r}".format(link))
        async with plugins.phish.session.request("HEAD", link, allow_redirects=False, timeout=5.0) as response:
            logger.debug("Link {!r} got {}, {!r}".format(link, response.status, response.headers.get("location")))
            if response.status in [301, 302] and "location" in response.headers:
                return response.headers["location"]
    except aiohttp.ClientError:
        pass
    return None

@plugins.commands.group("phish")
@plugins.privileges.priv("mod")
async def phish_command(ctx: discord.ext.commands.Context) -> None:
    """Manage the phishing domain list."""
    pass

def link_to_domain(link: str) -> str:
    if (match := re.match(r"\s*https?://([^/]*).*", link)) is not None:
        return match.group(1)
    else:
        return link.strip()

@phish_command.command("check")
async def phish_check(ctx: discord.ext.commands.Context, *,
    link: Union[util.discord.CodeBlock, util.discord.Inline, util.discord.Quoted]) -> None:
    """Check a link against the domain list."""
    domain = link_to_domain(link.text)
    checks = domain_checks(domain)
    output = []
    for check in checks:
        if check in local_whitelist:
            output.append(util.discord.format("{!i} is listed locally as safe.", check))
    for check in checks:
        if check in local_blacklist:
            output.append(util.discord.format("{!i} is listed locally as malicious.", check))
    for check in checks:
        if check in domains:
            output.append(util.discord.format("{!i} appears in the malicious domain list.", check))
    if len(output) == 0:
        output.append("The domain is not listed anywhere.")
    await ctx.send("\n".join(output))

@phish_command.command("add")
async def phish_add(ctx: discord.ext.commands.Context, *,
    link: Union[util.discord.CodeBlock, util.discord.Inline, util.discord.Quoted]) -> None:
    """Locally mark a domain as malicious."""
    domain = link_to_domain(link.text)
    checks = domain_checks(domain)
    output = []
    for check in checks:
        if check in local_whitelist:
            local_whitelist.remove(check)
            conf.local_whitelist = util.frozen_list.FrozenList(local_whitelist)
            output.append(util.discord.format("{!i} is no longer locally marked as safe.", check))
    for check in checks:
        if check in local_blacklist:
            output.append(util.discord.format("{!i} is already listed locally as malicious.", check))
    if not any(check in local_blacklist for check in checks):
        for check in checks:
            if check in domains:
                output.append(util.discord.format("{!i} already appears in the malicious domain list.", check))
        if not any(check in domains for check in checks):
            local_blacklist.add(domain)
            conf.local_blacklist = util.frozen_list.FrozenList(local_blacklist)
            output.append(util.discord.format("{!i} is now marked locally as malicious.", domain))
    await conf
    await ctx.send("\n".join(output))

@phish_command.command("remove")
async def phish_remove(ctx: discord.ext.commands.Context, *,
    link: Union[util.discord.CodeBlock, util.discord.Inline, util.discord.Quoted]) -> None:
    """Locally mark a domain as safe."""
    domain = link_to_domain(link.text)
    checks = domain_checks(domain)
    output = []
    for check in checks:
        if check in local_blacklist:
            local_blacklist.remove(check)
            conf.local_blacklist = util.frozen_list.FrozenList(local_blacklist)
            output.append(util.discord.format("{!i} is no longer locally marked as malicious.", check))
    for check in checks:
        if check in local_whitelist:
            output.append(util.discord.format("{!i} is already listed locally as safe.", check))
    if not any(check in local_whitelist for check in checks):
        for check in checks:
            if check in domains:
                output.append(util.discord.format("{!i} appears in the malicious domain list.", check))
        if any(check in domains for check in checks):
            local_whitelist.add(domain)
            conf.local_whitelist = util.frozen_list.FrozenList(local_whitelist)
            output.append(util.discord.format("{!i} is now marked locally as safe.", domain))
        else:
            output.append(util.discord.format("{!i} does not appear in the malicious domain list.", domain))
    await conf
    await ctx.send("\n".join(output))

@plugins.init
async def init() -> None:
    global conf, session, domains, local_blacklist, local_whitelist, ws_task
    conf = cast(PhishConf, await util.db.kv.load(__name__))
    if conf.local_blacklist is None:
        conf.local_blacklist = []
    if conf.local_whitelist is None:
        conf.local_whitelist = []
    local_blacklist = set(conf.local_blacklist)
    local_whitelist = set(conf.local_whitelist)
    session = aiohttp.ClientSession()
    plugins.finalizer(session.close)
    domains = set(await get_all_domains())
    if domains & local_blacklist:
        local_blacklist -= domains
        conf.local_blacklist = util.frozen_list.FrozenList(local_blacklist)
    await conf
    ws_task = asyncio.create_task(watch_websocket())
    plugins.finalizer(ws_task.cancel)

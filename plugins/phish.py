import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Iterable, List, Optional, Set, Union, cast

import aiohttp
from discord.ext.commands import group
from sqlalchemy import TEXT, BigInteger, Computed, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.acl import privileged
from bot.commands import Context, plugin_command
from bot.config import plugin_config_command
from bot.reactions import get_input
from bot.tasks import task
import plugins
import util.db.kv
from util.discord import CodeBlock, Inline, Quoted, Typing, format


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class GlobalConfig:
    __tablename__ = "config"
    __table_args__ = {"schema": "phish"}

    id: Mapped[int] = mapped_column(BigInteger, Computed("0"), primary_key=True)
    api_url: Mapped[Optional[str]] = mapped_column(TEXT)
    identity: Mapped[Optional[str]] = mapped_column(TEXT)
    submit_url: Mapped[Optional[str]] = mapped_column(TEXT)
    submit_token: Mapped[Optional[str]] = mapped_column(TEXT)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            id: int = ...,
            api_url: Optional[str] = ...,
            identity: Optional[str] = ...,
            submit_url: Optional[str] = ...,
            submit_token: Optional[str] = ...,
        ) -> None:
            ...


@registry.mapped
class ResolvedDomain:
    __tablename__ = "resolved_domains"
    __table_args__ = {"schema": "phish"}

    domain: Mapped[str] = mapped_column(TEXT, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, *, domain: str) -> None:
            ...


@registry.mapped
class BlockedDomain:
    __tablename__ = "blocked_domains"
    __table_args__ = {"schema": "phish"}

    domain: Mapped[str] = mapped_column(TEXT, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, *, domain: str) -> None:
            ...


@registry.mapped
class AllowedDomain:
    __tablename__ = "allowed_domains"
    __table_args__ = {"schema": "phish"}

    domain: Mapped[str] = mapped_column(TEXT, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, domain: str) -> None:
            ...


conf: GlobalConfig
conf_set = asyncio.Event()
resolve_domains: Set[str] = set()
domains: Set[str] = set()
local_blocklist: Set[str] = set()
local_allowlist: Set[str] = set()

logger = logging.getLogger(__name__)
http: aiohttp.ClientSession = aiohttp.ClientSession()
plugins.finalizer(http.close)
ws_task: asyncio.Task[None]


async def get_all_domains() -> List[str]:
    if conf.api_url is None:
        return []

    headers = {}
    if conf.identity is not None:
        headers["X-Identity"] = conf.identity
    async with http.get(conf.api_url + "/v2/all", headers=headers) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json"
        data = json.loads(await response.text())
        assert isinstance(data, list)
        for domain in data:
            assert isinstance(domain, str)
        return data


async def submit_link(link: str, reason: str) -> str:
    if conf.submit_url is None:
        return "Submission URL not configured"
    headers = {}
    if conf.submit_token is not None:
        headers["Authorization"] = conf.submit_token
    payload = {"url": link, "reason": reason}
    async with http.post(conf.submit_url, headers=headers, json=payload) as response:
        return await response.text()


@task(name="Phishing websocket task", every=0, exc_backoff_base=2)
async def websocket_task() -> None:
    global domains, local_blocklist, local_allowlist
    await conf_set.wait()
    if conf.api_url is None:
        await asyncio.sleep(600)
        return

    headers = {}
    if conf.identity is not None:
        headers["X-Identity"] = conf.identity
    ws = await http.ws_connect(conf.api_url + "/feed", headers=headers)
    logger.info("Websocket connected: {!r}".format(ws))
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                logger.debug("Got payload: {}".format(msg.data))
                payload = json.loads(msg.data)
                new_domains = set(payload["domains"])
                if payload["type"] == "add":
                    domains |= new_domains
                    if unblocked := local_blocklist & new_domains:
                        async with sessionmaker() as session:
                            stmt = delete(BlockedDomain).where(BlockedDomain.domain.in_(unblocked))
                            await session.execute(stmt)
                            local_blocklist -= unblocked
                            await session.commit()
                elif payload["type"] == "delete":
                    domains -= new_domains
                    if unallowed := local_allowlist & new_domains:
                        async with sessionmaker() as session:
                            stmt = delete(AllowedDomain).where(AllowedDomain.domain.in_(unallowed))
                            await session.execute(stmt)
                            local_allowlist -= unallowed
                            await session.commit()

            elif msg.type == aiohttp.WSMsgType.CLOSED:
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        await ws.close()
        logger.info("Websocket closed, restarting")


def should_resolve_domain(domain: str) -> bool:
    return domain.lower() in resolve_domains


def domain_checks(domain: str) -> Iterable[str]:
    checks = [domain]
    if domain.startswith("www."):
        checks.append(domain.removeprefix("www."))
    else:
        checks.append("www." + domain)
    return checks


def is_bad_domain(domain: str) -> bool:
    checks = domain_checks(domain)
    if any(domain in local_allowlist for domain in checks):
        return False
    if any(domain in local_blocklist for domain in checks):
        return True
    if any(domain in domains for domain in checks):
        return True
    return False


async def resolve_link(link: str) -> Optional[str]:
    try:
        logger.debug("Looking up {!r}".format(link))
        async with http.head(link, allow_redirects=False, timeout=5.0) as response:
            logger.debug("Link {!r} got {}, {!r}".format(link, response.status, response.headers.get("location")))
            if response.status in [301, 302] and "location" in response.headers:
                return response.headers["location"]
    except aiohttp.ClientError:
        pass
    return None


@plugin_command
@group("phish")
@privileged
async def phish_command(ctx: Context) -> None:
    """Manage the phishing domain list."""
    pass


def link_to_domain(link: str) -> str:
    if (match := re.match(r"\s*(?:https?://?)?([^/]*).*", link)) is not None:
        return match.group(1)
    else:
        return link.strip()


@phish_command.command("check")
@privileged
async def phish_check(ctx: Context, *, link: Union[CodeBlock, Inline, Quoted]) -> None:
    """Check a link against the domain list."""
    domain = link_to_domain(link.text)
    checks = domain_checks(domain)
    output = []
    for check in checks:
        if check in local_allowlist:
            output.append(format("{!i} is listed locally as safe.", check))
    for check in checks:
        if check in local_blocklist:
            output.append(format("{!i} is listed locally as malicious.", check))
    for check in checks:
        if check in domains:
            output.append(format("{!i} appears in the malicious domain list.", check))
    if len(output) == 0:
        output.append("The domain is not listed anywhere.")
    await ctx.send("\n".join(output))


@phish_command.command("add")
@privileged
async def phish_add(ctx: Context, *, link: Union[CodeBlock, Inline, Quoted]) -> None:
    """Locally mark a domain as malicious."""
    async with sessionmaker() as session:
        domain = link_to_domain(link.text)
        checks = domain_checks(domain)
        output = []
        do_submit = False
        for check in checks:
            if obj := await session.get(AllowedDomain, check):
                await session.delete(obj)
                local_allowlist.discard(obj.domain)
                output.append(format("{!i} is no longer locally marked as safe.", check))
        any_blocked = False
        for check in checks:
            if await session.get(BlockedDomain, check):
                output.append(format("{!i} is already listed locally as malicious.", check))
                any_blocked = True
        if not any_blocked:
            any_domain = False
            for check in checks:
                if check in domains:
                    output.append(format("{!i} already appears in the malicious domain list.", check))
                    any_domain = True
            if not any_domain:
                session.add(BlockedDomain(domain=domain))
                local_blocklist.add(domain)
                output.append(
                    format(
                        "{!i} is now marked locally as malicious. Submitting {!i} to the phishing database, "
                        'please input a reason (e.g. "nitro scam", \u274C to cancel):',
                        domain,
                        link.text,
                    )
                )
                do_submit = True
        await session.commit()

    msg = await ctx.send("\n".join(output))

    if do_submit:
        reason = await get_input(msg, ctx.author, {"\u274C": None}, timeout=300)
        if reason is not None:
            async with Typing(ctx):
                result = await submit_link(link.text, format("{!m}: {}", ctx.author, reason.content))
            await ctx.send(format("{!i}", result))


@phish_command.command("remove")
@privileged
async def phish_remove(ctx: Context, *, link: Union[CodeBlock, Inline, Quoted]) -> None:
    """Locally mark a domain as safe."""
    async with sessionmaker() as session:
        domain = link_to_domain(link.text)
        checks = domain_checks(domain)
        output = []
        for check in checks:
            if obj := await session.get(BlockedDomain, check):
                await session.delete(obj)
                local_blocklist.discard(obj.domain)
                output.append(format("{!i} is no longer locally marked as malicious.", check))
        any_allowed = False
        for check in checks:
            if await session.get(AllowedDomain, check):
                output.append(format("{!i} is already listed locally as safe.", check))
                any_allowed = True
        if not any_allowed:
            any_domain = False
            for check in checks:
                if check in domains:
                    output.append(format("{!i} appears in the malicious domain list.", check))
                    any_domain = True
            if any_domain:
                session.add(AllowedDomain(domain=domain))
                local_allowlist.add(domain)
                output.append(format("{!i} is now marked locally as safe.", domain))
            else:
                output.append(format("{!i} does not appear in the malicious domain list.", domain))
        await session.commit()
    await ctx.send("\n".join(output))


@plugins.init
async def init() -> None:
    global conf, http, domains, local_blocklist, local_allowlist, ws_task
    await util.db.init(util.db.get_ddl(CreateSchema("phish"), registry.metadata.create_all))
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        if not c:
            old_conf = await util.db.kv.load(__name__)
            c = GlobalConfig(
                api_url=cast(Optional[str], old_conf.api),
                identity=cast(Optional[str], old_conf.identity),
                submit_url=cast(Optional[str], old_conf.submit_url),
                submit_token=cast(Optional[str], old_conf.submit_token),
            )
            session.add(c)
            for domain in cast(List[str], old_conf.local_blacklist):
                session.add(BlockedDomain(domain=domain))
            for domain in cast(List[str], old_conf.local_whitelist):
                session.add(AllowedDomain(domain=domain))
            await session.commit()
        conf = c

        stmt = select(BlockedDomain.domain)
        local_blocklist = set((await session.execute(stmt)).scalars())
        stmt = select(AllowedDomain.domain)
        local_blocklist = set((await session.execute(stmt)).scalars())
        domains = set(await get_all_domains())

        if unblocked := domains & local_blocklist:
            stmt = delete(BlockedDomain).where(BlockedDomain.domain.in_(unblocked))
            await session.execute(stmt)
            local_blocklist -= unblocked
            await session.commit()

        conf_set.set()


@plugin_config_command
@group("phish")
async def config(ctx: Context) -> None:
    pass


@config.command("api_url")
async def config_api_url(ctx: Context, api_url: Optional[str]) -> None:
    global conf
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        assert c
        if api_url is None:
            await ctx.send("None" if c.api_url is None else format("{!i}", conf.api_url))
        else:
            c.api_url = None if api_url == "None" else api_url
            await session.commit()
            conf = c
            await ctx.send("\u2705")


@config.command("identity")
async def config_identity(ctx: Context, identity: Optional[str]) -> None:
    global conf
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        assert c
        if identity is None:
            await ctx.send("None" if c.identity is None else format("{!i}", conf.identity))
        else:
            c.identity = None if identity == "None" else identity
            await session.commit()
            conf = c
            await ctx.send("\u2705")


@config.command("submit_url")
async def config_submit_url(ctx: Context, submit_url: Optional[str]) -> None:
    global conf
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        assert c
        if submit_url is None:
            await ctx.send("None" if c.submit_url is None else format("{!i}", conf.submit_url))
        else:
            c.submit_url = None if submit_url == "None" else submit_url
            await session.commit()
            conf = c
            await ctx.send("\u2705")


@config.command("submit_token")
async def config_submit_token(ctx: Context, submit_token: Optional[str]) -> None:
    global conf
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        assert c
        if submit_token is None:
            await ctx.send("None" if c.submit_token is None else format("{!i}", conf.submit_token))
        else:
            c.submit_token = None if submit_token == "None" else submit_token
            await session.commit()
            conf = c
            await ctx.send("\u2705")


@config.group("shortener", invoke_without_command=True)
async def config_shortener(ctx: Context) -> None:
    async with sessionmaker() as session:
        stmt = select(ResolvedDomain)
        domains = (await session.execute(stmt)).scalars()
        await ctx.send(", ".join(format("{!i}", domain.domain) for domain in domains) or "No shorteners registered")


@config_shortener.command("add")
async def config_shortener_add(ctx: Context, domain: str) -> None:
    async with sessionmaker() as session:
        session.add(ResolvedDomain(domain=domain))
        await session.commit()
        await ctx.send("\u2705")


@config_shortener.command("remove")
async def config_shortener_remove(ctx: Context, domain: str) -> None:
    async with sessionmaker() as session:
        await session.delete(await session.get(ResolvedDomain, domain))
        await session.commit()
        await ctx.send("\u2705")

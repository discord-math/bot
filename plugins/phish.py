import asyncio
import aiohttp
import json
import re
import logging
from typing import List, Set, Iterable, Protocol, cast
import util.db.kv
import plugins

class PhishConf(Protocol):
    api: str
    identity: str

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
domain_regex: re.Pattern[str]

def update_domain_regex() -> None:
    global domain_regex
    if len(domains) == 0:
        regex = r"(?!)"
    else:
        regex = "".join((r"\bhttps?://(?:", "|".join(re.escape(domain) for domain in domains), ")/"))
    domain_regex = re.compile(regex, re.I)
update_domain_regex()

async def watch_websocket() -> None:
    global domains
    while True:
        try:
            ws = await session.ws_connect(conf.api + "/feed", headers={"X-Identity": conf.identity})
            logger.info("Websocket connected: {!r}".format(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        logger.debug("Got payload: {}".format(msg.data))
                        payload = json.loads(msg.data)
                        if payload["type"] == "add":
                            domains |= set(payload["domains"])
                        elif payload["type"] == "delete":
                            domains -= set(payload["domains"])
                        update_domain_regex()
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

@plugins.init
async def init() -> None:
    global conf, session, domains, ws_task
    conf = cast(PhishConf, await util.db.kv.load(__name__))
    session = aiohttp.ClientSession()
    plugins.finalizer(session.close)
    domains = set(await get_all_domains())
    update_domain_regex()
    ws_task = asyncio.create_task(watch_websocket())
    plugins.finalizer(ws_task.cancel)

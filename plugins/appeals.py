import logging
from typing import TYPE_CHECKING, Any, NoReturn, Optional, Protocol, cast

from aiohttp import (ClientSession, DummyCookieJar, FormData, TraceConfig, TraceRequestEndParams,
    TraceRequestExceptionParams, TraceRequestStartParams)
from aiohttp.web import (Application, AppRunner, HTTPBadRequest, HTTPForbidden, HTTPInternalServerError, HTTPSeeOther,
    HTTPTemporaryRedirect, Request, RouteTableDef, TCPSite)
import aiohttp_session
import discord
from discord import AllowedMentions, ButtonStyle, Interaction, InteractionType, PartialMessage, TextChannel
from discord.abc import Messageable
if TYPE_CHECKING:
    import discord.types.interactions
from discord.ext.commands import Cog
from discord.ui import Button, View
from sqlalchemy import BigInteger, Integer, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema
from yarl import URL

from bot.client import client
from bot.cogs import cog
import plugins
import plugins.tickets
import util.db
import util.db.kv
from util.discord import PlainItem, chunk_messages, format

logger = logging.getLogger(__name__)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class Appeal:
    __tablename__ = "appeals"
    __table_args__ = {"schema": "appeals"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    if TYPE_CHECKING:
        def __init__(self, *, user_id: int, channel_id: int, thread_id: Optional[int], message_id: Optional[int] = ...
            ) -> None:
            ...

    async def get_message(self) -> Optional[PartialMessage]:
        if self.message_id is None:
            return None
        channel_id = self.channel_id if self.thread_id is None else self.thread_id
        try:
            if not isinstance(channel := await client.fetch_channel(channel_id), Messageable):
                return None
        except (discord.NotFound, discord.Forbidden):
            return None
        return channel.get_partial_message(self.message_id)

class AppealsConf(Protocol):
    client_id: int
    client_secret: str
    guild: int
    channel: int
    max_appeals: int

conf: AppealsConf
http: ClientSession
runner: AppRunner

class AppealView(View):
    def __init__(self, appeal_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(Button(style=ButtonStyle.danger, label="Close",
            custom_id="{}:{}:Close".format(__name__, appeal_id)))

AUTHORIZE_URL = URL("https://discord.com/oauth2/authorize")
TOKEN_URL = URL("https://discord.com/api/oauth2/token")
ME_URL = URL("https://discord.com/api/users/@me")

# https://github.com/discord-math/discord-math.github.io/blob/main/_appeals/form.md
APPEAL_FORM_URL = URL("https://mathematics.gg/appeals/form")
# https://github.com/discord-math/discord-math.github.io/blob/main/_appeals/success.md
APPEAL_SUCCESS_URL = URL("https://mathematics.gg/appeals/success")

CALLBACK_URL = URL("https://api.mathematics.gg/appeals/callback")

routes = RouteTableDef()

@routes.get("/auth")
async def get_auth(request: Request) -> NoReturn:
    query = {"client_id": conf.client_id, "response_type": "code", "scope": "identify",
        "redirect_uri": str(CALLBACK_URL)}
    raise HTTPSeeOther(location=AUTHORIZE_URL.with_query(query))

async def get_user_id(token: str) -> int:
    async with http.get(ME_URL, headers={"Authorization": "Bearer {}".format(token)}) as r:
        user = await r.json()

    try:
        return int(user["id"])
    except (KeyError, ValueError):
        raise HTTPInternalServerError(text="No user id")

@routes.get("/callback")
async def get_callback(request: Request) -> NoReturn:
    if "error" in request.query:
        raise HTTPForbidden(text=request.query["error"])
    if "code" not in request.query:
        raise HTTPBadRequest(text="No access code provided")

    body = {"client_id": conf.client_id, "client_secret": conf.client_secret, "grant_type": "authorization_code",
        "code": request.query["code"], "redirect_uri": str(CALLBACK_URL)}
    async with http.post(TOKEN_URL, headers={"Accept": "application/json"}, data=FormData(body)) as r:
        result = await r.json()

    if "error_description" in result:
        raise HTTPForbidden(text=str(result["error_description"]))
    if "error" in result:
        raise HTTPForbidden(text=str(result["error"]))

    if "access_token" not in result:
        raise HTTPInternalServerError(text="No access token")
    token = result["access_token"]

    user_id = await get_user_id(token)

    async with sessionmaker() as session:
        # TODO: TOCTOU
        stmt = select(func.count(Appeal.id)).where(Appeal.user_id == user_id)
        num_appeals = cast(int, (await session.execute(stmt)).scalar())
        if num_appeals >= conf.max_appeals:
            raise HTTPForbidden(text="You already have {} active appeals".format(conf.max_appeals))

        in_guild = False
        in_banlist = False
        if guild := client.get_guild(conf.guild):
            async for entry in guild.bans(after=discord.Object(user_id - 1), limit=1):
                if entry.user.id == user_id:
                    in_banlist = True
            else:
                if guild.get_member(user_id):
                    in_guild = True

    cookie = await aiohttp_session.new_session(request)
    cookie["token"] = token

    if in_guild:
        location = APPEAL_FORM_URL.with_fragment("in_guild")
    elif not in_banlist:
        location = APPEAL_FORM_URL.with_fragment("not_in_banlist")
    else:
        location = APPEAL_FORM_URL
    raise HTTPTemporaryRedirect(location=location)

@routes.post("/submit")
async def post_submit(request: Request) -> NoReturn:
    cookie = await aiohttp_session.get_session(request)
    if "token" not in cookie:
        await get_auth(request)
    token = cookie["token"]

    post_data = await request.post()
    if "type" not in post_data or "appeal" not in post_data:
        raise HTTPBadRequest()
    kind = post_data["type"]
    reason = post_data.get("reason", "")
    appeal = post_data["appeal"]
    if not isinstance(kind, str) or not isinstance(reason, str) or not isinstance(appeal, str):
        raise HTTPBadRequest()
    kind = kind[:11]
    reason = reason[:4000]
    appeal = appeal[:4000]

    user_id = await get_user_id(token)

    async with sessionmaker() as session:
        # TODO: TOCTOU
        stmt = select(func.count(Appeal.id)).where(Appeal.user_id == user_id)
        num_appeals = cast(int, (await session.execute(stmt)).scalar())
        if num_appeals >= conf.max_appeals:
            raise HTTPForbidden(text="You already have {} active appeals".format(conf.max_appeals))

        if not (guild := client.get_guild(conf.guild)):
            raise HTTPInternalServerError()
        if not isinstance(channel := guild.get_channel(conf.channel), TextChannel):
            raise HTTPInternalServerError()

        last_message = None
        for content, _ in chunk_messages([PlainItem(format("**Ban Appeal from** {!m}:\n\n", user_id)),
            PlainItem("**Type:** {}\n".format(kind)), PlainItem("**Reason:** "), PlainItem(reason),
            PlainItem("\n**Appeal:** "), PlainItem(appeal)]):
            last_message = await channel.send(content, allowed_mentions=AllowedMentions.none())
        assert last_message

        thread = await last_message.create_thread(name=str(user_id))

        appeal = Appeal(user_id=user_id, channel_id=channel.id, thread_id=thread.id)
        session.add(appeal)
        await session.commit()

        msg = await thread.send(view=AppealView(appeal.id))
        appeal.message_id = msg.id
        await session.commit()

        embeds = plugins.tickets.summarise_tickets(await plugins.tickets.visible_tickets(session, user_id),
            "Tickets for {}".format(user_id), dm=False)
        if embeds:
            embeds = list(embeds)
            for i in range(0, len(embeds), 10):
                await thread.send(embeds=embeds[i:i + 10])

    raise HTTPSeeOther(location=APPEAL_SUCCESS_URL)

app = Application()
app.add_routes(routes)
aiohttp_session.setup(app, aiohttp_session.SimpleCookieStorage())

async def on_request_start(session: ClientSession, context: Any, params: TraceRequestStartParams) -> None:
    logger.debug("Sending request to {}".format(params.url))

async def on_request_end(session: ClientSession, context: Any, params: TraceRequestEndParams) -> None:
    logger.debug("Request to {} received {}".format(params.url, params.response.status))

async def on_request_exception(session: ClientSession, context: Any, params: TraceRequestExceptionParams) -> None:
    logger.debug("Request to {} received exception".format(params.url), exc_info=params.exception)

@plugins.init
async def init() -> None:
    global conf, http, runner

    conf = cast(AppealsConf, await util.db.kv.load(__name__))

    await util.db.init(util.db.get_ddl(
        CreateSchema("appeals"),
        registry.metadata.create_all))

    trace_config = TraceConfig()
    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_exception.append(on_request_exception)
    http = ClientSession(cookie_jar=DummyCookieJar(), trace_configs=[trace_config])
    plugins.finalizer(http.close)

    runner = AppRunner(app)
    await runner.setup()
    plugins.finalizer(runner.cleanup)
    site = TCPSite(runner, "127.0.0.1", 16720)
    await site.start()

async def close_appeal(interaction: Interaction, appeal_id: int) -> None:
    async with sessionmaker() as session:
        if not (appeal := await session.get(Appeal, appeal_id)):
            return

        await session.delete(appeal)
        if (msg := await appeal.get_message()):
            try:
                await msg.edit(content="Appeal handled (user may create new ones).", view=None)
            except discord.HTTPException:
                pass
        await session.commit()

@cog
class AppealsCog(Cog):
    @Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        if interaction.type != InteractionType.component or interaction.data is None:
            return
        data = cast("discord.types.interactions.MessageComponentInteractionData", interaction.data)
        if data["component_type"] != 2:
            return
        if ":" not in data["custom_id"]:
            return
        mod, rest = data["custom_id"].split(":", 1)
        if mod != __name__ or ":" not in rest:
            return
        appeal_id, action = rest.split(":", 1)
        try:
            appeal_id = int(appeal_id)
        except ValueError:
            return
        if action == "Close":
            await close_appeal(interaction, appeal_id)


"""For displaying paged embeds with reaction-based navigation."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from discord import Embed

from bot.commands import Context
from bot.reactions import ReactionMonitor


logger: logging.Logger = logging.getLogger(__name__)


class PagedEmbeds:
    """
    Display a list of embeds with ◀️/▶️ navigation and ❌ to close.

    Usage:
        pages = [...Embed, ...]
        await PagedEmbeds(ctx, pages).run()
    """

    __slots__ = ("ctx", "pages", "timeout_each")

    EMOJI_LEFT = "\u25c0\ufe0f"
    EMOJI_RIGHT = "\u25b6\ufe0f"
    EMOJI_CANCEL = "\u274c"

    ctx: Context
    pages: Sequence[Embed]
    timeout_each: Optional[float]

    def __init__(self, ctx: Context, pages: Sequence[Embed], timeout_each: Optional[float] = 60) -> None:
        self.ctx = ctx
        self.pages = pages
        self.timeout_each = timeout_each

    async def run(self) -> None:
        if not self.pages:
            return

        msg = await self.ctx.send(embed=self.pages[0])
        logger.debug("PagedEmbeds: started with %d pages", len(self.pages))

        # Add reactions once, keep order stable; only add arrows if multiple pages
        try:
            if len(self.pages) > 1:
                await msg.add_reaction(self.EMOJI_LEFT)
                await msg.add_reaction(self.EMOJI_RIGHT)
            await msg.add_reaction(self.EMOJI_CANCEL)
        except Exception:
            logger.debug("PagedEmbeds: failed to add reactions", exc_info=True)

        allowed = {self.EMOJI_CANCEL} | ({self.EMOJI_LEFT, self.EMOJI_RIGHT} if len(self.pages) > 1 else set())
        current = 0
        reason = "timeout"

        with ReactionMonitor(
            event="add",
            channel_id=msg.channel.id,
            message_id=msg.id,
            author_id=self.ctx.author.id,
            timeout_each=self.timeout_each,
            filter=lambda _, p: getattr(p.emoji, "name", None) in allowed,
        ) as mon:
            while True:
                try:
                    _, payload = await mon
                except asyncio.TimeoutError:
                    reason = "timeout"
                    break

                emoji_name = getattr(payload.emoji, "name", None) or payload.emoji
                if emoji_name == self.EMOJI_CANCEL:
                    reason = "cancel"
                    break
                elif emoji_name == self.EMOJI_LEFT:
                    current = (current - 1) % len(self.pages)
                elif emoji_name == self.EMOJI_RIGHT:
                    current = (current + 1) % len(self.pages)
                else:
                    continue

                try:
                    await msg.edit(embed=self.pages[current])
                except Exception:
                    reason = "edit-failed"
                    break

                try:
                    await msg.remove_reaction(payload.emoji, self.ctx.author)
                except Exception:
                    pass

        # Cleanup
        try:
            await msg.delete()
            await self.ctx.message.delete()
        except Exception:
            logger.error("PagedEmbeds: failed to delete message on %s", reason, exc_info=True)
        else:
            logger.debug("PagedEmbeds: cleaned up message on %s", reason)

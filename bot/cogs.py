from typing import Type, TypeVar

from discord.ext.commands import Cog

from bot.client import client
import plugins

T = TypeVar("T", bound=Cog)

def cog(cls: Type[T]) -> T:
    """Decorator for cog classes that are loaded/unloaded from the bot together with the plugin."""
    cog = cls()
    cog_name = "{}:{}:{}".format(cog.__module__, cog.__cog_name__, hex(id(cog)))
    cog.__cog_name__ = cog_name

    async def initialize_cog() -> None:
        await client.add_cog(cog)
        async def finalize_cog() -> None:
            await client.remove_cog(cog_name)
        plugins.finalizer(finalize_cog)
    plugins.init(initialize_cog)

    return cog

from typing import Type, TypeVar

import discord.ext.commands

import bot.client
import plugins

T = TypeVar("T", bound=discord.ext.commands.Cog)

def cog(cls: Type[T]) -> T:
    cog = cls()
    cog_name = "{}:{}:{}".format(cog.__module__, cog.__cog_name__, hex(id(cog)))
    cog.__cog_name__ = cog_name

    async def initialize_cog() -> None:
        await bot.client.client.add_cog(cog)
        async def finalize_cog() -> None:
            await bot.client.client.remove_cog(cog_name)
        plugins.finalizer(finalize_cog)
    plugins.init(initialize_cog)

    return cog

from typing import Type, TypeVar
import discord.ext.typed_commands
import plugins
import discord_client

T = TypeVar("T", bound=discord.ext.typed_commands.Cog[discord.ext.typed_commands.Context])

def cog(cls: Type[T]) -> T:
    cog = cls()
    cog_name = "{}:{}:{}".format(cog.__module__, cog.__cog_name__, hex(id(cog))) # type: ignore
    cog.__cog_name__ = cog_name # type: ignore

    discord_client.client.add_cog(cog)
    @plugins.finalizer
    def finalize_cog() -> None:
        discord_client.client.remove_cog(cog_name)

    return cog

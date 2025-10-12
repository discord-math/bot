

import abc
from dataclasses import dataclass
import logging
from typing import Any, Literal, Optional

from bot.acl import privileged
from bot.config import plugin_config_command
from discord.ext.commands import group
import discord
from bot.commands import Context, plugin_command
from discord.ext.commands import command
import aiohttp

from sqlalchemy import TEXT
from sqlalchemy.orm import Mapped, mapped_column
import sqlalchemy.orm
from sqlalchemy.ext.asyncio import async_sessionmaker

import plugins
from util.discord import UserError
import util.db

CAT_API_ROOT = 'https://api.thecatapi.com/v1/images/search'
DOG_API_ROOT = 'https://api.thedogapi.com/v1/images/search'
TIMEOUT = 10  # seconds

logger = logging.getLogger(__name__)

cat_api: Optional['AnimalApi'] = None
dog_api: Optional['AnimalApi'] = None

http = aiohttp.ClientSession()
plugins.finalizer(http.close)

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)

@registry.mapped
class GlobalConfig:
    __tablename__ = "config"
    __table_args__ = {"schema": "animals"}

    id: Mapped[int] = mapped_column(primary_key=True, default=0)
    cat_api_key: Mapped[str] = mapped_column(TEXT)
    dog_api_key: Mapped[str] = mapped_column(TEXT)

class AnimalRequest(abc.ABC):
    @abc.abstractmethod
    def to_dict(self) -> dict[str, Any]:
        ''' Converts the request to a dictionary of query parameters '''
        pass
    
@dataclass
class CatRequest(AnimalRequest):
    # https://developers.thecatapi.com/view-account/ylX4blBYT9FaoVd6OhvR

    limit: Optional[int] = None # API defaults to 1
    order: Literal['ASC', 'DESC', 'RAND', None] = None # API defaults to RAND
    #page: Optional[int] = None # only relevant for ASC/DESC search
    has_breeds: Optional[bool] = None # API defaults to all
    breed_ids: Optional[list[str]] = None # API defaults to all
    
    def to_dict(self) -> dict[str, Any]:
        result = {}
        if self.limit is not None:
            result['limit'] = self.limit
        if self.order is not None:
            result['order'] = self.order
        if self.has_breeds is not None:
            result['has_breeds'] = int(self.has_breeds)
        if self.breed_ids is not None:
            result['breed_ids'] = ','.join(self.breed_ids)
        return result

@dataclass
class DogRequest(AnimalRequest):
    # https://docs.thedogapi.com/docs/examples/images

    size: Literal['full', 'med', 'small', 'thumb'] = 'small'
    mime_types: Optional[list[Literal['jpg', 'png', 'gif']]] = None # API defaults to all
    format: Literal['json', 'src'] = 'json'
    order: Literal['ASC', 'DESC', 'RAND', None] = None # API defaults to RAND
    limit: Optional[int] = None # API defaults to 1
    #page: Optional[int] = None # only relevant for ASC/DESC search
    has_breeds: Optional[bool] = None # API defaults to all

    def to_dict(self) -> dict[str, Any]:
        result = {}
        result['size'] = self.size
        if self.mime_types is not None:
            result['mime_types'] = ','.join(self.mime_types)
        result['format'] = self.format
        result['order'] = self.order
        result['limit'] = self.limit
        if self.has_breeds is not None:
            result['has_breeds'] = int(self.has_breeds)
        return {k: v for k, v in result.items() if v is not None}

@dataclass
class AnimalResponse:
    # We could define separate CatResponse and DogResponse classes,
    #  but they're similar enough that it's simpler to just have one.

    id: str
    url: str
    width: int
    height: int
    categories: Optional[list[Any]] = None
    breeds: Optional[list[dict[str, Any]]] = None

    def get_weight(self) -> Optional[str]:
        # The weight comes back in different formats depending on the API and the breed
        if self.breeds:
            breed = self.breeds[0]
            try:
                return breed['weight']['metric'] + ' kg'
            except KeyError:
                pass

            try:
                return breed['weight']['imperial'] + ' lbs'
            except KeyError:
                pass

            try:
                return breed['weight'] + ''
            except KeyError:
                pass
        return None

    def get_life_span(self) -> Optional[str]:
        if self.breeds:
            breed = self.breeds[0]
            try:
                lifespan = breed['life_span']
                if 'years' not in lifespan:
                    lifespan += ' years'
                return lifespan
            except KeyError:
                pass
        return None

    def get_description(self) -> str:
        if self.breeds:
            breed = self.breeds[0]
            out = {}
            out['Name'] = breed.get('name', None)
            out['Temperament'] = breed.get('temperament', None)
            out['Origin'] = breed.get('origin', None)
            out['Description'] = breed.get('description', None)
            out['Weight'] = self.get_weight()
            out['Life span'] = self.get_life_span()
            if len(self.breeds) > 1:
                other_breeds = [b.get('name', None) for b in self.breeds[1:]]
                out['Other breeds'] = ', '.join(b for b in other_breeds if b)
            description = '\n'.join(f'{key}: {value}' for key, value in out.items() if value)
            return description
        else:
            return 'No breed information available.'

class AnimalApi:
    # The actual API structure is identical, just with different parameters and API keys.

    def __init__(self, api_root: str, api_key: str) -> None:
        self.api_root = api_root
        self.api_key = api_key

    async def fetch_random_animal(self, req: Optional[AnimalRequest] = None) -> AnimalResponse:
        params = req.to_dict() if req else {}
        logger.debug(f'Fetching random animal with params: {params}')
        headers = {'x-api-key': self.api_key}

        async with http.get(
            self.api_root,
            params=params,
            headers=headers,
            timeout=TIMEOUT,
        ) as resp:
            data = await resp.json()
            if not data:
                raise UserError('No animal found!')
            return AnimalResponse(**data[0])

def animal_to_embed(animal: AnimalResponse, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        url=animal.url,
        color=discord.Color.random()
    )
    embed.set_image(url=animal.url)
    footer = animal.get_description()
    embed.set_footer(text=footer)
    return embed

@plugin_command
@privileged
@command('cat')
async def random_cat(ctx: Context) -> None:
    ''' Fetches and displays a random cat image '''

    async with ctx.typing():
        if not cat_api:
            raise UserError('Cat API is not configured.')

        cat = await cat_api.fetch_random_animal()
        embed = animal_to_embed(cat, title='Here is your random cat! 🐱')
        await ctx.send(embed=embed)

@plugin_command
@privileged
@command('dog')
async def random_dog(ctx: Context) -> None:
    ''' Fetches and displays a random dog image '''

    async with ctx.typing():
        if not dog_api:
            raise UserError('Dog API is not configured.')
        
        dog = await dog_api.fetch_random_animal()
        embed = animal_to_embed(dog, title='Here is your random dog! 🐶')
        await ctx.send(embed=embed)

@plugins.init
async def init() -> None:
    global cat_api, dog_api
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await session.get(GlobalConfig, 0)
        if conf:
            cat_api = AnimalApi(
                CAT_API_ROOT,
                conf.cat_api_key
            )
            dog_api = AnimalApi(
                DOG_API_ROOT,
                conf.dog_api_key
            )
        else:
            raise UserError('No configuration found for animals plugin.')

@plugin_config_command
@group("animals")
@privileged
async def config(ctx: Context) -> None:
    pass

@config.command("cat_api_key")
async def set_cat_api_key(ctx: Context, api_key: str) -> None:
    global cat_api
    cat_api = AnimalApi(CAT_API_ROOT, api_key)
    async with sessionmaker() as session:
        conf = await session.get(GlobalConfig, 0)
        assert conf
        conf.cat_api_key = api_key
        await session.commit()
    await ctx.send(f'\u2705')

@config.command("dog_api_key")
async def set_dog_api_key(ctx: Context, api_key: str) -> None:
    global dog_api
    dog_api = AnimalApi(DOG_API_ROOT, api_key)
    async with sessionmaker() as session:
        conf = await session.get(GlobalConfig, 0)
        assert conf
        conf.dog_api_key = api_key
        await session.commit()
    await ctx.send(f'\u2705')
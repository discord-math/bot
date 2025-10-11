

import abc
from collections import defaultdict
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
import datetime as dt

from util.discord import UserError

CAT_API_ROOT = 'https://api.thecatapi.com/v1/images/search'
DOG_API_ROOT = 'https://api.thedogapi.com/v1/images/search'
TIMEOUT = 10  # seconds

def with_suffix(obj: dict[str, Any], *path: str, suffix: str) -> Optional[str]:
    val = obj
    try:
        for p in path:
            val = val[p]
    except (KeyError, TypeError):
        return None
    if val is not None:
        return f'{val}{suffix}'
    return None

class RateLimiter(abc.ABC):
    @abc.abstractmethod
    def is_allowed(self, ctx: Context) -> tuple[bool, str]:
        pass

class PerPersonRateLimiter(RateLimiter):
    def __init__(self, calls: int, period: dt.timedelta):
        self.calls = calls
        self.period = period
        self.users: dict[int, list[dt.datetime]] = defaultdict(list)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}<{self.calls=}, {self.period=}>'
    
    def __str__(self) -> str:
        return f'{self.__class__.__name__}<{self.calls} calls per {self.period}>'
    
    def _is_allowed(self, user_id: int) -> tuple[bool, str]:
        now = dt.datetime.now()
        timestamps = self.users[user_id]
        # Remove timestamps outside the period
        while timestamps and now - timestamps[0] > self.period:
            timestamps.pop(0)
        if len(timestamps) < self.calls:
            timestamps.append(now)
            return True, ''
        else:
            next_time = timestamps[0] + self.period
            return False, f'Rate limit exceeded: {self.calls} calls per {self.period}. Please try again <t:{int(next_time.timestamp())}:R>.'

    def is_allowed(self, ctx: Context):
        user_id = ctx.author.id
        return self._is_allowed(user_id)
    
class GlobalRateLimiter(PerPersonRateLimiter):
    def is_allowed(self, ctx: Context):
        return self._is_allowed(0)  # Use a single user ID for global rate limiting

@dataclass
class CatRequest:
    limit: Optional[int] = None
    page: Optional[int] = None
    order: Literal['ASC', 'DESC', 'RAND', None] = None
    has_breeds: Optional[bool] = None
    breed_ids: Optional[list[str]] = None
    #sub_id: Optional[str]

    def __post_init__(self):
        try:
            assert self.limit is None or 1 <= self.limit <= 100
            assert self.page is None or 0 <= self.page
        except AssertionError:
            raise ValueError()
    
    def to_dict(self) -> dict[str, Any]:
        result = {}
        if self.limit is not None:
            result['limit'] = self.limit
        if self.page is not None:
            result['page'] = self.page
        if self.order is not None:
            result['order'] = self.order
        if self.has_breeds is not None:
            result['has_breeds'] = int(self.has_breeds)
        if self.breed_ids is not None:
            result['breed_ids'] = ','.join(self.breed_ids)
        return result

@dataclass
class DogRequest:
    size: Literal['full', 'med', 'small', 'thumb'] = 'small'
    mime_types: Optional[list[Literal['jpg', 'png', 'gif']]] = None
    format: Literal['json', 'src'] = 'json'
    order: Literal['ASC', 'DESC', 'RAND', None] = None
    limit: Optional[int] = None
    page: Optional[int] = None
    has_breeds: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        result = {}
        result['size'] = self.size
        if self.mime_types is not None:
            result['mime_types'] = ','.join(self.mime_types)
        result['format'] = self.format
        result['order'] = self.order
        result['limit'] = self.limit
        result['page'] = self.page
        if self.has_breeds is not None:
            result['has_breeds'] = int(self.has_breeds)
        return {k: v for k, v in result.items() if v is not None}

@dataclass
class AnimalResponse:
    id: str
    url: str
    width: int
    height: int
    categories: Optional[list[Any]] = None
    breeds: Optional[list[dict[str, Any]]] = None

    def get_weight(self) -> Optional[str]:
        if self.breeds:
            breed = self.breeds[0]
            return (
                with_suffix(breed, 'weight', 'metric', suffix=' kg') or
                with_suffix(breed, 'weight', 'imperial', suffix=' lbs') or
                with_suffix(breed, 'weight', suffix='')
            )
        return None

    def get_life_span(self) -> Optional[str]:
        if self.breeds:
            breed = self.breeds[0]
            lifespan = breed.get('life_span', None)
            if not lifespan:
                return None
            if 'years' in lifespan:
                return lifespan
            else:
                return f'{lifespan} years'
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
    def __init__(self, api_root: str, api_key: Optional[str], rate_limiters: list[RateLimiter]) -> None:
        self.api_root = api_root
        self.api_key = api_key
        self.rate_limiters = rate_limiters
    
    def configure(self, api_key: str) -> None:
        self.api_key = api_key
    
    def get_configuration(self) -> str:
        out = ''
        out += f'API Root: {self.api_root}\n'
        out += f'API Key: {self.api_key}\n'
        for limiter in self.rate_limiters:
            out += f'Rate Limiter: {limiter}\n'
        return out

    async def fetch_random_animal(self, ctx: Context, params: dict[str, Any]) -> AnimalResponse:
        logging.debug(f'Fetching random animal with params: {params}')
        if not self.api_key:
            raise UserError('Animal API is not configured.')
        headers = {'x-api-key': self.api_key}
        for limiter in self.rate_limiters:
            allowed, message = limiter.is_allowed(ctx)
            if not allowed:
                raise UserError(message)
        async with aiohttp.ClientSession() as session:
            async with session.get(self.api_root, params=params, headers=headers, timeout=TIMEOUT) as resp:
                data = await resp.json()
                if not data:
                    raise UserError('No animal found!')
                return AnimalResponse(**data[0])

class CatApi(AnimalApi):
    def __init__(self, api_key: Optional[str], rate_limiters: list[RateLimiter]) -> None:
        super().__init__(CAT_API_ROOT, api_key, rate_limiters)

    async def fetch_random_cat(self, ctx: Context, req: CatRequest):
        return await self.fetch_random_animal(ctx, req.to_dict())

# cat api key should be set via the config command
cat_api = CatApi('', [
    # reasonable rate limits, I don't see a good way to configure these dynamically
    PerPersonRateLimiter(calls=3, period=dt.timedelta(minutes=1)),
    PerPersonRateLimiter(calls=20, period=dt.timedelta(hours=1)),
    PerPersonRateLimiter(calls=100, period=dt.timedelta(days=1)),

    GlobalRateLimiter(calls=10, period=dt.timedelta(minutes=1)),
])

class DogApi(AnimalApi):
    def __init__(self, api_key: Optional[str], rate_limiters: list[RateLimiter]) -> None:
        super().__init__(DOG_API_ROOT, api_key, rate_limiters)

    async def fetch_random_dog(self, ctx: Context, req: DogRequest):
        return await self.fetch_random_animal(ctx, req.to_dict())

# dog api key should be set via the config command
dog_api = DogApi('', [
    # reasonable rate limits, I don't see a good way to configure these dynamically
    PerPersonRateLimiter(calls=3, period=dt.timedelta(minutes=1)),
    PerPersonRateLimiter(calls=20, period=dt.timedelta(hours=1)),
    PerPersonRateLimiter(calls=100, period=dt.timedelta(days=1)),

    GlobalRateLimiter(calls=10, period=dt.timedelta(minutes=1)),
])

@plugin_command
@privileged
@command('cat')
async def random_cat(ctx: Context) -> None:
    ''' Fetches and displays a random cat image '''

    async with ctx.typing():
        # Prepare request params
        req = CatRequest()

        cat = await cat_api.fetch_random_cat(ctx, req)
        embed = discord.Embed(
            title='Here is your random cat! 🐱',
            url=cat.url,
            color=discord.Color.random()
        )
        embed.set_image(url=cat.url)
        footer = cat.get_description()
        if footer:
            footer += '\n'
        footer += 'thecatapi.com'
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

@plugin_command
@privileged
@command('dog')
async def random_dog(ctx: Context) -> None:
    ''' Fetches and displays a random dog image '''

    async with ctx.typing():
        # Prepare request params
        req = DogRequest()

        dog = await dog_api.fetch_random_dog(ctx, req)
        embed = discord.Embed(
            title='Here is your random dog! 🐶',
            url=dog.url,
            color=discord.Color.random()
        )
        embed.set_image(url=dog.url)
        footer = dog.get_description()
        if footer:
            footer += '\n'
        footer += 'thedogapi.com'
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

@plugin_config_command
@privileged
@group("animals", invoke_without_command=True)
async def animals_config(ctx: Context, cat_api_key: Optional[str], dog_api_key: Optional[str]) -> None:
    ''' Configures the animals plugin '''
    if cat_api_key:
        cat_api.configure(api_key=cat_api_key)
    if dog_api_key:
        dog_api.configure(api_key=dog_api_key)

    await ctx.send(f'Animals plugin configuration:\n**Cat**\n{cat_api.get_configuration()}\n**Dog**\n{dog_api.get_configuration()}')
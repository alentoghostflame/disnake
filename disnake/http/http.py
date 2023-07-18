# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from datetime import datetime
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Coroutine,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)
from urllib.parse import quote as _uriquote

import aiohttp
import yarl

from .. import core
from .. import __version__

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    T = TypeVar("T")
    BE = TypeVar("BE", bound=BaseException)
    Response = Coroutine[Any, Any, T]


__all__ = (
    "HTTPHandler",
    "Route",
)


async def json_or_text(response: aiohttp.ClientResponse) -> Union[Dict[str, Any], str]:
    text = await response.text(encoding="utf-8")
    try:
        if response.headers["content-type"] == "application/json":
            return core.from_json(text)
    except KeyError:
        # Thanks Cloudflare
        pass

    return text


_DEFAULT_API_VERSION: Literal[10] = 10

_API_VERSION: Literal[9, 10] = _DEFAULT_API_VERSION


class UnsupportedAPIVersion(UserWarning):
    """Warning category raised when changing the API version to an unsupported version."""


def _modify_api_version(version: Literal[9, 10]):
    """Modify the API version used by the HTTP client.

    Additional versions may be added around the time of a Discord API
    version bump to allow temporarily downgrading to an older API version
    or upgrading to a newer version that is not yet supported by the library.

    Changing the API version from the default is not supported and may result in
    unexpected behaviour.
    """
    available_versions = (9, 10)

    if version not in available_versions:
        raise ValueError(f"Only API versions {available_versions} are available.")

    if version != _DEFAULT_API_VERSION:
        warnings.warn(
            "Changing the API version is not supported and may result in unexpected behaviour.",
            category=UnsupportedAPIVersion,
            stacklevel=2,
        )

    global _API_VERSION
    _API_VERSION = version

    Route.BASE = f"https://discord.com/api/v{version}"


def _get_logging_auth(auth: str | None) -> str:
    if auth is None:
        return "None"
    elif len(auth) < 12:  # This shouldn't ever occur, but whatever.
        return "[redacted]"
    else:
        return f"{auth[:12]}[redacted]"


class Route:
    BASE: ClassVar[str] = f"https://discord.com/api/v{_DEFAULT_API_VERSION}"

    def __init__(self, method: str, path: str, **parameters: Any) -> None:
        self.path: str = path
        self.method: str = method
        url = self.BASE + self.path
        if parameters:
            url = url.format_map(
                {k: _uriquote(v) if isinstance(v, str) else v for k, v in parameters.items()}
            )
        self.url: str = url

        # major parameters:
        self.channel_id: Optional[Snowflake] = parameters.get("channel_id")
        self.guild_id: Optional[Snowflake] = parameters.get("guild_id")
        self.webhook_id: Optional[Snowflake] = parameters.get("webhook_id")
        self.webhook_token: Optional[str] = parameters.get("webhook_token")

    @property
    def bucket(self) -> str:
        # the bucket is just method + path w/ major parameters
        return f"{self.channel_id}:{self.guild_id}:{self.path}"


class RateLimitMigrating(core.DiscordException):
    ...


class IncorrectBucket(core.DiscordException):
    ...


class RateLimit:
    """Used to time gate a large batch of requests to only occur X every Y seconds. Used via ``async with``

    NOT THREAD SAFE.

    Parameters
    ----------
    time_offset: :class:`float`
        Number in seconds to increase all timers by. Used for lag compensation.
    """

    def __init__(self, time_offset: float = 0.3) -> None:
        self.limit: int = 1
        """Maximum amount of requests before requests have to wait for the rate limit to reset."""
        self.remaining: int = 1
        """Remaining amount of requests before requests have to wait for the rate limit to reset."""
        self.reset: datetime | None = None
        """Datetime that the bucket roughly will be reset at."""
        self.reset_after: float = 1.0
        """Amount of seconds roughly until the rate limit will be reset."""
        self.bucket: str | None = None
        """Name of the bucket, if it has one."""

        self._time_offset: float = time_offset
        """Number in seconds to increase all timers by. Used for lag compensation."""
        self._first_update: bool = True
        """If the next update to be ran will be the first."""
        self._reset_remaining_task: asyncio.Task | None = None
        """Holds the task object for resetting the remaining count."""
        self._on_reset_event: asyncio.Event = asyncio.Event()
        """Used to indicate when the rate limit is ready to be acquired."""
        self._on_reset_event.set()
        self._deny: bool = False
        """Set to error all acquiring requests with a 404 value error."""
        self._migrating: str | None = None
        """When this RateLimit is being deprecated and acquiring requests need to migrate to a different RateLimit, this
        variable should be set to the different RateLimit/buckets string name.
        """

    @property
    def resetting(self) -> bool:
        return self._reset_remaining_task is not None and not self._reset_remaining_task.done()

    async def update(self, response: aiohttp.ClientResponse) -> None:
        """Updates the rate limit with information from the response."""

        if response.headers.get("X-RateLimit-Global") == "true":
            # The response is intended for the global rate limit, not a regular rate limit.
            return

        # Updates the bucket name. The bucket name not existing as fine, as ``None`` is desired for that.
        # This is done immediately, so we can error out if we get an update not for this bucket.
        x_bucket = response.headers.get("X-RateLimit-Bucket")

        if self.bucket == x_bucket:
            pass  # Don't need to set it again.
        elif self.bucket is None:
            self.bucket = x_bucket
        else:
            raise IncorrectBucket(
                f"Update given for bucket {x_bucket}, but this RateLimit is for bucket {self.bucket}!"
            )

        if response.status == 404:
            self._deny = True

        # Updates the limit if it exists.
        x_limit = response.headers.get("X-RateLimit-Limit")
        self.limit = 1 if x_limit is None else int(x_limit)

        # Updates the remaining left if it exists, being pessimistic.
        x_remaining = response.headers.get("X-RateLimit-Remaining")

        if x_remaining is None:
            self.remaining = 1
        elif self._first_update:
            self.remaining = int(x_remaining)
        else:
            # If requests come back out of order, it's possible that we could get a wrong amount remaining.
            # It's best to be pessimistic and assume it cannot go back up unless the reset task occurs.
            self.remaining = (
                int(x_remaining) if int(x_remaining) < self.remaining else self.remaining
            )

        # Updates the datetime of the reset.
        x_reset = response.headers.get("X-RateLimit-Reset")
        if x_reset is not None:
            self.reset = datetime.utcfromtimestamp(float(x_reset))

        # Updates the reset-after count, being pessimistic.
        x_reset_after = response.headers.get("X-RateLimit-Reset-After")
        if x_reset_after is not None:
            x_reset_after = float(x_reset_after) + self._time_offset
            if self.reset_after is None:
                self.reset_after = x_reset_after
            else:
                if self.reset_after < x_reset_after:
                    _log.debug(
                        "Bucket %s: Reset after time increased, adapting reset time.", self.bucket
                    )
                    self.reset_after = x_reset_after
                    self.start_reset_task()

        if not self.resetting:
            self.start_reset_task()

        # If for whatever reason we have requests remaining but the reset event isn't set, set it.
        if 0 < self.remaining and not self._on_reset_event.is_set():
            _log.debug(
                "Bucket %s: Updated with remaining %s, setting reset event.",
                self.bucket,
                self.remaining,
            )
            self._on_reset_event.set()

        # If this is our first update, indicate that all future updates aren't the first.
        if self._first_update:
            self._first_update = False

        _log.debug(
            "Bucket %s: Updated with limit %s, remaining %s, reset %s, and reset_after %s seconds.",
            self.bucket,
            self.limit,
            self.remaining,
            self.reset,
            self.reset_after,
        )

    def start_reset_task(self) -> None:
        """Starts the reset task, non-blocking."""
        if self.resetting:
            _log.debug("Bucket %s: Reset task already running, cancelling.", self.bucket)
            self._reset_remaining_task.cancel()  # pyright: ignore [reportOptionalMemberAccess]

        loop = asyncio.get_running_loop()
        _log.debug("Bucket %s: Resetting after %s seconds.", self.bucket, self.reset_after)
        self._reset_remaining_task = loop.create_task(self.reset_remaining(self.reset_after))

    async def reset_remaining(self, time: float) -> None:
        """|coro|
        Sleeps for the specified amount of time, then resets the remaining request count to the limit.

        Parameters
        ----------
        time: :class:`float`
            Amount of time to sleep until the request count is reset to the limit. ``time_offset`` is not added to
            this number.
        """
        await asyncio.sleep(time)
        self.remaining = self.limit
        self._on_reset_event.set()
        _log.debug("Bucket %s: Reset, allowing requests to continue.", self.bucket)

    @property
    def migrating(self) -> str | None:
        """If not ``None``, this indicates what bucket acquiring requests should migrate to."""
        return self._migrating

    def migrate_to(self, bucket: str) -> None:
        """Signals to acquiring requests, both present and future, that they need to migrate to a new bucket."""
        self._migrating = bucket
        self.remaining = self.limit
        self._on_reset_event.set()
        _log.debug(
            "Bucket %s: Deprecating, acquiring requests will migrate to a new bucket.", bucket
        )

    async def __aenter__(self) -> None:
        await self.acquire()
        return None

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.release()

    def locked(self) -> bool:
        return self.remaining <= 0

    async def acquire(self) -> bool:
        # If no more requests can be made but the event is set, clear it.
        if self.remaining <= 0 and self._on_reset_event.is_set():
            _log.debug(
                "Bucket %s: Hit the remaining request limit of %s, locking until reset.",
                self.bucket,
                self.limit,
            )
            self._on_reset_event.clear()
            if not self.resetting:
                self.start_reset_task()

        # Waits in a loop for the event to be set, clearing the event as needed and looping.
        while not self._on_reset_event.is_set():
            _log.debug("Bucket %s: Not set yet, waiting for it to be set.", self.bucket)
            await self._on_reset_event.wait()

            if self.remaining <= 0 and self._on_reset_event.is_set():
                _log.debug(
                    "Bucket %s: Hit the remaining limit of %s, locking until reset.",
                    self.bucket,
                    self.limit,
                )
                self._on_reset_event.clear()
                if not self.resetting:
                    self.start_reset_task()

        if self.migrating:
            raise RateLimitMigrating(
                f"This RateLimit is deprecated, you need to migrate to bucket {self.migrating}"
            )
        elif self._deny:
            raise ValueError("This request path 404'd and is now denied.")

        _log.debug("Bucket %s: Continuing with request.", self.bucket)
        self.remaining -= 1
        return True

    def release(self) -> None:
        # Basically a placeholder, could probably be removed ;)
        pass


class GlobalRateLimit(RateLimit):
    """
    Represents the global rate limit, and thus has to have slightly modified behavior.

    Still not thread safe.
    """

    async def acquire(self) -> bool:
        ret = await super().acquire()
        # As updates are little weird, it's best to start the reset task as soon as the first request has acquired.
        if not self.resetting:
            self.start_reset_task()

        return ret

    async def update(self, response: aiohttp.ClientResponse) -> None:
        if response.headers.get("X-RateLimit-Global") != "true":
            # The response is intended for the regular rate limit, not a global rate limit.
            return

        if response.status == 429:
            # Oh dear, we hit the rate limit.
            _log.warning("Global rate limit 429 encountered, setting remaining to 0.")
            self.remaining = 0
            if response.headers.get("X-RateLimit-Scope") == "global":
                data = await response.json()
                _log.warning(data)
                if (retry_after := data.get("retry_after")) or (
                    retry_after := response.headers.get("Retry-After")
                ):
                    _log.debug(
                        "Got global retry_after, resetting global after %s seconds", retry_after
                    )
                    self.reset_after = float(retry_after) + self._time_offset
                    if self.resetting:
                        self._reset_remaining_task.cancel()  # pyright: ignore [reportOptionalMemberAccess]

                    self.start_reset_task()

            self._on_reset_event.clear()
            if not self.resetting:
                self.start_reset_task()

            _log.warning("Cleared global ratelimit, waiting for reset.")


# For some reason, the Discord voice websocket expects this header to be
# completely lowercase while aiohttp respects spec and does it as case-insensitive
aiohttp.hdrs.WEBSOCKET = "websocket"  # type: ignore


class HTTPHandler:
    """Represents an HTTP handler for sending HTTP requests to the Discord API.

    Also, not thread safe.

    Parameters
    ----------
    connector
    default_max_per_second: :class:`int`
        Maximum amount of requests per second per authorization.

        Discord by default only allows 50 requests per second, but if your bot has had its maximum increased, then
        increase this parameter.
    time_offset: :class:`float`
        Amount of seconds added to all ratelimit timers for lag compensation.

        Due to latency and Discord servers not perfectly time synced, having no offset can cause 429's to occur even
        with us following the reported X-RateLimit-Reset-After.

        Increasing will protect from erroneous 429s but will slow bucket resets, lowering max theoretical speed.

        Decreasing will hasten bucket resets and increase max theoretical speed but may cause 429s.
    default_auth: Optional[:class:`str`]
        Default string to use in the Authorization header if it's not manually provided.
    proxy
    proxy_auth
    loop
    dispatch
    """

    def __init__(
        self,
        connector: Optional[aiohttp.BaseConnector] = None,
        *,
        default_max_per_second: int = 50,
        time_offset: float = 0.0,
        default_auth: Optional[str] = None,
        proxy: Optional[str] = None,
        proxy_auth: Optional[aiohttp.BasicAuth] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        dispatch: Callable,
    ) -> None:
        # TODO: Think about adding ratelimit_multiplier? Would reduce the internal RateLimit.limit by that
        #  float (such as 0.7) and could allow people to run multiple NC bots/processes on the same token while avoiding
        #  ratelimit issues. Could also help with replit-style scenarios.
        self.__session: aiohttp.ClientSession = core.MISSING  # Filled on-demand in request.
        self._connector = connector
        self._default_max_per_second = default_max_per_second
        """Maximum amount of requests per second per authorization."""
        self._time_offset = time_offset
        """Amount of seconds added to all ratelimit timers for lag compensation."""
        self._default_auth = None
        # For consistency with possible future changes to set_default_auth.
        self._set_default_auth(default_auth)
        self._proxy = proxy
        self._proxy_auth: Optional[aiohttp.BasicAuth] = proxy_auth
        # loop is truthy by default it seems, so this works.
        self._loop: asyncio.AbstractEventLoop = loop or asyncio.get_event_loop()
        self._dispatch = dispatch

        user_agent = "DiscordBot (https://github.com/nextcord/nextcord/ {0}) Python/{1[0]}.{1[1]} aiohttp/{2}"
        self._user_agent: str = user_agent.format(
            __version__, sys.version_info, aiohttp.__version__
        )

        self._buckets: dict[str, RateLimit] = {}
        """{"Discord bucket name": RateLimit}"""
        self._global_rate_limits: dict[str | None, RateLimit] = {}
        """{"Auth string": RateLimit}, None for auth-less ratelimit."""
        self._url_rate_limits: dict[tuple[str, str, str | None], RateLimit] = {}
        """{("METHOD", "Route.bucket", "auth string"): RateLimit} auth string may be None to indicate auth-less."""

    def _make_global_rate_limit(self, auth: str | None, max_per_second: int) -> GlobalRateLimit:
        _log.debug(
            "Creating global ratelimit for auth %s with max per second %s.",
            _get_logging_auth(auth),
            max_per_second,
        )
        rate_limit = GlobalRateLimit(time_offset=self._time_offset)
        rate_limit.limit = max_per_second
        rate_limit.remaining = max_per_second
        rate_limit.reset_after = 1 + self._time_offset
        rate_limit.bucket = f"Global {_get_logging_auth(auth) if auth else 'Unauthorized'}"

        self._global_rate_limits[auth] = rate_limit
        return rate_limit

    def _make_url_rate_limit(self, method: str, route: Route, auth: str | None) -> RateLimit:
        _log.debug(
            "Making URL rate limit for %s %s %s", method, route.bucket, _get_logging_auth(auth)
        )
        ret = RateLimit(time_offset=self._time_offset)
        self._url_rate_limits[(method, route.bucket, auth)] = ret
        return ret

    def _set_url_rate_limit(
        self, method: str, route: Route, auth: str | None, rate_limit: RateLimit
    ) -> None:
        self._url_rate_limits[(method, route.bucket, auth)] = rate_limit

    def _get_url_rate_limit(self, method: str, route: Route, auth: str | None) -> RateLimit | None:
        return self._url_rate_limits.get((method, route.bucket, auth), None)

    def _set_default_auth(self, auth: str | None) -> None:
        self._default_auth = auth

    def _make_headers(
        self, original_headers: dict[str, str], *, auth: str | None = None
    ) -> dict[str, str]:
        ret = original_headers.copy()
        if "Authorization" not in ret and self._default_auth:
            ret["Authorization"] = self._default_auth if auth is None else auth

        if "User-Agent" not in ret and self._user_agent:
            ret["User-Agent"] = self._user_agent

        return ret

    # def recreate(self) -> None:
    #     if self.__session.closed:
    #         self.__session = aiohttp.ClientSession(
    #             connector=self._connector, ws_response_class=DiscordClientWebSocketResponse
    #         )

    # async def ws_connect(self, url: str, *, compress: int = 0) -> Any:
    #     kwargs = {
    #         "proxy_auth": self._proxy_auth,
    #         "proxy": self._proxy,
    #         "max_msg_size": 0,
    #         "timeout": 30.0,
    #         "autoclose": False,
    #         "headers": {
    #             "User-Agent": self._user_agent,
    #         },
    #         "compress": compress,
    #     }
    #
    #     return await self.__session.ws_connect(url, **kwargs)

    async def request(
        self,
        route: Route,
        *,
        files: Optional[Sequence[File]] = None,
        form: Optional[Iterable[Dict[str, Any]]] = None,
        auth: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        if not self.__session or self.__session.closed:
            # TODO: Do we even use a websocket response here? is that JUST for the gateway stuff?
            # self.__session = aiohttp.ClientSession(
            #     connector=self._connector, ws_response_class=DiscordClientWebSocketResponse
            # )
            self.__session = aiohttp.ClientSession()

        headers = self._make_headers(kwargs.pop("headers", {}), auth=auth)

        try:
            reason = kwargs.pop("reason")
        except KeyError:
            pass
        else:
            if reason:
                headers["X-Audit-Log-Reason"] = _uriquote(reason, safe="/ ")

        auth = headers.get("Authorization")

        # If a global rate limit for this authorization doesn't exist yet, make it.
        if (global_rate_limit := self._global_rate_limits.get(auth)) is None:
            global_rate_limit = self._make_global_rate_limit(auth, self._default_max_per_second)

        global_rate_limit = cast(GlobalRateLimit, global_rate_limit)

        # If a rate limit for this url path doesn't exist yet, make it.
        if (url_rate_limit := self._get_url_rate_limit(route.method, route, auth)) is None:
            url_rate_limit = self._make_url_rate_limit(route.method, route, auth)

        max_retry_count = 5
        rate_limit_path = (
            route.method,
            route.bucket,
            _get_logging_auth(auth),
        )  # Only use this for logging.
        ret: Any | None = None
        response: aiohttp.ClientResponse | None = None

        # The loop is to allow migration to a different RateLimit if needed.
        # If we hit this loop max_retry_count times, something is wrong. Either we're migrating buckets way
        #  too much, 429s keep getting hit, or something is internally wrong.
        for retry_count in range(max_retry_count):  # To prevent infinite loops.
            should_retry = False
            try:
                async with global_rate_limit:
                    async with url_rate_limit:
                        # This check is for asyncio.gather()'d requests where the rate limit can change.
                        if (
                            temp := self._get_url_rate_limit(route.method, route, auth)
                        ) is not url_rate_limit and not None:
                            temp = cast(RateLimit, temp)
                            _log.debug(
                                "Route %s had the rate limit changed, resetting and retrying.",
                                rate_limit_path,
                            )
                            url_rate_limit = temp
                            continue

                        if files:
                            for f in files:
                                f.reset(seek=retry_count)

                        if form:
                            form_data = aiohttp.FormData(quote_fields=False)
                            for params in form:
                                form_data.add_field(**params)
                            kwargs["data"] = form_data

                        async with self.__session.request(
                            method=route.method,
                            url=route.url,
                            headers=headers,
                            proxy=self._proxy,
                            proxy_auth=self._proxy_auth,
                            **kwargs,
                        ) as response:
                            _log.debug(
                                "%s %s with %s has returned %s",
                                route.method,
                                route.url,
                                kwargs.get("data"),
                                response.status,
                            )

                            await global_rate_limit.update(response)
                            try:
                                await url_rate_limit.update(response)
                            except IncorrectBucket as e:
                                # This condition can be met when doing asyncio.gather()'d requests.
                                if (
                                    temp := self._buckets.get(
                                        # The empty string default makes pyright happy. (hopefully)
                                        response.headers.get("X-RateLimit-Bucket", "")
                                    )
                                ) is not None:
                                    _log.debug(
                                        "Route %s was given a different bucket, found it.",
                                        rate_limit_path,
                                    )
                                    url_rate_limit = temp
                                    self._set_url_rate_limit(
                                        route.method, route, auth, url_rate_limit
                                    )
                                    await url_rate_limit.update(response)
                                else:
                                    _log.debug(
                                        "Route %s was given a different bucket, making a new one: %s",
                                        rate_limit_path,
                                        e,
                                    )
                                    url_rate_limit = self._make_url_rate_limit(
                                        route.method, route, auth
                                    )
                                    await url_rate_limit.update(response)

                            if url_rate_limit.bucket is not None and self._buckets.get(
                                url_rate_limit.bucket
                            ) not in (url_rate_limit, None):
                                # If the current RateLimit bucket name exists, but the stored RateLimit is not the
                                #  current RateLimit, finish up and signal that the current bucket should be migrated
                                #  to the stored one.
                                _log.debug(
                                    "Route %s with bucket %s already exists, migrating other possible requests to "
                                    "that bucket.",
                                    rate_limit_path,
                                    url_rate_limit.bucket,
                                )
                                correct_rate_limit = self._buckets[url_rate_limit.bucket]
                                self._set_url_rate_limit(
                                    route.method, route, auth, correct_rate_limit
                                )
                                if correct_rate_limit.bucket:
                                    # Signals to all requests waiting to acquire to migrate.
                                    url_rate_limit.migrate_to(correct_rate_limit.bucket)
                                else:
                                    raise ValueError(
                                        f"Migrating to bucket {correct_rate_limit.bucket}, but "
                                        f"correct_rate_limit.bucket is falsey. This is likely an internal Nextcord "
                                        f"issue and should be reported."
                                    )
                                # Update the correct RateLimit object with our findings.
                                await correct_rate_limit.update(response)
                            elif url_rate_limit.bucket is not None:
                                self._buckets[url_rate_limit.bucket] = url_rate_limit

                            # even errors have text involved in them so this is safe to call
                            ret = await json_or_text(response)

                            if response.status >= 400:
                                # >= 500 was considered, but stuff like 501 and 505+ are not good to retry on.
                                if response.status in {500, 502, 504}:
                                    _log.info(
                                        "Path %s encountered a Discord server issue, retrying.",
                                        rate_limit_path,
                                    )
                                    await asyncio.sleep(1 + retry_count * 2)
                                    should_retry = True
                                elif response.status == 401:
                                    _log.warning(
                                        "Path %s resulted in error 401, rejected authorization?",
                                        rate_limit_path,
                                    )
                                    raise core.Unauthorized(response, ret)
                                elif response.status == 403:
                                    _log.warning(
                                        "Path %s resulted in error 403, check your permissions?",
                                        rate_limit_path,
                                    )
                                    raise core.Forbidden(response, ret)
                                elif response.status == 404:
                                    _log.warning(
                                        "Path %s resulted in error 404, check your path?",
                                        rate_limit_path,
                                    )
                                    raise core.NotFound(response, ret)
                                elif response.status == 429:
                                    _log.warning(
                                        "Path %s resulted in error 429, rate limit exceeded. Retrying.",
                                        rate_limit_path,
                                    )
                                    self._dispatch(
                                        "http_ratelimit",
                                        url_rate_limit.limit,
                                        url_rate_limit.remaining,
                                        url_rate_limit.reset_after,
                                        url_rate_limit.bucket,
                                        response.headers.get("X-RateLimit-Scope"),
                                    )
                                    should_retry = True
                                elif response.status >= 500:
                                    raise core.DiscordServerError(response, ret)
                                else:
                                    raise core.HTTPException(response, ret)

            # This is handling exceptions from the request
            except OSError as e:
                # Connection reset by peer
                if retry_count < max_retry_count - 1 and e.errno in (54, 10054):
                    await asyncio.sleep(1 + retry_count * 2)
                    continue

                raise

            except RateLimitMigrating:
                if url_rate_limit.migrating is None:
                    raise ValueError(
                        "RateLimitMigrating raised, but RateLimit.migrating is None. This is an internal Nextcord "
                        "error and should be reported!"
                    )
                else:
                    url_rate_limit = self._buckets.get(url_rate_limit.migrating)
                    if url_rate_limit is None:
                        # This means we have an internal issue that we need to fix.
                        raise ValueError(
                            "RateLimit said to migrate, but the RateLimit to migrate was not found? This is an "
                            "internal Nextcord error and should be reported!"
                        )

            else:
                if not should_retry:
                    break

            if retry_count >= max_retry_count - 1:
                _log.error(
                    "Hit retry %s/%s on %s, either something is wrong with Discord or Nextcord.",
                    retry_count + 1,
                    max_retry_count,
                    rate_limit_path,
                )
                if response is not None:
                    if response.status >= 500:
                        raise core.DiscordServerError(response, ret)

                    raise core.HTTPException(response, ret)

        return ret

    async def get_from_cdn(self, url: str) -> bytes:
        async with self.__session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
            elif resp.status == 404:
                raise core.NotFound(resp, "asset not found")
            elif resp.status == 403:
                raise core.Forbidden(resp, "cannot retrieve asset")
            else:
                raise core.HTTPException(resp, "failed to get asset")

    # state management

    async def close(self) -> None:
        if self.__session:
            await self.__session.close()

    # # login management
    #
    # async def static_login(self, auth: str) -> user.User:
    #     # TODO: Change this? This is literally just fetching /users/@me AKA "Get Current User", and is totally
    #     #  usable with OAuth2. This doesn't actually have anything to do with logging in.
    #     self._set_default_auth(auth)
    #
    #     try:
    #         data = await self.request(Route("GET", "/users/@me"))
    #     except HTTPException as exc:
    #         if exc.status == 401:
    #             raise LoginFailure("Improper token has been passed.") from exc
    #         raise
    #
    #     return data
    #
    # async def exchange_access_code(
    #     self, *, client_id: int, client_secret: str, code: str, redirect_uri: str
    # ):
    #     # TODO: Look into how viable this function is here.
    #     # This doesn't actually have hard ratelimits it seems? Not in the headers at least. The default bucket should
    #     #  keep it at 1 every 1 second.
    #     data = {
    #         "client_id": client_id,
    #         "client_secret": client_secret,
    #         "grant_type": "authorization_code",
    #         "code": code,
    #         "redirect_uri": redirect_uri,
    #     }
    #     return await self.request(Route("POST", "/oauth2/token"), data=data)
    #
    # async def get_current_user(self, *, auth: str | None = None):
    #     return await self.request(Route("GET", "/users/@me"), auth=auth)
    #
    # def logout(self) -> Response[None]:
    #     # TODO: Is this only for user bots? Can we get rid of it?
    #     return self.request(Route("POST", "/auth/logout"))
    #
    # # Group functionality
    #
    # def start_group(
    #     self, user_id: Snowflake, recipients: List[int]
    # ) -> Response[channel.GroupDMChannel]:
    #     payload = {
    #         "recipients": recipients,
    #     }
    #
    #     return self.request(
    #         Route("POST", "/users/{user_id}/channels", user_id=user_id), json=payload
    #     )
    #
    # def leave_group(self, channel_id) -> Response[None]:
    #     return self.request(Route("DELETE", "/channels/{channel_id}", channel_id=channel_id))
    #
    # # Message management
    #
    # def start_private_message(self, user_id: Snowflake) -> Response[channel.DMChannel]:
    #     payload = {
    #         "recipient_id": user_id,
    #     }
    #
    #     return self.request(Route("POST", "/users/@me/channels"), json=payload)
    #
    # def get_message_payload(
    #     self,
    #     content: Optional[str],
    #     *,
    #     tts: bool = False,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[str, int]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     message_reference: Optional[message.MessageReference] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     flags: Optional[int] = None,
    # ) -> Dict[str, Any]:
    #     payload: Dict[str, Any] = {
    #         "tts": tts,
    #     }
    #
    #     if content is not None:
    #         payload["content"] = content
    #
    #     if embed is not None:
    #         payload["embeds"] = [embed]
    #
    #     if embeds is not None:
    #         payload["embeds"] = embeds
    #
    #     if nonce is not None:
    #         payload["nonce"] = nonce
    #
    #     if allowed_mentions is not None:
    #         payload["allowed_mentions"] = allowed_mentions
    #
    #     if message_reference is not None:
    #         payload["message_reference"] = message_reference
    #
    #     if components is not None:
    #         payload["components"] = components
    #
    #     if stickers is not None:
    #         payload["sticker_ids"] = stickers
    #
    #     if flags is not None:
    #         payload["flags"] = flags
    #
    #     return payload
    #
    # def send_message(
    #     self,
    #     channel_id: Snowflake,
    #     content: Optional[str],
    #     *,
    #     tts: bool = False,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[int, str]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     message_reference: Optional[message.MessageReference] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     flags: Optional[int] = None,
    # ) -> Response[message.Message]:
    #     r = Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)
    #     payload = self.get_message_payload(
    #         content,
    #         tts=tts,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         message_reference=message_reference,
    #         stickers=stickers,
    #         components=components,
    #         flags=flags,
    #     )
    #
    #     return self.request(r, json=payload)
    #
    # def send_typing(self, channel_id: Snowflake) -> Response[None]:
    #     return self.request(Route("POST", "/channels/{channel_id}/typing", channel_id=channel_id))
    #
    # def get_message_multipart_form(
    #     self,
    #     payload: Dict[str, Any],
    #     message_key: Optional[str] = None,
    #     *,
    #     files: Sequence[File],
    #     content: Optional[str] = None,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[str, int]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     message_reference: Optional[message.MessageReference] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     attachments: Optional[List[Dict[str, Any]]] = None,
    #     flags: Optional[int] = None,
    # ) -> List[Dict[str, Any]]:
    #     form: List[Dict[str, Any]] = []
    #
    #     payload["attachments"] = attachments or []
    #
    #     msg_payload = self.get_message_payload(
    #         content,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         message_reference=message_reference,
    #         stickers=stickers,
    #         components=components,
    #         flags=flags,
    #     )
    #
    #     if message_key is not None:
    #         payload[message_key] = msg_payload
    #     else:
    #         payload.update(msg_payload)
    #
    #     for index, file in enumerate(files):
    #         payload["attachments"].append(
    #             {
    #                 "id": index,
    #                 "filename": file.filename,
    #                 "description": file.description,
    #             }
    #         )
    #         form.append(
    #             {
    #                 "name": f"files[{index}]",
    #                 "value": file.fp,
    #                 "filename": file.filename,
    #                 "content_type": "application/octet-stream",
    #             }
    #         )
    #     form.append({"name": "payload_json", "value": utils.to_json(payload)})
    #
    #     return form
    #
    # def send_multipart_helper(
    #     self,
    #     route: Route,
    #     *,
    #     files: Sequence[File],
    #     content: Optional[str] = None,
    #     tts: bool = False,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[str, int]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     message_reference: Optional[message.MessageReference] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     attachments: Optional[List[Dict[str, Any]]] = None,
    #     flags: Optional[int] = None,
    # ) -> Response[message.Message]:
    #     payload: Dict[str, Any] = {
    #         "tts": tts,
    #         "attachments": attachments or [],
    #     }
    #     form = self.get_message_multipart_form(
    #         payload=payload,
    #         files=files,
    #         content=content,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         message_reference=message_reference,
    #         stickers=stickers,
    #         components=components,
    #         attachments=attachments,
    #         flags=flags,
    #     )
    #     return self.request(route, form=form, files=files)
    #
    # def send_files(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     files: Sequence[File],
    #     content: Optional[str] = None,
    #     tts: bool = False,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[int, str]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     message_reference: Optional[message.MessageReference] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     flags: Optional[int] = None,
    # ) -> Response[message.Message]:
    #     r = Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)
    #     return self.send_multipart_helper(
    #         r,
    #         files=files,
    #         content=content,
    #         tts=tts,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         message_reference=message_reference,
    #         stickers=stickers,
    #         components=components,
    #         flags=flags,
    #     )
    #
    # def delete_message(
    #     self, channel_id: Snowflake, message_id: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/messages/{message_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def delete_messages(
    #     self, channel_id: Snowflake, message_ids: SnowflakeList, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route("POST", "/channels/{channel_id}/messages/bulk-delete", channel_id=channel_id)
    #     payload = {
    #         "messages": message_ids,
    #     }
    #
    #     return self.request(r, json=payload, reason=reason)
    #
    # def edit_message(
    #     self, channel_id: Snowflake, message_id: Snowflake, **fields: Any
    # ) -> Response[message.Message]:
    #     r = Route(
    #         "PATCH",
    #         "/channels/{channel_id}/messages/{message_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     if "files" in fields:
    #         return self.send_multipart_helper(r, **fields)
    #     return self.request(r, json=fields)
    #
    # def add_reaction(
    #     self, channel_id: Snowflake, message_id: Snowflake, emoji: str
    # ) -> Response[None]:
    #     r = Route(
    #         "PUT",
    #         "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #         emoji=emoji,
    #     )
    #     return self.request(r)
    #
    # def remove_reaction(
    #     self, channel_id: Snowflake, message_id: Snowflake, emoji: str, member_id: Snowflake
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{member_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #         member_id=member_id,
    #         emoji=emoji,
    #     )
    #     return self.request(r)
    #
    # def remove_own_reaction(
    #     self, channel_id: Snowflake, message_id: Snowflake, emoji: str
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #         emoji=emoji,
    #     )
    #     return self.request(r)
    #
    # def get_reaction_users(
    #     self,
    #     channel_id: Snowflake,
    #     message_id: Snowflake,
    #     emoji: str,
    #     limit: int,
    #     after: Optional[Snowflake] = None,
    # ) -> Response[List[user.User]]:
    #     r = Route(
    #         "GET",
    #         "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #         emoji=emoji,
    #     )
    #
    #     params: Dict[str, Any] = {
    #         "limit": limit,
    #     }
    #     if after:
    #         params["after"] = after
    #     return self.request(r, params=params)
    #
    # def clear_reactions(self, channel_id: Snowflake, message_id: Snowflake) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/messages/{message_id}/reactions",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #
    #     return self.request(r)
    #
    # def clear_single_reaction(
    #     self, channel_id: Snowflake, message_id: Snowflake, emoji: str
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #         emoji=emoji,
    #     )
    #     return self.request(r)
    #
    # def get_message(
    #     self, channel_id: Snowflake, message_id: Snowflake
    # ) -> Response[message.Message]:
    #     r = Route(
    #         "GET",
    #         "/channels/{channel_id}/messages/{message_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     return self.request(r)
    #
    # def get_channel(self, channel_id: Snowflake) -> Response[channel.Channel]:
    #     r = Route("GET", "/channels/{channel_id}", channel_id=channel_id)
    #     return self.request(r)
    #
    # def logs_from(
    #     self,
    #     channel_id: Snowflake,
    #     limit: int,
    #     before: Optional[Snowflake] = None,
    #     after: Optional[Snowflake] = None,
    #     around: Optional[Snowflake] = None,
    # ) -> Response[List[message.Message]]:
    #     params: Dict[str, Any] = {
    #         "limit": limit,
    #     }
    #
    #     if before is not None:
    #         params["before"] = before
    #     if after is not None:
    #         params["after"] = after
    #     if around is not None:
    #         params["around"] = around
    #
    #     return self.request(
    #         Route("GET", "/channels/{channel_id}/messages", channel_id=channel_id), params=params
    #     )
    #
    # def publish_message(
    #     self, channel_id: Snowflake, message_id: Snowflake
    # ) -> Response[message.Message]:
    #     return self.request(
    #         Route(
    #             "POST",
    #             "/channels/{channel_id}/messages/{message_id}/crosspost",
    #             channel_id=channel_id,
    #             message_id=message_id,
    #         )
    #     )
    #
    # def pin_message(
    #     self, channel_id: Snowflake, message_id: Snowflake, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "PUT",
    #         "/channels/{channel_id}/pins/{message_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def unpin_message(
    #     self, channel_id: Snowflake, message_id: Snowflake, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/pins/{message_id}",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def pins_from(self, channel_id: Snowflake) -> Response[List[message.Message]]:
    #     return self.request(Route("GET", "/channels/{channel_id}/pins", channel_id=channel_id))
    #
    # # Member management
    #
    # def kick(
    #     self, user_id: Snowflake, guild_id: Snowflake, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id
    #     )
    #     return self.request(r, reason=reason)
    #
    # def ban(
    #     self,
    #     user_id: Snowflake,
    #     guild_id: Snowflake,
    #     delete_message_seconds: int = 86400,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route("PUT", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id)
    #     params = {
    #         "delete_message_seconds": delete_message_seconds,
    #     }
    #
    #     return self.request(r, params=params, reason=reason)
    #
    # def unban(
    #     self, user_id: Snowflake, guild_id: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route("DELETE", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id)
    #     return self.request(r, reason=reason)
    #
    # def guild_voice_state(
    #     self,
    #     user_id: Snowflake,
    #     guild_id: Snowflake,
    #     *,
    #     mute: Optional[bool] = None,
    #     deafen: Optional[bool] = None,
    #     reason: Optional[str] = None,
    # ) -> Response[member.Member]:
    #     r = Route(
    #         "PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id
    #     )
    #     payload: Dict[str, bool] = {}
    #     if mute is not None:
    #         payload["mute"] = mute
    #
    #     if deafen is not None:
    #         payload["deaf"] = deafen
    #
    #     return self.request(r, json=payload, reason=reason)
    #
    # def edit_profile(self, payload: Dict[str, Any]) -> Response[user.User]:
    #     return self.request(Route("PATCH", "/users/@me"), json=payload)
    #
    # def change_my_nickname(
    #     self,
    #     guild_id: Snowflake,
    #     nickname: str,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[member.Nickname]:
    #     r = Route("PATCH", "/guilds/{guild_id}/members/@me/nick", guild_id=guild_id)
    #     payload = {
    #         "nick": nickname,
    #     }
    #     return self.request(r, json=payload, reason=reason)
    #
    # def change_nickname(
    #     self,
    #     guild_id: Snowflake,
    #     user_id: Snowflake,
    #     nickname: str,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[member.Member]:
    #     r = Route(
    #         "PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id
    #     )
    #     payload = {
    #         "nick": nickname,
    #     }
    #     return self.request(r, json=payload, reason=reason)
    #
    # def edit_my_voice_state(self, guild_id: Snowflake, payload: Dict[str, Any]) -> Response[None]:
    #     r = Route("PATCH", "/guilds/{guild_id}/voice-states/@me", guild_id=guild_id)
    #     return self.request(r, json=payload)
    #
    # def edit_voice_state(
    #     self, guild_id: Snowflake, user_id: Snowflake, payload: Dict[str, Any]
    # ) -> Response[None]:
    #     r = Route(
    #         "PATCH", "/guilds/{guild_id}/voice-states/{user_id}", guild_id=guild_id, user_id=user_id
    #     )
    #     return self.request(r, json=payload)
    #
    # def edit_member(
    #     self,
    #     guild_id: Snowflake,
    #     user_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    #     **fields: Any,
    # ) -> Response[member.MemberWithUser]:
    #     r = Route(
    #         "PATCH", "/guilds/{guild_id}/members/{user_id}", guild_id=guild_id, user_id=user_id
    #     )
    #     return self.request(r, json=fields, reason=reason)
    #
    # # Channel management
    #
    # def edit_channel(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    #     **options: Any,
    # ) -> Response[channel.Channel]:
    #     r = Route("PATCH", "/channels/{channel_id}", channel_id=channel_id)
    #     valid_keys = (
    #         "name",
    #         "parent_id",
    #         "topic",
    #         "bitrate",
    #         "nsfw",
    #         "user_limit",
    #         "position",
    #         "permission_overwrites",
    #         "rate_limit_per_user",
    #         "type",
    #         "rtc_region",
    #         "video_quality_mode",
    #         "archived",
    #         "auto_archive_duration",
    #         "locked",
    #         "invitable",
    #         "default_auto_archive_duration",
    #         "flags",
    #         "default_sort_order",
    #         "default_forum_layout",
    #         "default_thread_rate_limit_per_user",
    #         "default_reaction_emoji",
    #         "available_tags",
    #         "applied_tags",
    #     )
    #     payload = {k: v for k, v in options.items() if k in valid_keys}
    #     return self.request(r, reason=reason, json=payload)
    #
    # def bulk_channel_update(
    #     self,
    #     guild_id: Snowflake,
    #     data: List[guild.ChannelPositionUpdate],
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route("PATCH", "/guilds/{guild_id}/channels", guild_id=guild_id)
    #     return self.request(r, json=data, reason=reason)
    #
    # def create_channel(
    #     self,
    #     guild_id: Snowflake,
    #     channel_type: channel.ChannelType,
    #     *,
    #     reason: Optional[str] = None,
    #     **options: Any,
    # ) -> Response[channel.GuildChannel]:
    #     payload = {
    #         "type": channel_type,
    #     }
    #
    #     valid_keys = (
    #         "name",
    #         "parent_id",
    #         "topic",
    #         "bitrate",
    #         "nsfw",
    #         "user_limit",
    #         "position",
    #         "permission_overwrites",
    #         "rate_limit_per_user",
    #         "rtc_region",
    #         "video_quality_mode",
    #         "auto_archive_duration",
    #         "default_sort_order",
    #         "default_thread_rate_limit_per_user",
    #         "default_reaction_emoji",
    #         "available_tags",
    #         "default_forum_layout",
    #     )
    #     payload.update({k: v for k, v in options.items() if k in valid_keys and v is not None})
    #
    #     return self.request(
    #         Route("POST", "/guilds/{guild_id}/channels", guild_id=guild_id),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # def delete_channel(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     return self.request(
    #         Route("DELETE", "/channels/{channel_id}", channel_id=channel_id), reason=reason
    #     )
    #
    # # Thread management
    #
    # def start_thread_with_message(
    #     self,
    #     channel_id: Snowflake,
    #     message_id: Snowflake,
    #     *,
    #     name: str,
    #     auto_archive_duration: threads.ThreadArchiveDuration,
    #     reason: Optional[str] = None,
    # ) -> Response[threads.Thread]:
    #     payload = {
    #         "name": name,
    #         "auto_archive_duration": auto_archive_duration,
    #     }
    #
    #     route = Route(
    #         "POST",
    #         "/channels/{channel_id}/messages/{message_id}/threads",
    #         channel_id=channel_id,
    #         message_id=message_id,
    #     )
    #     return self.request(route, json=payload, reason=reason)
    #
    # def start_thread_without_message(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     name: str,
    #     auto_archive_duration: threads.ThreadArchiveDuration,
    #     type: threads.ThreadType,
    #     invitable: bool = True,
    #     reason: Optional[str] = None,
    # ) -> Response[threads.Thread]:
    #     payload = {
    #         "name": name,
    #         "auto_archive_duration": auto_archive_duration,
    #         "type": type,
    #         "invitable": invitable,
    #     }
    #
    #     route = Route("POST", "/channels/{channel_id}/threads", channel_id=channel_id)
    #     return self.request(route, json=payload, reason=reason)
    #
    # def start_thread_in_forum_channel(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     name: str,
    #     auto_archive_duration: threads.ThreadArchiveDuration,
    #     rate_limit_per_user: int,
    #     content: Optional[str] = None,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[str, int]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     applied_tag_ids: Optional[List[str]] = None,
    #     flags: Optional[int] = None,
    #     reason: Optional[str] = None,
    # ) -> Response[threads.Thread]:
    #     payload = {
    #         "name": name,
    #         "auto_archive_duration": auto_archive_duration,
    #         "rate_limit_per_user": rate_limit_per_user,
    #         "applied_tags": applied_tag_ids or [],
    #     }
    #     msg_payload = self.get_message_payload(
    #         content=content,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         stickers=stickers,
    #         components=components,
    #         flags=flags,
    #     )
    #     if msg_payload != {}:
    #         payload["message"] = msg_payload
    #     params = {"use_nested_fields": "true"}
    #     route = Route("POST", "/channels/{channel_id}/threads", channel_id=channel_id)
    #     return self.request(route, json=payload, reason=reason, params=params)
    #
    # def start_thread_in_forum_channel_with_files(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     name: str,
    #     auto_archive_duration: threads.ThreadArchiveDuration,
    #     rate_limit_per_user: int,
    #     files: Sequence[File],
    #     content: Optional[str] = None,
    #     embed: Optional[embed.Embed] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     nonce: Optional[Union[str, int]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    #     stickers: Optional[List[int]] = None,
    #     components: Optional[List[components.Component]] = None,
    #     attachments: Optional[List[Dict[str, Any]]] = None,
    #     applied_tag_ids: Optional[List[str]] = None,
    #     flags: Optional[int] = None,
    #     reason: Optional[str] = None,
    # ) -> Response[threads.Thread]:
    #     payload = {
    #         "name": name,
    #         "auto_archive_duration": auto_archive_duration,
    #         "rate_limit_per_user": rate_limit_per_user,
    #         "attachments": attachments or [],
    #         "applied_tags": applied_tag_ids or [],
    #     }
    #     form = self.get_message_multipart_form(
    #         payload=payload,
    #         message_key="message",
    #         files=files,
    #         content=content,
    #         embed=embed,
    #         embeds=embeds,
    #         nonce=nonce,
    #         allowed_mentions=allowed_mentions,
    #         stickers=stickers,
    #         components=components,
    #         attachments=attachments,
    #         flags=flags,
    #     )
    #     params = {"use_nested_fields": "true"}
    #     route = Route("POST", "/channels/{channel_id}/threads", channel_id=channel_id)
    #     return self.request(route, form=form, files=files, reason=reason, params=params)
    #
    # def join_thread(self, channel_id: Snowflake) -> Response[None]:
    #     return self.request(
    #         Route("POST", "/channels/{channel_id}/thread-members/@me", channel_id=channel_id)
    #     )
    #
    # def add_user_to_thread(self, channel_id: Snowflake, user_id: Snowflake) -> Response[None]:
    #     return self.request(
    #         Route(
    #             "PUT",
    #             "/channels/{channel_id}/thread-members/{user_id}",
    #             channel_id=channel_id,
    #             user_id=user_id,
    #         )
    #     )
    #
    # def leave_thread(self, channel_id: Snowflake) -> Response[None]:
    #     return self.request(
    #         Route("DELETE", "/channels/{channel_id}/thread-members/@me", channel_id=channel_id)
    #     )
    #
    # def remove_user_from_thread(self, channel_id: Snowflake, user_id: Snowflake) -> Response[None]:
    #     route = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/thread-members/{user_id}",
    #         channel_id=channel_id,
    #         user_id=user_id,
    #     )
    #     return self.request(route)
    #
    # def get_public_archived_threads(
    #     self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    # ) -> Response[threads.ThreadPaginationPayload]:
    #     route = Route(
    #         "GET", "/channels/{channel_id}/threads/archived/public", channel_id=channel_id
    #     )
    #
    #     params: Dict[str, Union[int, Snowflake]] = {}
    #     if before:
    #         params["before"] = before
    #     params["limit"] = limit
    #     return self.request(route, params=params)
    #
    # def get_private_archived_threads(
    #     self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    # ) -> Response[threads.ThreadPaginationPayload]:
    #     route = Route(
    #         "GET", "/channels/{channel_id}/threads/archived/private", channel_id=channel_id
    #     )
    #
    #     params: Dict[str, Union[int, Snowflake]] = {}
    #     if before:
    #         params["before"] = before
    #     params["limit"] = limit
    #     return self.request(route, params=params)
    #
    # def get_joined_private_archived_threads(
    #     self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    # ) -> Response[threads.ThreadPaginationPayload]:
    #     route = Route(
    #         "GET",
    #         "/channels/{channel_id}/users/@me/threads/archived/private",
    #         channel_id=channel_id,
    #     )
    #     params: Dict[str, Union[int, Snowflake]] = {}
    #     if before:
    #         params["before"] = before
    #     params["limit"] = limit
    #     return self.request(route, params=params)
    #
    # def get_active_threads(self, guild_id: Snowflake) -> Response[threads.ThreadPaginationPayload]:
    #     route = Route("GET", "/guilds/{guild_id}/threads/active", guild_id=guild_id)
    #     return self.request(route)
    #
    # def get_thread_members(self, channel_id: Snowflake) -> Response[List[threads.ThreadMember]]:
    #     route = Route("GET", "/channels/{channel_id}/thread-members", channel_id=channel_id)
    #     return self.request(route)
    #
    # # Webhook management
    #
    # def create_webhook(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     name: str,
    #     avatar: Optional[str] = None,
    #     reason: Optional[str] = None,
    # ) -> Response[webhook.Webhook]:
    #     payload: Dict[str, Any] = {
    #         "name": name,
    #     }
    #     if avatar is not None:
    #         payload["avatar"] = avatar
    #
    #     r = Route("POST", "/channels/{channel_id}/webhooks", channel_id=channel_id)
    #     return self.request(r, json=payload, reason=reason)
    #
    # def channel_webhooks(self, channel_id: Snowflake) -> Response[List[webhook.Webhook]]:
    #     return self.request(Route("GET", "/channels/{channel_id}/webhooks", channel_id=channel_id))
    #
    # def guild_webhooks(self, guild_id: Snowflake) -> Response[List[webhook.Webhook]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/webhooks", guild_id=guild_id))
    #
    # def get_webhook(self, webhook_id: Snowflake) -> Response[webhook.Webhook]:
    #     return self.request(Route("GET", "/webhooks/{webhook_id}", webhook_id=webhook_id))
    #
    # def follow_webhook(
    #     self,
    #     channel_id: Snowflake,
    #     webhook_channel_id: Snowflake,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     payload = {
    #         "webhook_channel_id": str(webhook_channel_id),
    #     }
    #     return self.request(
    #         Route("POST", "/channels/{channel_id}/followers", channel_id=channel_id),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # # Guild management
    #
    # def get_guilds(
    #     self,
    #     limit: int,
    #     before: Optional[Snowflake] = None,
    #     after: Optional[Snowflake] = None,
    # ) -> Response[List[guild.Guild]]:
    #     params: Dict[str, Any] = {
    #         "limit": limit,
    #     }
    #
    #     if before:
    #         params["before"] = before
    #     if after:
    #         params["after"] = after
    #
    #     return self.request(Route("GET", "/users/@me/guilds"), params=params)
    #
    # def leave_guild(self, guild_id: Snowflake) -> Response[None]:
    #     return self.request(Route("DELETE", "/users/@me/guilds/{guild_id}", guild_id=guild_id))
    #
    # def get_guild(self, guild_id: Snowflake, *, with_counts: bool = True) -> Response[guild.Guild]:
    #     params = {"with_counts": int(with_counts)}
    #     return self.request(Route("GET", "/guilds/{guild_id}", guild_id=guild_id), params=params)
    #
    # def get_guild_preview(self, guild_id: Snowflake) -> Response[guild.GuildPreview]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/preview", guild_id=guild_id))
    #
    # def delete_guild(self, guild_id: Snowflake) -> Response[None]:
    #     return self.request(Route("DELETE", "/guilds/{guild_id}", guild_id=guild_id))
    #
    # def create_guild(self, name: str, region: str, icon: Optional[str]) -> Response[guild.Guild]:
    #     payload = {
    #         "name": name,
    #         "region": region,
    #     }
    #     if icon:
    #         payload["icon"] = icon
    #
    #     return self.request(Route("POST", "/guilds"), json=payload)
    #
    # def edit_guild(
    #     self, guild_id: Snowflake, *, reason: Optional[str] = None, **fields: Any
    # ) -> Response[guild.Guild]:
    #     valid_keys = (
    #         "name",
    #         "region",
    #         "icon",
    #         "afk_timeout",
    #         "owner_id",
    #         "afk_channel_id",
    #         "splash",
    #         "discovery_splash",
    #         "features",
    #         "verification_level",
    #         "system_channel_id",
    #         "default_message_notifications",
    #         "description",
    #         "explicit_content_filter",
    #         "banner",
    #         "system_channel_flags",
    #         "rules_channel_id",
    #         "public_updates_channel_id",
    #         "preferred_locale",
    #     )
    #
    #     payload = {k: v for k, v in fields.items() if k in valid_keys}
    #
    #     return self.request(
    #         Route("PATCH", "/guilds/{guild_id}", guild_id=guild_id), json=payload, reason=reason
    #     )
    #
    # def get_template(self, code: str) -> Response[template.Template]:
    #     return self.request(Route("GET", "/guilds/templates/{code}", code=code))
    #
    # def guild_templates(self, guild_id: Snowflake) -> Response[List[template.Template]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/templates", guild_id=guild_id))
    #
    # def create_template(
    #     self, guild_id: Snowflake, payload: template.CreateTemplate
    # ) -> Response[template.Template]:
    #     return self.request(
    #         Route("POST", "/guilds/{guild_id}/templates", guild_id=guild_id), json=payload
    #     )
    #
    # def sync_template(self, guild_id: Snowflake, code: str) -> Response[template.Template]:
    #     return self.request(
    #         Route("PUT", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code)
    #     )
    #
    # def edit_template(self, guild_id: Snowflake, code: str, payload) -> Response[template.Template]:
    #     valid_keys = (
    #         "name",
    #         "description",
    #     )
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}
    #     return self.request(
    #         Route("PATCH", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code),
    #         json=payload,
    #     )
    #
    # def delete_template(self, guild_id: Snowflake, code: str) -> Response[None]:
    #     return self.request(
    #         Route("DELETE", "/guilds/{guild_id}/templates/{code}", guild_id=guild_id, code=code)
    #     )
    #
    # def create_from_template(
    #     self, code: str, name: str, region: str, icon: Optional[str]
    # ) -> Response[guild.Guild]:
    #     payload = {
    #         "name": name,
    #         "region": region,
    #     }
    #     if icon:
    #         payload["icon"] = icon
    #     return self.request(Route("POST", "/guilds/templates/{code}", code=code), json=payload)
    #
    # def get_bans(
    #     self,
    #     guild_id: Snowflake,
    #     limit: Optional[int] = None,
    #     before: Optional[Snowflake] = None,
    #     after: Optional[Snowflake] = None,
    # ) -> Response[List[guild.Ban]]:
    #     params: Dict[str, Union[int, Snowflake]] = {}
    #
    #     if limit is not None:
    #         params["limit"] = limit
    #     if before is not None:
    #         params["before"] = before
    #     if after is not None:
    #         params["after"] = after
    #
    #     return self.request(
    #         Route("GET", "/guilds/{guild_id}/bans", guild_id=guild_id), params=params
    #     )
    #
    # def get_ban(self, user_id: Snowflake, guild_id: Snowflake) -> Response[guild.Ban]:
    #     return self.request(
    #         Route("GET", "/guilds/{guild_id}/bans/{user_id}", guild_id=guild_id, user_id=user_id)
    #     )
    #
    # def get_vanity_code(self, guild_id: Snowflake) -> Response[invite.VanityInvite]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/vanity-url", guild_id=guild_id))
    #
    # def change_vanity_code(
    #     self, guild_id: Snowflake, code: str, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     payload: Dict[str, Any] = {"code": code}
    #     return self.request(
    #         Route("PATCH", "/guilds/{guild_id}/vanity-url", guild_id=guild_id),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # def get_all_guild_channels(self, guild_id: Snowflake) -> Response[List[guild.GuildChannel]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/channels", guild_id=guild_id))
    #
    # def get_members(
    #     self, guild_id: Snowflake, limit: int, after: Optional[Snowflake]
    # ) -> Response[List[member.MemberWithUser]]:
    #     params: Dict[str, Any] = {
    #         "limit": limit,
    #     }
    #     if after:
    #         params["after"] = after
    #
    #     r = Route("GET", "/guilds/{guild_id}/members", guild_id=guild_id)
    #     return self.request(r, params=params)
    #
    # def get_member(
    #     self, guild_id: Snowflake, member_id: Snowflake
    # ) -> Response[member.MemberWithUser]:
    #     return self.request(
    #         Route(
    #             "GET",
    #             "/guilds/{guild_id}/members/{member_id}",
    #             guild_id=guild_id,
    #             member_id=member_id,
    #         )
    #     )
    #
    # def prune_members(
    #     self,
    #     guild_id: Snowflake,
    #     days: int,
    #     compute_prune_count: bool,
    #     roles: List[str],
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[guild.GuildPrune]:
    #     payload: Dict[str, Any] = {
    #         "days": days,
    #         "compute_prune_count": "true" if compute_prune_count else "false",
    #     }
    #     if roles:
    #         payload["include_roles"] = ", ".join(roles)
    #
    #     return self.request(
    #         Route("POST", "/guilds/{guild_id}/prune", guild_id=guild_id),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # def estimate_pruned_members(
    #     self,
    #     guild_id: Snowflake,
    #     days: int,
    #     roles: List[str],
    # ) -> Response[guild.GuildPrune]:
    #     params: Dict[str, Any] = {
    #         "days": days,
    #     }
    #     if roles:
    #         params["include_roles"] = ", ".join(roles)
    #
    #     return self.request(
    #         Route("GET", "/guilds/{guild_id}/prune", guild_id=guild_id), params=params
    #     )
    #
    # def get_sticker(self, sticker_id: Snowflake) -> Response[sticker.Sticker]:
    #     return self.request(Route("GET", "/stickers/{sticker_id}", sticker_id=sticker_id))
    #
    # def list_premium_sticker_packs(self) -> Response[sticker.ListPremiumStickerPacks]:
    #     return self.request(Route("GET", "/sticker-packs"))
    #
    # def get_all_guild_stickers(self, guild_id: Snowflake) -> Response[List[sticker.GuildSticker]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/stickers", guild_id=guild_id))
    #
    # def get_guild_sticker(
    #     self, guild_id: Snowflake, sticker_id: Snowflake
    # ) -> Response[sticker.GuildSticker]:
    #     return self.request(
    #         Route(
    #             "GET",
    #             "/guilds/{guild_id}/stickers/{sticker_id}",
    #             guild_id=guild_id,
    #             sticker_id=sticker_id,
    #         )
    #     )
    #
    # def create_guild_sticker(
    #     self,
    #     guild_id: Snowflake,
    #     payload: sticker.CreateGuildSticker,
    #     file: File,
    #     reason: Optional[str],
    # ) -> Response[sticker.GuildSticker]:
    #     initial_bytes = file.fp.read(16)
    #
    #     try:
    #         mime_type = utils._get_mime_type_for_image(initial_bytes)
    #     except InvalidArgument:
    #         if initial_bytes.startswith(b"{"):
    #             mime_type = "application/json"
    #         else:
    #             mime_type = "application/octet-stream"
    #     finally:
    #         file.reset()
    #
    #     form: List[Dict[str, Any]] = [
    #         {
    #             "name": "file",
    #             "value": file.fp,
    #             "filename": file.filename,
    #             "content_type": mime_type,
    #         }
    #     ]
    #
    #     for k, v in payload.items():
    #         form.append(
    #             {
    #                 "name": k,
    #                 "value": v,
    #             }
    #         )
    #
    #     return self.request(
    #         Route("POST", "/guilds/{guild_id}/stickers", guild_id=guild_id),
    #         form=form,
    #         files=[file],
    #         reason=reason,
    #     )
    #
    # def modify_guild_sticker(
    #     self,
    #     guild_id: Snowflake,
    #     sticker_id: Snowflake,
    #     payload: sticker.EditGuildSticker,
    #     reason: Optional[str],
    # ) -> Response[sticker.GuildSticker]:
    #     return self.request(
    #         Route(
    #             "PATCH",
    #             "/guilds/{guild_id}/stickers/{sticker_id}",
    #             guild_id=guild_id,
    #             sticker_id=sticker_id,
    #         ),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # def delete_guild_sticker(
    #     self, guild_id: Snowflake, sticker_id: Snowflake, reason: Optional[str]
    # ) -> Response[None]:
    #     return self.request(
    #         Route(
    #             "DELETE",
    #             "/guilds/{guild_id}/stickers/{sticker_id}",
    #             guild_id=guild_id,
    #             sticker_id=sticker_id,
    #         ),
    #         reason=reason,
    #     )
    #
    # def get_all_custom_emojis(self, guild_id: Snowflake) -> Response[List[emoji.Emoji]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/emojis", guild_id=guild_id))
    #
    # def get_custom_emoji(self, guild_id: Snowflake, emoji_id: Snowflake) -> Response[emoji.Emoji]:
    #     return self.request(
    #         Route(
    #             "GET", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id
    #         )
    #     )
    #
    # def create_custom_emoji(
    #     self,
    #     guild_id: Snowflake,
    #     name: str,
    #     image: Optional[str],
    #     *,
    #     roles: Optional[SnowflakeList] = None,
    #     reason: Optional[str] = None,
    # ) -> Response[emoji.Emoji]:
    #     payload = {
    #         "name": name,
    #         "image": image,
    #         "roles": roles or [],
    #     }
    #
    #     r = Route("POST", "/guilds/{guild_id}/emojis", guild_id=guild_id)
    #     return self.request(r, json=payload, reason=reason)
    #
    # def delete_custom_emoji(
    #     self,
    #     guild_id: Snowflake,
    #     emoji_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id
    #     )
    #     return self.request(r, reason=reason)
    #
    # def edit_custom_emoji(
    #     self,
    #     guild_id: Snowflake,
    #     emoji_id: Snowflake,
    #     *,
    #     payload: Dict[str, Any],
    #     reason: Optional[str] = None,
    # ) -> Response[emoji.Emoji]:
    #     r = Route(
    #         "PATCH", "/guilds/{guild_id}/emojis/{emoji_id}", guild_id=guild_id, emoji_id=emoji_id
    #     )
    #     return self.request(r, json=payload, reason=reason)
    #
    # def get_all_integrations(self, guild_id: Snowflake) -> Response[List[integration.Integration]]:
    #     r = Route("GET", "/guilds/{guild_id}/integrations", guild_id=guild_id)
    #
    #     return self.request(r)
    #
    # def create_integration(
    #     self, guild_id: Snowflake, type: integration.IntegrationType, id: int
    # ) -> Response[None]:
    #     payload = {
    #         "type": type,
    #         "id": id,
    #     }
    #
    #     r = Route("POST", "/guilds/{guild_id}/integrations", guild_id=guild_id)
    #     return self.request(r, json=payload)
    #
    # def edit_integration(
    #     self, guild_id: Snowflake, integration_id: Snowflake, **payload: Any
    # ) -> Response[None]:
    #     r = Route(
    #         "PATCH",
    #         "/guilds/{guild_id}/integrations/{integration_id}",
    #         guild_id=guild_id,
    #         integration_id=integration_id,
    #     )
    #
    #     return self.request(r, json=payload)
    #
    # def sync_integration(self, guild_id: Snowflake, integration_id: Snowflake) -> Response[None]:
    #     r = Route(
    #         "POST",
    #         "/guilds/{guild_id}/integrations/{integration_id}/sync",
    #         guild_id=guild_id,
    #         integration_id=integration_id,
    #     )
    #
    #     return self.request(r)
    #
    # def delete_integration(
    #     self, guild_id: Snowflake, integration_id: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/guilds/{guild_id}/integrations/{integration_id}",
    #         guild_id=guild_id,
    #         integration_id=integration_id,
    #     )
    #
    #     return self.request(r, reason=reason)
    #
    # def get_audit_logs(
    #     self,
    #     guild_id: Snowflake,
    #     limit: int = 100,
    #     before: Optional[Snowflake] = None,
    #     after: Optional[Snowflake] = None,
    #     user_id: Optional[Snowflake] = None,
    #     action_type: Optional[AuditLogAction] = None,
    # ) -> Response[audit_log.AuditLog]:
    #     params: Dict[str, Any] = {"limit": limit}
    #     if before:
    #         params["before"] = before
    #     if after:
    #         params["after"] = after
    #     if user_id:
    #         params["user_id"] = user_id
    #     if action_type:
    #         params["action_type"] = action_type.value
    #
    #     r = Route("GET", "/guilds/{guild_id}/audit-logs", guild_id=guild_id)
    #     return self.request(r, params=params)
    #
    # def get_widget(self, guild_id: Snowflake) -> Response[widget.Widget]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/widget.json", guild_id=guild_id))
    #
    # def edit_widget(self, guild_id: Snowflake, payload) -> Response[widget.WidgetSettings]:
    #     return self.request(
    #         Route("PATCH", "/guilds/{guild_id}/widget", guild_id=guild_id), json=payload
    #     )
    #
    # # Invite management
    #
    # def create_invite(
    #     self,
    #     channel_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    #     max_age: int = 0,
    #     max_uses: int = 0,
    #     temporary: bool = False,
    #     unique: bool = True,
    #     target_type: Optional[invite.InviteTargetType] = None,
    #     target_user_id: Optional[Snowflake] = None,
    #     target_application_id: Optional[Snowflake] = None,
    # ) -> Response[invite.Invite]:
    #     r = Route("POST", "/channels/{channel_id}/invites", channel_id=channel_id)
    #     payload = {
    #         "max_age": max_age,
    #         "max_uses": max_uses,
    #         "temporary": temporary,
    #         "unique": unique,
    #     }
    #
    #     if target_type:
    #         payload["target_type"] = target_type
    #
    #     if target_user_id:
    #         payload["target_user_id"] = target_user_id
    #
    #     if target_application_id:
    #         payload["target_application_id"] = str(target_application_id)
    #
    #     return self.request(r, reason=reason, json=payload)
    #
    # def get_invite(
    #     self, invite_id: str, *, with_counts: bool = True, with_expiration: bool = True
    # ) -> Response[invite.Invite]:
    #     params = {
    #         "with_counts": int(with_counts),
    #         "with_expiration": int(with_expiration),
    #     }
    #     return self.request(
    #         Route("GET", "/invites/{invite_id}", invite_id=invite_id), params=params
    #     )
    #
    # def invites_from(self, guild_id: Snowflake) -> Response[List[invite.Invite]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/invites", guild_id=guild_id))
    #
    # def invites_from_channel(self, channel_id: Snowflake) -> Response[List[invite.Invite]]:
    #     return self.request(Route("GET", "/channels/{channel_id}/invites", channel_id=channel_id))
    #
    # def delete_invite(self, invite_id: str, *, reason: Optional[str] = None) -> Response[None]:
    #     return self.request(
    #         Route("DELETE", "/invites/{invite_id}", invite_id=invite_id), reason=reason
    #     )
    #
    # # Role management
    #
    # def get_roles(self, guild_id: Snowflake) -> Response[List[role.Role]]:
    #     return self.request(Route("GET", "/guilds/{guild_id}/roles", guild_id=guild_id))
    #
    # def edit_role(
    #     self,
    #     guild_id: Snowflake,
    #     role_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    #     **fields: Any,
    # ) -> Response[role.Role]:
    #     r = Route("PATCH", "/guilds/{guild_id}/roles/{role_id}", guild_id=guild_id, role_id=role_id)
    #     valid_keys = (
    #         "name",
    #         "permissions",
    #         "color",
    #         "hoist",
    #         "mentionable",
    #         "icon",
    #         "unicode_emoji",
    #     )
    #     payload = {k: v for k, v in fields.items() if k in valid_keys}
    #     return self.request(r, json=payload, reason=reason)
    #
    # def delete_role(
    #     self, guild_id: Snowflake, role_id: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE", "/guilds/{guild_id}/roles/{role_id}", guild_id=guild_id, role_id=role_id
    #     )
    #     return self.request(r, reason=reason)
    #
    # def replace_roles(
    #     self,
    #     user_id: Snowflake,
    #     guild_id: Snowflake,
    #     role_ids: List[int],
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[member.MemberWithUser]:
    #     return self.edit_member(guild_id=guild_id, user_id=user_id, roles=role_ids, reason=reason)
    #
    # def create_role(
    #     self, guild_id: Snowflake, *, reason: Optional[str] = None, **fields: Any
    # ) -> Response[role.Role]:
    #     r = Route("POST", "/guilds/{guild_id}/roles", guild_id=guild_id)
    #     return self.request(r, json=fields, reason=reason)
    #
    # def move_role_position(
    #     self,
    #     guild_id: Snowflake,
    #     positions: List[guild.RolePositionUpdate],
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[List[role.Role]]:
    #     r = Route("PATCH", "/guilds/{guild_id}/roles", guild_id=guild_id)
    #     return self.request(r, json=positions, reason=reason)
    #
    # def add_role(
    #     self,
    #     guild_id: Snowflake,
    #     user_id: Snowflake,
    #     role_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route(
    #         "PUT",
    #         "/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
    #         guild_id=guild_id,
    #         user_id=user_id,
    #         role_id=role_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def remove_role(
    #     self,
    #     guild_id: Snowflake,
    #     user_id: Snowflake,
    #     role_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
    #         guild_id=guild_id,
    #         user_id=user_id,
    #         role_id=role_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def edit_channel_permissions(
    #     self,
    #     channel_id: Snowflake,
    #     target: Snowflake,
    #     allow: str,
    #     deny: str,
    #     type: channel.OverwriteType,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     payload = {"id": target, "allow": allow, "deny": deny, "type": type}
    #     r = Route(
    #         "PUT",
    #         "/channels/{channel_id}/permissions/{target}",
    #         channel_id=channel_id,
    #         target=target,
    #     )
    #     return self.request(r, json=payload, reason=reason)
    #
    # def delete_channel_permissions(
    #     self, channel_id: Snowflake, target: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/channels/{channel_id}/permissions/{target}",
    #         channel_id=channel_id,
    #         target=target,
    #     )
    #     return self.request(r, reason=reason)
    #
    # # Voice management
    #
    # def move_member(
    #     self,
    #     user_id: Snowflake,
    #     guild_id: Snowflake,
    #     channel_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[member.MemberWithUser]:
    #     return self.edit_member(
    #         guild_id=guild_id, user_id=user_id, channel_id=channel_id, reason=reason
    #     )
    #
    # # Stage instance management
    #
    # def get_stage_instance(self, channel_id: Snowflake) -> Response[channel.StageInstance]:
    #     return self.request(Route("GET", "/stage-instances/{channel_id}", channel_id=channel_id))
    #
    # def create_stage_instance(
    #     self, *, reason: Optional[str], **payload: Any
    # ) -> Response[channel.StageInstance]:
    #     valid_keys = (
    #         "channel_id",
    #         "topic",
    #         "privacy_level",
    #     )
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}
    #
    #     return self.request(Route("POST", "/stage-instances"), json=payload, reason=reason)
    #
    # def edit_stage_instance(
    #     self, channel_id: Snowflake, *, reason: Optional[str] = None, **payload: Any
    # ) -> Response[None]:
    #     valid_keys = (
    #         "topic",
    #         "privacy_level",
    #     )
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}
    #
    #     return self.request(
    #         Route("PATCH", "/stage-instances/{channel_id}", channel_id=channel_id),
    #         json=payload,
    #         reason=reason,
    #     )
    #
    # def delete_stage_instance(
    #     self, channel_id: Snowflake, *, reason: Optional[str] = None
    # ) -> Response[None]:
    #     return self.request(
    #         Route("DELETE", "/stage-instances/{channel_id}", channel_id=channel_id), reason=reason
    #     )
    #
    # # Application commands (global)
    #
    # def get_global_commands(
    #     self, application_id: Snowflake, with_localizations: bool = True
    # ) -> Response[List[interactions.ApplicationCommand]]:
    #     params: Dict[str, str] = {}
    #     if with_localizations:
    #         params["with_localizations"] = "true"
    #
    #     return self.request(
    #         Route("GET", "/applications/{application_id}/commands", application_id=application_id),
    #         params=params,
    #     )
    #
    # def get_global_command(
    #     self, application_id: Snowflake, command_id: Snowflake
    # ) -> Response[interactions.ApplicationCommand]:
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/commands/{command_id}",
    #         application_id=application_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r)
    #
    # def upsert_global_command(
    #     self, application_id: Snowflake, payload
    # ) -> Response[interactions.ApplicationCommand]:
    #     r = Route("POST", "/applications/{application_id}/commands", application_id=application_id)
    #     return self.request(r, json=payload)
    #
    # def edit_global_command(
    #     self,
    #     application_id: Snowflake,
    #     command_id: Snowflake,
    #     payload: interactions.EditApplicationCommand,
    # ) -> Response[interactions.ApplicationCommand]:
    #     valid_keys = (
    #         "name",
    #         "description",
    #         "options",
    #     )
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}  # type: ignore
    #     r = Route(
    #         "PATCH",
    #         "/applications/{application_id}/commands/{command_id}",
    #         application_id=application_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # def delete_global_command(
    #     self, application_id: Snowflake, command_id: Snowflake
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/applications/{application_id}/commands/{command_id}",
    #         application_id=application_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r)
    #
    # def bulk_upsert_global_commands(
    #     self, application_id: Snowflake, payload
    # ) -> Response[List[interactions.ApplicationCommand]]:
    #     r = Route("PUT", "/applications/{application_id}/commands", application_id=application_id)
    #     return self.request(r, json=payload)
    #
    # # Application commands (guild)
    #
    # def get_guild_commands(
    #     self, application_id: Snowflake, guild_id: Snowflake, with_localizations: bool = True
    # ) -> Response[List[interactions.ApplicationCommand]]:
    #     params: Dict[str, str] = {}
    #     if with_localizations:
    #         params["with_localizations"] = "true"
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/guilds/{guild_id}/commands",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #     )
    #     return self.request(r, params=params)
    #
    # def get_guild_command(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     command_id: Snowflake,
    # ) -> Response[interactions.ApplicationCommand]:
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r)
    #
    # def upsert_guild_command(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     payload: interactions.EditApplicationCommand,
    # ) -> Response[interactions.ApplicationCommand]:
    #     r = Route(
    #         "POST",
    #         "/applications/{application_id}/guilds/{guild_id}/commands",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # def edit_guild_command(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     command_id: Snowflake,
    #     payload: interactions.EditApplicationCommand,
    # ) -> Response[interactions.ApplicationCommand]:
    #     valid_keys = (
    #         "name",
    #         "description",
    #         "options",
    #     )
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}  # type: ignore
    #     r = Route(
    #         "PATCH",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # def delete_guild_command(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     command_id: Snowflake,
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r)
    #
    # def bulk_upsert_guild_commands(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     payload: List[interactions.EditApplicationCommand],
    # ) -> Response[List[interactions.ApplicationCommand]]:
    #     r = Route(
    #         "PUT",
    #         "/applications/{application_id}/guilds/{guild_id}/commands",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # # Interaction responses
    #
    # def _edit_webhook_helper(
    #     self,
    #     route: Route,
    #     file: Optional[File] = None,
    #     content: Optional[str] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    # ):
    #     payload: Dict[str, Any] = {}
    #     if content:
    #         payload["content"] = content
    #     if embeds:
    #         payload["embeds"] = embeds
    #     if allowed_mentions:
    #         payload["allowed_mentions"] = allowed_mentions
    #
    #     form: List[Dict[str, Any]] = [
    #         {
    #             "name": "payload_json",
    #             "value": utils.to_json(payload),
    #         }
    #     ]
    #
    #     if file:
    #         form.append(
    #             {
    #                 "name": "file",
    #                 "value": file.fp,
    #                 "filename": file.filename,
    #                 "content_type": "application/octet-stream",
    #             }
    #         )
    #
    #     return self.request(route, form=form, files=[file] if file else None)
    #
    # def create_interaction_response(
    #     self,
    #     interaction_id: Snowflake,
    #     token: str,
    #     *,
    #     type: InteractionResponseType,
    #     data: Optional[interactions.InteractionApplicationCommandCallbackData] = None,
    # ) -> Response[None]:
    #     r = Route(
    #         "POST",
    #         "/interactions/{interaction_id}/{interaction_token}/callback",
    #         interaction_id=interaction_id,
    #         interaction_token=token,
    #     )
    #     payload: Dict[str, Any] = {
    #         "type": type,
    #     }
    #
    #     if data is not None:
    #         payload["data"] = data
    #
    #     return self.request(r, json=payload)
    #
    # def get_original_interaction_response(
    #     self,
    #     application_id: Snowflake,
    #     token: str,
    # ) -> Response[message.Message]:
    #     r = Route(
    #         "GET",
    #         "/webhooks/{application_id}/{interaction_token}/messages/@original",
    #         application_id=application_id,
    #         interaction_token=token,
    #     )
    #     return self.request(r)
    #
    # def edit_original_interaction_response(
    #     self,
    #     application_id: Snowflake,
    #     token: str,
    #     file: Optional[File] = None,
    #     content: Optional[str] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    # ) -> Response[message.Message]:
    #     r = Route(
    #         "PATCH",
    #         "/webhooks/{application_id}/{interaction_token}/messages/@original",
    #         application_id=application_id,
    #         interaction_token=token,
    #     )
    #     return self._edit_webhook_helper(
    #         r, file=file, content=content, embeds=embeds, allowed_mentions=allowed_mentions
    #     )
    #
    # def delete_original_interaction_response(
    #     self, application_id: Snowflake, token: str
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/webhooks/{application_id}/{interaction_token}/messages/@original",
    #         application_id=application_id,
    #         interaction_token=token,
    #     )
    #     return self.request(r)
    #
    # def create_followup_message(
    #     self,
    #     application_id: Snowflake,
    #     token: str,
    #     files: Optional[List[File]] = None,
    #     content: Optional[str] = None,
    #     tts: bool = False,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    # ) -> Response[message.Message]:
    #     if files is None:
    #         files = []
    #
    #     r = Route(
    #         "POST",
    #         "/webhooks/{application_id}/{interaction_token}",
    #         application_id=application_id,
    #         interaction_token=token,
    #     )
    #     return self.send_multipart_helper(
    #         r,
    #         content=content,
    #         files=files,
    #         tts=tts,
    #         embeds=embeds,
    #         allowed_mentions=allowed_mentions,
    #     )
    #
    # def edit_followup_message(
    #     self,
    #     application_id: Snowflake,
    #     token: str,
    #     message_id: Snowflake,
    #     file: Optional[File] = None,
    #     content: Optional[str] = None,
    #     embeds: Optional[List[embed.Embed]] = None,
    #     allowed_mentions: Optional[message.AllowedMentions] = None,
    # ) -> Response[message.Message]:
    #     r = Route(
    #         "PATCH",
    #         "/webhooks/{application_id}/{interaction_token}/messages/{message_id}",
    #         application_id=application_id,
    #         interaction_token=token,
    #         message_id=message_id,
    #     )
    #     return self._edit_webhook_helper(
    #         r, file=file, content=content, embeds=embeds, allowed_mentions=allowed_mentions
    #     )
    #
    # def delete_followup_message(
    #     self, application_id: Snowflake, token: str, message_id: Snowflake
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/webhooks/{application_id}/{interaction_token}/messages/{message_id}",
    #         application_id=application_id,
    #         interaction_token=token,
    #         message_id=message_id,
    #     )
    #     return self.request(r)
    #
    # def get_guild_application_command_permissions(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    # ) -> Response[List[interactions.GuildApplicationCommandPermissions]]:
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/permissions",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #     )
    #     return self.request(r)
    #
    # def get_application_command_permissions(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     command_id: Snowflake,
    # ) -> Response[interactions.GuildApplicationCommandPermissions]:
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}/permissions",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r)
    #
    # def edit_application_command_permissions(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     command_id: Snowflake,
    #     payload: interactions.BaseGuildApplicationCommandPermissions,
    # ) -> Response[None]:
    #     r = Route(
    #         "PUT",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}/permissions",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #         command_id=command_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # def bulk_edit_guild_application_command_permissions(
    #     self,
    #     application_id: Snowflake,
    #     guild_id: Snowflake,
    #     payload: List[interactions.PartialGuildApplicationCommandPermissions],
    # ) -> Response[None]:
    #     r = Route(
    #         "PUT",
    #         "/applications/{application_id}/guilds/{guild_id}/commands/permissions",
    #         application_id=application_id,
    #         guild_id=guild_id,
    #     )
    #     return self.request(r, json=payload)
    #
    # # Misc
    #
    # def application_info(self) -> Response[appinfo.AppInfo]:
    #     return self.request(Route("GET", "/oauth2/applications/@me"))
    #
    # @staticmethod
    # def format_websocket_url(url: str, encoding: str = "json", zlib: bool = True) -> str:
    #     if zlib:
    #         value = "{url}?encoding={encoding}&v={version}&compress=zlib-stream"
    #     else:
    #         value = "{url}?encoding={encoding}&v={version}"
    #     return value.format(url=url, encoding=encoding, version=_API_VERSION)
    #
    # async def get_gateway(self, *, encoding: str = "json", zlib: bool = True) -> str:
    #     try:
    #         data = await self.request(Route("GET", "/gateway"))
    #     except HTTPException as exc:
    #         raise GatewayNotFound() from exc
    #
    #     return self.format_websocket_url(data["url"], encoding, zlib)
    #
    async def get_bot_gateway(
            self, *, encoding: str = "json", zlib: bool = True
    ) -> tuple[int, str, dict]:
        try:
            data = await self.request(Route("GET", "/gateway/bot"))
        except core.HTTPException as exc:
            raise core.GatewayNotFound from exc

        return (
            data["shards"],
            self._format_gateway_url(data["url"], encoding=encoding, zlib=zlib),
            data["session_start_limit"],
        )
    #
    # def get_user(self, user_id: Snowflake) -> Response[user.User]:
    #     return self.request(Route("GET", "/users/{user_id}", user_id=user_id))
    #
    # def get_guild_events(
    #     self, guild_id: Snowflake, with_user_count: bool
    # ) -> Response[List[scheduled_events.ScheduledEvent]]:
    #     params: Dict[str, Any] = {"with_user_count": str(with_user_count)}
    #     r = Route("GET", "/guilds/{guild_id}/scheduled-events", guild_id=guild_id)
    #     return self.request(r, params=params)
    #
    # def create_event(
    #     self, guild_id: Snowflake, *, reason: Optional[str] = None, **payload: Any
    # ) -> Response[scheduled_events.ScheduledEvent]:
    #     valid_keys = {
    #         "channel_id",
    #         "entity_metadata",
    #         "name",
    #         "privacy_level",
    #         "scheduled_start_time",
    #         "scheduled_end_time",
    #         "description",
    #         "entity_type",
    #         "image",
    #     }
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}
    #     r = Route("POST", "/guilds/{guild_id}/scheduled-events", guild_id=guild_id)
    #     return self.request(r, json=payload, reason=reason)
    #
    # def get_event(
    #     self, guild_id: Snowflake, event_id: Snowflake, with_user_count: bool
    # ) -> Response[scheduled_events.ScheduledEvent]:
    #     params: Dict[str, Any] = {"with_user_count": str(with_user_count)}
    #     r = Route(
    #         "GET",
    #         "/guilds/{guild_id}/scheduled-events/{event_id}",
    #         guild_id=guild_id,
    #         event_id=event_id,
    #     )
    #     return self.request(r, params=params)
    #
    # def edit_event(
    #     self,
    #     guild_id: Snowflake,
    #     event_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    #     **payload: Any,
    # ) -> Response[scheduled_events.ScheduledEvent]:
    #     valid_keys = {
    #         "channel_id",
    #         "event_metadata",
    #         "name",
    #         "privacy_level",
    #         "scheduled_start_time",
    #         "scheduled_end_time",
    #         "description",
    #         "entity_type",
    #         "status",
    #         "image",
    #     }
    #     payload = {k: v for k, v in payload.items() if k in valid_keys}
    #     r = Route(
    #         "PATCH",
    #         "/guilds/{guild_id}/scheduled-events/{event_id}",
    #         guild_id=guild_id,
    #         event_id=event_id,
    #     )
    #     return self.request(r, json=payload, reason=reason)
    #
    # def delete_event(self, guild_id: Snowflake, event_id: Snowflake) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/guilds/{guild_id}/scheduled-events/{event_id}",
    #         guild_id=guild_id,
    #         event_id=event_id,
    #     )
    #     return self.request(r)
    #
    # def get_event_users(
    #     self,
    #     guild_id: Snowflake,
    #     event_id: Snowflake,
    #     *,
    #     limit: int = MISSING,
    #     with_member: bool = MISSING,
    #     before: Optional[Snowflake] = None,
    #     after: Optional[Snowflake] = None,
    # ) -> Response[List[scheduled_events.ScheduledEventUser]]:
    #     params: Dict[str, Any] = {}
    #     if limit is not MISSING:
    #         params["limit"] = limit
    #     if with_member is not MISSING:
    #         params["with_member"] = str(with_member)
    #     if before is not None:
    #         params["before"] = before
    #     if after is not None:
    #         params["after"] = after
    #     r = Route(
    #         "GET",
    #         "/guilds/{guild_id}/scheduled-events/{event_id}/users",
    #         guild_id=guild_id,
    #         event_id=event_id,
    #     )
    #     return self.request(r, params=params)
    #
    # def list_guild_auto_moderation_rules(
    #     self, guild_id: Snowflake
    # ) -> Response[List[auto_moderation.AutoModerationRule]]:
    #     r = Route("GET", "/guilds/{guild_id}/auto-moderation/rules", guild_id=guild_id)
    #     return self.request(r)
    #
    # def get_auto_moderation_rule(
    #     self,
    #     guild_id: Snowflake,
    #     auto_moderation_rule_id: Snowflake,
    # ) -> Response[auto_moderation.AutoModerationRule]:
    #     r = Route(
    #         "GET",
    #         "/guilds/{guild_id}/auto-moderation/rules/{auto_moderation_rule_id}",
    #         guild_id=guild_id,
    #         auto_moderation_rule_id=auto_moderation_rule_id,
    #     )
    #     return self.request(r)
    #
    # def create_auto_moderation_rule(
    #     self,
    #     guild_id: Snowflake,
    #     data: auto_moderation.AutoModerationRuleCreate,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[auto_moderation.AutoModerationRule]:
    #     valid_keys = (
    #         "trigger_metadata",
    #         "enabled",
    #         "exempt_roles",
    #         "exempt_channels",
    #         "name",
    #         "event_type",
    #         "trigger_type",
    #         "actions",
    #     )
    #
    #     payload = {k: v for k, v in data.items() if k in valid_keys}
    #
    #     r = Route("POST", "/guilds/{guild_id}/auto-moderation/rules", guild_id=guild_id)
    #     return self.request(r, json=payload, reason=reason)
    #
    # def modify_auto_moderation_rule(
    #     self,
    #     guild_id: Snowflake,
    #     auto_moderation_rule_id: Snowflake,
    #     data: auto_moderation.AutoModerationRuleModify,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[auto_moderation.AutoModerationRule]:
    #     valid_keys = (
    #         "name",
    #         "event_type",
    #         "trigger_metadata",
    #         "actions",
    #         "enabled",
    #         "exempt_roles",
    #         "exempt_channels",
    #     )
    #
    #     payload = {k: v for k, v in data.items() if k in valid_keys}
    #
    #     r = Route(
    #         "PATCH",
    #         "/guilds/{guild_id}/auto-moderation/rules/{auto_moderation_rule_id}",
    #         guild_id=guild_id,
    #         auto_moderation_rule_id=auto_moderation_rule_id,
    #     )
    #     return self.request(r, json=payload, reason=reason)
    #
    # def delete_auto_moderation_rule(
    #     self,
    #     guild_id: Snowflake,
    #     auto_moderation_rule_id: Snowflake,
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[None]:
    #     r = Route(
    #         "DELETE",
    #         "/guilds/{guild_id}/auto-moderation/rules/{auto_moderation_rule_id}",
    #         guild_id=guild_id,
    #         auto_moderation_rule_id=auto_moderation_rule_id,
    #     )
    #     return self.request(r, reason=reason)
    #
    # def get_role_connection_metadata(
    #     self, application_id: Snowflake
    # ) -> Response[List[role_connections.ApplicationRoleConnectionMetadata]]:
    #     r = Route(
    #         "GET",
    #         "/applications/{application_id}/role-connections/metadata",
    #         application_id=application_id,
    #     )
    #     return self.request(r)
    #
    # def update_role_connection_metadata(
    #     self,
    #     application_id: Snowflake,
    #     data: List[role_connections.ApplicationRoleConnectionMetadata],
    #     *,
    #     reason: Optional[str] = None,
    # ) -> Response[List[role_connections.ApplicationRoleConnectionMetadata]]:
    #     r = Route(
    #         "PUT",
    #         "/applications/{application_id}/role-connections/metadata",
    #         application_id=application_id,
    #     )
    #     return self.request(r, json=data, reason=reason)

    @staticmethod
    def _format_gateway_url(url: str, *, encoding: str, zlib: bool) -> str:
        _url = yarl.URL(url)
        params = _url.query.copy()
        params["v"] = str(_API_VERSION)
        params["encoding"] = encoding
        if zlib:
            params["compress"] = "zlib-stream"
        else:
            params.popall("compress", None)
        return str(_url.with_query(params))

    def get_user(self, user_id: Snowflake) -> Response[user.User]:
        return self.request(Route("GET", "/users/{user_id}", user_id=user_id))

# SPDX-License-Identifier: MIT

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import aiohttp

from .. import core


if TYPE_CHECKING:
    from ..http import HTTPHandler


class DiscordWebSocket:
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE = 3
    VOICE_STATE = 4
    VOICE_PING = 5
    RESUME = 6
    RECONNECT = 7
    REQUEST_MEMBERS = 8
    INVALIDATE_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11
    GUILD_SYNC = 12

    def __init__(self):
        self.events = core.DispatchFramework()
        self._http: HTTPHandler | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._shard_id: int | None = None
        self._shard_count: int | None = None
        self._gateway_url: str | None = None
        self._token: str | None = None
        self._intents: int | None = None

    def set_http_handler(self, http_handler: HTTPHandler):
        self._http = http_handler

    def get_identify_payload(self) -> dict:
        if self._token is None:
            raise core.DiscordException("Cannot create identify payload without token being set.")

        if self._intents is None:
            raise core.DiscordException("Cannot create identify payload without intents being set.")

        ret = {
            "op": self.IDENTIFY,
            "d": {
                "token": self._token,
                "properties": {
                    "os": sys.platform,
                    "browser": "disnake",
                    "device": "disnake",
                },
                "large_threshold": 250,
                "intents": self._intents,
            },
        }

        if self._shard_id is not None and self._shard_count is not None:
            ret["d"]["shard"] = self._shard_id, self._shard_count

        # TODO: Presence stuff?

        return ret

    async def start(
            self,
            *,
            shard_id: int,
            shard_count: int,
            gateway_url: str,
            token: str,
            intents: int
    ):
        if self._http is None:
            raise core.DiscordException("The HTTP Handler must be set before starting.")

        if self._ws and not self._ws.closed:
            raise core.DiscordException("The Websocket is already started.")

        self._shard_id = shard_id
        self._shard_count = shard_count
        self._gateway_url = gateway_url
        self._token = token
        self._intents = intents

        if self._session is None:
            self._session = aiohttp.ClientSession()

        self._ws = self._session.ws_connect(
            url=gateway_url,
            max_msg_size=0,
            timeout=30.0,
            autoclose=False,
            headers={"User-Agent": self._http._user_agent},  # TODO: Think about a better way to do this? A custom
                                                             #  Gateway User-Agent? Does Discord hate that?
            compress=0,
        )




# SPDX-License-Identifier: MIT

from ..core import DispatchFramework
from ..http import HTTPHandler


__all__ = (
    "Bot",
)


class Bot:
    def __init__(self):
        self.events = DispatchFramework()
        self.http = HTTPHandler(dispatch=self.events.dispatch)  # TODO: Should HTTP have its own DispatchFramework?

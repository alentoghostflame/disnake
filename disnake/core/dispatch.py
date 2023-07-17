from __future__ import annotations
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import Enum
from types import MethodType
from typing import Callable

import asyncio


__all__ = (
    "DispatchFramework",
    "listen",
    "ListenerClass",
    "WaitForCheck",
)


EVENT_TYPE = str | type | Enum


class ListenerClass:
    event: str | type
    callback: Callable

    def __init__(self, event: str | type, callback: Callable):
        self.event = event
        self.callback = callback


class WaitForCheck:
    def __init__(self, future: asyncio.Future, check: Callable[..., bool]):
        self.future: asyncio.Future = future
        self.check: Callable[..., bool] = check

    def __await__(self):
        yield from self.future


class DispatchFramework:
    __permanent_listeners__: dict[EVENT_TYPE, set[Callable]]
    __temporary_listeners__: dict[EVENT_TYPE, set[WaitForCheck]]

    def __new__(cls, *args, **kwargs):
        new_cls = super(DispatchFramework, cls).__new__(cls)
        new_cls._discover_listeners()
        return new_cls

    def _discover_listeners(self):
        self.__permanent_listeners__ = {}
        self.__temporary_listeners__ = {}
        for base in reversed(self.__class__.__mro__):
            for elem, value in base.__dict__.items():
                if isinstance(value, staticmethod):
                    value = value.__func__

                if isinstance(value, ListenerClass):
                    if not self.__permanent_listeners__.get(value.event):
                        self.__permanent_listeners__[value.event] = set()

                    base.__dict__[elem] = MethodType(value.event, self)
                    self.__permanent_listeners__[value.event].add(base.__dict__[elem])

    def add_listener(self, func: ListenerClass | Callable, event: EVENT_TYPE):
        if isinstance(func, ListenerClass):
            event = event or func.event
            func = func.callback

        if event not in self.__permanent_listeners__:
            self.__permanent_listeners__[event] = set()

        self.__permanent_listeners__[event].add(func)

    def remove_listener(self, func: ListenerClass | Callable, event: str | type | Enum) -> bool:
        if isinstance(func, ListenerClass):
            event = event or func.event
            func = func.callback

        if func in self.__permanent_listeners__.get(event, set()):
            self.__permanent_listeners__[event].remove(func)
            return True
        else:
            return False

    def listen(self, event: str | type | Enum | None = None):
        def wrapper(func: Callable):
            name = event or func.__name__
            self.add_listener(func=func, event=name)
            return func

        return wrapper

    async def wait_for(self, event: type | str | Enum, check: Callable[..., bool] | None = None, timeout: float | None = None):
        future = asyncio.get_running_loop().create_future()
        if not check:
            def _check(*args, **kwargs):
                return True

            check = _check

        if event not in self.__temporary_listeners__:
            self.__temporary_listeners__[event] = set()

        check = WaitForCheck(future, check)
        self.__temporary_listeners__[event].add(check)

        try:
            ret = await asyncio.wait_for(future, timeout=timeout)
        except Exception as e:
            raise e
        else:
            return ret
        finally:
            self.__temporary_listeners__[event].discard(check)

    def dispatch(self, event: type | str | Enum, *args, **kwargs):
        loop = asyncio.get_running_loop()
        if event in self.__temporary_listeners__:
            temp_listeners = self.__temporary_listeners__[event].copy()
            for listener in temp_listeners:
                if listener.future.cancelled():
                    self.__temporary_listeners__[event].remove(listener)
                else:
                    try:
                        result = listener.check(*args, **kwargs)
                    except Exception as e:
                        listener.future.set_exception(e)
                    else:
                        if result:
                            match len(args):
                                case 0:
                                    listener.future.set_result(None)
                                case 1:
                                    listener.future.set_result(args[0])
                                case _:
                                    listener.future.set_result(args)

                            self.__temporary_listeners__[event].remove(listener)

        for listener in self.__permanent_listeners__.get(event, set()):
            loop.create_task(listener(*args, **kwargs))


def listen(listen_for: str | type | Enum):
    def wrapper(func: Callable) -> ListenerClass:
        if asyncio.iscoroutinefunction(func):
            ret = ListenerClass(listen_for, func)
            ret.__call__ = func
            return ret
        else:
            raise ValueError("Given function is not a coroutine.")

    return wrapper

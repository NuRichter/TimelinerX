"""Cooperative cancellation shared by import, render and encode stages."""

from __future__ import annotations

import threading

from ..core.errors import RenderCancelledError


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled by user") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RenderCancelledError(self.reason or "cancelled")

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


NEVER_CANCELLED = CancelToken()

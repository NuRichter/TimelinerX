"""Background workers. The UI thread never parses, renders or encodes."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..core.fallback import FallbackDecision, FallbackPolicy
from ..utils.cancel import CancelToken


class Worker(QThread):
    """Runs ``fn(worker)`` in a thread; emits ``done(result)`` or ``failed(exc)``."""

    done = Signal(object)
    failed = Signal(object)
    progress = Signal(float, str)
    event = Signal(dict)

    def __init__(self, fn: Callable[["Worker"], Any], parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = CancelToken()

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except BaseException as e:  # noqa: BLE001
            e.__traceback_text__ = traceback.format_exc()  # type: ignore[attr-defined]
            self.failed.emit(e)


class ConfirmBridge(QObject):
    """Lets a worker thread ask the user to consent to a fallback and wait for the answer."""

    ask = Signal(object, object)  # decision, holder

    def __init__(self, parent_widget):
        super().__init__()
        self.parent_widget = parent_widget
        self.ask.connect(self._on_ask)

    def _on_ask(self, decision: FallbackDecision, holder: dict):
        from PySide6.QtWidgets import QMessageBox
        from ..i18n import tr
        box = QMessageBox(self.parent_widget)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(tr("ui.fallback.title"))
        box.setText(tr("ui.fallback.text", subsystem=decision.subsystem))
        box.setInformativeText(
            f"{tr('ui.fallback.requested')}: {decision.requested}\n"
            f"{tr('ui.fallback.proposed')}: {decision.proposed}\n\n"
            f"{tr('ui.fallback.reason')}: {decision.reason}\n\n"
            f"{tr('ui.fallback.impact')}: {decision.impact}")
        accept = box.addButton(tr("ui.fallback.accept"), QMessageBox.AcceptRole)
        box.addButton(tr("ui.fallback.stop"), QMessageBox.RejectRole)
        box.exec()
        holder["ok"] = box.clickedButton() is accept
        holder["event"].set()

    def confirmer(self, decision: FallbackDecision) -> bool:
        holder = {"event": threading.Event(), "ok": False}
        self.ask.emit(decision, holder)
        holder["event"].wait()
        return bool(holder["ok"])

    def policy(self, accept_all: bool = False) -> FallbackPolicy:
        return FallbackPolicy(confirmer=self.confirmer, accept_all=accept_all)

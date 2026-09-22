"""Reusable UI building blocks."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..i18n import tr
from .icons import icon


class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 16, 18, 16)
        self.lay.setSpacing(10)
        if title:
            t = QLabel(title)
            t.setObjectName("SectionTitle")
            self.lay.addWidget(t)

    def add(self, w):
        if isinstance(w, QWidget):
            self.lay.addWidget(w)
        else:
            self.lay.addLayout(w)
        return w


class Banner(QFrame):
    """Status banner: colour + icon + text (never colour alone)."""

    ICONS = {"info": "info", "warn": "warn", "danger": "error", "ok": "ok"}

    def __init__(self, kind: str = "info", text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setProperty("kind", kind)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.ic = QLabel()
        self.ic.setFixedSize(20, 20)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.ic, 0, Qt.AlignTop)
        lay.addWidget(self.text, 1)
        self.set(kind, text)

    def set(self, kind: str, text: str):
        from .main_window import current_tokens
        t = current_tokens()
        col = {"info": t.accent, "warn": t.warn, "danger": t.danger, "ok": t.ok}[kind]
        self.setProperty("kind", kind)
        self.ic.setPixmap(icon(self.ICONS[kind], col, 20).pixmap(20, 20))
        self.text.setText(text)
        self.setAccessibleName(f"{kind}: {text}")
        self.style().unpolish(self)
        self.style().polish(self)


class StatTile(QWidget):
    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.label = QLabel(label)
        self.label.setObjectName("StatLabel")
        lay.addWidget(self.value)
        lay.addWidget(self.label)

    def set(self, value: str):
        self.value.setText(value)
        self.setAccessibleName(f"{self.label.text()}: {value}")


def page_header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("PageSub")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def muted(text: str) -> QLabel:
    l = QLabel(text)
    l.setProperty("muted", "true")
    l.setWordWrap(True)
    return l


def button_text(text: str) -> str:
    """Escape '&' so Qt does not treat it as a mnemonic marker."""
    return text.replace("&", "&&")


def primary_button(text: str) -> QPushButton:
    b = QPushButton(button_text(text))
    b.setProperty("primary", "true")
    b.setCursor(Qt.PointingHandCursor)
    return b


class ScrollPage(QScrollArea):
    """A page whose content scrolls; content max width keeps lines readable."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.inner = QWidget()
        self.inner.setObjectName("Root")
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(32, 26, 32, 32)
        self.lay.setSpacing(16)
        self.setWidget(self.inner)

    def finish(self):
        self.lay.addStretch(1)


class DropZone(QFrame):
    fileDropped = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        self.ic = QLabel()
        self.ic.setAlignment(Qt.AlignCenter)
        self.lbl = QLabel(text)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        lay.addWidget(self.ic)
        lay.addWidget(self.lbl)
        from .main_window import current_tokens
        self.ic.setPixmap(icon("import", current_tokens().accent, 32).pixmap(32, 32))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.fileDropped.emit(urls[0].toLocalFile())


class BarChart(QWidget):
    """Points per month; labelled axis, accessible summary."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: List[tuple] = []
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, data: List[tuple]):
        self.data = data
        if data:
            peak = max(data, key=lambda d: d[1])
            self.setAccessibleDescription(f"{len(data)} months; busiest {peak[0]} with {peak[1]:,} points")
        self.update()

    def paintEvent(self, _):
        from .main_window import current_tokens
        t = current_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if not self.data:
            p.setPen(QColor(t.text_2))
            p.drawText(self.rect(), Qt.AlignCenter, tr("ui.analysis.no_data"))
            return
        top, bottom, left = 10, 26, 44
        mx = max(v for _, v in self.data) or 1
        n = len(self.data)
        bw = (w - left - 8) / n
        p.setPen(QPen(QColor(t.border), 1))
        for frac in (0, 0.5, 1.0):
            y = top + (h - top - bottom) * (1 - frac)
            p.drawLine(left, int(y), w - 8, int(y))
            p.setPen(QColor(t.text_2))
            f = p.font()
            f.setPixelSize(10)
            p.setFont(f)
            p.drawText(QRectF(0, y - 7, left - 6, 14), Qt.AlignRight | Qt.AlignVCenter, f"{int(mx * frac):,}")
            p.setPen(QPen(QColor(t.border), 1))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.accent))
        step = max(1, n // 12)
        for i, (lab, v) in enumerate(self.data):
            bh = (h - top - bottom) * v / mx
            x = left + i * bw + bw * 0.15
            r = QRectF(x, h - bottom - bh, max(1.0, bw * 0.7), bh)
            p.drawRoundedRect(r, 2, 2)
            if i % step == 0:
                p.setPen(QColor(t.text_2))
                p.drawText(QRectF(left + i * bw - 20, h - bottom + 4, bw + 40, 16), Qt.AlignHCenter, lab)
                p.setPen(Qt.NoPen)
        p.end()


class RouteOverview(QWidget):
    """Whole-timeline route sketch (projected, decimated). No map tiles, no network."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.xs = self.ys = None
        self.setMinimumHeight(260)

    def set_route(self, xs, ys):
        self.xs, self.ys = xs, ys
        self.update()

    def paintEvent(self, _):
        import numpy as np
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF
        from .main_window import current_tokens
        t = current_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(t.surface_2))
        if self.xs is None or len(self.xs) < 2:
            p.setPen(QColor(t.text_2))
            p.drawText(self.rect(), Qt.AlignCenter, tr("ui.analysis.no_data"))
            return
        xs, ys = np.asarray(self.xs), np.asarray(self.ys)
        mnx, mxx, mny, mxy = xs.min(), xs.max(), ys.min(), ys.max()
        sx = max(mxx - mnx, 1.0)
        sy = max(mxy - mny, 1.0)
        k = min((self.width() - 24) / sx, (self.height() - 24) / sy)
        ox = (self.width() - sx * k) / 2
        oy = (self.height() - sy * k) / 2
        px = ox + (xs - mnx) * k
        py = oy + (mxy - ys) * k
        d = np.hypot(np.diff(px), np.diff(py))
        c = np.concatenate([[0], np.cumsum(d)])
        keep = np.concatenate([[True], np.floor(c[1:]) != np.floor(c[:-1])])
        pen = QPen(QColor(t.accent), 1.6)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(a, b) for a, b in zip(px[keep], py[keep])]))
        p.end()


class ThemeSwatch(QPushButton):
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setCheckable(True)
        self.setFixedSize(QSize(150, 96))
        self.setToolTip(theme.name + (" — experimental" if theme.experimental else ""))
        self.setAccessibleName(theme.name)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, e):
        from .main_window import current_tokens
        tk = current_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        pal = self.theme.palette
        p.fillPath(path, QColor(pal.background))
        p.setPen(QPen(QColor(pal.route), 3))
        p.drawLine(int(r.left() + 14), int(r.bottom() - 34), int(r.right() - 34), int(r.top() + 20))
        p.setBrush(QColor(pal.marker_core))
        p.setPen(QPen(QColor(pal.marker_ring), 2))
        p.drawEllipse(QRectF(r.right() - 40, r.top() + 14, 12, 12))
        p.setPen(QColor(pal.text_primary))
        f = QFont(p.font())
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        from PySide6.QtGui import QFontMetrics
        name = QFontMetrics(f).elidedText(self.theme.name, Qt.ElideRight, int(r.width() - 20))
        p.drawText(QRectF(r.left() + 10, r.bottom() - 22, r.width() - 20, 18), Qt.AlignLeft | Qt.AlignVCenter, name)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(tk.focus if self.isChecked() else tk.border), 3 if self.isChecked() else 1))
        p.drawPath(path)
        if self.hasFocus():
            p.setPen(QPen(QColor(tk.focus), 2, Qt.DashLine))
            p.drawRoundedRect(r.adjusted(3, 3, -3, -3), 7, 7)
        p.end()


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color: rgba(127,127,127,60);")
    return f


class SegmentedOptions(QWidget):
    """A labelled row of mutually exclusive choices with a live description underneath.

    ``options`` is a list of (key, label, description). ``set_badges`` can attach a short
    per-option figure (e.g. how many long trips each detection level finds).
    """

    changed = Signal(str)

    def __init__(self, title: str, options, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup
        self._opts = list(options)
        self._badges: Dict[str, str] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel(title)
        head.setObjectName("OptionTitle")
        lay.addWidget(head)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        for i, (key, label, _desc) in enumerate(self._opts):
            b = QPushButton(button_text(label))
            b.setCheckable(True)
            b.setProperty("segment", "first" if i == 0 else "last" if i == len(self._opts) - 1 else "mid")
            b.setCursor(Qt.PointingHandCursor)
            b.setAccessibleName(f"{title}: {label}")
            b.setMinimumHeight(34)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b, i)
            self.buttons[key] = b
            row.addWidget(b)
        lay.addLayout(row)
        self.desc = QLabel("")
        self.desc.setProperty("muted", "true")
        self.desc.setWordWrap(True)
        self.desc.setMinimumHeight(34)
        lay.addWidget(self.desc)
        self.group.idToggled.connect(self._toggled)

    def _toggled(self, i: int, on: bool):
        if not on:
            return
        key = self._opts[i][0]
        self.desc.setText(self._opts[i][2])
        self.changed.emit(key)

    def value(self) -> str:
        i = self.group.checkedId()
        return self._opts[i][0] if i >= 0 else self._opts[0][0]

    def set_value(self, key: str) -> None:
        keys = [o[0] for o in self._opts]
        i = keys.index(key) if key in keys else 0
        self.group.blockSignals(True)
        self.group.button(i).setChecked(True)
        self.group.blockSignals(False)
        self.desc.setText(self._opts[i][2])

    def set_badges(self, badges: Dict[str, str]) -> None:
        self._badges = dict(badges)
        for key, label, _ in self._opts:
            b = self.buttons[key]
            extra = self._badges.get(key)
            b.setText(button_text(f"{label} · {extra}" if extra else label))

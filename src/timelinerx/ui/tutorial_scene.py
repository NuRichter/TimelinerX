"""Motion-graphic tutorial: how to export Google Timeline on a phone and import it into TimelinerX.

The whole animation is a pure function ``draw(painter, t, …)`` on a 1920×1080 virtual canvas, so
the same code drives the in-app player (any window size, pausable, seekable) and the MP4 export.
Everything is drawn with QPainter (no bitmaps): a generic phone, generic menus (labels come from the
translation files, as the phone shows them in that language), a laptop and a miniature TimelinerX.

Menu paths (checked against current Google help and Timeline Visualizer documentation, 2026):
  Android: Settings → Location → Location services → Timeline → Export Timeline data → Continue → Save
  iPhone:  Google Maps → profile picture → Your Timeline → ⋯ → Location & privacy settings →
           Export Timeline data → Save to Files
Google does not offer Timeline through Takeout or the web any more; the export happens on the phone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen

from ..core.easing import smootherstep, smoothstep
from ..i18n import tr

W, H = 1920.0, 1080.0
STEP_S = 2.8          # one phone step
TRANSFER_S = 4.2
IMPORT_S = 5.4
OUTRO_S = 2.6


@dataclass
class Palette:
    bg0: str
    bg1: str
    panel: str
    text: str
    text2: str
    accent: str
    accent2: str
    ok: str
    device: str
    screen: str
    row: str
    border: str

    @classmethod
    def dark(cls) -> "Palette":
        return cls("#141c33", "#080c18", "#111729", "#e8ecf4", "#9ea9bf", "#2f8bff", "#2fd6fb", "#5fcf87",
                   "#1b2336", "#f7f8fb", "#ffffff", "#dfe4ee")

    @classmethod
    def light(cls) -> "Palette":
        return cls("#f3f6fb", "#e6ecf6", "#ffffff", "#111827", "#4a5466", "#1463e6", "#12a8d8", "#1d7a3c",
                   "#1b2336", "#f7f8fb", "#ffffff", "#dfe4ee")


@dataclass
class Screen:
    kind: str                         # home | list | dialog | save | map | share | timeline
    title: str = ""
    rows: List[str] = field(default_factory=list)
    target: int = 0                   # index of the row/button/app that gets tapped
    toggles: Sequence[int] = ()       # rows drawn with an "on" switch
    note: str = ""


def phone_screens(platform: str) -> List[Tuple[Screen, str]]:
    """(screen, caption) per step, localised."""
    if platform == "android":
        return [
            (Screen("home", rows=[tr("tut.app.phone"), tr("tut.app.messages"), tr("tut.app.camera"),
                                  tr("tut.app.maps"), tr("tut.a.settings"), tr("tut.app.photos")], target=4),
             tr("tut.a.step1")),
            (Screen("list", tr("tut.a.settings"), [tr("tut.a.network"), tr("tut.a.devices"), tr("tut.a.apps"),
                                                   tr("tut.a.location"), tr("tut.a.security")], target=3),
             tr("tut.a.step2")),
            (Screen("list", tr("tut.a.location"), [tr("tut.a.use_location"), tr("tut.a.app_perm"),
                                                   tr("tut.a.loc_services")], target=2, toggles=(0,)),
             tr("tut.a.step3")),
            (Screen("list", tr("tut.a.loc_services"), [tr("tut.a.emergency"), tr("tut.a.accuracy"),
                                                       tr("tut.a.timeline")], target=2),
             tr("tut.a.step4")),
            (Screen("list", tr("tut.a.timeline"), [tr("tut.a.timeline"), tr("tut.a.autodelete"),
                                                   tr("tut.a.backup"), tr("tut.export")], target=3,
                    toggles=(0,)),
             tr("tut.a.step5")),
            (Screen("dialog", tr("tut.export_q"), [tr("tut.cancel"), tr("tut.continue")], target=1,
                    note=tr("tut.export_body")),
             tr("tut.a.step6")),
            (Screen("save", tr("tut.save_title"), [tr("tut.cancel"), tr("tut.save")], target=1,
                    note="Timeline.json"),
             tr("tut.a.step7")),
        ]
    return [
        (Screen("home", rows=[tr("tut.app.messages"), tr("tut.app.photos"), tr("tut.app.camera"),
                              tr("tut.i.files"), tr("tut.app.maps"), tr("tut.app.settings_ios")], target=4),
         tr("tut.i.step1")),
        (Screen("map", tr("tut.app.maps")), tr("tut.i.step2")),
        (Screen("list", tr("tut.i.account"), [tr("tut.i.your_timeline"), tr("tut.i.your_data"),
                                              tr("tut.i.settings"), tr("tut.i.help")], target=0),
         tr("tut.i.step3")),
        (Screen("timeline", tr("tut.i.your_timeline")), tr("tut.i.step4")),
        (Screen("list", tr("tut.i.your_timeline"), [tr("tut.i.privacy"), tr("tut.i.show_places")], target=0),
         tr("tut.i.step5")),
        (Screen("list", tr("tut.i.privacy"), [tr("tut.a.timeline"), tr("tut.a.autodelete"), tr("tut.export")],
                target=2, toggles=(0,)),
         tr("tut.i.step6")),
        (Screen("share", "location-history.json", [tr("tut.i.airdrop"), tr("tut.i.save_files"),
                                                   tr("tut.i.mail")], target=1),
         tr("tut.i.step7")),
    ]


def total_duration(platform: str) -> float:
    return len(phone_screens(platform)) * STEP_S + TRANSFER_S + IMPORT_S + OUTRO_S


def step_list(platform: str) -> List[str]:
    """Captions for the side panel: phone steps + transfer + import."""
    return [c for _, c in phone_screens(platform)] + [tr("tut.step_transfer"), tr("tut.step_import")]


def step_starts(platform: str) -> List[float]:
    n = len(phone_screens(platform))
    out = [i * STEP_S for i in range(n)]
    out.append(n * STEP_S)
    out.append(n * STEP_S + TRANSFER_S)
    return out


# ============================================================== drawing helpers
def _c(hexs: str, a: float = 1.0) -> QColor:
    c = QColor(hexs)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def _font(family: str, px: float, bold: bool = False) -> QFont:
    from ..rendering.qt import families
    f = QFont()
    f.setFamilies([family] + families("ui")[1:])
    f.setPixelSize(max(6, int(round(px))))
    f.setBold(bold)
    return f


def _rrect(p: QPainter, r: QRectF, rad: float, fill: QColor, pen: Optional[QPen] = None):
    path = QPainterPath()
    path.addRoundedRect(r, rad, rad)
    p.setPen(pen if pen is not None else Qt.NoPen)
    p.setBrush(fill)
    p.drawPath(path)


def _ripple(p: QPainter, c: QPointF, k: float, color: str):
    """Tap feedback: a fingertip dot that presses (k 0…1) and a ring that expands."""
    if k <= 0:
        return
    r = 26 + 40 * smoothstep(k)
    p.setPen(QPen(_c(color, 0.55 * (1 - k)), 4))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(c, r, r)


def _finger(p: QPainter, c: QPointF, press: float, alpha: float):
    if alpha <= 0:
        return
    s = 1.0 - 0.18 * press
    p.setPen(QPen(_c("#ffffff", 0.9 * alpha), 3))
    p.setBrush(_c("#0b1020", 0.55 * alpha))
    p.drawEllipse(c, 24 * s, 24 * s)


# ============================================================== the scene
class TutorialScene:
    def __init__(self, platform: str = "android", palette: Optional[Palette] = None, font_family: str = "",
                 rtl: bool = False):
        self.platform = platform
        self.pal = palette or Palette.dark()
        self.ff = font_family
        self.rtl = rtl
        self.screens = phone_screens(platform)
        self.steps = step_list(platform)
        self.starts = step_starts(platform)
        self.duration = total_duration(platform)

    def step_at(self, t: float) -> int:
        k = 0
        for i, s in enumerate(self.starts):
            if t >= s:
                k = i
        return k

    # ---------------------------------------------------------------- frame
    def draw(self, p: QPainter, t: float, width: float, height: float):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        k = min(width / W, height / H)
        ox, oy = (width - W * k) / 2, (height - H * k) / 2
        p.fillRect(QRectF(0, 0, width, height), QColor(self.pal.bg1))
        p.translate(ox, oy)
        p.scale(k, k)
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0, QColor(self.pal.bg0))
        g.setColorAt(1, QColor(self.pal.bg1))
        p.fillRect(QRectF(0, 0, W, H), g)
        self._panel(p, t)
        n = len(self.screens)
        phone_end = n * STEP_S
        if t < phone_end:
            self._phone_stage(p, t)
        elif t < phone_end + TRANSFER_S:
            self._transfer(p, (t - phone_end) / TRANSFER_S)
        elif t < phone_end + TRANSFER_S + IMPORT_S:
            self._import(p, (t - phone_end - TRANSFER_S) / IMPORT_S)
        else:
            self._outro(p, min(1.0, (t - phone_end - TRANSFER_S - IMPORT_S) / OUTRO_S))
        p.restore()

    # ---------------------------------------------------------- side panel
    def _panel(self, p: QPainter, t: float):
        pal = self.pal
        x0, y0 = 96.0, 120.0
        p.setPen(QColor(pal.accent2))
        p.setFont(_font(self.ff, 22, True))
        p.drawText(QRectF(x0, y0 - 56, 700, 30), Qt.AlignLeft | Qt.AlignVCenter,
                   tr("tut.platform.android" if self.platform == "android" else "tut.platform.iphone").upper())
        p.setPen(QColor(pal.text))
        p.setFont(_font(self.ff, 50, True))
        p.drawText(QRectF(x0, y0 - 20, 760, 130), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                   tr("tut.headline"))
        cur = self.step_at(t)
        y = y0 + 150
        row_h = 68.0
        for i, cap in enumerate(self.steps):
            active = i == cur
            done = i < cur
            circle = QPointF(x0 + 22, y + row_h / 2)
            p.setPen(Qt.NoPen)
            p.setBrush(_c(pal.accent if (active or done) else pal.text2, 1.0 if active or done else 0.35))
            p.drawEllipse(circle, 20, 20)
            p.setPen(QColor("#ffffff"))
            p.setFont(_font(self.ff, 19, True))
            p.drawText(QRectF(circle.x() - 20, circle.y() - 20, 40, 40), Qt.AlignCenter,
                       "✓" if done else str(i + 1))
            p.setPen(_c(pal.text if active else pal.text2, 1.0 if active else 0.8))
            p.setFont(_font(self.ff, 24 if active else 21, active))
            p.drawText(QRectF(x0 + 60, y, 640, row_h), Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, cap)
            if i < len(self.steps) - 1:
                p.setPen(QPen(_c(pal.text2, 0.25), 2))
                p.drawLine(QPointF(circle.x(), circle.y() + 22), QPointF(circle.x(), circle.y() + row_h - 22))
            y += row_h
        # progress bar
        prog = max(0.0, min(1.0, t / self.duration))
        _rrect(p, QRectF(x0, H - 110, 680, 8), 4, _c(pal.text2, 0.2))
        _rrect(p, QRectF(x0, H - 110, 680 * prog, 8), 4, QColor(pal.accent))

    # ------------------------------------------------------------ the phone
    def _phone_rect(self, scale: float = 1.0, cx: float = 1340.0, cy: float = 540.0) -> QRectF:
        w, h = 420 * scale, 860 * scale
        return QRectF(cx - w / 2, cy - h / 2, w, h)

    def _phone_frame(self, p: QPainter, r: QRectF):
        pal = self.pal
        shadow = QRectF(r.adjusted(10, 24, 10, 24))
        _rrect(p, shadow, 64 * r.width() / 420, _c("#000000", 0.35))
        _rrect(p, r, 64 * r.width() / 420, QColor(pal.device))
        s = r.width() / 420
        screen = r.adjusted(16 * s, 16 * s, -16 * s, -16 * s)
        _rrect(p, screen, 50 * s, QColor(pal.screen))
        return screen

    def _phone_stage(self, p: QPainter, t: float):
        i = min(len(self.screens) - 1, int(t // STEP_S))
        lt = t - i * STEP_S
        r = self._phone_rect()
        # phone floats in on the very first step
        if i == 0 and lt < 0.6:
            e = smootherstep(lt / 0.6)
            r = self._phone_rect(0.92 + 0.08 * e, cy=540 + 80 * (1 - e))
            p.setOpacity(e)
        screen = self._phone_frame(p, r)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(screen, 50, 50)
        p.setClipPath(clip)
        # slide transition from the previous screen
        enter = smootherstep(min(1.0, lt / 0.45))
        if i > 0 and enter < 1.0:
            p.save()
            p.translate(-screen.width() * 0.35 * enter, 0)
            p.setOpacity(1.0 - enter)
            prev, _ = self.screens[i - 1]
            self._screen(p, screen, prev, tapped=True)
            p.restore()
            p.translate(screen.width() * (1 - enter), 0)
        scr, _ = self.screens[i]
        target = self._screen(p, screen, scr, tapped=lt > 1.9)
        p.restore()
        p.setOpacity(1.0)
        # pointer: moves in, taps, lifts
        if target is not None:
            start = QPointF(screen.center().x() + 40, screen.bottom() - 60)
            mv = smootherstep((lt - 0.55) / 1.1)
            pos = QPointF(start.x() + (target.x() - start.x()) * mv, start.y() + (target.y() - start.y()) * mv)
            press = smoothstep((lt - 1.7) / 0.18) * (1 - smoothstep((lt - 2.05) / 0.2))
            alpha = smoothstep((lt - 0.45) / 0.25) * (1 - smoothstep((lt - 2.45) / 0.3))
            _ripple(p, target, max(0.0, min(1.0, (lt - 1.8) / 0.6)) if lt > 1.8 else 0.0, self.pal.accent)
            _finger(p, pos, press, alpha)

    def _status_bar(self, p: QPainter, s: QRectF, dark: bool = False):
        col = "#ffffff" if dark else "#111827"
        p.setPen(QColor(col))
        p.setFont(_font(self.ff, 17, True))
        p.drawText(QRectF(s.left() + 30, s.top() + 14, 120, 28), Qt.AlignLeft | Qt.AlignVCenter, "9:41")
        p.setBrush(QColor(col))
        p.setPen(Qt.NoPen)
        for k in range(3):
            p.drawRoundedRect(QRectF(s.right() - 90 + k * 10, s.top() + 30 - k * 4, 6, 8 + k * 4), 1, 1)
        p.drawRoundedRect(QRectF(s.right() - 52, s.top() + 20, 30, 14), 3, 3)

    def _screen(self, p: QPainter, s: QRectF, scr: Screen, tapped: bool) -> Optional[QPointF]:
        """Draws one phone screen; returns the tap target point."""
        pal = self.pal
        if scr.kind == "home":
            g = QLinearGradient(s.topLeft(), s.bottomRight())
            g.setColorAt(0, QColor("#3b5bdb"))
            g.setColorAt(1, QColor("#0c8599"))
            p.fillRect(s, g)
            self._status_bar(p, s, dark=True)
            cols, size = 3, 88.0
            gap = (s.width() - cols * size) / (cols + 1)
            tgt = None
            colors = ["#37b24d", "#1c7ed6", "#495057", "#f03e3e", "#868e96", "#f59f00"]
            for k, name in enumerate(scr.rows):
                cx = s.left() + gap + (k % cols) * (size + gap)
                cy = s.top() + 150 + (k // cols) * 170
                rr = QRectF(cx, cy, size, size)
                hot = tapped and k == scr.target
                _rrect(p, rr.adjusted(-4, -4, 4, 4) if hot else rr, 24, QColor(colors[k % len(colors)]))
                self._app_glyph(p, rr, name, k == scr.target)
                p.setPen(QColor("#ffffff"))
                p.setFont(_font(self.ff, 16))
                p.drawText(QRectF(cx - 24, cy + size + 8, size + 48, 26), Qt.AlignHCenter | Qt.AlignTop, name)
                if k == scr.target:
                    tgt = rr.center()
            return tgt
        p.fillRect(s, QColor(pal.screen))
        self._status_bar(p, s)
        if scr.kind == "map":
            m = QRectF(s.left(), s.top() + 60, s.width(), s.height() - 60)
            p.fillRect(m, QColor("#e9efe6"))
            p.setPen(QPen(QColor("#ffffff"), 14))
            for k in range(5):
                p.drawLine(QPointF(m.left(), m.top() + 120 + k * 130), QPointF(m.right(), m.top() + 60 + k * 150))
            p.setPen(QPen(QColor("#f8e08e"), 18))
            p.drawLine(QPointF(m.left() + 60, m.bottom()), QPointF(m.right() - 40, m.top() + 40))
            _rrect(p, QRectF(s.left() + 22, s.top() + 70, s.width() - 110, 58), 29, QColor("#ffffff"),
                   QPen(QColor("#dfe4ee"), 1.5))
            p.setPen(QColor("#6b7280"))
            p.setFont(_font(self.ff, 18))
            p.drawText(QRectF(s.left() + 48, s.top() + 70, s.width() - 170, 58), Qt.AlignVCenter | Qt.AlignLeft,
                       tr("tut.i.search"))
            av = QPointF(s.right() - 52, s.top() + 99)
            p.setPen(QPen(QColor("#ffffff"), 3))
            p.setBrush(QColor("#7048e8"))
            p.drawEllipse(av, 24, 24)
            p.setPen(QColor("#ffffff"))
            p.setFont(_font(self.ff, 20, True))
            p.drawText(QRectF(av.x() - 24, av.y() - 24, 48, 48), Qt.AlignCenter, "A")
            return av
        if scr.kind == "timeline":
            self._title(p, s, scr.title, dots=True)
            day = QRectF(s.left() + 24, s.top() + 150, s.width() - 48, 280)
            _rrect(p, day, 20, QColor("#eef2f8"))
            p.setPen(QPen(QColor(pal.accent), 5, Qt.SolidLine, Qt.RoundCap))
            path = QPainterPath(QPointF(day.left() + 40, day.bottom() - 50))
            path.cubicTo(QPointF(day.left() + 120, day.top() + 40), QPointF(day.right() - 160, day.bottom() - 30),
                         QPointF(day.right() - 40, day.top() + 60))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            for k in range(3):
                _rrect(p, QRectF(s.left() + 24, s.top() + 460 + k * 76, s.width() - 48, 60), 14, QColor("#f1f3f7"))
            return QPointF(s.right() - 44, s.top() + 100)
        if scr.kind in ("list",):
            self._title(p, s, scr.title)
            tgt = None
            for k, row in enumerate(scr.rows):
                rr = QRectF(s.left() + 18, s.top() + 150 + k * 92, s.width() - 36, 78)
                hot = tapped and k == scr.target
                _rrect(p, rr, 18, _c(pal.accent, 0.16) if hot else QColor("#ffffff"),
                       QPen(_c(pal.accent, 0.9), 2) if hot else QPen(QColor("#e5e8ef"), 1.2))
                p.setPen(QColor("#111827"))
                p.setFont(_font(self.ff, 21, k == scr.target))
                p.drawText(rr.adjusted(24, 0, -90, 0), Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap, row)
                if k in scr.toggles:
                    tg = QRectF(rr.right() - 78, rr.center().y() - 16, 56, 32)
                    _rrect(p, tg, 16, QColor(pal.accent))
                    p.setBrush(QColor("#ffffff"))
                    p.drawEllipse(QPointF(tg.right() - 16, tg.center().y()), 12, 12)
                else:
                    p.setPen(QPen(QColor("#9aa3b2"), 3, Qt.SolidLine, Qt.RoundCap))
                    c = QPointF(rr.right() - 34, rr.center().y())
                    p.drawLine(QPointF(c.x() - 6, c.y() - 9), c)
                    p.drawLine(QPointF(c.x() - 6, c.y() + 9), c)
                if k == scr.target:
                    tgt = QPointF(rr.left() + rr.width() * 0.45, rr.center().y())
            return tgt
        if scr.kind in ("dialog", "save", "share"):
            p.fillRect(s, _c("#000000", 0.28))
            if scr.kind == "share":
                box = QRectF(s.left() + 14, s.bottom() - 470, s.width() - 28, 450)
            else:
                box = QRectF(s.left() + 34, s.center().y() - 190, s.width() - 68, 360)
            _rrect(p, box, 28, QColor("#ffffff"))
            p.setPen(QColor("#111827"))
            p.setFont(_font(self.ff, 24, True))
            p.drawText(QRectF(box.left() + 30, box.top() + 26, box.width() - 60, 70),
                       Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, scr.title)
            if scr.kind == "dialog":
                p.setPen(QColor("#4a5466"))
                p.setFont(_font(self.ff, 18))
                p.drawText(QRectF(box.left() + 30, box.top() + 100, box.width() - 60, 150),
                           Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, scr.note)
            elif scr.kind == "save":
                fld = QRectF(box.left() + 30, box.top() + 110, box.width() - 60, 64)
                _rrect(p, fld, 12, QColor("#f3f5f9"), QPen(_c(pal.accent, 0.9), 2))
                self._doc(p, QPointF(fld.left() + 34, fld.center().y()), 0.5)
                p.setPen(QColor("#111827"))
                p.setFont(_font(self.ff, 20, True))
                p.drawText(fld.adjusted(64, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft, scr.note)
                p.setPen(QColor("#6b7280"))
                p.setFont(_font(self.ff, 16))
                p.drawText(QRectF(fld.left(), fld.bottom() + 10, fld.width(), 30), Qt.AlignLeft,
                           tr("tut.downloads"))
            else:
                self._doc(p, QPointF(box.left() + 48, box.top() + 120), 0.7)
                opts = scr.rows
                tgt = None
                for k, o in enumerate(opts):
                    rr = QRectF(box.left() + 20, box.top() + 180 + k * 80, box.width() - 40, 66)
                    hot = tapped and k == scr.target
                    _rrect(p, rr, 14, _c(pal.accent, 0.16) if hot else QColor("#f3f5f9"),
                           QPen(_c(pal.accent, 0.9), 2) if hot else None)
                    p.setPen(QColor("#111827"))
                    p.setFont(_font(self.ff, 20, k == scr.target))
                    p.drawText(rr.adjusted(22, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft, o)
                    if k == scr.target:
                        tgt = rr.center()
                return tgt
            # buttons (dialog / save)
            tgt = None
            bw = 150.0
            for k, label in enumerate(scr.rows):
                br = QRectF(box.right() - 30 - (len(scr.rows) - k) * (bw + 12) + 12, box.bottom() - 90, bw, 60)
                primary = k == scr.target
                hot = tapped and primary
                _rrect(p, br, 30, QColor(pal.accent) if primary else QColor("#ffffff"),
                       None if primary else QPen(QColor("#d6dce6"), 1.5))
                if hot:
                    _rrect(p, br.adjusted(-5, -5, 5, 5), 34, _c(pal.accent, 0.25))
                p.setPen(QColor("#ffffff") if primary else QColor(pal.accent))
                p.setFont(_font(self.ff, 19, True))
                p.drawText(br, Qt.AlignCenter, label)
                if primary:
                    tgt = br.center()
            return tgt
        return None

    def _title(self, p: QPainter, s: QRectF, title: str, dots: bool = False):
        p.setPen(QPen(QColor("#374151"), 3.5, Qt.SolidLine, Qt.RoundCap))
        a = QPointF(s.left() + 34, s.top() + 100)
        p.drawLine(a, QPointF(a.x() + 12, a.y() - 12))
        p.drawLine(a, QPointF(a.x() + 12, a.y() + 12))
        p.setPen(QColor("#111827"))
        p.setFont(_font(self.ff, 26, True))
        p.drawText(QRectF(s.left() + 70, s.top() + 70, s.width() - 150, 60), Qt.AlignVCenter | Qt.AlignLeft, title)
        if dots:
            p.setBrush(QColor("#374151"))
            p.setPen(Qt.NoPen)
            for k in range(3):
                p.drawEllipse(QPointF(s.right() - 44, s.top() + 88 + k * 12), 3.5, 3.5)

    def _app_glyph(self, p: QPainter, r: QRectF, name: str, target: bool):
        p.save()
        p.setPen(QPen(QColor("#ffffff"), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        c = r.center()
        if target and self.platform == "android":                 # gear
            p.drawEllipse(c, 16, 16)
            for k in range(8):
                a = k * math.pi / 4
                p.drawLine(QPointF(c.x() + 20 * math.cos(a), c.y() + 20 * math.sin(a)),
                           QPointF(c.x() + 28 * math.cos(a), c.y() + 28 * math.sin(a)))
        elif target:                                              # map pin
            path = QPainterPath(QPointF(c.x(), c.y() + 28))
            path.cubicTo(QPointF(c.x() - 40, c.y() - 10), QPointF(c.x() - 18, c.y() - 34), QPointF(c.x(), c.y() - 30))
            path.cubicTo(QPointF(c.x() + 18, c.y() - 34), QPointF(c.x() + 40, c.y() - 10), QPointF(c.x(), c.y() + 28))
            p.setBrush(QColor("#ffffff"))
            p.drawPath(path)
            p.setBrush(QColor("#e03131"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(c.x(), c.y() - 8), 9, 9)
        else:
            p.drawRoundedRect(QRectF(c.x() - 18, c.y() - 18, 36, 36), 8, 8)
        p.restore()

    def _doc(self, p: QPainter, c: QPointF, s: float, alpha: float = 1.0):
        """A JSON document glyph."""
        w, h = 64 * s, 80 * s
        r = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
        path = QPainterPath(r.topLeft())
        path.lineTo(r.right() - 18 * s, r.top())
        path.lineTo(r.right(), r.top() + 18 * s)
        path.lineTo(r.bottomRight())
        path.lineTo(r.bottomLeft())
        path.closeSubpath()
        p.setPen(QPen(_c(self.pal.accent, alpha), 3 * max(0.5, s)))
        p.setBrush(_c("#ffffff", alpha))
        p.drawPath(path)
        p.setPen(_c(self.pal.accent, alpha))
        p.setFont(_font(self.ff, 18 * s, True))
        p.drawText(r.adjusted(0, 14 * s, 0, 0), Qt.AlignCenter, "{ }")

    # ------------------------------------------------------------ transfer
    def _laptop(self, p: QPainter, cx: float, cy: float, s: float) -> QRectF:
        pal = self.pal
        sw, sh = 700 * s, 440 * s
        lid = QRectF(cx - sw / 2, cy - sh / 2 - 30 * s, sw, sh)
        _rrect(p, lid.adjusted(8, 18, 8, 18), 22 * s, _c("#000000", 0.3))
        _rrect(p, lid, 22 * s, QColor(pal.device))
        screen = lid.adjusted(18 * s, 18 * s, -18 * s, -18 * s)
        base = QRectF(cx - sw * 0.62, lid.bottom(), sw * 1.24, 30 * s)
        path = QPainterPath()
        path.moveTo(base.left() + 30 * s, base.top())
        path.lineTo(base.right() - 30 * s, base.top())
        path.lineTo(base.right(), base.bottom())
        path.lineTo(base.left(), base.bottom())
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2a3550"))
        p.drawPath(path)
        return screen

    def _transfer(self, p: QPainter, u: float):
        pal = self.pal
        e = smootherstep(min(1.0, u / 0.25))
        # phone shrinks to the left, laptop rises on the right
        pr = self._phone_rect(1.0 - 0.45 * e, cx=1340 - 340 * e, cy=540 + 60 * e)
        scr = self._phone_frame(p, pr)
        p.fillRect(scr.adjusted(8, 8, -8, -8), QColor(pal.screen))
        self._doc(p, scr.center(), 0.9 * (1 - 0.45 * e), 1.0 - smoothstep((u - 0.3) / 0.1))
        p.setOpacity(e)
        lap = self._laptop(p, 1500, 560 + 40 * (1 - e), 0.78)
        _rrect(p, lap, 10, QColor(pal.panel))
        p.setOpacity(1.0)
        # the file flies along an arc (on-brand: like a flight in the video)
        f = smootherstep(max(0.0, min(1.0, (u - 0.32) / 0.5)))
        a = scr.center()
        b = QPointF(lap.center().x(), lap.center().y())
        c = QPointF((a.x() + b.x()) / 2, min(a.y(), b.y()) - 260)
        path = QPainterPath(a)
        path.quadTo(c, b)
        if f > 0:
            dash = QPen(_c(pal.accent2, 0.9), 4, Qt.DashLine, Qt.RoundCap)
            p.setPen(dash)
            p.setBrush(Qt.NoBrush)
            sub = QPainterPath(a)
            n = 40
            for k in range(1, int(n * f) + 1):
                sub.lineTo(path.pointAtPercent(k / n))
            p.drawPath(sub)
            pt = path.pointAtPercent(f)
            self._doc(p, pt, 0.8)
        # transfer options
        p.setFont(_font(self.ff, 22, True))
        opts = [tr("tut.via.usb"), tr("tut.via.share"), tr("tut.via.cloud"), tr("tut.via.email")]
        x = 880.0
        for k, o in enumerate(opts):
            al = smoothstep((u - 0.45 - k * 0.07) / 0.15)
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(o) + 44
            rr = QRectF(x, 930, w, 52)
            p.setOpacity(al)
            _rrect(p, rr, 26, QColor(pal.panel), QPen(_c(pal.accent, 0.8), 2))
            p.setPen(QColor(pal.text))
            p.drawText(rr, Qt.AlignCenter, o)
            p.setOpacity(1.0)
            x += w + 16

    # -------------------------------------------------------------- import
    def _mini_app(self, p: QPainter, s: QRectF):
        pal = self.pal
        _rrect(p, s, 8, QColor(pal.panel))
        side = QRectF(s.left(), s.top(), s.width() * 0.2, s.height())
        p.fillRect(side, _c(pal.text2, 0.1))
        from .icons import brand_image
        im = brand_image(pal.panel.lower() not in ("#ffffff",))
        if not im.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            ratio = im.height() / max(1, im.width())
            p.drawImage(QRectF(side.left() + 12, side.top() + 18, side.width() - 24, (side.width() - 24) * ratio),
                        im, QRectF(im.rect()))
        for k in range(8):
            _rrect(p, QRectF(side.left() + 14, side.top() + 70 + k * 30, side.width() * (0.5 + 0.06 * (k % 3)), 12),
                   6, _c(pal.accent if k == 1 else pal.text2, 0.7 if k == 1 else 0.25))
        return QRectF(side.right() + 24, s.top() + 20, s.width() - side.width() - 48, s.height() - 40)

    def _import(self, p: QPainter, u: float):
        pal = self.pal
        lap = self._laptop(p, 1340, 520, 1.0)
        body = self._mini_app(p, lap)
        p.setPen(QColor(pal.text))
        p.setFont(_font(self.ff, 26, True))
        p.drawText(QRectF(body.left(), body.top(), body.width(), 40), Qt.AlignLeft, tr("ui.import.title"))
        zone = QRectF(body.left(), body.top() + 56, body.width(), body.height() * 0.5)
        drop = smoothstep((u - 0.18) / 0.12)
        pen = QPen(_c(pal.accent, 0.5 + 0.5 * drop), 3, Qt.DashLine)
        _rrect(p, zone, 16, _c(pal.accent, 0.05 + 0.12 * drop), pen)
        p.setPen(QColor(pal.text2))
        p.setFont(_font(self.ff, 20))
        if u < 0.3:
            p.drawText(zone.adjusted(20, 0, -20, 0), Qt.AlignCenter | Qt.TextWordWrap, tr("ui.import.drop"))
        # the file drops in
        fall = smootherstep(min(1.0, u / 0.28))
        doc_c = QPointF(zone.center().x(), zone.top() - 260 * (1 - fall) + zone.height() * 0.42 * fall)
        if u < 0.36:
            self._doc(p, doc_c, 1.0, 1.0 - smoothstep((u - 0.3) / 0.06))
        # parsing progress and result
        prog = smoothstep((u - 0.34) / 0.3)
        if u >= 0.3:
            bar = QRectF(zone.left() + 40, zone.center().y() - 10, zone.width() - 80, 20)
            _rrect(p, bar, 10, _c(pal.text2, 0.2))
            _rrect(p, QRectF(bar.left(), bar.top(), bar.width() * prog, bar.height()), 10, QColor(pal.accent))
            p.setPen(QColor(pal.text))
            p.setFont(_font(self.ff, 20, True))
            label = tr("tut.parsing") if prog < 1.0 else tr("ui.import.ok")
            p.drawText(QRectF(bar.left(), bar.top() - 50, bar.width(), 36), Qt.AlignLeft | Qt.AlignVCenter,
                       ("✓  " if prog >= 1.0 else "") + label)
        # a route sketch draws itself below
        g = smoothstep((u - 0.66) / 0.3)
        if g > 0:
            area = QRectF(zone.left(), zone.bottom() + 24, zone.width(), body.bottom() - zone.bottom() - 24)
            _rrect(p, area, 14, _c(pal.text2, 0.08))
            pts = [(0.08, 0.8), (0.22, 0.62), (0.3, 0.7), (0.45, 0.3), (0.62, 0.45), (0.74, 0.2), (0.9, 0.35)]
            path = QPainterPath(QPointF(area.left() + pts[0][0] * area.width(), area.top() + pts[0][1] * area.height()))
            for x, y in pts[1:]:
                path.lineTo(area.left() + x * area.width(), area.top() + y * area.height())
            p.setPen(QPen(QColor(pal.accent), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            sub = QPainterPath(path.pointAtPercent(0))
            for k in range(1, int(60 * g) + 1):
                sub.lineTo(path.pointAtPercent(k / 60))
            p.drawPath(sub)
            end = path.pointAtPercent(g)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent2))
            p.drawEllipse(end, 8, 8)

    def _outro(self, p: QPainter, u: float):
        pal = self.pal
        from .icons import brand_image
        e = smootherstep(min(1.0, u / 0.5))
        im = brand_image(pal.bg0.lower() not in ("#f3f6fb",))
        p.setOpacity(e)
        if not im.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            w = 620 * (0.94 + 0.06 * e)
            h = w * im.height() / max(1, im.width())
            p.drawImage(QRectF(1340 - w / 2, 430 - h / 2, w, h), im, QRectF(im.rect()))
        p.setPen(QColor(pal.text))
        p.setFont(_font(self.ff, 34, True))
        p.drawText(QRectF(900, 560, 880, 60), Qt.AlignCenter, tr("tut.done"))
        p.setPen(QColor(pal.text2))
        p.setFont(_font(self.ff, 22))
        p.drawText(QRectF(900, 630, 880, 90), Qt.AlignCenter | Qt.TextWordWrap, tr("tut.done_sub"))
        p.setOpacity(1.0)

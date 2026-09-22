"""Journey sketch: a live, tile-free drawing of the selected route.

Local episodes are drawn quietly, long trips (as the current Long-trip detection
sees them) in the accent colour, flights as dashed arcs with a small plane at
their apex. It redraws instantly when the detection level changes, so the
effect of a setting is visible before anything is rendered.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import tr
from ..journeys.journey import Journey, build_legs, transfer_threshold_km
from ..timeline.modes import Mode


class JourneySketch(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName(tr("ui.journey.sketch"))
        self._j: Optional[Journey] = None
        self._detection = "balanced"

    def sizeHint(self) -> QSize:
        return QSize(560, 300)

    def set_journey(self, j: Optional[Journey], detection: str = "balanced") -> None:
        self._j = j
        self._detection = detection
        self.update()

    def set_detection(self, detection: str) -> None:
        self._detection = detection
        self.update()

    # ---------------------------------------------------------------- paint
    def paintEvent(self, _e):
        from .main_window import current_tokens
        t = current_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.fillPath(path, QColor(t.surface_2))
        j = self._j
        if j is None or len(j.cum_km) < 2:
            p.setPen(QColor(t.text_2))
            p.drawText(r, Qt.AlignCenter, tr("ui.journey.sketch_empty"))
            p.end()
            return
        xs = np.asarray(j.xs)
        ys = np.asarray(j.ys)
        # include flight arc apexes in the bounds
        ex, ey = [xs], [ys]
        for arc in j.arcs.values():
            ax, ay = arc.at(np.linspace(0, 1, 9))
            ex.append(np.asarray(ax))
            ey.append(np.asarray(ay))
        allx = np.concatenate(ex)
        ally = np.concatenate(ey)
        mnx, mxx = float(allx.min()), float(allx.max())
        mny, mxy = float(ally.min()), float(ally.max())
        pad = 22
        w = max(1.0, r.width() - 2 * pad)
        h = max(1.0, r.height() - 2 * pad - 18)
        sx = max(mxx - mnx, 1.0)
        sy = max(mxy - mny, 1.0)
        k = min(w / sx, h / sy)
        ox = r.left() + pad + (w - sx * k) / 2
        oy = r.top() + pad + (h - sy * k) / 2

        def P(x, y):
            return QPointF(ox + (x - mnx) * k, oy + (mxy - y) * k)

        flights = np.asarray(j.hop_mode) == Mode.FLIGHT
        legs = build_legs(j.cum_km, transfer_threshold_km(j.cum_km, self._detection), flights)
        cum = np.asarray(j.cum_km)
        long_hop = np.zeros(len(cum), bool)
        for a, b, is_t in legs:
            if is_t:
                long_hop |= (cum > a + 1e-9) & (cum <= b + 1e-9)
        muted = QColor(t.text_2)
        muted.setAlphaF(0.55)
        accent = QColor(t.accent)
        # local episodes first, long trips on top
        for want_long in (False, True):
            pen = QPen(accent if want_long else muted, 2.2 if want_long else 1.3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            run = QPainterPath()
            started = False
            for i in range(1, len(cum)):
                is_long = bool(long_hop[i])
                if is_long != want_long or i in j.arcs:
                    started = False
                    continue
                a, b = P(xs[i - 1], ys[i - 1]), P(xs[i], ys[i])
                if not started:
                    run.moveTo(a)
                    started = True
                run.lineTo(b)
            p.drawPath(run)
        # flight arcs
        pen = QPen(accent, 1.8)
        pen.setDashPattern([3.0, 2.4])
        for i, arc in j.arcs.items():
            fx, fy = arc.at(np.linspace(0, 1, 40))
            pa = QPainterPath(P(fx[0], fy[0]))
            for x, y in zip(fx[1:], fy[1:]):
                pa.lineTo(P(x, y))
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(pa)
            mx, my = arc.at(0.5)
            dx, dy = arc.tangent(0.5)
            self._plane(p, P(float(mx), float(my)), math.atan2(-dy, dx), accent, QColor(t.surface))
        # endpoints
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.ok))
        p.drawEllipse(P(xs[0], ys[0]), 4.5, 4.5)
        p.setBrush(QColor(t.danger))
        p.drawEllipse(P(xs[-1], ys[-1]), 4.5, 4.5)
        # legend
        n_long = sum(1 for leg in legs if leg[2])
        p.setPen(QColor(t.text_2))
        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)
        p.drawText(QRectF(r.left() + 12, r.bottom() - 22, r.width() - 24, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   tr("ui.journey.sketch_legend", trips=n_long, flights=len(j.arcs)))
        p.end()

    @staticmethod
    def _plane(p: QPainter, c: QPointF, heading: float, fill: QColor, outline: QColor):
        from ..rendering.frame_renderer import PLANE_OUTLINE
        L = 9.0
        ca, sa = math.cos(heading), math.sin(heading)
        path = QPainterPath()
        for k, (x, y) in enumerate(PLANE_OUTLINE):
            pt = QPointF(c.x() + (x * ca - y * sa) * L, c.y() + (x * sa + y * ca) * L)
            if k == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()
        p.setPen(QPen(outline, 1.2))
        p.setBrush(fill)
        p.drawPath(path)

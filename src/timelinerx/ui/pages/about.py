"""About TimelinerX: app information, an interactive "how it works" flowchart and credits."""

from __future__ import annotations

import math
import platform
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from ... import (APP_NAME, AUTHOR_WORKSPACE, GITHUB_URL, LINKEDIN_URL, RENDER_ENGINE_VERSION,
                 __version__)
from ...i18n import tr
from ...utils.paths import data_dir, logs_dir
from ..icons import brand_pixmap, icon
from ..widgets import Card, ScrollPage, muted, page_header


@dataclass
class FlowNode:
    key: str
    icon: str
    page: Optional[str]        # page to open from the details panel
    col: float                 # grid position (column, row); row 0 = main line
    row: float
    optional: bool = False


NODES: List[FlowNode] = [
    FlowNode("import", "import", "import", 0, 0),
    FlowNode("repair", "repair", "import", 0.5, -1, optional=True),
    FlowNode("journey", "journey", "journey", 1, 0),
    FlowNode("camera", "preview", "journey", 2, 0),
    FlowNode("preview", "play", "preview", 2.5, 1, optional=True),
    FlowNode("tiles", "visual", "visual", 3, 0),
    FlowNode("frames", "video", "video", 4, 0),
    FlowNode("encode", "queue", "queue", 5, 0),
    FlowNode("verify", "ok", "library", 6, 0),
]
EDGES: List[Tuple[str, str, bool]] = [          # (from, to, main line)
    ("import", "journey", True), ("import", "repair", False), ("repair", "journey", False),
    ("journey", "camera", True), ("camera", "preview", False), ("camera", "tiles", True),
    ("tiles", "frames", True), ("frames", "encode", True), ("encode", "verify", True),
]


class FlowChart(QWidget):
    """Interactive pipeline diagram. Hover highlights a stage and its connections, click (or
    Tab/arrow keys + Enter) selects it; animated pulses show data moving along the main line
    (static when reduced motion is on)."""

    selected = Signal(str)

    def __init__(self, reduced_motion: bool = False, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr("ui.about.flow"))
        self.hover: Optional[str] = None
        self.sel: str = "import"
        self._rects = {}
        self._phase = 0.0
        self.reduced = reduced_motion
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def sizeHint(self) -> QSize:
        return QSize(1000, 320)

    def showEvent(self, e):
        if not self.reduced:
            self._timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def _tick(self):
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    # ------------------------------------------------------------ geometry
    def _layout(self):
        W, H = self.width(), self.height()
        nw, nh = min(118.0, (W - 40) / 8.2), 64.0
        x0 = 20 + nw / 2
        dx = (W - 40 - nw) / 6.0
        yc = H / 2 + 6
        dy = 94.0
        self._rects = {}
        for n in NODES:
            cx = x0 + n.col * dx
            cy = yc + n.row * dy
            self._rects[n.key] = QRectF(cx - nw / 2, cy - nh / 2, nw, nh)

    def _node_at(self, pos) -> Optional[str]:
        for k, r in self._rects.items():
            if r.contains(pos):
                return k
        return None

    # ------------------------------------------------------------- events
    def mouseMoveEvent(self, e):
        k = self._node_at(e.position())
        if k != self.hover:
            self.hover = k
            self.setCursor(Qt.PointingHandCursor if k else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        k = self._node_at(e.position())
        if k:
            self.select(k)

    def keyPressEvent(self, e):
        order = [n.key for n in sorted(NODES, key=lambda n: (n.col, n.row))]
        i = order.index(self.sel)
        if e.key() in (Qt.Key_Right, Qt.Key_Down):
            self.select(order[min(len(order) - 1, i + 1)])
        elif e.key() in (Qt.Key_Left, Qt.Key_Up):
            self.select(order[max(0, i - 1)])
        else:
            super().keyPressEvent(e)

    def select(self, key: str):
        self.sel = key
        self.setAccessibleDescription(tr(f"ui.about.node.{key}"))
        self.update()
        self.selected.emit(key)

    # -------------------------------------------------------------- paint
    def paintEvent(self, _e):
        from ..main_window import current_tokens
        t = current_tokens()
        self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        focus = self.hover or self.sel
        linked = {focus}
        for a, b, _ in EDGES:
            if focus in (a, b):
                linked |= {a, b}
        accent = QColor(t.accent)
        line = QColor(t.border)
        # edges
        paths = {}
        rows = {n.key: n.row for n in NODES}
        for a, b, main in EDGES:
            ra, rb = self._rects[a], self._rects[b]
            off = ra.width() * 0.2
            if rows[a] == rows[b]:
                s, e, kind = QPointF(ra.right(), ra.center().y()), QPointF(rb.left(), rb.center().y()), "h"
            elif rows[b] < rows[a]:                                   # up into a branch
                s, e, kind = QPointF(ra.center().x() + off, ra.top()), QPointF(rb.left(), rb.center().y()), "vh"
            elif rows[a] < 0:                                         # from the upper branch back down
                s, e, kind = QPointF(ra.right(), ra.center().y()), QPointF(rb.center().x() - off, rb.top()), "hv"
            else:                                                     # down into a branch
                s, e, kind = QPointF(ra.center().x() + off, ra.bottom()), QPointF(rb.left(), rb.center().y()), "vh"
            path = QPainterPath(s)
            if kind == "h":
                path.lineTo(e)
            elif kind == "vh":
                c = QPointF(s.x(), e.y())
                path.cubicTo(c, c, e)
            else:
                c = QPointF(e.x(), s.y())
                path.cubicTo(c, c, e)
            paths[(a, b)] = path
            hot = a in linked and b in linked and focus in (a, b)
            pen = QPen(accent if hot else line, 2.4 if hot else 1.6)
            if not main:
                pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            # arrowhead
            ang = path.angleAtPercent(0.999)
            rad = math.radians(-ang)
            tip = path.pointAtPercent(1.0)
            p.setBrush(accent if hot else line)
            p.setPen(Qt.NoPen)
            ah = QPainterPath(tip)
            ah.lineTo(tip.x() - 8 * math.cos(rad) + 4 * math.sin(rad), tip.y() - 8 * math.sin(rad) - 4 * math.cos(rad))
            ah.lineTo(tip.x() - 8 * math.cos(rad) - 4 * math.sin(rad), tip.y() - 8 * math.sin(rad) + 4 * math.cos(rad))
            ah.closeSubpath()
            p.drawPath(ah)
        # data pulses on the main line
        if not self.reduced:
            main_edges = [(a, b) for a, b, m in EDGES if m]
            for k, (a, b) in enumerate(main_edges):
                ph = (self._phase * len(main_edges) - k) % len(main_edges)
                if 0 <= ph < 1:
                    pt = paths[(a, b)].pointAtPercent(ph)
                    glow = QColor(t.accent)
                    for rr, al in ((9, 0.18), (5, 0.45), (3, 1.0)):
                        glow.setAlphaF(al)
                        p.setBrush(glow)
                        p.drawEllipse(pt, rr, rr)
        # nodes
        f = QFont(self.font())
        f.setPixelSize(12)
        fb = QFont(f)
        fb.setBold(True)
        for n in NODES:
            r = self._rects[n.key]
            is_sel = n.key == self.sel
            is_hot = n.key == self.hover or n.key in linked
            bg = QColor(t.accent_soft) if is_sel else QColor(t.surface)
            border = QColor(t.accent) if (is_sel or is_hot) else QColor(t.border)
            path = QPainterPath()
            path.addRoundedRect(r, 12, 12)
            if is_sel:
                sh = QColor(t.accent)
                sh.setAlphaF(0.18)
                p.fillPath(path.translated(0, 3), sh)
            pen = QPen(border, 2.0 if is_sel else 1.3)
            if n.optional:
                pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.setBrush(bg)
            p.drawPath(path)
            ic = icon(n.icon, t.accent if (is_sel or is_hot) else t.text_2, 20)
            ic.paint(p, int(r.center().x() - 10), int(r.top() + 9), 20, 20)
            p.setPen(QColor(t.text))
            p.setFont(fb if is_sel else f)
            p.drawText(QRectF(r.left() + 4, r.top() + 31, r.width() - 8, r.height() - 33),
                       Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, tr(f"ui.about.node.{n.key}"))
        if self.hasFocus():
            r = self._rects[self.sel].adjusted(-4, -4, 4, 4)
            pen = QPen(QColor(t.focus), 2)
            pen.setDashPattern([2, 2])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 14, 14)
        p.end()


class AboutPage(ScrollPage):
    def __init__(self, state, reduced_motion: bool = False):
        super().__init__()
        self.state = state
        self.lay.addWidget(page_header(tr("ui.about.title"), tr("ui.about.subtitle")))

        hero = Card()
        hl = QHBoxLayout()
        self.logo = QLabel()
        self.logo.setAccessibleName(APP_NAME)
        hl.addWidget(self.logo, 0, Qt.AlignVCenter)
        hl.addSpacing(18)
        info = QVBoxLayout()
        name = QLabel(tr("ui.about.tagline"))
        name.setObjectName("SectionTitle")
        name.setWordWrap(True)
        info.addWidget(name)
        info.addWidget(muted(tr("ui.about.version", version=__version__, engine=RENDER_ENGINE_VERSION)))
        info.addWidget(muted(tr("ui.about.privacy")))
        hl.addLayout(info, 1)
        hero.add(hl)
        self.lay.addWidget(hero)

        flow = Card(tr("ui.about.flow"))
        flow.add(muted(tr("ui.about.flow_note")))
        self.chart = FlowChart(reduced_motion)
        flow.add(self.chart)
        det = QFrameLike()
        self.d_title = QLabel("")
        self.d_title.setObjectName("SectionTitle")
        self.d_body = muted("")
        self.d_body.setTextFormat(Qt.PlainText)
        self.d_meta = muted("")
        drow = QHBoxLayout()
        drow.addWidget(self.d_meta, 1)
        self.d_open = QPushButton("")
        self.d_open.clicked.connect(self._open_page)
        drow.addWidget(self.d_open)
        det.lay.addWidget(self.d_title)
        det.lay.addWidget(self.d_body)
        det.lay.addLayout(drow)
        flow.add(det)
        self.lay.addWidget(flow)
        self.chart.selected.connect(self._show)

        sysc = Card(tr("ui.about.system"))
        g = QGridLayout()
        g.setHorizontalSpacing(24)
        from PySide6 import __version__ as pyside_version
        rows = [(tr("ui.about.row.version"), f"{__version__}"),
                (tr("ui.about.row.engine"), RENDER_ENGINE_VERSION),
                (tr("ui.about.row.platform"), f"{platform.system()} {platform.release()}"),
                (tr("ui.about.row.runtime"), f"Python {platform.python_version()} · Qt/PySide {pyside_version}"),
                (tr("ui.about.row.license"), tr("ui.about.license"))]
        for i, (k, v) in enumerate(rows):
            kl = QLabel(k)
            kl.setProperty("muted", "true")
            vl = QLabel(v)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(kl, i, 0)
            g.addWidget(vl, i, 1)
        g.setColumnStretch(1, 1)
        sysc.add(g)
        brow = QHBoxLayout()
        b1 = QPushButton(tr("ui.settings.open_data"))
        b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        b2 = QPushButton(tr("ui.settings.open_logs"))
        b2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir()))))
        b3 = QPushButton(tr("ui.tutorial.open"))
        b3.clicked.connect(lambda: state.navigate.emit("tutorial"))
        for b in (b1, b2, b3):
            brow.addWidget(b)
        brow.addStretch(1)
        sysc.add(brow)
        self.lay.addWidget(sysc)

        cr = Card(tr("ui.about.credits"))
        cr.add(muted(tr("ui.about.credits_text")))
        self.lay.addWidget(cr)

        made = Card()
        ml = QVBoxLayout()
        ml.setSpacing(10)
        mt = QLabel(tr("ui.about.made_with", workspace=AUTHOR_WORKSPACE))
        mt.setObjectName("MadeWith")
        mt.setAlignment(Qt.AlignHCenter)
        ml.addWidget(mt)
        links = QHBoxLayout()
        links.addStretch(1)
        self.link_buttons = []
        for key, ic, url in (("linkedin", "linkedin", LINKEDIN_URL), ("github", "github", GITHUB_URL)):
            b = QPushButton(tr(f"ui.about.{key}"))
            b.setObjectName("LinkButton")
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(url)
            b.setAccessibleName(f"{tr(f'ui.about.{key}')} — {url}")
            b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            self.link_buttons.append((b, ic))
            links.addWidget(b)
        links.addStretch(1)
        ml.addLayout(links)
        made.add(ml)
        self.lay.addWidget(made)
        self.finish()
        self.apply_theme()
        self.chart.select("import")

    def apply_theme(self):
        from ..main_window import DARK, current_tokens
        t = current_tokens()
        self.logo.setPixmap(brand_pixmap(t is DARK, 260))
        for b, ic in self.link_buttons:
            b.setIcon(icon(ic, t.accent, 18))
        self.chart.update()

    def _show(self, key: str):
        node = next(n for n in NODES if n.key == key)
        self.d_title.setText(tr(f"ui.about.node.{key}"))
        self.d_body.setText(tr(f"ui.about.detail.{key}"))
        self.d_meta.setText(tr("ui.about.optional") if node.optional else tr("ui.about.local"))
        self._page = node.page
        self.d_open.setText(tr("ui.about.open_page", page=tr(f"ui.nav.{node.page}")))
        self.d_open.setVisible(node.page is not None)

    def _open_page(self):
        if getattr(self, "_page", None):
            self.state.navigate.emit(self._page)


class QFrameLike(QWidget):
    """Plain container with a surface background for the flowchart detail panel."""

    def __init__(self):
        super().__init__()
        self.setObjectName("FlowDetail")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 14)
        self.lay.setSpacing(6)

from __future__ import annotations

from collections import OrderedDict

import numpy as np
from PySide6.QtWidgets import QGridLayout, QHBoxLayout

from ...core.geo import project_route
from ...i18n import month_name, tr
from ..widgets import BarChart, Card, RouteOverview, ScrollPage, StatTile, muted, page_header, primary_button


class AnalysisPage(ScrollPage):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.lay.addWidget(page_header(tr("ui.analysis.title"), tr("ui.analysis.subtitle")))
        stats = Card(tr("ui.analysis.summary"))
        row = QHBoxLayout()
        self.t = {k: StatTile(tr(f"ui.analysis.{k}")) for k in ("points", "days", "range", "months", "format")}
        for w in self.t.values():
            row.addWidget(w)
        stats.add(row)
        self.lay.addWidget(stats)
        grid = QGridLayout()
        grid.setSpacing(16)
        c1 = Card(tr("ui.analysis.activity"))
        self.chart = BarChart()
        c1.add(self.chart)
        grid.addWidget(c1, 0, 0)
        c2 = Card(tr("ui.analysis.overview"))
        self.overview = RouteOverview()
        c2.add(self.overview)
        c2.add(muted(tr("ui.analysis.overview_note")))
        grid.addWidget(c2, 0, 1)
        self.lay.addLayout(grid)
        b = primary_button(tr("ui.analysis.next"))
        b.clicked.connect(lambda: state.navigate.emit("journey"))
        r2 = QHBoxLayout()
        r2.addWidget(b)
        r2.addStretch(1)
        self.lay.addLayout(r2)
        self.finish()
        state.timelineChanged.connect(self.refresh)

    def refresh(self):
        tl = self.state.timeline
        if tl is None:
            return
        cols = tl.semantic if len(tl.semantic) else tl.raw
        days = cols.local_dates()
        rng = tl.date_range()
        self.t["points"].set(f"{tl.point_count:,}")
        self.t["days"].set(f"{len(np.unique(days)):,}")
        self.t["range"].set(f"{rng[0]} → {rng[1]}" if rng else "—")
        months = days.astype("datetime64[M]")
        um, cnt = np.unique(months, return_counts=True)
        self.t["months"].set(str(len(um)))
        self.t["format"].set(tl.diagnostics.detected_format.split(" (")[0])
        data = []
        lang = self.state.settings.ui.language
        for m, c in zip(um, cnt):
            y, mo = str(m).split("-")
            data.append((f"{month_name(int(mo), lang, short=True)} {y[2:]}", int(c)))
        self.chart.set_data(data[-36:])
        n = len(cols)
        step = max(1, n // 20000)
        xs, ys = project_route(cols.lat[::step].tolist(), cols.lon[::step].tolist())
        self.overview.set_route(xs, ys)

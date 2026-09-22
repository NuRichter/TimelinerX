from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton)

from ...i18n import tr
from ...projects.project import Project
from ..widgets import Card, ScrollPage, muted, page_header


class LibraryPage(ScrollPage):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.lay.addWidget(page_header(tr("ui.library.title"), tr("ui.library.subtitle")))
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("ui.library.search"))
        self.search.setAccessibleName(tr("ui.library.search"))
        self.search.textChanged.connect(self.refresh)
        top.addWidget(self.search)
        self.lay.addLayout(top)
        c = Card()
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(240, 135))
        self.grid.setGridSize(QSize(270, 215))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setWordWrap(True)
        self.grid.setMinimumHeight(460)
        self.grid.setAccessibleName(tr("ui.library.title"))
        self.grid.itemActivated.connect(lambda _: self.play())
        self.grid.currentItemChanged.connect(self.show_details)
        c.add(self.grid)
        self.details = muted(tr("ui.library.select"))
        c.add(self.details)
        row = QHBoxLayout()
        self.btns = {}
        for key, fn in (("play", self.play), ("folder", self.open_folder), ("reveal", self.reveal),
                        ("rerender", self.rerender), ("remove", self.remove), ("delete", self.delete_file)):
            b = QPushButton(tr(f"ui.library.{key}"))
            b.clicked.connect(fn)
            if key in ("remove", "delete"):
                b.setProperty("danger", "true")
            row.addWidget(b)
            self.btns[key] = b
        row.addStretch(1)
        c.add(row)
        self.lay.addWidget(c)
        self.finish()
        state.libraryChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.grid.clear()
        for v in self.state.library.list(self.search.text().strip()):
            label = f"{v.title}\n{v.width}×{v.height} · {v.fps} fps · {v.duration_s:.0f} s"
            if not v.exists:
                label += f"\n({tr('ui.missing')})"
            it = QListWidgetItem(label)
            if v.thumbnail and Path(v.thumbnail).is_file():
                it.setIcon(QIcon(QPixmap(v.thumbnail)))
            it.setData(Qt.UserRole, v.id)
            self.grid.addItem(it)
        self.show_details()

    def current(self):
        it = self.grid.currentItem()
        return self.state.library.get(it.data(Qt.UserRole)) if it else None

    def show_details(self, *_):
        v = self.current()
        for b in self.btns.values():
            b.setEnabled(v is not None)
        if v is None:
            self.details.setText(tr("ui.library.select"))
            return
        extra = json.loads(v.extra or "{}")
        self.details.setText(tr("ui.library.details", path=v.path, start=v.date_start, end=v.date_end,
                                size=f"{v.size_bytes / 2 ** 20:,.1f}", codec=v.codec, encoder=v.encoder,
                                theme=v.theme, camera=v.camera_mode, engine=v.render_engine_version)
                             + (" · HDR10" if extra.get("hdr") else ""))

    def play(self):
        v = self.current()
        if v and v.exists:
            QDesktopServices.openUrl(QUrl.fromLocalFile(v.path))

    def open_folder(self):
        v = self.current()
        if v:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(v.path).parent)))

    def reveal(self):
        v = self.current()
        if not v:
            return
        if sys.platform == "win32" and v.exists:
            subprocess.Popen(["explorer", "/select,", str(Path(v.path))])  # argument list, no shell
        elif sys.platform == "darwin" and v.exists:
            subprocess.Popen(["open", "-R", v.path])
        else:
            self.open_folder()

    def rerender(self):
        v = self.current()
        if not v:
            return
        meta = Path(v.path + ".nrmeta.json")
        if not meta.is_file():
            QMessageBox.information(self, tr("ui.library.rerender"), tr("ui.library.no_meta"))
            return
        d = json.loads(meta.read_text(encoding="utf-8"))
        proj = Project.from_dict(d["project"])
        if proj.camera.keyframes and any("lat" not in k for k in proj.camera.keyframes):
            proj.camera.keyframes = []
            QMessageBox.information(self, tr("ui.library.rerender"), tr("ui.library.kf_dropped"))
        self.state.set_project(proj)
        self.state.navigate.emit("video")

    def remove(self):
        v = self.current()
        if v and QMessageBox.question(self, tr("ui.library.remove"),
                                      tr("ui.library.remove_confirm", title=v.title)) == QMessageBox.Yes:
            self.state.library.remove(v.id, delete_file=False)
            self.state.libraryChanged.emit()

    def delete_file(self):
        v = self.current()
        if v and QMessageBox.warning(self, tr("ui.library.delete"), tr("ui.library.delete_confirm", path=v.path),
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.state.library.remove(v.id, delete_file=True)
            self.state.libraryChanged.emit()

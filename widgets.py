"""Media Compressor - piccoli componenti riutilizzabili."""
from __future__ import annotations

import re

from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QLayout, QLineEdit, QPushButton, QWidget
from PySide6.QtCore import Qt

import icons


def repolish(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def label(text, obj=None, wrap=False) -> QLabel:
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    lab.setWordWrap(wrap)
    return lab


def sep() -> QFrame:
    f = QFrame()
    f.setObjectName("sep")
    return f


def a11y_pass(root: QWidget):
    """Pulsanti col focus solo da tastiera, nome accessibile ai pulsanti-icona e ai campi (dall'etichetta
    che li precede)."""
    for b in root.findChildren(QPushButton):
        if b.focusPolicy() != Qt.NoFocus:
            b.setFocusPolicy(Qt.TabFocus)
        if not b.text().strip() and not b.accessibleName():
            tip = b.toolTip().split("  (")[0].strip()
            if tip:
                b.setAccessibleName(tip)
    layouts = ([root.layout()] if root.layout() else []) + root.findChildren(QLayout)
    for lay in layouts:
        last = ""
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, QLabel):
                txt = re.sub(r"<[^>]+>", "", w.text()).strip()
                if txt:
                    last = txt
            elif isinstance(w, (QComboBox, QLineEdit)) and not w.accessibleName():
                name = last or w.toolTip()
                if name:
                    w.setAccessibleName(name)


class IconLabel(QWidget):
    """Icona vettoriale che si ricolora col tema. Trasparente al mouse."""

    def __init__(self, name="check", role="text2", size=16, width=2.0, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._w = width
        self.set_icon(name, role)

    def set_icon(self, name, role=None):
        if role:
            self._role = role
        self._ic = icons.icon(name, self._role, width=self._w)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._ic.paint(p, self.rect())

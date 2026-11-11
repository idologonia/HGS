"""Small reusable widgets and formatting helpers."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from hgs.gui.theme.dark import ACCENT, MUTED, STATE_COLORS

STATE_NAMES = {
    "running": "ПРАЦЮЄ", "starting": "ЗАПУСК…", "stopping": "ЗУПИНКА…", "stopped": "ЗУПИНЕНО",
    "crashed": "ВПАВ", "failed": "ПОМИЛКА (ЗУПИНЕНО)", "error": "ПОМИЛКА",
}


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def show_error(parent: QWidget, title: str, message: str) -> None:
    QMessageBox.critical(parent, title, message)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")


class StateBadge(QLabel):
    def set_state(self, state: str) -> None:
        color = STATE_COLORS.get(state, MUTED)
        self.setText(f"● {STATE_NAMES.get(state, state.upper())}")
        self.setStyleSheet(f"color: {color}; font-weight: 600;")


class StatCard(Card):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.title = QLabel(title)
        self.title.setObjectName("Muted")
        self.value = QLabel("—")
        self.value.setObjectName("StatValue")
        self.sub = QLabel("")
        self.sub.setObjectName("Muted")
        self.sub.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, value: str, sub: str = "", color: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")
        self.sub.setText(sub)


class Sparkline(QWidget):
    """Tiny line chart of the last N values (two series)."""

    def __init__(self, points: int = 60, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.a: deque[float] = deque([0.0] * points, maxlen=points)
        self.b: deque[float] = deque([0.0] * points, maxlen=points)
        self.setMinimumHeight(48)

    def push(self, a: float, b: float) -> None:
        self.a.append(a)
        self.b.append(b)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        top = max(max(self.a), max(self.b), 1.0)
        w, h = self.width(), self.height() - 2
        for series, color in ((self.a, ACCENT), (self.b, "#3ecf8e")):
            path = QPainterPath()
            n = len(series)
            for i, v in enumerate(series):
                pt = QPointF(i * w / max(n - 1, 1), h - (v / top) * (h - 2) + 1)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(QColor(color), 1.6))
            p.drawPath(path)
        p.end()


class Banner(QFrame):
    """Visible error/notice strip with an optional action button."""

    def __init__(self, kind: str = "error", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner" if kind == "error" else "InfoBanner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.label, 1)
        self.action = QPushButton()
        self.action.hide()
        lay.addWidget(self.action)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(32)
        self.close_btn.clicked.connect(self.hide)
        lay.addWidget(self.close_btn)
        self.hide()

    def show_message(self, text: str, action: str | None = None, closable: bool = True) -> None:
        self.label.setText(text)
        self.action.setVisible(action is not None)
        if action:
            self.action.setText(action)
        self.close_btn.setVisible(closable)
        self.show()

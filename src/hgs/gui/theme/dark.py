"""Dark theme: color tokens and the Qt style sheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0f1115"
SURFACE = "#171a21"
SURFACE_2 = "#1e222b"
BORDER = "#2a2f3a"
TEXT = "#e6e8ee"
MUTED = "#8a91a0"
ACCENT = "#4f8cff"
GREEN = "#3ecf8e"
YELLOW = "#f2c94c"
RED = "#ff5c5c"
ORANGE = "#ff9f43"

STATE_COLORS = {
    "running": GREEN,
    "starting": YELLOW,
    "stopping": YELLOW,
    "stopped": MUTED,
    "crashed": ORANGE,
    "failed": RED,
    "error": RED,
}

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog, #Page {{ background: {BG}; }}
QWidget#Sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QListWidget#Nav {{ background: transparent; border: none; outline: none; }}
QListWidget#Nav::item {{ padding: 10px 14px; border-radius: 6px; margin: 2px 8px; color: {MUTED}; }}
QListWidget#Nav::item:selected {{ background: {SURFACE_2}; color: {TEXT}; }}
QListWidget#Nav::item:hover:!selected {{ background: {SURFACE_2}; }}
QLabel#AppTitle {{ font-size: 12pt; font-weight: 700; letter-spacing: 1px; padding: 16px 14px 12px 14px; }}
QLabel#PageTitle {{ font-size: 16pt; font-weight: 600; }}
QLabel#Muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#CardTitle {{ font-size: 12pt; font-weight: 600; }}
QLabel#StatValue {{ font-size: 18pt; font-weight: 600; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#Banner {{ background: #3a1f24; border: 1px solid {RED}; border-radius: 8px; }}
QFrame#InfoBanner {{ background: #2e2a1a; border: 1px solid {YELLOW}; border-radius: 8px; }}
QPushButton {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #565c69; border-color: {SURFACE_2}; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#Primary:disabled {{ background: #2b3d63; border-color: #2b3d63; color: #8796b5; }}
QPushButton#Danger:hover {{ border-color: {RED}; color: {RED}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QTableWidget, QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {ACCENT};
}}
QHeaderView::section {{ background: {SURFACE_2}; border: none; padding: 6px; color: {MUTED}; }}
QTableWidget {{ gridline-color: {BORDER}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, SURFACE),
        (QPalette.AlternateBase, SURFACE_2), (QPalette.Text, TEXT), (QPalette.Button, SURFACE_2),
        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, SURFACE_2), (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED),
    ):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(QSS)

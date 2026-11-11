"""Small helpers for read-only tables."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    return t


def fill(table: QTableWidget, rows: list[list[str]], keys: list[object] | None = None) -> None:
    """Replace the rows, keeping the selection on the same key if possible."""
    from PySide6.QtCore import Qt

    selected = selected_key(table)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            if c == 0 and keys is not None:
                item.setData(Qt.UserRole, keys[r])
            table.setItem(r, c, item)
    if selected is not None and keys is not None and selected in keys:
        table.selectRow(keys.index(selected))


def selected_key(table: QTableWidget) -> object | None:
    from PySide6.QtCore import Qt

    row = table.currentRow()
    if row < 0 or table.item(row, 0) is None:
        return None
    return table.item(row, 0).data(Qt.UserRole)


def ago(ts: float | str | None) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, str):
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return ts
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s} с тому"
    if s < 3600:
        return f"{s // 60} хв тому"
    if s < 86400:
        return f"{s // 3600} год тому"
    return time.strftime("%Y-%m-%d", time.localtime(ts))

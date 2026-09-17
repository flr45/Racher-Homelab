from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from station_events import alarm_event_timeline, recent_alarm_events, stats_snapshot


def _format_time(value: str | None, with_seconds: bool = False) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d-%m-%Y %H:%M:%S" if with_seconds else "%d-%m-%Y %H:%M")
    except ValueError:
        return str(value)


class _StatCard(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        self.value = QLabel("—")
        self.value.setObjectName("metric")
        layout.addWidget(heading)
        layout.addWidget(self.value)


class EventDetailDialog(QDialog):
    def __init__(self, event: dict, parent=None) -> None:
        super().__init__(parent)
        self.event = event
        self.setWindowTitle(f"Alarmhændelse #{event['id']} · SBR Pager Gateway")
        self.resize(980, 590)
        root = QVBoxLayout(self)

        title = QLabel(f"Alarmhændelse #{event['id']}")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        root.addWidget(title)

        meta = QLabel(
            f"{_format_time(str(event['started_at']))} · "
            f"Station: {event.get('station') or '—'} · "
            f"Status: {'Komplet' if event.get('status') == 'complete' else 'Afventer'}"
        )
        meta.setObjectName("muted")
        root.addWidget(meta)

        description = QLabel(
            f"Alarmtype: {event.get('alarm_type') or '—'}\n"
            f"Adresse: {event.get('address') or '—'}\n"
            f"Sending 2/opfølgninger: {event.get('followup_count') or 0}"
        )
        description.setWordWrap(True)
        root.addWidget(description)

        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(
            ["Modtaget", "Type", "Afsender", "Original tekst", "Sendt", "Fejl"]
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setColumnWidth(0, 155)
        table.setColumnWidth(1, 155)
        table.setColumnWidth(2, 135)
        table.setColumnWidth(3, 390)
        rows = alarm_event_timeline(int(event["id"]))
        table.setRowCount(len(rows))
        labels = {
            "alarm_prealert": "Første varsling",
            "alarm_complete": "Komplet alarm",
            "sending2_prealert": "Sending 2 · varsling",
            "sending2_complete": "Sending 2 · komplet",
        }
        for index, row in enumerate(rows):
            values = [
                _format_time(str(row["received_at"]), with_seconds=True),
                labels.get(str(row["kind"]), str(row["kind"])),
                str(row["sender"]),
                str(row["raw_body"]),
                str(row["sent_count"]),
                str(row["failed_count"]),
            ]
            for column, value in enumerate(values):
                table.setItem(index, column, QTableWidgetItem(value))
        table.resizeRowsToContents()
        root.addWidget(table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close = QPushButton("Luk")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)


class AlarmEventsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.events: list[dict] = []
        self.setWindowTitle("Alarmhændelser og statistik · SBR Pager Gateway")
        self.resize(1120, 720)
        root = QVBoxLayout(self)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Alarmhændelser og statistik")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        subtitle = QLabel("Første varsling, komplet melding og Sending 2 samlet som én hændelse.")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        refresh = QPushButton("Opdater")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        cards = QGridLayout()
        self.today = _StatCard("ALARMer I DAG")
        self.days7 = _StatCard("SENESTE 7 DAGE")
        self.days30 = _StatCard("SENESTE 30 DAGE")
        self.waiting = _StatCard("AFVENTER")
        self.first = _StatCard("FØRSTE WHATSAPP")
        self.complete = _StatCard("KOMPLET MELDING")
        for index, card in enumerate(
            [self.today, self.days7, self.days30, self.waiting, self.first, self.complete]
        ):
            cards.addWidget(card, index // 3, index % 3)
        root.addLayout(cards)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("muted")
        root.addWidget(self.summary)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Tid", "Station", "Alarmtype", "Adresse", "Status", "Sending 2", "Afsender"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 300)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 90)
        self.table.doubleClicked.connect(self.open_selected)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        open_button = QPushButton("Vis valgt hændelse")
        open_button.clicked.connect(self.open_selected)
        actions.addWidget(open_button)
        actions.addStretch()
        close = QPushButton("Luk")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        stats = stats_snapshot()
        self.today.value.setText(str(stats["today"]))
        self.days7.value.setText(str(stats["days7"]))
        self.days30.value.setText(str(stats["days30"]))
        self.waiting.value.setText(str(stats["waiting"]))
        self.first.value.setText(
            f"{stats['avg_first']} sek." if stats["avg_first"] is not None else "—"
        )
        self.complete.value.setText(
            f"{stats['avg_complete']} sek." if stats["avg_complete"] is not None else "—"
        )

        top_types = ", ".join(f"{name} ({count})" for name, count in stats["top_types"])
        self.summary.setText(
            f"Sending 2/opfølgninger de seneste 30 dage: {stats['followups']}"
            + (f" · Hyppigste alarmtyper: {top_types}" if top_types else "")
        )

        self.events = recent_alarm_events(250)
        self.table.setRowCount(len(self.events))
        for index, event in enumerate(self.events):
            values = [
                _format_time(str(event["started_at"])),
                str(event.get("station") or "—"),
                str(event.get("alarm_type") or "—"),
                str(event.get("address") or "—"),
                "Komplet" if event.get("status") == "complete" else "Afventer",
                str(event.get("followup_count") or 0),
                str(event.get("sender") or "—"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, int(event["id"]))
                self.table.setItem(index, column, item)
        self.table.resizeRowsToContents()

    def open_selected(self, *_args) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.events):
            return
        EventDetailDialog(self.events[row], self).exec()

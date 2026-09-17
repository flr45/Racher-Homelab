from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from alarm_stats import statistics_snapshot
from station_events import alarm_event_timeline, recent_alarm_events


def _format_time(value: str | None, with_seconds: bool = False) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime(
            "%d-%m-%Y %H:%M:%S" if with_seconds else "%d-%m-%Y %H:%M"
        )
    except ValueError:
        return str(value)


def _duration_summary(values: dict) -> str:
    if values.get("avg") is None:
        return "Ingen data endnu"
    return (
        f"Gns. {values['avg']} sek. · "
        f"hurtigste {values['min']} sek. · langsomste {values['max']} sek."
    )


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
        self.detail = QLabel("")
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)


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
            f"Sending 2/opfølgninger: {event.get('followup_count') or 0}\n"
            f"Første WhatsApp: {_format_time(event.get('first_delivered_at'), True)} · "
            f"Komplet: {_format_time(event.get('completed_at'), True)}"
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
        self.resize(1180, 760)
        root = QVBoxLayout(self)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Alarmhændelser og statistik")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        subtitle = QLabel(
            "Første varsling, komplet melding og Sending 2 samlet som én hændelse."
        )
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        period_label = QLabel("Periode")
        header.addWidget(period_label)
        self.period = QComboBox()
        self.period.addItem("30 dage", 30)
        self.period.addItem("90 dage", 90)
        self.period.addItem("365 dage", 365)
        self.period.currentIndexChanged.connect(self.refresh_statistics)
        header.addWidget(self.period)

        refresh = QPushButton("Opdater")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.events_page = QWidget()
        events_layout = QVBoxLayout(self.events_page)
        self.events_table = QTableWidget(0, 7)
        self.events_table.setHorizontalHeaderLabels(
            ["Tid", "Station", "Alarmtype", "Adresse", "Status", "Sending 2", "Afsender"]
        )
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.events_table.setSelectionMode(QTableWidget.SingleSelection)
        self.events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.events_table.setColumnWidth(0, 145)
        self.events_table.setColumnWidth(1, 120)
        self.events_table.setColumnWidth(2, 190)
        self.events_table.setColumnWidth(3, 300)
        self.events_table.setColumnWidth(4, 100)
        self.events_table.setColumnWidth(5, 90)
        self.events_table.doubleClicked.connect(self.open_selected)
        events_layout.addWidget(self.events_table, 1)
        event_actions = QHBoxLayout()
        open_button = QPushButton("Vis valgt hændelse")
        open_button.clicked.connect(self.open_selected)
        event_actions.addWidget(open_button)
        event_actions.addStretch()
        events_layout.addLayout(event_actions)
        self.tabs.addTab(self.events_page, "Hændelser")

        self.stats_page = QWidget()
        stats_layout = QVBoxLayout(self.stats_page)
        cards = QGridLayout()
        self.total = _StatCard("ALARMER")
        self.complete_pct = _StatCard("KOMPLETTE")
        self.followups = _StatCard("MED SENDING 2")
        self.peak = _StatCard("TRAVLESTE TIDSPUNKT")
        self.first = _StatCard("FØRSTE WHATSAPP")
        self.complete_time = _StatCard("KOMPLET MELDING")
        for index, card in enumerate(
            [
                self.total,
                self.complete_pct,
                self.followups,
                self.peak,
                self.first,
                self.complete_time,
            ]
        ):
            cards.addWidget(card, index // 3, index % 3)
        stats_layout.addLayout(cards)

        self.daily_label = QLabel("")
        self.daily_label.setWordWrap(True)
        self.daily_label.setObjectName("muted")
        stats_layout.addWidget(self.daily_label)

        self.distributions = QTableWidget(0, 8)
        self.distributions.setHorizontalHeaderLabels(
            [
                "Ugedag",
                "Antal",
                "Klokkeslæt",
                "Antal",
                "Alarmtype",
                "Antal",
                "Station",
                "Antal",
            ]
        )
        self.distributions.verticalHeader().setVisible(False)
        self.distributions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.distributions.setSelectionMode(QTableWidget.NoSelection)
        self.distributions.horizontalHeader().setStretchLastSection(True)
        stats_layout.addWidget(self.distributions, 1)
        self.tabs.addTab(self.stats_page, "Statistik")

        close_row = QHBoxLayout()
        close_row.addStretch()
        close = QPushButton("Luk")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        root.addLayout(close_row)
        self.refresh()

    def refresh(self) -> None:
        self.refresh_events()
        self.refresh_statistics()

    def refresh_events(self) -> None:
        self.events = recent_alarm_events(500)
        self.events_table.setRowCount(len(self.events))
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
                self.events_table.setItem(index, column, item)
        self.events_table.resizeRowsToContents()

    def refresh_statistics(self, *_args) -> None:
        days = int(self.period.currentData() or 30)
        stats = statistics_snapshot(days)

        self.total.value.setText(str(stats["total"]))
        self.total.detail.setText(f"Seneste {days} dage")
        self.complete_pct.value.setText(f"{stats['complete_pct']}%")
        self.complete_pct.detail.setText(f"{stats['complete']} af {stats['total']} hændelser")
        self.followups.value.setText(f"{stats['followup_pct']}%")
        self.followups.detail.setText(
            f"{stats['followup_events']} hændelser · {stats['followup_total']} opfølgninger"
        )
        peak_hour = stats["peak_hour"]["name"] if stats["peak_hour"] else "—"
        peak_day = stats["peak_weekday"]["name"] if stats["peak_weekday"] else "Ikke nok data"
        self.peak.value.setText(peak_hour)
        self.peak.detail.setText(peak_day)
        self.first.value.setText(
            f"{stats['first']['avg']} sek." if stats["first"]["avg"] is not None else "—"
        )
        self.first.detail.setText(_duration_summary(stats["first"]))
        self.complete_time.value.setText(
            f"{stats['complete_time']['avg']} sek."
            if stats["complete_time"]["avg"] is not None
            else "—"
        )
        self.complete_time.detail.setText(_duration_summary(stats["complete_time"]))

        daily_nonzero = [row for row in stats["daily"] if row["count"]]
        self.daily_label.setText(
            "Alarmer pr. dag · seneste 30 dage: "
            + (
                " · ".join(f"{row['date']}: {row['count']}" for row in daily_nonzero)
                if daily_nonzero
                else "ingen registrerede alarmer"
            )
        )

        weekdays = stats["weekdays"]
        hours = stats["hours"]
        types = stats["types"]
        stations = stats["stations"]
        row_count = max(len(weekdays), len(hours), len(types), len(stations), 1)
        self.distributions.setRowCount(row_count)
        for row_index in range(row_count):
            values: list[str] = []
            if row_index < len(weekdays):
                values.extend([weekdays[row_index]["name"], str(weekdays[row_index]["count"])])
            else:
                values.extend(["", ""])
            if row_index < len(hours):
                values.extend([hours[row_index]["name"], str(hours[row_index]["count"])])
            else:
                values.extend(["", ""])
            if row_index < len(types):
                values.extend([str(types[row_index][0]), str(types[row_index][1])])
            else:
                values.extend(["", ""])
            if row_index < len(stations):
                values.extend([str(stations[row_index][0]), str(stations[row_index][1])])
            else:
                values.extend(["", ""])
            for column, value in enumerate(values):
                self.distributions.setItem(row_index, column, QTableWidgetItem(value))

    def open_selected(self, *_args) -> None:
        row = self.events_table.currentRow()
        if row < 0 or row >= len(self.events):
            return
        EventDetailDialog(self.events[row], self).exec()

from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from station_events import (
    delete_admin_station,
    delete_station_rule,
    list_admin_stations,
    list_station_rules,
    recipient_admin_station,
    recipient_filters,
    save_admin_station,
    save_station_rule,
    set_recipient_admin_station,
    set_recipient_filters,
    set_station_rule_active,
)
from storage import list_recipients


class StationRulesTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)

        info = QLabel(
            "Alarmstationer bruges til at genkende stationen i en SMS. "
            "Alias kan fx være stationskode eller alternative navne."
        )
        info.setWordWrap(True)
        info.setObjectName("muted")
        root.addWidget(info)

        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("fx Slagelse")
        self.alias_input = QLineEdit()
        self.alias_input.setPlaceholderText("fx SLG, Station Slagelse")
        form.addRow("Stationsnavn", self.name_input)
        form.addRow("Alias (kommasepareret)", self.alias_input)
        root.addLayout(form)

        add_row = QHBoxLayout()
        add_row.addStretch()
        add_button = QPushButton("Tilføj station")
        add_button.setObjectName("primary")
        add_button.clicked.connect(self.add_station)
        add_row.addWidget(add_button)
        root.addLayout(add_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Station", "Alias", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        toggle = QPushButton("Aktiver / pause")
        toggle.clicked.connect(self.toggle_selected)
        actions.addWidget(toggle)
        delete = QPushButton("Slet")
        delete.clicked.connect(self.delete_selected)
        actions.addWidget(delete)
        actions.addStretch()
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        rows = list_station_rules()
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            name = QTableWidgetItem(str(row["name"]))
            name.setData(Qt.UserRole, int(row["id"]))
            name.setData(Qt.UserRole + 1, bool(row["active"]))
            self.table.setItem(index, 0, name)
            self.table.setItem(index, 1, QTableWidgetItem(str(row["aliases"] or "")))
            self.table.setItem(index, 2, QTableWidgetItem("Aktiv" if row["active"] else "Pauset"))

    def _selected(self) -> tuple[int, bool] | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Stationer", "Vælg først en station.")
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return int(item.data(Qt.UserRole)), bool(item.data(Qt.UserRole + 1))

    def add_station(self) -> None:
        try:
            save_station_rule(self.name_input.text(), self.alias_input.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Stationer", str(exc))
            return
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, "Stationer", "Stationen findes allerede.")
            return
        self.name_input.clear()
        self.alias_input.clear()
        self.refresh()

    def toggle_selected(self) -> None:
        selected = self._selected()
        if not selected:
            return
        station_id, active = selected
        set_station_rule_active(station_id, not active)
        self.refresh()

    def delete_selected(self) -> None:
        selected = self._selected()
        if not selected:
            return
        station_id, _ = selected
        if QMessageBox.question(self, "Stationer", "Slet den valgte station og dens alarmfiltre?") == QMessageBox.Yes:
            delete_station_rule(station_id)
            self.refresh()


class RecipientFiltersTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        left.addWidget(QLabel("WhatsApp-modtagere"))
        self.people = QTableWidget(0, 3)
        self.people.setHorizontalHeaderLabels(["Navn", "Telefon", "Alarmfilter"])
        self.people.horizontalHeader().setStretchLastSection(True)
        self.people.verticalHeader().setVisible(False)
        self.people.setSelectionBehavior(QTableWidget.SelectRows)
        self.people.setSelectionMode(QTableWidget.SingleSelection)
        self.people.setEditTriggers(QTableWidget.NoEditTriggers)
        self.people.itemSelectionChanged.connect(self.load_selected)
        left.addWidget(self.people, 1)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        heading = QLabel("Modtag alarmer fra")
        heading.setStyleSheet("font-weight: 650;")
        right.addWidget(heading)
        hint = QLabel("Ingen markeringer = alle stationer.")
        hint.setObjectName("muted")
        right.addWidget(hint)
        self.station_list = QListWidget()
        right.addWidget(self.station_list, 1)
        save = QPushButton("Gem alarmfilter")
        save.setObjectName("primary")
        save.clicked.connect(self.save_selected)
        right.addWidget(save)
        root.addLayout(right, 2)
        self.refresh()

    def refresh(self) -> None:
        recipients = list_recipients()
        self.people.setRowCount(len(recipients))
        for index, recipient in enumerate(recipients):
            filters = recipient_filters(int(recipient["id"]))
            name = QTableWidgetItem(str(recipient["name"]))
            name.setData(Qt.UserRole, int(recipient["id"]))
            self.people.setItem(index, 0, name)
            self.people.setItem(index, 1, QTableWidgetItem(str(recipient["phone"])))
            self.people.setItem(index, 2, QTableWidgetItem(", ".join(filters) if filters else "Alle"))
        if recipients:
            self.people.selectRow(0)
        else:
            self.station_list.clear()

    def _recipient_id(self) -> int | None:
        row = self.people.currentRow()
        if row < 0:
            return None
        item = self.people.item(row, 0)
        return int(item.data(Qt.UserRole)) if item else None

    def load_selected(self) -> None:
        recipient_id = self._recipient_id()
        self.station_list.clear()
        if recipient_id is None:
            return
        selected = {value.casefold() for value in recipient_filters(recipient_id)}
        for station in list_station_rules():
            if not station["active"]:
                continue
            item = QListWidgetItem(str(station["name"]))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if str(station["name"]).casefold() in selected else Qt.Unchecked)
            self.station_list.addItem(item)

    def save_selected(self) -> None:
        recipient_id = self._recipient_id()
        if recipient_id is None:
            QMessageBox.information(self, "Alarmfilter", "Vælg først en modtager.")
            return
        stations = []
        for index in range(self.station_list.count()):
            item = self.station_list.item(index)
            if item.checkState() == Qt.Checked:
                stations.append(item.text())
        set_recipient_filters(recipient_id, stations)
        self.refresh()


class PeopleStationsTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        info = QLabel(
            "Denne stationstilknytning er kun administrativ gruppering af personer. "
            "Den påvirker ikke alarmfilteret ovenfor."
        )
        info.setWordWrap(True)
        info.setObjectName("muted")
        root.addWidget(info)

        create_row = QHBoxLayout()
        self.station_input = QLineEdit()
        self.station_input.setPlaceholderText("Nyt stationsnavn")
        create_row.addWidget(self.station_input, 1)
        add = QPushButton("Opret station")
        add.clicked.connect(self.add_station)
        create_row.addWidget(add)
        root.addLayout(create_row)

        self.station_table = QTableWidget(0, 2)
        self.station_table.setHorizontalHeaderLabels(["Station", "Personer"])
        self.station_table.horizontalHeader().setStretchLastSection(True)
        self.station_table.verticalHeader().setVisible(False)
        self.station_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.station_table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.station_table, 1)

        delete_row = QHBoxLayout()
        delete = QPushButton("Slet valgt station")
        delete.clicked.connect(self.delete_station)
        delete_row.addWidget(delete)
        delete_row.addStretch()
        root.addLayout(delete_row)

        self.people_table = QTableWidget(0, 3)
        self.people_table.setHorizontalHeaderLabels(["Person", "Telefon", "Station"])
        self.people_table.horizontalHeader().setStretchLastSection(True)
        self.people_table.verticalHeader().setVisible(False)
        self.people_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.people_table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.people_table, 2)

        assign = QHBoxLayout()
        self.assignment_list = QListWidget()
        self.assignment_list.setMaximumHeight(110)
        assign.addWidget(self.assignment_list, 1)
        set_button = QPushButton("Flyt valgt person")
        set_button.clicked.connect(self.assign_person)
        assign.addWidget(set_button)
        clear_button = QPushButton("Uden station")
        clear_button.clicked.connect(self.clear_person_station)
        assign.addWidget(clear_button)
        root.addLayout(assign)
        self.refresh()

    def refresh(self) -> None:
        stations = list_admin_stations()
        recipients = list_recipients()
        counts = {int(station["id"]): 0 for station in stations}
        memberships: dict[int, int | None] = {}
        for recipient in recipients:
            sid = recipient_admin_station(int(recipient["id"]))
            memberships[int(recipient["id"])] = sid
            if sid in counts:
                counts[sid] += 1

        self.station_table.setRowCount(len(stations))
        for index, station in enumerate(stations):
            item = QTableWidgetItem(str(station["name"]))
            item.setData(Qt.UserRole, int(station["id"]))
            self.station_table.setItem(index, 0, item)
            self.station_table.setItem(index, 1, QTableWidgetItem(str(counts[int(station["id"])])))

        names = {int(station["id"]): str(station["name"]) for station in stations}
        self.people_table.setRowCount(len(recipients))
        for index, recipient in enumerate(recipients):
            item = QTableWidgetItem(str(recipient["name"]))
            item.setData(Qt.UserRole, int(recipient["id"]))
            self.people_table.setItem(index, 0, item)
            self.people_table.setItem(index, 1, QTableWidgetItem(str(recipient["phone"])))
            sid = memberships[int(recipient["id"])]
            self.people_table.setItem(index, 2, QTableWidgetItem(names.get(sid, "Uden station")))

        self.assignment_list.clear()
        for station in stations:
            item = QListWidgetItem(str(station["name"]))
            item.setData(Qt.UserRole, int(station["id"]))
            self.assignment_list.addItem(item)

    def add_station(self) -> None:
        try:
            save_admin_station(self.station_input.text())
        except (ValueError, sqlite3.IntegrityError) as exc:
            QMessageBox.warning(self, "Personstationer", str(exc) if isinstance(exc, ValueError) else "Stationen findes allerede.")
            return
        self.station_input.clear()
        self.refresh()

    def delete_station(self) -> None:
        row = self.station_table.currentRow()
        if row < 0:
            return
        item = self.station_table.item(row, 0)
        if item and QMessageBox.question(self, "Personstationer", "Slet den valgte station?") == QMessageBox.Yes:
            delete_admin_station(int(item.data(Qt.UserRole)))
            self.refresh()

    def _selected_person(self) -> int | None:
        row = self.people_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Personstationer", "Vælg først en person.")
            return None
        item = self.people_table.item(row, 0)
        return int(item.data(Qt.UserRole)) if item else None

    def assign_person(self) -> None:
        recipient_id = self._selected_person()
        station_item = self.assignment_list.currentItem()
        if recipient_id is None or station_item is None:
            return
        set_recipient_admin_station(recipient_id, int(station_item.data(Qt.UserRole)))
        self.refresh()

    def clear_person_station(self) -> None:
        recipient_id = self._selected_person()
        if recipient_id is None:
            return
        set_recipient_admin_station(recipient_id, None)
        self.refresh()


class StationsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stationer og alarmfiltre · SBR Pager Gateway")
        self.resize(950, 680)
        root = QVBoxLayout(self)
        title = QLabel("Stationer og alarmfiltre")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        root.addWidget(title)

        self.tabs = QTabWidget()
        self.rules_tab = StationRulesTab()
        self.filters_tab = RecipientFiltersTab()
        self.people_tab = PeopleStationsTab()
        self.tabs.addTab(self.rules_tab, "Alarmstationer")
        self.tabs.addTab(self.filters_tab, "Alarmfiltre")
        self.tabs.addTab(self.people_tab, "Personplacering")
        self.tabs.currentChanged.connect(self.refresh_current)
        root.addWidget(self.tabs, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close = QPushButton("Luk")
        close.clicked.connect(self.accept)
        close_row.addWidget(close)
        root.addLayout(close_row)

    def refresh_current(self, _index: int) -> None:
        widget = self.tabs.currentWidget()
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            refresh()

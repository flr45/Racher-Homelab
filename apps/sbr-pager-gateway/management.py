from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from policy import normalize_phone
from storage import (
    delete_allowed_sender,
    delete_recipient,
    list_allowed_senders,
    list_recipients,
    save_allowed_sender,
    save_recipient,
    set_allowed_sender_active,
    set_recipient_active,
)


class _DirectoryDialog(QDialog):
    def __init__(self, title: str, description: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(650, 500)

        root = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 22px; font-weight: 650;")
        root.addWidget(heading)

        info = QLabel(description)
        info.setWordWrap(True)
        info.setStyleSheet("color: #666;")
        root.addWidget(info)

        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Navn")
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("fx +4512345678")
        form.addRow("Navn", self.name_input)
        form.addRow("Telefon", self.phone_input)
        root.addLayout(form)

        add_row = QHBoxLayout()
        add_row.addStretch()
        self.add_button = QPushButton("Tilføj")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self.add_entry)
        add_row.addWidget(self.add_button)
        root.addLayout(add_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Navn", "Telefon", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.toggle_button = QPushButton("Aktiver / pause")
        self.toggle_button.clicked.connect(self.toggle_selected)
        actions.addWidget(self.toggle_button)

        self.delete_button = QPushButton("Slet")
        self.delete_button.clicked.connect(self.delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch()

        close_button = QPushButton("Luk")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        root.addLayout(actions)

    def selected_id_and_active(self) -> tuple[int, bool] | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, self.windowTitle(), "Vælg først en række.")
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        entry_id = item.data(Qt.UserRole)
        active = bool(item.data(Qt.UserRole + 1))
        return int(entry_id), active

    def add_entry(self) -> None:
        raise NotImplementedError

    def toggle_selected(self) -> None:
        raise NotImplementedError

    def delete_selected(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        raise NotImplementedError


class SendersDialog(_DirectoryDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(
            "Godkendte SMS-afsendere",
            "Kun aktive afsendere på denne liste må videresendes til WhatsApp.",
            parent,
        )
        self.refresh()

    def refresh(self) -> None:
        rows = list_allowed_senders()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            name = QTableWidgetItem(str(row["name"]))
            name.setData(Qt.UserRole, int(row["id"]))
            name.setData(Qt.UserRole + 1, bool(row["active"]))
            self.table.setItem(row_index, 0, name)
            self.table.setItem(row_index, 1, QTableWidgetItem(str(row["phone"])))
            self.table.setItem(row_index, 2, QTableWidgetItem("Aktiv" if row["active"] else "Pauset"))

    def add_entry(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, self.windowTitle(), "Skriv et navn.")
            return
        try:
            phone = normalize_phone(self.phone_input.text())
            save_allowed_sender(name, phone)
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, self.windowTitle(), "Telefonnummeret findes allerede.")
            return

        self.name_input.clear()
        self.phone_input.clear()
        self.refresh()

    def toggle_selected(self) -> None:
        selected = self.selected_id_and_active()
        if not selected:
            return
        entry_id, active = selected
        set_allowed_sender_active(entry_id, not active)
        self.refresh()

    def delete_selected(self) -> None:
        selected = self.selected_id_and_active()
        if not selected:
            return
        entry_id, _ = selected
        if QMessageBox.question(self, self.windowTitle(), "Slet den valgte afsender?") == QMessageBox.Yes:
            delete_allowed_sender(entry_id)
            self.refresh()


class RecipientsDialog(_DirectoryDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(
            "WhatsApp-modtagere",
            "Aktive modtagere får de SMS-beskeder, som godkendes af gatewayen.",
            parent,
        )
        self.refresh()

    def refresh(self) -> None:
        rows = list_recipients()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            name = QTableWidgetItem(str(row["name"]))
            name.setData(Qt.UserRole, int(row["id"]))
            name.setData(Qt.UserRole + 1, bool(row["active"]))
            self.table.setItem(row_index, 0, name)
            self.table.setItem(row_index, 1, QTableWidgetItem(str(row["phone"])))
            self.table.setItem(row_index, 2, QTableWidgetItem("Aktiv" if row["active"] else "Pauset"))

    def add_entry(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, self.windowTitle(), "Skriv et navn.")
            return
        try:
            phone = normalize_phone(self.phone_input.text())
            save_recipient(name, phone)
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, self.windowTitle(), "Telefonnummeret findes allerede.")
            return

        self.name_input.clear()
        self.phone_input.clear()
        self.refresh()

    def toggle_selected(self) -> None:
        selected = self.selected_id_and_active()
        if not selected:
            return
        entry_id, active = selected
        set_recipient_active(entry_id, not active)
        self.refresh()

    def delete_selected(self) -> None:
        selected = self.selected_id_and_active()
        if not selected:
            return
        entry_id, _ = selected
        if QMessageBox.question(self, self.windowTitle(), "Slet den valgte modtager?") == QMessageBox.Yes:
            delete_recipient(entry_id)
            self.refresh()

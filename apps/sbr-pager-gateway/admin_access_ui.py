from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from admin_auth import pin_is_configured, set_admin_pin, verify_admin_pin


class AdminPinDialog(QDialog):
    def __init__(self, setup: bool, parent=None) -> None:
        super().__init__(parent)
        self.setup = setup
        self.setWindowTitle("Admin · SBR Pager Gateway")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(13)

        title = QLabel("Opret admin-PIN" if setup else "Admin-adgang")
        title.setStyleSheet("font-size: 21px; font-weight: 650;")
        root.addWidget(title)

        info = QLabel(
            "Vælg en lokal 6-cifret PIN til avancerede indstillinger."
            if setup
            else "Indtast den lokale 6-cifrede admin-PIN."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        form = QFormLayout()
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.Password)
        self.pin.setMaxLength(6)
        self.pin.setPlaceholderText("6 cifre")
        self.pin.returnPressed.connect(self.submit)
        form.addRow("PIN", self.pin)

        self.confirm = None
        if setup:
            self.confirm = QLineEdit()
            self.confirm.setEchoMode(QLineEdit.Password)
            self.confirm.setMaxLength(6)
            self.confirm.setPlaceholderText("Gentag PIN")
            self.confirm.returnPressed.connect(self.submit)
            form.addRow("Gentag PIN", self.confirm)
        root.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Annuller")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        submit = QPushButton("Gem PIN" if setup else "Åbn Admin")
        submit.setObjectName("primary")
        submit.clicked.connect(self.submit)
        buttons.addWidget(submit)
        root.addLayout(buttons)

        self.pin.setFocus()

    def submit(self) -> None:
        value = self.pin.text().strip()
        if self.setup:
            assert self.confirm is not None
            if value != self.confirm.text().strip():
                QMessageBox.warning(self, "Admin-PIN", "De to PIN-koder er ikke ens.")
                self.confirm.clear()
                self.confirm.setFocus()
                return
            try:
                set_admin_pin(value)
            except ValueError as exc:
                QMessageBox.warning(self, "Admin-PIN", str(exc))
                return
            self.accept()
            return

        if not verify_admin_pin(value):
            QMessageBox.warning(self, "Admin-PIN", "Forkert admin-PIN.")
            self.pin.clear()
            self.pin.setFocus()
            return
        self.accept()


def request_admin_access(parent=None) -> bool:
    dialog = AdminPinDialog(setup=not pin_is_configured(), parent=parent)
    return dialog.exec() == QDialog.Accepted

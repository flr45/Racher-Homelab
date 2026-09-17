from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from delivery import WhatsAppDeliveryEngine, send_test_to_active_recipients
from whatsapp_engine import WhatsAppBridgeManager


class WhatsAppStatusWorker(QThread):
    status_changed = Signal(dict)

    def __init__(self, bridge: WhatsAppBridgeManager, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self.status_changed.emit(self.bridge.status(timeout=0.7))
            self.msleep(1500)


class DeliveryWorker(QThread):
    message_updated = Signal(int, str)

    def __init__(self, bridge: WhatsAppBridgeManager, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.engine: WhatsAppDeliveryEngine | None = None

    def run(self) -> None:
        self.engine = WhatsAppDeliveryEngine(
            self.bridge,
            on_update=lambda message_id, status: self.message_updated.emit(message_id, status),
        )
        self.engine.run()

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()


class TestMessageWorker(QThread):
    completed = Signal(int, int)
    failed = Signal(str)

    def __init__(self, bridge: WhatsAppBridgeManager, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge

    def run(self) -> None:
        try:
            sent, failed = send_test_to_active_recipients(self.bridge)
            self.completed.emit(sent, failed)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class WhatsAppLoginDialog(QDialog):
    def __init__(self, bridge: WhatsAppBridgeManager, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle("WhatsApp · SBR Pager Gateway")
        self.resize(470, 580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("Forbind WhatsApp")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        layout.addWidget(title)

        help_text = QLabel(
            "Åbn WhatsApp på telefonen → Indstillinger/Menu → Forbundne enheder → "
            "Forbind en enhed, og scan QR-koden. Sessionen gemmes lokalt på denne pc."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("muted")
        layout.addWidget(help_text)

        self.status_label = QLabel("Starter WhatsApp…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.qr_label = QLabel("Afventer QR-kode…")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(360, 360)
        self.qr_label.setStyleSheet(
            "background: white; border: 1px solid #d8d8d8; border-radius: 8px; padding: 12px;"
        )
        layout.addWidget(self.qr_label, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.logout_button = QPushButton("Log WhatsApp ud")
        self.logout_button.clicked.connect(self.logout)
        buttons.addWidget(self.logout_button)
        close_button = QPushButton("Luk")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        status = self.bridge.status(timeout=0.5)
        state = str(status.get("state") or "unknown")
        detail = str(status.get("detail") or "")
        self.status_label.setText(detail or state)

        if state == "online":
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText("✓ WhatsApp er forbundet")
            self.qr_label.setStyleSheet(
                "background: #f0fff0; color: #107c10; border: 1px solid #9ed49e; "
                "border-radius: 8px; padding: 12px; font-size: 20px; font-weight: 650;"
            )
            return

        if self.bridge.qr_path.exists():
            pixmap = QPixmap(str(self.bridge.qr_path))
            if not pixmap.isNull():
                self.qr_label.setStyleSheet(
                    "background: white; border: 1px solid #d8d8d8; border-radius: 8px; padding: 12px;"
                )
                self.qr_label.setText("")
                self.qr_label.setPixmap(
                    pixmap.scaled(340, 340, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                return

        self.qr_label.setPixmap(QPixmap())
        if state == "runtime_missing":
            self.qr_label.setText("WhatsApp-runtime mangler i denne udviklingsinstallation.")
        elif state == "error":
            self.qr_label.setText("WhatsApp kunne ikke starte. Se status/log for detaljer.")
        else:
            self.qr_label.setText("Afventer QR-kode…")

    def logout(self) -> None:
        answer = QMessageBox.question(
            self,
            "Log WhatsApp ud",
            "Vil du logge den tilknyttede WhatsApp-session ud? Der skal scannes en ny QR-kode bagefter.",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.bridge.logout()
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "WhatsApp", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().closeEvent(event)

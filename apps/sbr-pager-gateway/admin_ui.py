from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import station_events
from admin_access_ui import request_admin_access
from storage import data_dir, get_setting, set_setting, utcnow_iso
from system_link import system_link_status, test_system_link


def _setting_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _setting_int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _format_time(value: str | None) -> str:
    if not value:
        return "Aldrig"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d-%m-%Y %H:%M:%S")
    except ValueError:
        return str(value)


class SystemLinkTestWorker(QThread):
    completed = Signal()
    failed = Signal(str)

    def __init__(self, timeout: float, parent=None) -> None:
        super().__init__(parent)
        self.timeout = timeout

    def run(self) -> None:
        try:
            test_system_link(self.timeout)
            self.completed.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class AdvancedSettingsDialog(QDialog):
    """PIN-protected operational settings.

    Everyday sender, recipient, station and alarm-filter administration remains
    login-free. Only transport mirroring and low-level timing live here.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.test_worker: SystemLinkTestWorker | None = None
        self.setWindowTitle("Avancerede indstillinger · SBR Pager Gateway")
        self.resize(680, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(15)

        title = QLabel("Avancerede indstillinger")
        title.setStyleSheet("font-size: 23px; font-weight: 650;")
        root.addWidget(title)

        info = QLabel(
            "Disse indstillinger vedrører transport, timing og systemintegration. "
            "Afsendere, modtagere, stationer og alarmfiltre administreres i den normale app."
        )
        info.setWordWrap(True)
        info.setObjectName("muted")
        root.addWidget(info)

        link_group = QGroupBox("System Link")
        link_form = QFormLayout(link_group)

        status = system_link_status()
        self.link_enabled = QCheckBox("Aktivér System Link")
        self.link_enabled.setChecked(bool(status["enabled"]))
        link_form.addRow("Status", self.link_enabled)

        if status["provisioned"]:
            registration_text = "Registreret automatisk"
        elif status["bootstrap_configured"]:
            registration_text = "Afventer automatisk registrering"
        else:
            registration_text = "Installer mangler provisioneringsdata"
        registration = QLabel(registration_text)
        registration.setObjectName("ok" if status["provisioned"] else "warn")
        link_form.addRow("Registrering", registration)

        client_id = QLabel(status["client_id"] or "—")
        client_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        link_form.addRow("Klient-ID", client_id)

        endpoint = QLabel(status["endpoint"] or "—")
        endpoint.setWordWrap(True)
        endpoint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        link_form.addRow("Endpoint", endpoint)

        self.link_timeout = QDoubleSpinBox()
        self.link_timeout.setRange(1.0, 30.0)
        self.link_timeout.setDecimals(1)
        self.link_timeout.setSuffix(" sek.")
        self.link_timeout.setValue(_setting_float("system_link_timeout_seconds", 5.0))
        link_form.addRow("Timeout", self.link_timeout)

        self.link_retry = QSpinBox()
        self.link_retry.setRange(5, 3600)
        self.link_retry.setSuffix(" sek.")
        self.link_retry.setValue(_setting_int("system_link_retry_seconds", 30))
        link_form.addRow("Retry efter fejl", self.link_retry)

        self.link_status = QLabel(
            f"Kø: {status['pending']} · Fejl: {status['failed']} · "
            f"Leveret: {status['sent']} · Sidste succes: {_format_time(status['last_success'])}"
        )
        self.link_status.setWordWrap(True)
        self.link_status.setObjectName("muted")
        link_form.addRow("Drift", self.link_status)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test System Link")
        self.test_button.clicked.connect(self.test_link)
        test_row.addWidget(self.test_button)
        test_row.addStretch()
        link_form.addRow("", test_row)
        root.addWidget(link_group)

        timing_group = QGroupBox("Timing og gateway")
        timing_form = QFormLayout(timing_group)

        self.sms_delay = QDoubleSpinBox()
        self.sms_delay.setRange(0.0, 300.0)
        self.sms_delay.setDecimals(1)
        self.sms_delay.setSingleStep(0.5)
        self.sms_delay.setSuffix(" sek.")
        self.sms_delay.setValue(_setting_float("sms_forward_delay_seconds", 0.0))
        timing_form.addRow("Delay før WhatsApp", self.sms_delay)

        self.modem_poll = QDoubleSpinBox()
        self.modem_poll.setRange(1.0, 30.0)
        self.modem_poll.setDecimals(1)
        self.modem_poll.setSingleStep(0.5)
        self.modem_poll.setSuffix(" sek.")
        self.modem_poll.setValue(_setting_float("modem_poll_seconds", 2.0))
        timing_form.addRow("SMS-modem polling", self.modem_poll)

        self.whatsapp_poll = QDoubleSpinBox()
        self.whatsapp_poll.setRange(1.0, 30.0)
        self.whatsapp_poll.setDecimals(1)
        self.whatsapp_poll.setSingleStep(0.5)
        self.whatsapp_poll.setSuffix(" sek.")
        self.whatsapp_poll.setValue(_setting_float("whatsapp_poll_seconds", 2.0))
        timing_form.addRow("WhatsApp-kø polling", self.whatsapp_poll)

        self.sending2_window = QSpinBox()
        self.sending2_window.setRange(15, 720)
        self.sending2_window.setSuffix(" min.")
        self.sending2_window.setValue(_setting_int("sending2_link_minutes", 120))
        timing_form.addRow("Sending 2 koblingsvindue", self.sending2_window)

        delay_help = QLabel(
            "Delay gælder kun før WhatsApp-afsendelse. SMS'en gemmes straks lokalt. "
            "System Link kører uafhængigt, og modemmet fortsætter med at læse nye SMS'er."
        )
        delay_help.setWordWrap(True)
        delay_help.setObjectName("muted")
        timing_form.addRow("", delay_help)
        root.addWidget(timing_group)

        system_group = QGroupBox("System")
        system_form = QFormLayout(system_group)
        data_label = QLabel(str(data_dir()))
        data_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        system_form.addRow("Lokal data", data_label)
        root.addWidget(system_group)

        root.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Annuller")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Gem")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def exec(self) -> int:  # noqa: A003
        if not request_admin_access(self.parent()):
            return QDialog.Rejected
        return super().exec()

    def save(self) -> None:
        enabling_link = self.link_enabled.isChecked()
        was_enabled = (get_setting("system_link_enabled", "0") or "0") == "1"
        if enabling_link and not was_enabled:
            set_setting("system_link_started_at", utcnow_iso())

        values = {
            "system_link_enabled": "1" if enabling_link else "0",
            "system_link_timeout_seconds": str(self.link_timeout.value()),
            "system_link_retry_seconds": str(self.link_retry.value()),
            "sms_forward_delay_seconds": str(self.sms_delay.value()),
            "modem_poll_seconds": str(self.modem_poll.value()),
            "whatsapp_poll_seconds": str(self.whatsapp_poll.value()),
            "sending2_link_minutes": str(self.sending2_window.value()),
        }
        for key, value in values.items():
            set_setting(key, value)

        station_events.EVENT_LINK_MINUTES = int(self.sending2_window.value())
        self.accept()

    def test_link(self) -> None:
        if self.test_worker and self.test_worker.isRunning():
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("Tester…")
        self.test_worker = SystemLinkTestWorker(self.link_timeout.value(), self)
        self.test_worker.completed.connect(self.on_test_ok)
        self.test_worker.failed.connect(self.on_test_failed)
        self.test_worker.finished.connect(self.on_test_finished)
        self.test_worker.start()

    def on_test_ok(self) -> None:
        QMessageBox.information(self, "System Link", "Forbindelsen svarede korrekt.")

    def on_test_failed(self, message: str) -> None:
        QMessageBox.warning(self, "System Link", message)

    def on_test_finished(self) -> None:
        self.test_button.setEnabled(True)
        self.test_button.setText("Test System Link")

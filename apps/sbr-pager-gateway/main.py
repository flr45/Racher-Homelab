from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from modem import ModemInfo, discover_modems
from sms_engine import SmsModemEngine
from storage import init_database, recent_messages


APP_STYLE = """
QWidget {
    background: #f3f3f3;
    color: #202020;
    font-family: "Segoe UI";
    font-size: 14px;
}
QMainWindow { background: #f3f3f3; }
QLabel#appTitle { font-size: 27px; font-weight: 650; }
QLabel#subtitle, QLabel#muted { color: #666666; }
QFrame#card {
    background: #ffffff;
    border: 1px solid #dddddd;
    border-radius: 10px;
}
QLabel#cardTitle {
    color: #666666;
    font-size: 12px;
    font-weight: 650;
}
QLabel#metric { font-size: 22px; font-weight: 650; }
QLabel#ok { color: #107c10; font-weight: 650; }
QLabel#warn { color: #9a6700; font-weight: 650; }
QLabel#bad { color: #c42b1c; font-weight: 650; }
QPushButton {
    background: #ffffff;
    border: 1px solid #c9c9c9;
    border-radius: 6px;
    padding: 8px 13px;
    font-weight: 500;
}
QPushButton:hover { background: #f8f8f8; border-color: #999999; }
QPushButton:disabled { color: #999999; background: #eeeeee; }
QPushButton#primary {
    color: white;
    background: #0067c0;
    border-color: #0067c0;
}
QPushButton#primary:hover { background: #005a9e; }
QPushButton#danger {
    color: white;
    background: #c42b1c;
    border-color: #c42b1c;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #dddddd;
    border-radius: 8px;
    gridline-color: #eeeeee;
    selection-background-color: #cce4f7;
    selection-color: #202020;
}
QHeaderView::section {
    background: #fafafa;
    color: #555555;
    border: 0;
    border-bottom: 1px solid #dddddd;
    padding: 8px;
    font-size: 12px;
    font-weight: 650;
}
"""


class ModemScanWorker(QThread):
    finished_scan = Signal(list)

    def run(self) -> None:
        self.finished_scan.emit(discover_modems())


class GatewayWorker(QThread):
    state_changed = Signal(str, str)
    message_received = Signal(dict)

    def __init__(self, port_name: str, parent=None) -> None:
        super().__init__(parent)
        self.port_name = port_name
        self.engine: SmsModemEngine | None = None

    def run(self) -> None:
        self.engine = SmsModemEngine(
            self.port_name,
            on_status=lambda state, detail: self.state_changed.emit(state, detail),
            on_message=lambda message: self.message_received.emit(message),
        )
        self.engine.run()

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()


class MetricCard(QFrame):
    def __init__(self, title: str, metric: str, detail: str = "", state: str = "warn") -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.metric_label = QLabel(metric)
        self.metric_label.setObjectName("metric")
        self.state_label = QLabel(detail)
        self.state_label.setObjectName(state)
        self.state_label.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(self.metric_label)
        layout.addWidget(self.state_label)
        layout.addStretch()

    def set_status(self, metric: str, detail: str, state: str = "ok") -> None:
        self.metric_label.setText(metric)
        self.state_label.setText(detail)
        self.state_label.setObjectName(state)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        init_database()

        self.setWindowTitle("SBR Pager Gateway")
        self.resize(1180, 760)
        self.scan_worker: ModemScanWorker | None = None
        self.gateway_worker: GatewayWorker | None = None
        self.selected_modem: ModemInfo | None = None

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(28, 24, 28, 28)
        main.setSpacing(18)

        header = QHBoxLayout()
        brand = QVBoxLayout()
        title = QLabel("SBR Pager Gateway")
        title.setObjectName("appTitle")
        subtitle = QLabel("Lokal SMS-modem → WhatsApp gateway til Windows")
        subtitle.setObjectName("subtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch()

        self.scan_button = QPushButton("Søg efter SMS-modem")
        self.scan_button.clicked.connect(self.scan_modem)
        header.addWidget(self.scan_button)

        self.start_button = QPushButton("Start gateway")
        self.start_button.setObjectName("primary")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_gateway)
        header.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_gateway)
        header.addWidget(self.stop_button)
        main.addLayout(header)

        card_grid = QGridLayout()
        card_grid.setHorizontalSpacing(14)
        card_grid.setVerticalSpacing(14)

        self.modem_card = MetricCard("SMS MODEM", "Ikke fundet", "Tilslut et USB GSM/SMS-modem", "warn")
        self.signal_card = MetricCard("SIGNAL", "—", "Afventer modem", "warn")
        self.whatsapp_card = MetricCard("WHATSAPP", "Ikke konfigureret", "WhatsApp-modulet kobles på næste", "warn")
        self.gateway_card = MetricCard("GATEWAY", "Stoppet", "Ingen nye SMS læses endnu", "warn")

        card_grid.addWidget(self.modem_card, 0, 0)
        card_grid.addWidget(self.signal_card, 0, 1)
        card_grid.addWidget(self.whatsapp_card, 0, 2)
        card_grid.addWidget(self.gateway_card, 0, 3)
        main.addLayout(card_grid)

        section = QHBoxLayout()
        section_title = QLabel("Seneste beskeder")
        section_title.setStyleSheet("font-size: 18px; font-weight: 650;")
        section.addWidget(section_title)
        section.addStretch()
        refresh_button = QPushButton("Opdater")
        refresh_button.clicked.connect(self.refresh_history)
        section.addWidget(refresh_button)
        main.addLayout(section)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Tid", "Afsender", "Besked", "Status", "WhatsApp"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 490)
        self.table.setColumnWidth(3, 160)
        main.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.modem_detail = QLabel("Modem: ikke fundet")
        self.modem_detail.setObjectName("muted")
        bottom.addWidget(self.modem_detail)
        bottom.addStretch()

        self.history_button = QPushButton("Historik")
        self.senders_button = QPushButton("Afsendere")
        self.recipients_button = QPushButton("Modtagere")
        self.settings_button = QPushButton("Indstillinger")
        for button in (
            self.history_button,
            self.senders_button,
            self.recipients_button,
            self.settings_button,
        ):
            button.clicked.connect(self.show_module_placeholder)
            bottom.addWidget(button)
        main.addLayout(bottom)

        self.refresh_history()
        self.scan_modem()

    def scan_modem(self) -> None:
        if self.gateway_worker and self.gateway_worker.isRunning():
            QMessageBox.information(self, "SBR Pager Gateway", "Stop gatewayen før der søges efter modem igen.")
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return

        self.scan_button.setEnabled(False)
        self.scan_button.setText("Søger…")
        self.start_button.setEnabled(False)
        self.modem_card.set_status("Søger…", "Scanner Windows COM-porte", "warn")

        self.scan_worker = ModemScanWorker(self)
        self.scan_worker.finished_scan.connect(self.on_modems_found)
        self.scan_worker.start()

    def on_modems_found(self, modems: list[ModemInfo]) -> None:
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Søg efter SMS-modem")

        if not modems:
            self.selected_modem = None
            self.modem_card.set_status("Ikke fundet", "Ingen AT-kompatible modemmer fundet", "bad")
            self.signal_card.set_status("—", "Kontrollér dongle og Windows-driver", "bad")
            self.modem_detail.setText("Modem: ikke fundet")
            self.start_button.setEnabled(False)
            return

        modem = modems[0]
        self.selected_modem = modem
        display_name = " ".join(
            part for part in (modem.manufacturer, modem.model)
            if part and part != "Ukendt"
        ).strip() or "GSM modem"

        self.modem_card.set_status(display_name, f"{modem.port} · SIM: {modem.sim_status}", "ok")
        if modem.signal_percent is None:
            self.signal_card.set_status("Ukendt", modem.network_status, "warn")
        else:
            self.signal_card.set_status(f"{modem.signal_percent}%", modem.network_status, "ok")

        self.modem_detail.setText(f"Modem: {display_name} på {modem.port}")
        self.start_button.setEnabled(True)

    def start_gateway(self) -> None:
        if not self.selected_modem:
            return
        if self.gateway_worker and self.gateway_worker.isRunning():
            return

        self.gateway_worker = GatewayWorker(self.selected_modem.port, self)
        self.gateway_worker.state_changed.connect(self.on_gateway_state)
        self.gateway_worker.message_received.connect(self.on_message_received)
        self.gateway_worker.finished.connect(self.on_gateway_finished)
        self.gateway_worker.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.scan_button.setEnabled(False)
        self.gateway_card.set_status("Starter…", f"Åbner {self.selected_modem.port}", "warn")

    def stop_gateway(self) -> None:
        if self.gateway_worker and self.gateway_worker.isRunning():
            self.stop_button.setEnabled(False)
            self.gateway_card.set_status("Stopper…", "Lukker modemforbindelsen sikkert", "warn")
            self.gateway_worker.stop()

    def on_gateway_state(self, state: str, detail: str) -> None:
        if state == "online":
            self.gateway_card.set_status("Aktiv", detail, "ok")
        elif state == "connecting":
            self.gateway_card.set_status("Forbinder…", detail, "warn")
        elif state == "offline":
            self.gateway_card.set_status("Fejl", detail, "bad")
        else:
            self.gateway_card.set_status("Stoppet", detail, "warn")

    def on_gateway_finished(self) -> None:
        self.gateway_card.set_status("Stoppet", "Gatewayen er stoppet", "warn")
        self.start_button.setEnabled(self.selected_modem is not None)
        self.stop_button.setEnabled(False)
        self.scan_button.setEnabled(True)

    def on_message_received(self, _message: dict) -> None:
        self.refresh_history()

    def refresh_history(self) -> None:
        rows = recent_messages(150)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            timestamp = self._format_time(str(row["received_at"]))
            status = self._friendly_status(str(row["processing_status"]))
            sent_count = int(row.get("sent_count") or 0)
            failed_count = int(row.get("failed_count") or 0)
            if sent_count:
                whatsapp = f"Sendt til {sent_count}"
            elif failed_count:
                whatsapp = f"Fejl ({failed_count})"
            else:
                whatsapp = "—"

            values = [
                timestamp,
                str(row["sender"]),
                str(row["body"]),
                status,
                whatsapp,
            ]
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(value))

        self.table.resizeRowsToContents()

    @staticmethod
    def _format_time(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%d-%m-%Y %H:%M")
        except ValueError:
            return value

    @staticmethod
    def _friendly_status(value: str) -> str:
        labels = {
            "received": "Modtaget",
            "bootstrap_skipped": "Gammel SMS · ikke sendt",
            "accepted": "Godkendt",
            "rejected": "Afvist",
            "forwarded": "Videresendt",
            "failed": "Fejl",
        }
        return labels.get(value, value)

    def show_module_placeholder(self) -> None:
        sender = self.sender()
        name = sender.text() if isinstance(sender, QPushButton) else "Modul"
        QMessageBox.information(
            self,
            name,
            f"{name}-modulet bygges som en separat del af SBR Pager Gateway.\n\n"
            "SMS-modtagelse og lokal historik er den første aktive kerne.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.gateway_worker and self.gateway_worker.isRunning():
            self.gateway_worker.stop()
            self.gateway_worker.wait(3500)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SBR Pager Gateway")
    app.setOrganizationName("SBR")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sys

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from modem import ModemInfo, discover_modems


APP_STYLE = """
QWidget {
    background: #0b0f14;
    color: #eef4fb;
    font-family: "Segoe UI";
    font-size: 14px;
}
QMainWindow { background: #0b0f14; }
QLabel#appTitle { font-size: 28px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #98a9bd; }
QFrame#card {
    background: #121923;
    border: 1px solid #263447;
    border-radius: 16px;
}
QLabel#cardTitle {
    color: #98a9bd;
    font-size: 12px;
    font-weight: 700;
}
QLabel#metric { font-size: 24px; font-weight: 700; }
QLabel#ok { color: #39d98a; font-weight: 700; }
QLabel#warn { color: #ffd166; font-weight: 700; }
QLabel#bad { color: #ff6b6b; font-weight: 700; }
QPushButton {
    background: #1c2a3a;
    border: 1px solid #263447;
    border-radius: 10px;
    padding: 10px 14px;
    font-weight: 600;
}
QPushButton:hover { background: #24374b; }
QPushButton#primary {
    background: #1d6a49;
    border-color: #2a9167;
}
QPushButton#primary:hover { background: #247e58; }
QTableWidget {
    background: #121923;
    border: 1px solid #263447;
    border-radius: 14px;
    gridline-color: #213044;
    selection-background-color: #1c2a3a;
}
QHeaderView::section {
    background: #121923;
    color: #98a9bd;
    border: 0;
    border-bottom: 1px solid #263447;
    padding: 9px;
    font-size: 12px;
    font-weight: 700;
}
"""


class ModemScanWorker(QThread):
    finished_scan = Signal(list)

    def run(self) -> None:
        self.finished_scan.emit(discover_modems())


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
        self.setWindowTitle("SBR Pager Gateway")
        self.resize(1180, 760)
        self.worker: ModemScanWorker | None = None

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(28, 24, 28, 28)
        main.setSpacing(18)

        header = QHBoxLayout()
        brand = QVBoxLayout()
        title = QLabel("SBR Pager Gateway")
        title.setObjectName("appTitle")
        subtitle = QLabel("SMS-modem → WhatsApp · Windows gateway")
        subtitle.setObjectName("subtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch()

        self.scan_button = QPushButton("Søg efter SMS-modem")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.scan_modem)
        header.addWidget(self.scan_button)
        main.addLayout(header)

        card_grid = QGridLayout()
        card_grid.setHorizontalSpacing(14)
        card_grid.setVerticalSpacing(14)

        self.modem_card = MetricCard("SMS MODEM", "Ikke fundet", "Tilslut et USB GSM/SMS-modem", "warn")
        self.signal_card = MetricCard("SIGNAL", "—", "Afventer modem", "warn")
        self.whatsapp_card = MetricCard("WHATSAPP", "Ikke klar", "Opsætning kommer i næste trin", "warn")
        self.gateway_card = MetricCard("GATEWAY", "Stoppet", "Ingen beskeder videresendes endnu", "warn")

        card_grid.addWidget(self.modem_card, 0, 0)
        card_grid.addWidget(self.signal_card, 0, 1)
        card_grid.addWidget(self.whatsapp_card, 0, 2)
        card_grid.addWidget(self.gateway_card, 0, 3)
        main.addLayout(card_grid)

        section = QHBoxLayout()
        section_title = QLabel("Seneste beskeder")
        section_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        section.addWidget(section_title)
        section.addStretch()
        main.addLayout(section)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Tid", "Afsender", "Besked", "Status", "WhatsApp"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        main.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.modem_detail = QLabel("Modem: ikke fundet")
        self.modem_detail.setObjectName("muted")
        bottom.addWidget(self.modem_detail)
        bottom.addStretch()

        for text in ("Historik", "Afsendere", "Modtagere", "Indstillinger"):
            bottom.addWidget(QPushButton(text))
        main.addLayout(bottom)

    def scan_modem(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Søger…")
        self.modem_card.set_status("Søger…", "Scanner Windows COM-porte", "warn")

        self.worker = ModemScanWorker(self)
        self.worker.finished_scan.connect(self.on_modems_found)
        self.worker.start()

    def on_modems_found(self, modems: list[ModemInfo]) -> None:
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Søg efter SMS-modem")

        if not modems:
            self.modem_card.set_status("Ikke fundet", "Ingen AT-kompatible modemmer fundet", "bad")
            self.signal_card.set_status("—", "Kontrollér dongle og Windows-driver", "bad")
            self.modem_detail.setText("Modem: ikke fundet")
            return

        modem = modems[0]
        display_name = " ".join(part for part in (modem.manufacturer, modem.model) if part and part != "Ukendt").strip()
        display_name = display_name or "GSM modem"
        self.modem_card.set_status(display_name, f"{modem.port} · SIM: {modem.sim_status}", "ok")

        if modem.signal_percent is None:
            self.signal_card.set_status("Ukendt", modem.network_status, "warn")
        else:
            self.signal_card.set_status(f"{modem.signal_percent}%", modem.network_status, "ok")

        self.modem_detail.setText(f"Modem: {display_name} på {modem.port}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SBR Pager Gateway")
    app.setOrganizationName("SBR")
    app.setStyleSheet(APP_STYLE)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

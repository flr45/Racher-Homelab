# SBR Pager Gateway

Selvstændigt Windows-program til at modtage SMS direkte fra et USB GSM/SMS-modem og videresende godkendte beskeder til WhatsApp.

## Mål

Programmet skal kunne installeres på en almindelig Windows-maskine uden Raspberry Pi, Docker, Python eller Node installeret separat.

Første version bygges omkring tre lokale moduler:

1. **SMS modem** – finder en kompatibel COM-port, tester AT-kommandoer og læser SMS'er.
2. **Gateway** – filtrering, deduplikering, kø, historik og SQLite-data.
3. **WhatsApp** – lokal WhatsApp-session og videresendelse til konfigurerede modtagere.

## V0.1

Den første milepæl er Windows-GUI + automatisk modemdetektion.

Kør udviklingsversionen:

```powershell
cd apps\sbr-pager-gateway
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Produktnavn

- Visningsnavn: `SBR Pager Gateway`
- Windows app-id: `dk.sbr.pagergateway`
- Planlagt installer: `SBR-Pager-Gateway-Setup.exe`

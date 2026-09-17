# SBR Pager Gateway

Selvstændigt Windows-program til at modtage SMS direkte fra et USB GSM/SMS-modem og videresende godkendte beskeder til WhatsApp.

## Isolation

SBR Pager Gateway er et nyt program med egen kode under `apps/sbr-pager-gateway/`. Det eksisterende SMS-gateway/WhatsApp-system under `services/` ændres ikke af dette projekt.

## Mål

Programmet skal kunne installeres på en almindelig Windows-maskine uden Raspberry Pi, Docker, Python eller Node installeret separat.

Første version bygges omkring tre lokale moduler:

1. **SMS modem** – finder en kompatibel COM-port, tester AT-kommandoer og læser SMS'er.
2. **Gateway** – filtrering, deduplikering, kø, historik og SQLite-data.
3. **WhatsApp** – lokal WhatsApp-session og videresendelse til konfigurerede modtagere.

## V0.1

Den første testversion indeholder Windows-GUI, automatisk modemdetektion, lokal SQLite-historik, afsender-/modtageradministration, QR-login til WhatsApp og videresendelse af godkendte SMS'er.

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

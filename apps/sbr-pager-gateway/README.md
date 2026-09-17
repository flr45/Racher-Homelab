# SBR Pager Gateway

Selvstændigt Windows-program til at modtage SMS direkte fra et USB GSM/SMS-modem og videresende godkendte beskeder til WhatsApp.

## Isolation

SBR Pager Gateway er et nyt program med egen kode under `apps/sbr-pager-gateway/`. Det eksisterende SMS-gateway/WhatsApp-system under `services/` ændres ikke af dette projekt.

## Lokal brugerflade

Programmet kører lokalt på Windows-pc'en og kræver derfor ikke login til den normale brugerflade. Afsendere, WhatsApp-modtagere, stationer, personplacering, alarmfiltre, historik og statistik administreres direkte i programmet.

Avancerede driftsindstillinger ligger separat under **Avanceret** (`Ctrl+Shift+F12`). Her findes blandt andet System Link, SMS-delay, modem-polling, WhatsApp-køinterval, retry/timeout og Sending 2-koblingsvindue.

## System Link

System Link er en valgfri ekstern integration, som kan spejle godkendte alarmdata til et konfigureret HTTP/HTTPS-endpoint uden at blokere WhatsApp. Leveringer køes lokalt og retry'es ved fejl. En delt nøgle bruges kun til HMAC-SHA256-signering og sendes ikke i requesten.

Payloaden indeholder samme alarmtekst som WhatsApp samt relevante metadata som afsender, modtagelsestid, station, alarmtype, adresse og hændelses-/Sending 2-data, når de findes.

## Mål

Programmet skal kunne installeres på en almindelig Windows-maskine uden Raspberry Pi, Docker, Python eller Node installeret separat.

Første version bygges omkring fire lokale moduler:

1. **SMS modem** – finder en kompatibel COM-port, tester AT-kommandoer og læser SMS'er.
2. **Gateway** – filtrering, deduplikering, kø, historik, stationer og SQLite-data.
3. **WhatsApp** – lokal WhatsApp-session og videresendelse til konfigurerede modtagere.
4. **System Link** – valgfri, uafhængig spejling til et eksternt endpoint.

## V0.1

Den første testversion indeholder Windows-GUI, automatisk modemdetektion, lokal SQLite-historik, afsender-/modtageradministration, stationsfiltre, alarmhændelser/statistik, QR-login til WhatsApp, videresendelse af godkendte SMS'er og avancerede driftsindstillinger.

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

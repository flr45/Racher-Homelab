# SBR Pager Gateway

Selvstændigt Windows-program til at modtage SMS direkte fra et USB GSM/SMS-modem og videresende godkendte beskeder til WhatsApp.

## Isolation

SBR Pager Gateway er et nyt program med egen kode under `apps/sbr-pager-gateway/`. Det eksisterende SMS-gateway/WhatsApp-system under `services/` ændres ikke af dette projekt.

## Lokal brugerflade

Programmet kører lokalt på Windows-pc'en og kræver derfor ikke login til den normale brugerflade. Afsendere, WhatsApp-modtagere, stationer, personplacering, alarmfiltre, historik og statistik administreres direkte i programmet.

Avancerede driftsindstillinger ligger separat under **Avanceret** (`Ctrl+Shift+F12`). Første gang vælges en lokal 6-cifret admin-PIN. PIN'en gemmes ikke i klartekst; kun PBKDF2-salt/hash gemmes lokalt.

## System Link

System Link spejler godkendte alarmdata til Pi/homelab uden at blokere WhatsApp. Leveringer køes lokalt og retry'es ved fejl.

Den færdige release-installer kan provisionere System Link helt automatisk:

1. Installeren indeholder HTTPS provisionerings-endpoint + en engangskode.
2. Første start registrerer pc'en automatisk hos System Link Receiver.
3. Receiveren returnerer et unikt klient-ID og en unik klientnøgle til netop den pc.
4. Klientnøglen gemmes krypteret lokalt med Windows DPAPI.
5. System Link aktiveres automatisk og efterfølgende requests HMAC-signeres med klientens egen nøgle.

Den permanente master-nøgle ligger kun på Pi'en. Den er aldrig en del af Windows-installeren. Engangskoden kan kun provisionere én ny installation og er værdiløs efter brug.

Payloaden indeholder samme alarmtekst som WhatsApp samt relevante metadata som afsender, modtagelsestid, station, alarmtype, adresse og hændelses-/Sending 2-data, når de findes.

## Mål

Programmet skal kunne installeres på en almindelig Windows-maskine uden Raspberry Pi, Docker, Python, Node, Tailscale eller andre separate runtime-installationer.

Programmet bygges omkring fire lokale moduler:

1. **SMS modem** – finder en kompatibel COM-port, tester AT-kommandoer og læser SMS'er.
2. **Gateway** – filtrering, deduplikering, kø, historik, stationer og SQLite-data.
3. **WhatsApp** – lokal WhatsApp-session og videresendelse til konfigurerede modtagere.
4. **System Link** – uafhængig HTTPS-spejling med automatisk klientprovisionering.

## V0.1

Testversionen indeholder Windows-GUI, automatisk modemdetektion, lokal SQLite-historik, afsender-/modtageradministration, stationsfiltre, alarmhændelser/statistik, QR-login til WhatsApp, videresendelse af godkendte SMS'er, System Link og PIN-beskyttede avancerede driftsindstillinger.

Kør udviklingsversionen:

```powershell
cd apps\sbr-pager-gateway
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

`build_config.py` indeholder tomme development-placeholders. Release-workflowet overskriver dem kun i CI-workspacet umiddelbart før PyInstaller-builden. Rigtige provisioneringskoder må aldrig committes.

## Produktnavn

- Visningsnavn: `SBR Pager Gateway`
- Windows app-id: `dk.sbr.pagergateway`
- Installer: `SBR-Pager-Gateway-Setup.exe`

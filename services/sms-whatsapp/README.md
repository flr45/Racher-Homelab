# SBR Pager · SMS → WhatsApp

SMS fra godkendte afsendere samles som alarmhændelser og videresendes til
aktive WhatsApp-modtagere via OpenWA. Stationsvalg og Test opt-in bevares.

## Drift

`SIM800C / Huawei USB → SMS Gateway → vedvarende WhatsApp-kø → OpenWA`

Alternativt: `Cudy LT300 LAN → AT-webformular → samme SMS Gateway og kø`.
Cudy-adapteren er forberedt ud fra den officielle LT300-demo. Den skal
idriftsættes på den konkrete router/firmware, før USB erstattes. Den modtager
SMS; udgående SMS er fortsat en USB-funktion.

## Brugerflade

- Overblik med modem, WhatsApp, alarmhændelser, modtagere og leveringskø.
- Brugere & stationer med almindelige stationsvalg og separat Test opt-in.
- Alarmhistorik, statistik, kort og hændelsernes Sending 2-tidslinjer.
- Forbindelser med USB/Cudy-status og en Cudy-test uden netværksændringer.
- Indstillinger med godkendte SMS-afsendere og pre-alarm ventetid.
- Tekniske logs er foldet sammen; status opdateres uden at genindlæse formularer.

## Leveringssikkerhed

Alle modtagerjobs gemmes, før modemmet får kvittering. WhatsApp-kald kører i
baggrunden. Fejl forsøges igen med stigende ventetid; pending jobs genoptages
efter genstart. Gamle alarmer, pauserede/slettede modtagere og fravalgte
stationer kontrolleres før et nyt forsøg. OpenWA skal kvittere med besked-id,
før en levering mærkes sendt. Et netværksbrud efter faktisk afsendelse men før
kvittering kan stadig give en gentagelse; OpenWA-grænsefladen garanterer ikke
præcis én levering.

Kør én Gunicorn worker, som i Dockerfile. Ingest og køarbejder har separate
låse, så langsom netværksafsendelse ikke spærrer for modtagelsen.
`SMS_WHATSAPP_RETRY_WORKER=false` stopper baggrundsarbejderen og bruges kun i
test/vedligehold. I normal drift skal den være `true`.

## Installation

Se [opdaterings- og Cudy-guide](../../docs/SBR-PAGER-UPDATE.md).
Programmet bruger eksisterende SQLite- og OpenWA-volumener.
Admin bindes fortsat til konfigureret localhost/Tailscale-IP; Pagerens
opstart efter Tailscale styres fortsat af `sbr-pager-boot.service`.

## Test

```bash
python -m pip install -r services/sms-whatsapp/requirements.txt pyserial==3.5 -r tests/pager/requirements.txt
python -m pytest -q tests/pager
```

Regressionstests dækker kø/genstart, langsom WhatsApp, deduplikering,
stationsvalg, sletning, ugyldigt input, dansk tegnsæt og Cudys login/AT-formular.
Dockerfile indeholder desuden de eksisterende alarm-, modem- og PDU-kontroller.

### Afsenderfilter

Indstillinger har en afkrydsningsboks til at videresende SMS fra alle
telefonnumre. Valget gemmes i den eksisterende runtime-indstillingstabel og
bevares efter genstart. Slå den fra for at bruge listen med godkendte numre
igen. Stationsvalg, Test-opt-in og modemstøjsfilter gælder fortsat; tidligere
afviste SMS genudsendes ikke. API-token og administratorlogin kræves stadig.

### Drift og test

Overblik viser separat DNS/HTTPS-kontrol, SMS-status og OpenWA-status.
`/enkelt-test` køer en manuel test til én aktiv bruger og viser OpenWA-id og
svartid; resultatet er ikke telefonens leverings-/læsekvittering. Testen
genudsendes ikke automatisk efter fejl eller afbrudt afsendelse.
`/diagnostik` viser gemte afvisningsårsager og leveringsfejl uden netværkskald
eller afsendelse. Alle sider og status-API'er kræver administratorlogin.

Operational administration now includes daily consistent SQLite backups with configuration restore, bounded maintenance/pilot modes, explicit approval of stale or interrupted deliveries, searchable message history, per-recipient timelines, configuration audit, routing preview, startup checks, sanitized diagnostic export and a private status-only screen. Test SMS are explicitly marked; potential duplicate text is flagged without suppressing legitimate alarms. Subscription lookups are batched and existing databases receive time/status indexes. The worker starts only after safety controls and crash recovery are initialized. Optional retention defaults to disabled. See `docs/SBR-PAGER-UPDATE.md` for all controls, backup limits and offline database recovery.

Recipient station matrices support confirmed bulk updates; recipient failure history includes measured attempts and current unresolved jobs. Daily reports use Danish calendar days and distinguish processing, queue wait and OpenWA response time. Multipart diagnostics use the active reader's actual PDU scans, with bounded text-free status storage. Backups can be restored into an isolated test database manually or weekly. The mobile menu collapses, history filters stay per login session, and separate LT300/USB commissioning checklists record manual physical-test results. These controls do not install the software or send commissioning messages automatically; LT300 outgoing SMS remains unsupported pending firmware verification.

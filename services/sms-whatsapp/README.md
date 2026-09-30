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

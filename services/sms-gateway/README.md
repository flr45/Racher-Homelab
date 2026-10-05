# SMS Gateway

Den eksisterende USB-reader understøtter SIM800C (CH340/CH341), Huawei og
AT-kompatible modemer. Kun én reader ejer USB-porten. Der er nu også en
netværksreader til Cudy LT300s originale LuCI AT-webformular.

## Valg af SMS-kilde

| Kilde | Compose-fil | Modtagelse | Afsendelse |
|---|---|---|---|
| USB, standard | `compose/sms-gateway/docker-compose.yml` | Ja | Ja |
| Cudy LT300, skal routertestes | `compose/sms-gateway/cudy.yml` | Forberedt | Ikke understøttet af adapteren |

Kør kun én af dem ad gangen. Cudy-filen har ingen USB-device mapping,
men genbruger samme `sms-gateway` projektnavn, container og datavolumen.
Boot og watchdog vælger fil ud fra `SMS_MODEM_DRIVER=usb` eller `cudy` i `.env`.
Se [opdaterings- og Cudy-guide](../../docs/SBR-PAGER-UPDATE.md).

Begge readers genbruger PDU-dekodning, delte SMS'er, pre-alarmer,
Sending 2/parent-kobling, redigering af persondata og direkte SBR Pager-ingest.
SMS slettes først fra kilden, når importen er kvitteret. Modem-id'er gemmes
som kvitteringer, så et nyt importforsøg ikke opretter endnu en statuskommando.
En beskadiget PDU stopper ikke behandlingen af de øvrige alarmer.

Cudy-adapteren følger det observerede login og AT-formularformat i Cudys
[LT300-emulator](https://support.cudy.com/emulator/LT300/).
Det er en firmwareafhængig webgrænseflade, ikke en dokumenteret SMS-API.
Den ændrer ikke APN, netværk, SIM-lager eller radiofunktion og udfører ingen
router-reset. SMS-tilstanden gendannes efter læsning. Routerens egen
SMS-funktion må ikke samtidig slette beskeder før import.

## API

Alle `/api/*`-kald kræver `Authorization: Bearer $SMS_GATEWAY_API_TOKEN`.
`GET /health` er offentligt og viser, hvis reader-status er forældet.

- `POST /api/incoming`, `GET /api/messages`
- `POST /api/outgoing`, `POST /api/outgoing/claim`
- `GET /api/outgoing/<id>`, `POST /api/outgoing/<id>/complete`
- `POST /api/commands/claim`, `POST /api/commands/<id>/complete`
- `POST /api/cudy/probe`: godkendt test af login, SIM, signal, registrering og lager.

Ved Cudy-kilden afvises nye udgående SMS med HTTP 409; eksisterende
USB-outboxjobs beholdes og claim'es ikke. SMS-statuskommandoer kan derfor
ikke besvares via denne adapter. Vagtbytte-forwarding forbliver styret af den
eksisterende `VAGTBYTTE_FORWARD_ENABLED` og er som standard slået fra i Compose.

## Prøve uden fysisk router

```bash
python -m pip install -r services/sms-whatsapp/requirements.txt pyserial==3.5 -r tests/pager/requirements.txt
python -m pytest -q tests/pager
```

Testene bruger simulerede router- og WhatsApp-svar og sender ingen beskeder.

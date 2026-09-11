# SMS → WhatsApp Gateway

Selvstændigt Racher-Homelab-modul som læser nye indgående SMS'er fra den eksisterende `sms-gateway`, godkender afsenderen mod en administrerbar allowlist og videresender teksten til aktive WhatsApp-modtagere via OpenWA.

## Funktioner

- Flere godkendte SMS-afsendere, administreret i web-UI.
- Flere WhatsApp-modtagere med aktiv/pause og sletning.
- Første start springer eksisterende SMS-historik over, så gamle alarmer ikke udsendes.
- Deduplikering via SMS-gatewayens message-id.
- Standard-kommandoerne `status`, `server status` og `serverstatus` videresendes ikke.
- Log over accepterede/afviste SMS'er og WhatsApp-leveringer.
- Testbesked fra admin-siden.
- OpenWA-status på dashboardet.
- Fejl i OpenWA påvirker ikke SMS-modemmet, Vagtbytte eller den eksisterende SMS-kø.

## Dataflow

`Huawei E180 → sms-gateway → sms-whatsapp poller → allowlist → OpenWA → WhatsApp-modtagere`

Polleren bruger den eksisterende `GET /api/messages` med `SMS_GATEWAY_API_TOKEN`. Modemporten åbnes derfor fortsat kun af den eksisterende modem-reader.

## OpenWA

OpenWA skal være netværksmæssigt tilgængelig fra `racher-sms-whatsapp`. Standard er `http://openwa:2785/api`. API-key sendes i `X-API-Key`, og `SMS_WHATSAPP_OPENWA_SESSION_ID` skal være OpenWA-sessionens id/UUID.

Hvis din eksisterende OpenWA-container (`racher-pager-openwa`) kører i en anden Compose-stack, kan den uden at eksponere API'et offentligt kobles på det fælles backend-netværk:

```bash
docker network connect backend racher-pager-openwa
```

Derefter skal containeren kunne nås på et navn på det netværk. Hvis DNS-navnet `openwa` ikke findes dér, kan `SMS_WHATSAPP_OPENWA_URL` sættes til containerens navn, fx `http://racher-pager-openwa:2785/api`.

## Start

```bash
cd ~/Racher-Homelab
docker compose --env-file .env -f compose/sms-whatsapp/compose.yml up -d --build
```

Admin-UI bindes som standard kun på hostens `127.0.0.1:8091`. Eksponér den via din eksisterende reverse proxy/Tailscale, hvis den skal åbnes fra andre enheder.

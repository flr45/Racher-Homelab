# SBR Pager Gateway – Windows architecture

Dette er et nyt, selvstændigt program. Eksisterende services under `services/sms-gateway/` og `services/sms-whatsapp/` ændres ikke.

## Moduler

- `modem.py` – registrering og kommunikation med USB GSM/SMS-modem via Windows COM-port.
- `main.py` – Windows GUI.
- Kommende `database.py` – lokal SQLite og migrations.
- Kommende `sms_engine.py` – SMS-modtagelse, PDU, deduplikering og kø.
- Kommende `whatsapp_engine.py` – WhatsApp-session og leveringer.
- Kommende UI-sider for historik, afsendere, modtagere, stationer, hændelser, statistik og indstillinger.

## Isolationsregel

Windows-arbejdet må kun skrive under `apps/sbr-pager-gateway/` samt eventuelle nye build/workflow-filer specifikt for dette program. Eksisterende produktionskode ændres ikke.

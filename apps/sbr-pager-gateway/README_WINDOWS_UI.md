# Windows UI structure

SBR Pager Gateway skal bruge et almindeligt Windows-layout frem for at efterligne web-UI'et visuelt.

Foreslået hovednavigation:

- Dashboard
- Historik
- Afsendere
- Modtagere
- WhatsApp
- Indstillinger

Dashboardet viser modemstatus, signal, WhatsApp-status, gatewaystatus og seneste beskeder.

Alle funktioner implementeres i det nye Windows-projekt under `apps/sbr-pager-gateway/`. Eksisterende services ændres ikke.

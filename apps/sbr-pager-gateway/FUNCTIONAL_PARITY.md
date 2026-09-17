# Funktionel paritet

SBR Pager Gateway for Windows er et nyt, separat program. Det eksisterende SMS/WhatsApp-system må ikke ændres.

Windows-programmet må gerne have et naturligt Windows-look. Kravet er i stedet, at det overtager de samme centrale funktioner som det nuværende system.

## Funktioner der skal med

- Automatisk registrering og status på SMS-modem.
- Modtagelse af SMS direkte fra USB GSM/SMS-dongle.
- Deduplikering af indgående beskeder.
- Godkendte SMS-afsendere med aktiv/pause/slet.
- WhatsApp-modtagere med aktiv/pause/slet.
- Videresendelse af godkendte SMS'er til aktive WhatsApp-modtagere.
- Ignorering af kommandoerne `status`, `server status` og `serverstatus`.
- Historik over indgående SMS'er og leveringer.
- Leveringsstatus og fejlvisning.
- Testbesked.
- WhatsApp-status.
- Lokal SQLite-database.
- Første start må ikke videresende gammel SMS-historik.
- Baggrundskørsel/systembakke.
- Autostart med Windows.

## UI-retning

Brug native Windows-komponenter og almindelig Windows-navigation, men bevar den samme informationsstruktur: status, historik, afsendere, modtagere, test og indstillinger.

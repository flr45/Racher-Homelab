# Sikkerhed

- Del aldrig `.env`, adgangskoder, API-nøgler eller private certifikater i GitHub.
- Eksponér kun port 80 og 443 mod internettet. Portainer, Nginx Proxy Manager admin og databaser bør kun være tilgængelige lokalt eller via Tailscale.
- Brug stærke, unikke adgangskoder og tofaktor-login, hvor det understøttes.
- Hold Raspberry Pi OS og Docker-images opdaterede.
- Brug Uptime Kuma til driftsovervågning, men ikke som eneste sikkerhedskontrol.
- Test backups ved jævnligt at gendanne en kopi.
- Gem mindst én backup uden for Raspberry Pi'en.
- Giv ikke containere `privileged: true`, medmindre der er et dokumenteret behov.
- Montér kun Docker-socketten i Portainer; andre containere skal normalt ikke have adgang til den.
- Publicér ikke PostgreSQL-, Redis-, Portainer- eller admin-porte direkte på routeren.

## Før offentlig adgang

1. Fast IP eller DHCP-reservation til Raspberry Pi'en.
2. Domæne/subdomæne peger korrekt.
3. Reverse proxy og gyldigt TLS-certifikat virker.
4. Routeren videresender kun 80/443.
5. Standard-login er ændret.
6. Backup er kørt og kontrolleret.
7. Uptime Kuma overvåger tjenesten.

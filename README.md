# 🚀 Racher HomeLab

Mit private HomeLab bygget omkring en Raspberry Pi 5. Målet er at samle egne webapplikationer på én server med enkel drift, overvågning, HTTPS og backup.

## Hardware

- Raspberry Pi 5, 8 GB
- Pimoroni NVMe Base
- 1 TB NVMe SSD, 2280
- Raspberry Pi Active Cooler

## Infrastruktur

| Tjeneste | Formål | Status |
|---|---|---|
| Docker Compose | Containerdrift | Forberedt |
| Portainer | Administration af containere | Forberedt |
| Nginx Proxy Manager | Reverse proxy og HTTPS | Forberedt |
| Uptime Kuma | Driftsovervågning | Forberedt |
| PostgreSQL | Fælles database | Forberedt |
| Redis | Cache og kø | Forberedt |
| Tailscale | Sikker fjernadgang | Planlagt |

## Første projekter

1. ⏱️ Minutregnskab
2. 🚒 Vagtbytte
3. 🚑 Indsatsbrief
4. 📍 Adresseopslag
5. 📦 Pakkeliste
6. 🚨 Brandvagt App

## Repositorystruktur

```text
Racher-Homelab/
├── compose/
│   ├── core/             # Nginx Proxy Manager, Portainer og Uptime Kuma
│   ├── data/             # PostgreSQL og Redis
│   └── minutregnskab/    # Første app-deployment
├── docs/
│   ├── INSTALLATION.md
│   └── SECURITY.md
├── scripts/
│   ├── bootstrap.sh
│   ├── install-racher-os.sh
│   ├── backup.sh
│   └── update-stacks.sh
├── .env.example
├── .gitignore
└── README.md
```

## Installation på Raspberry Pi

Brug 64-bit Raspberry Pi OS. Kør bootstrap som din normale bruger:

```bash
git clone https://github.com/flr45/Racher-Homelab.git ~/homelab/Racher-Homelab
cd ~/homelab/Racher-Homelab
chmod +x scripts/*.sh
./scripts/bootstrap.sh
```

Log ud og ind igen. Kør derefter install-scriptet:

```bash
cd ~/homelab/Racher-Homelab
./scripts/install-racher-os.sh
```

Første kørsel opretter `.env` med filrettighed `0600` og stopper, indtil obligatoriske adgangskoder er udskiftet. Næste kørsel validerer Compose-filerne, henter images, starter data- og core-stacks og foretager healthcheck. Ved mislykket opstart stoppes de stacks, som install-scriptet selv har startet.

Den fulde trin-for-trin-guide ligger i [docs/INSTALLATION.md](docs/INSTALLATION.md). Læs også [docs/SECURITY.md](docs/SECURITY.md), før tjenester gøres tilgængelige fra internettet.

## Backup

```bash
./scripts/backup.sh
```

Scriptet tager komprimerede kopier af de vigtigste Docker-volumener og beholder som udgangspunkt 14 dage. En ekstern kopi skal senere tilføjes, fordi backup på samme SSD ikke beskytter mod diskfejl.

## Opdatering

```bash
./scripts/update-stacks.sh
```

## Status

- [x] GitHub-repository oprettet
- [x] Grundstruktur og dokumentation forberedt
- [x] Core Compose-stack forberedt
- [x] PostgreSQL og Redis forberedt
- [x] Backup- og opdateringsscripts forberedt
- [x] Transaktionel Raspberry Pi-bootstrap og installer forberedt
- [x] Minutregnskab deployment-skabelon forberedt
- [ ] Hardware modtaget
- [ ] Raspberry Pi OS installeret
- [ ] Docker installeret på Raspberry Pi
- [ ] Første backup testet med gendannelse
- [ ] Minutregnskab-image publiceret
- [ ] Første projekt online

## Projektmål

Serveren skal være nem at vedligeholde, udvide og genskabe efter hardwarefejl. Konfiguration gemmes i GitHub, mens adgangskoder og andre hemmeligheder kun gemmes lokalt i `.env`.

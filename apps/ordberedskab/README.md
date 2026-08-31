# Ordberedskab v1

Et lille Flask-baseret diktat- og stavetræningsprogram med politi, brand, ambulance og redningsberedskab som tema.

## Funktioner
- Elev-login og admin-login
- Én sætning ad gangen med ét manglende ord
- Dansk oplæsning via browserens Web Speech API
- Normal og langsom oplæsning
- Grøn markering ved korrekt svar
- Hints efter flere forkerte forsøg
- Progression og ord, der kræver ekstra træning
- Adaptiv udvælgelse: svære/ukendte ord vises oftere
- Adminpanel til at oprette og aktivere/deaktivere øvelser
- 20 medfølgende eksempeløvelser

## Docker på Racher-Homelab
Tilføj disse værdier til repoets `.env`:

```env
ORDBEREDSKAB_SECRET_KEY=skift-denne-til-en-lang-tilfaeldig-noegle
ORDBEREDSKAB_ADMIN_PASSWORD=skift-admin-adgangskoden
ORDBEREDSKAB_STUDENT_PASSWORD=skift-elev-adgangskoden
ORDBEREDSKAB_PORT=5050
```

Start derefter fra roden af `Racher-Homelab`:

```bash
docker compose --env-file .env -f compose/ordberedskab/compose.yml up -d --build
```

Kontrollér status:

```bash
docker compose --env-file .env -f compose/ordberedskab/compose.yml ps
docker logs --tail=100 ordberedskab-web
```

Åbn derefter:

```text
http://SERVER-IP:5050
```

Databasen gemmes i Docker-volumen `ordberedskab_ordberedskab_data`, så brugere, øvelser og progression overlever genstart og nye builds.

## Lokal udvikling uden Docker
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Første login
Ved en helt ny database oprettes som standard:

Elev:
- Brugernavn: `elev`
- Adgangskode: værdien i `ORDBEREDSKAB_STUDENT_PASSWORD` (fallback `elev123`)

Admin:
- Brugernavn: `admin`
- Adgangskode: værdien i `ORDBEREDSKAB_ADMIN_PASSWORD` (fallback `admin123`)

Skift adgangskoderne i `.env` før første produktionsstart.

## Oplæsning
Oplæsningen bruger stemmer fra elevens browser/enhed. På iPhone/iPad/Safari vil den normalt vælge en dansk systemstemme, hvis en sådan er installeret. Det skjulte ord er stadig med i den oplæste sætning, så øvelsen fungerer som diktat.

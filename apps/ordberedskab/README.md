# Ordberedskab v1

Et Flask-baseret diktat- og stavetræningsprogram med politi, brand, ambulance og redningsberedskab som tema.

## Funktioner
- Elev-login og admin-login
- Én sætning ad gangen med ét manglende ord
- Naturlig dansk oplæsning via OpenAI `gpt-4o-mini-tts`
- Normal og langsom oplæsning
- Lokal lyd-cache, så samme sætning ikke genereres igen og igen
- Browserens danske systemstemme som fallback
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

ORDBEREDSKAB_OPENAI_API_KEY=skift-til-din-api-noegle
ORDBEREDSKAB_TTS_MODEL=gpt-4o-mini-tts
ORDBEREDSKAB_TTS_VOICE=marin
ORDBEREDSKAB_TTS_SPEED_NORMAL=0.96
ORDBEREDSKAB_TTS_SPEED_SLOW=0.72
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

Databasen og TTS-cachen gemmes i Docker-volumen `ordberedskab_ordberedskab_data`, så brugere, progression og allerede genererede lydfiler overlever genstart og nye builds.

## OpenAI TTS
Ved første tryk på en oplæsningsknap genereres en MP3-fil med OpenAI Speech API. Filen caches under `/data/tts-cache`. Næste gang samme sætning afspilles med samme stemme, model og hastighed, bruges den lokale fil uden et nyt API-kald.

Standardindstillinger:
- Model: `gpt-4o-mini-tts`
- Stemme: `marin`
- Normal hastighed: `0.96`
- Langsom hastighed: `0.72`

Hvis OpenAI API ikke er tilgængeligt, returnerer serveren teksten til browseren, som forsøger at bruge en dansk Web Speech-stemme som fallback.

Det korrekte svarord ligger ikke længere direkte i JavaScript-koden ved normal drift; serveren sammensætter hele sætningen og sender kun lydfilen til browseren.

## Lokal udvikling uden Docker
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY='...'
flask --app wsgi run --host 0.0.0.0 --port 5050
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

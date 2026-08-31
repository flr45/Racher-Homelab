# Ordberedskab

Et Flask-baseret diktat- og stavetræningsprogram til en ordblind elev i 9. klasse med politi, brand, ambulance og redningsberedskab som tema.

## Funktioner
- Elev-login og admin-login
- Én sætning ad gangen med ét manglende ord
- Naturlig dansk oplæsning via OpenAI `gpt-4o-mini-tts`
- Normal og langsom oplæsning
- Lokal lyd-cache, så samme sætning ikke genereres igen og igen
- Næste øvelse og dens lyd forberedes i baggrunden
- To staveforsøg; derefter vises det korrekte ord
- Individuel sværhedsgrad 1-4
- Historik over gennemførte sætninger
- Personlig progression og ord der kræver ekstra træning
- 500 faste startøvelser fordelt på fire fagområder og niveau 1-4
- Automatisk OpenAI-generering af nye sætninger, når en elev har gennemført alle nye sætninger på sit valgte niveau
- Manuel AI-generering fra adminpanelet
- Browserens danske systemstemme som fallback ved TTS-fejl

## Øvelsesbank
Grundbanken indeholder 500 faste øvelser:
- 125 Politi
- 125 Brand
- 125 Ambulance
- 125 Redningsberedskab

De ligger som TSV-filer under `seed/`. Ved opstart tilføjes kun sætninger, der ikke allerede findes, så eksisterende brugere, progression og egne admin-øvelser bevares.

Når en bruger ikke har flere nye øvelser på sit valgte niveau, genererer OpenAI som standard 20 nye øvelser. Hvis genereringen fejler, fortsætter programmet med relevante repetitionsøvelser i stedet for at gå i stå.

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
ORDBEREDSKAB_GENERATION_MODEL=gpt-5.4-mini
ORDBEREDSKAB_GENERATION_BATCH=20
```

Start eller opdatér derefter fra roden af `Racher-Homelab`:

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

Databasen og TTS-cachen gemmes i Docker-volumen `ordberedskab_ordberedskab_data`, så brugere, progression og genererede lydfiler overlever genstart og nye builds.

## OpenAI TTS
Ved første oplæsning genereres en MP3-fil med OpenAI Speech API. Filen caches under `/data/tts-cache`. Næste gang samme sætning afspilles med samme stemme, model og hastighed, bruges den lokale fil uden et nyt API-kald.

Standardindstillinger:
- Model: `gpt-4o-mini-tts`
- Stemme: `marin`
- Normal hastighed: `0.96`
- Langsom hastighed: `0.72`

## Automatisk AI-generering
Generatoren bruger samme `ORDBEREDSKAB_OPENAI_API_KEY` som TTS. Den genererer valideret, struktureret indhold med præcis én blank `______`, ét svarord, en gyldig kategori og det valgte niveau.

Standard:
- Genereringsmodel: `gpt-5.4-mini`
- Batch: 20 nye øvelser
- Maksimalt 40 ved manuel generering i admin

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

Skift adgangskoderne i `.env` før egentlig brug.

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

## Hurtig start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Åbn derefter: http://SERVER-IP:5050

## Demo-login
Elev:
- Brugernavn: `elev`
- Adgangskode: `elev123`

Admin:
- Brugernavn: `admin`
- Adgangskode: `admin123`

## Vigtigt før rigtig brug
Sæt en stærk SECRET_KEY, og skift demo-adgangskoderne. Eksempel:
```bash
export SECRET_KEY='en-meget-lang-tilfaeldig-hemmelig-noegle'
python app.py
```

## Oplæsning
Oplæsningen bruger stemmer fra elevens browser/enhed. På iPhone/iPad/Safari vil den normalt vælge en dansk systemstemme, hvis en sådan er installeret.

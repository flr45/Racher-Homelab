# Pager Gateway Training / Replay

Training-centret er admin-only og er lavet til gamle PDW-/POCSAG-logs samt eksisterende RIC-lister.

## Sikkerhedsgrænse

Replay kalder aldrig live `ingest_event()` og skriver ikke til den normale `messages`-tabel.

Det betyder:

- ingen Web Push,
- ingen Pushover,
- ingen replay-rækker i det normale brugerfeed,
- ingen automatisk ændring af live RIC-/stationsrouting under analysen.

Replay-data gemmes i særskilte `training_*`-tabeller. Først når admin vælger forslag/feedback og trykker **Anvend godkendt læring**, overføres de valgte dele til den rigtige model.

## Replay-flow

1. Åbn admin -> **Træning**.
2. Indsæt gamle loglinjer eller vælg en `.txt`, `.log` eller `.csv` fil.
3. Tryk **Analyser uden at sende**.
4. Rapporten viser bl.a.:
   - parserbare linjer,
   - rigtige/leveringsberettigede meldinger,
   - umiddelbare dubletter,
   - allerede lærte støjmønstre,
   - ukendte mønstre,
   - meldinger uden kendt område,
   - nye stations-/områdeforslag,
   - nye RIC -> område-forslag.
5. Marker replay-meldinger som **Relevant** eller **Støj**. Flere kan vælges og vurderes samlet.
6. Sæt stations- og RIC-forslag til **Godkend**, **Afvis** eller **Afventer**.
7. Tryk **Anvend godkendt læring**.

En træningskørsel kan kun anvendes én gang, så samme feedback ikke tælles flere gange ved et uheld.

## Dubletter i replay

To identiske offentlige meldingstekster direkte efter hinanden markeres som dublet uanset RIC. Begge replay-rækker gemmes i træningskørslen, men rapporten tæller kun den første som leveringsberettiget.

Dette ændrer ikke live-dubletvinduet. Live-systemet bruger fortsat det konfigurerede tidsvindue, standard 30 sekunder.

## Stationslæring

Replay bruger den samme parser til tydelige stationsformuleringer som live-systemet, men opretter ikke automatisk noget under analysen.

Et navn som fx `Næstved Brandvæsen` kan derfor blive et forslag. Et almindeligt stednavn/adresse alene bliver ikke til en station.

## Bulk RIC-import

Admin kan indsætte eller indlæse en tekst-/CSV-fil.

Understøttede separatorer:

- semikolon,
- tabulator,
- komma.

Anbefalet format:

```text
RIC;Område;Beskrivelse;Aktiv
1234567;Slagelse;Primær alarmgruppe;1
2345678;Næstved;Sekundær alarmgruppe;1
3456789;Holbæk;Test/teknik;0
```

`Aktiv` er valgfri og er aktiv som standard. Værdier som `0`, `false`, `nej`, `no`, `off` eller `inaktiv` fortolkes som deaktiveret.

**Forhåndsvis** viser gyldige rækker og fejl uden at ændre RIC-registeret.

Ved import kan admin vælge **Opret manglende områder automatisk**. Eksisterende RIC-koder overskrives ikke; de springes over og rapporteres som eksisterende.

## Kapacitetsgrænser

For at undgå at en browser/import ved et uheld overbelaster Pi'en er standardgrænserne:

- replay: højst 20.000 ikke-tomme linjer pr. kørsel,
- RIC-import: højst 5.000 ikke-tomme linjer pr. import,
- UI viser højst de første 500 replay-meldinger ad gangen,
- backend gemmer hele den parserbare replay-kørsel.

## Backup

Training-tabeller og anvendt læring ligger i samme SQLite-database som gatewayens øvrige metadata og følger derfor den eksisterende konsistente SQLite-backup.

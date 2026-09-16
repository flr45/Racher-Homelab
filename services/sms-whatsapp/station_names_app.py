"""Human-friendly station names for SBR Pager history and statistics.

Station codes remain unchanged internally so alarm matching, filtering and stored
history continue to use the original codes. This final UI layer only changes
how the codes are presented to administrators.
"""

import history_app as previous

app = previous.app

STATION_NAMES = {
    "A": "Slagelse",
    "S": "Sorø",
    "L": "Skælskør",
    "R": "Ruds Vedby",
    "K": "Korsør",
    "B": "Storebælt",
    "ISL": "ISL",
}


def station_name(value) -> str:
    if not value:
        return "—"
    code = str(value).strip().upper()
    return STATION_NAMES.get(code, code)


app.jinja_env.globals["station_name"] = station_name

# Statistics page: show names, while form values and filter URLs keep using codes.
previous.STATISTICS_HTML = previous.STATISTICS_HTML.replace(
    " · Station {{ selected_station }}",
    " · {{ station_name(selected_station) }}",
)
previous.STATISTICS_HTML = previous.STATISTICS_HTML.replace(
    ">Station {{ code }}</option>",
    ">{{ station_name(code) }}</option>",
)
previous.STATISTICS_HTML = previous.STATISTICS_HTML.replace(
    "<strong>{{ row.station }}</strong>",
    "<strong>{{ station_name(row.station) }}</strong>",
)
previous.STATISTICS_HTML = previous.STATISTICS_HTML.replace(
    "<td>{{ event.station or '—' }}</td>",
    "<td>{{ station_name(event.station) }}</td>",
)

# Alarm detail page.
previous.EVENT_DETAIL_HTML = previous.EVENT_DETAIL_HTML.replace(
    "Station {{ event.station or '—' }}",
    "{{ station_name(event.station) }}",
)

# Alarm map popup. Coordinates and stored event data stay unchanged.
previous.MAP_HTML = previous.MAP_HTML.replace(
    "const points = {{ points|tojson }};",
    "const points = {{ points|tojson }};\n  const stationNames = {A:'Slagelse',S:'Sorø',L:'Skælskør',R:'Ruds Vedby',K:'Korsør',B:'Storebælt',ISL:'ISL'};",
)
previous.MAP_HTML = previous.MAP_HTML.replace(
    "(item.station ? 'Station '+item.station+' · ' : '')+(item.alarm_type || 'Alarm')",
    "(item.station ? (stationNames[item.station] || item.station)+' · ' : '')+(item.alarm_type || 'Alarm')",
)

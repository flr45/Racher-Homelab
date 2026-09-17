from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class StationEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self.temp.name

        import storage
        import station_events

        self.storage = storage
        self.station_events = station_events
        storage.init_database()
        station_events.ensure_station_event_schema()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_station_routing_and_event_grouping(self) -> None:
        storage = self.storage
        events = self.station_events

        events.save_station_rule("Slagelse", "SLG, Station Slagelse")
        events.save_station_rule("Korsør", "KOR")

        all_id = storage.save_recipient("Alle", "+4511111111")
        slagelse_id = storage.save_recipient("Slagelse-vagt", "+4522222222")
        korsoer_id = storage.save_recipient("Korsør-vagt", "+4533333333")
        events.set_recipient_filters(slagelse_id, ["Slagelse"])
        events.set_recipient_filters(korsoer_id, ["Korsør"])

        self.assertEqual(events.resolve_station("Alarm (SLG) · Brand · Testvej 1"), "Slagelse")
        recipients = events.active_recipients_for_station("Slagelse")
        names = {row["name"] for row in recipients}
        self.assertEqual(names, {"Alle", "Slagelse-vagt"})
        self.assertNotIn("Korsør-vagt", names)
        self.assertTrue(all_id)

        pre_id, _ = storage.store_inbound_message(
            source_id="test-pre",
            sender="+4599999999",
            body="Alarm modtaget (SLG) · Bygningsbrand · Testvej 1 · afventer resten",
            received_at="2026-09-17T16:00:00Z",
            modem_port="COM9",
            processing_status="accepted_pending_whatsapp",
        )
        event_id, station, kind = events.record_alarm_message(
            pre_id,
            sender="+4599999999",
            body="Alarm modtaget (SLG) · Bygningsbrand · Testvej 1 · afventer resten",
            received_at="2026-09-17T16:00:00Z",
        )
        self.assertEqual(station, "Slagelse")
        self.assertEqual(kind, "alarm_prealert")

        complete_id, _ = storage.store_inbound_message(
            source_id="test-complete",
            sender="+4599999999",
            body="Alarm (SLG) · Bygningsbrand · Testvej 1, 4200 Slagelse",
            received_at="2026-09-17T16:01:00Z",
            modem_port="COM9",
            processing_status="accepted_pending_whatsapp",
        )
        event_id_2, _, kind_2 = events.record_alarm_message(
            complete_id,
            sender="+4599999999",
            body="Alarm (SLG) · Bygningsbrand · Testvej 1, 4200 Slagelse",
            received_at="2026-09-17T16:01:00Z",
        )
        self.assertEqual(kind_2, "alarm_complete")
        self.assertEqual(event_id_2, event_id)

        alarm = next(row for row in events.recent_alarm_events() if row["id"] == event_id)
        self.assertEqual(alarm["status"], "complete")
        self.assertEqual(alarm["station"], "Slagelse")
        self.assertGreaterEqual(len(events.alarm_event_timeline(event_id)), 2)

    def test_filtered_recipient_does_not_receive_unknown_station(self) -> None:
        storage = self.storage
        events = self.station_events
        recipient_id = storage.save_recipient("Kun Slagelse", "+4544444444")
        events.set_recipient_filters(recipient_id, ["Slagelse"])
        self.assertEqual(events.active_recipients_for_station(None), [])


if __name__ == "__main__":
    unittest.main()

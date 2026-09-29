from __future__ import annotations

import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from burst_consensus import PocsagBurstConsensus, consensus_message, consensus_quality, same_nr_burst
from operations import OperationsStore
from storage import Storage


class BurstConsensusTests(unittest.TestCase):
    def setUp(self):
        # These are the public-text equivalents of the real PDL copies observed
        # on 20-08-2026 at 12:35 after Danish ISO-646 translation.
        self.copies = [
            "@8 NR RI(1+5)M+S · Ringsted(?vømmeland · BRANDALARM · 4100 Ringsted",
            "@8 NR RM*1+5)M+S · R`ngsted Svømmeland · BRANDALARM · 4100 Ringsted",
            "@8 NR RI(1+5)M+S · Ringsted Svlmv2v",
            "@8 NR RI(1+5)M+S · Ringstul Svømmeland · BRANDALARM · 410x Zingsted",
        ]
        self.expected = (
            "@8 NR RI(1+5)M+S · Ringsted Svømmeland · BRANDALARM · 4100 Ringsted"
        )

    def test_observed_first_three_copies_reconstruct_working_reference(self):
        self.assertEqual(consensus_message(self.copies[:3]), self.expected)

    def test_observed_four_copies_reconstruct_working_reference(self):
        self.assertEqual(consensus_message(self.copies), self.expected)

    def test_observed_full_corrupt_copies_belong_to_same_burst(self):
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[1]))
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[3]))

    def test_observed_short_copy_can_join_clean_dispatch_key(self):
        self.assertTrue(same_nr_burst(self.copies[0], self.copies[2]))

    def test_two_different_full_ringsted_dispatches_are_not_merged_by_city_alone(self):
        other = (
            "@8 NR RI(1+5)M+S · Ringsted Station · AUTOMATISK BRANDALARM · 4100 Ringsted"
        )
        self.assertFalse(same_nr_burst(self.copies[0], other))

    def test_non_nr_message_is_not_a_burst_candidate(self):
        self.assertFalse(
            same_nr_burst(
                self.copies[0],
                "$9 ISL-Forespørgsel · 4100 Ringsted · lugt af brændt plastic",
            )
        )

    def test_single_copy_can_never_claim_high_decode_confidence(self):
        quality = consensus_quality([self.expected])
        self.assertEqual(quality["copy_count"], 1)
        self.assertEqual(quality["label"], "low")
        self.assertLess(quality["confidence"], 0.50)

    def test_three_observed_copies_wait_for_more_evidence(self):
        quality = consensus_quality(self.copies[:3])
        self.assertEqual(quality["copy_count"], 3)
        self.assertEqual(quality["label"], "medium")
        self.assertLess(quality["confidence"], 0.82)

    def test_four_observed_copies_reach_high_decode_confidence(self):
        quality = consensus_quality(self.copies)
        self.assertEqual(quality["copy_count"], 4)
        self.assertEqual(quality["label"], "high")
        self.assertGreaterEqual(quality["confidence"], 0.85)

    def test_three_clean_copies_can_flush_early(self):
        quality = consensus_quality([self.expected, self.expected, self.expected])
        self.assertEqual(quality["copy_count"], 3)
        self.assertGreaterEqual(quality["confidence"], 0.82)


    def test_recent_pending_burst_is_recovered_after_restart(self):
        class Adaptive:
            @staticmethod
            def exact_signature(message):
                return "sig:" + str(message)

            @staticmethod
            def automatic_noise_reason(_message):
                return None

            @staticmethod
            def learned_relevance(_message):
                return {
                    "classification": "unknown",
                    "score": 1.0,
                    "reason": "test: unknown defaults to delivery",
                }

            @staticmethod
            def observe(_message_id, _message):
                return None

        class Routing:
            @staticmethod
            def classify(_ric, station, _message):
                return station or "Ringsted", "test"

        class AlarmFilter:
            @staticmethod
            def match(_message):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pager.db")
            storage = Storage(db_path)
            OperationsStore(db_path)
            notified = []
            core = SimpleNamespace(
                storage=storage,
                adaptive=Adaptive(),
                routing=Routing(),
                public_message=lambda value: str(value or "").strip(),
                ingest_event=lambda _event: -1,
                maybe_notify_pushover=lambda message_id, _event: notified.append(message_id),
                send_web_push_for_event=lambda _message_id, _event: None,
                app=SimpleNamespace(logger=logging.getLogger("burst-recovery-test")),
            )
            consensus = PocsagBurstConsensus(core, AlarmFilter())
            message_id = storage.add_message({
                "received_at": datetime.now(timezone.utc).isoformat(),
                "protocol": "POCSAG",
                "baud": 1200,
                "ric": "0006120",
                "station": "Ringsted",
                "message": self.expected,
                "raw_line": "RAW RECOVERY COPY",
                "source": "pdl-file",
                "message_fingerprint": "sig:" + self.expected,
                "relevance_class": "unknown",
                "relevance_score": 0.75,
                "suppressed_reason": "burst-candidate",
                "delivery_eligible": False,
                "decision_reason": "afventer burst",
            })

            recovered = consensus.recover_pending()

            self.assertEqual(recovered, 1)
            row = storage.list_messages(limit=1)[0]
            self.assertEqual(row["id"], message_id)
            self.assertEqual(row["delivery_eligible"], 1)
            self.assertIsNone(row["suppressed_reason"])
            self.assertEqual(notified, [message_id])

    def test_stale_pending_burst_is_not_delivered_late(self):
        class Adaptive:
            @staticmethod
            def exact_signature(message):
                return "sig:" + str(message)

            @staticmethod
            def automatic_noise_reason(_message):
                return None

            @staticmethod
            def learned_relevance(_message):
                return {"classification": "unknown", "score": 1.0, "reason": "test"}

            @staticmethod
            def observe(_message_id, _message):
                return None

        class Routing:
            @staticmethod
            def classify(_ric, station, _message):
                return station or "Ringsted", "test"

        class AlarmFilter:
            @staticmethod
            def match(_message):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "pager.db")
            storage = Storage(db_path)
            OperationsStore(db_path)
            notified = []
            core = SimpleNamespace(
                storage=storage,
                adaptive=Adaptive(),
                routing=Routing(),
                public_message=lambda value: str(value or "").strip(),
                ingest_event=lambda _event: -1,
                maybe_notify_pushover=lambda message_id, _event: notified.append(message_id),
                send_web_push_for_event=lambda _message_id, _event: None,
                app=SimpleNamespace(logger=logging.getLogger("burst-recovery-stale-test")),
            )
            consensus = PocsagBurstConsensus(core, AlarmFilter())
            message_id = storage.add_message({
                "received_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                "protocol": "POCSAG",
                "baud": 1200,
                "ric": "0006120",
                "station": "Ringsted",
                "message": self.expected,
                "raw_line": "RAW STALE COPY",
                "source": "pdl-file",
                "message_fingerprint": "sig:" + self.expected,
                "relevance_class": "unknown",
                "relevance_score": 0.75,
                "suppressed_reason": "burst-candidate",
                "delivery_eligible": False,
                "decision_reason": "afventer burst",
            })
            old_ingested = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            with storage.connect() as conn:
                conn.execute("UPDATE messages SET ingested_at=? WHERE id=?", (old_ingested, message_id))

            recovered = consensus.recover_pending()

            self.assertEqual(recovered, 0)
            row = storage.list_messages(limit=1)[0]
            self.assertEqual(row["delivery_eligible"], 0)
            self.assertEqual(row["suppressed_reason"], "burst-candidate")
            self.assertEqual(notified, [])


if __name__ == "__main__":
    unittest.main()

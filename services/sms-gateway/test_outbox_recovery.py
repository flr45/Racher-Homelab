from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

import modem_reader
import queued_app as runtime


class OutboxRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = runtime.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        cls.context.pop()

    def setUp(self):
        runtime.db.session.query(runtime.OutboundMessage).delete()
        runtime.db.session.commit()

    def test_claim_is_only_reserved_until_worker_starts_modem_send(self):
        message = runtime.enqueue_sms("+4512345678", "test")

        claimed = runtime.claim_outbound_message()

        self.assertEqual(claimed.id, message.id)
        self.assertEqual(claimed.status, "reserved")
        self.assertTrue(runtime.start_outbound_message(claimed))
        self.assertEqual(claimed.status, "sending")

    def test_stale_reserved_delivery_is_safe_to_requeue(self):
        message = runtime.enqueue_sms("+4512345678", "test")
        claimed = runtime.claim_outbound_message()
        claimed.claimed_at = runtime.utcnow() - timedelta(minutes=10)
        runtime.db.session.commit()

        result = runtime.recover_stale_outbound_messages()
        runtime.db.session.refresh(message)

        self.assertEqual(result, {"requeued": 1, "unknown": 0})
        self.assertEqual(message.status, "pending")
        self.assertIsNone(message.claimed_at)

        claimed_again = runtime.claim_outbound_message()
        self.assertEqual(claimed_again.id, message.id)
        self.assertEqual(claimed_again.status, "reserved")
        self.assertEqual(claimed_again.attempts, 2)

    def test_stale_sending_delivery_becomes_unknown_and_is_not_retried(self):
        message = runtime.enqueue_sms("+4512345678", "test")
        claimed = runtime.claim_outbound_message()
        self.assertTrue(runtime.start_outbound_message(claimed))
        claimed.claimed_at = runtime.utcnow() - timedelta(minutes=10)
        runtime.db.session.commit()

        result = runtime.recover_stale_outbound_messages()
        runtime.db.session.refresh(message)

        self.assertEqual(result, {"requeued": 0, "unknown": 1})
        self.assertEqual(message.status, "unknown")
        self.assertIn("dublet-SMS", message.error)
        self.assertIsNone(runtime.claim_outbound_message())


class ModemWorkerDeliveryStateTests(unittest.TestCase):
    def test_ambiguous_modem_result_is_never_auto_retried(self):
        completed = []

        with patch.object(
            modem_reader,
            "claim_outgoing",
            return_value={"id": 12, "recipient": "+4512345678", "body": "test"},
        ), patch.object(
            modem_reader,
            "start_outgoing",
            return_value={"id": 12, "status": "sending"},
        ), patch.object(
            modem_reader,
            "send_outgoing_sms",
            side_effect=modem_reader.AmbiguousSmsSendError("kvittering mangler"),
        ), patch.object(
            modem_reader,
            "complete_outgoing",
            side_effect=lambda message_id, status, error=None, retry=False: completed.append(
                (message_id, status, error, retry)
            ) or {"retried": False},
        ), patch.object(modem_reader, "write_status"):
            modem_reader.process_outbox(object())

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0][0], 12)
        self.assertEqual(completed[0][1], "unknown")
        self.assertFalse(completed[0][3])

    def test_explicit_modem_rejection_can_use_bounded_retry(self):
        completed = []

        with patch.object(
            modem_reader,
            "claim_outgoing",
            return_value={"id": 13, "recipient": "+4512345678", "body": "test"},
        ), patch.object(
            modem_reader,
            "start_outgoing",
            return_value={"id": 13, "status": "sending"},
        ), patch.object(
            modem_reader,
            "send_outgoing_sms",
            side_effect=RuntimeError("+CMS ERROR: 500"),
        ), patch.object(
            modem_reader,
            "complete_outgoing",
            side_effect=lambda message_id, status, error=None, retry=False: completed.append(
                (message_id, status, error, retry)
            ) or {"retried": True},
        ), patch.object(modem_reader, "write_status"):
            modem_reader.process_outbox(object())

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0][1], "failed")
        self.assertTrue(completed[0][3])


if __name__ == "__main__":
    unittest.main()

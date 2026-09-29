from __future__ import annotations

import unittest

from sms_pdu import parse_cmgl_response_detailed


VALID_PDU = (
    "06915404969912040A91542272306900006280107085538031A8600A442DCFE920F80364EEC8C9E9331D8466CCE9653AE8DD963FC86550BB4C0675400CD003C4012D400E"
)


class MalformedPduIsolationTests(unittest.TestCase):
    def test_bad_pdu_does_not_hide_valid_sms_in_same_cmgl_batch(self):
        response = (
            "\r\n+CMGL: 1,0,,1\r\n00\r\n"
            f"+CMGL: 4,0,,{len(VALID_PDU) // 2}\r\n{VALID_PDU}\r\n"
            "\r\nOK\r\n"
        )

        messages, errors = parse_cmgl_response_detailed(response)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["index"], 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["index"], 4)
        self.assertEqual(messages[0]["sender"], "+4522270396")
        self.assertIn("færdigt høstet område", messages[0]["body"])

    def test_missing_hex_body_is_reported_without_exception(self):
        response = "\r\n+CMGL: 7,0,,4\r\nNOT-A-PDU\r\n\r\nOK\r\n"

        messages, errors = parse_cmgl_response_detailed(response)

        self.assertEqual(messages, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["index"], 7)
        self.assertIn("ingen gyldig hex-PDU", errors[0]["error"])


if __name__ == "__main__":
    unittest.main()

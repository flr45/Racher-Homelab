import os
import tempfile
import unittest

from rss_updates import RSSStore, clean_text, parse_feed_xml, validate_feed_url
from storage import Storage


class RSSUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "pager.db")
        self.storage = Storage(self.db)
        self.admin_id = self.storage.create_user(
            "rssadmin", "RSS Admin", "hash", "admin", None
        )
        self.user_id = self.storage.create_user(
            "rssuser", "RSS User", "hash", "user", self.admin_id
        )
        self.rss = RSSStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_official_police_feeds_are_seeded(self):
        feeds = self.rss.list_feeds()
        urls = {row["url"]: row for row in feeds}
        url = "https://via.ritzau.dk/rss/short-messages/latest?publisherId=90594"
        self.assertIn(url, urls)
        self.assertEqual(urls[url]["name"], "Sydsjællands og Lolland-Falsters Politi")
        self.assertEqual(urls[url]["kind"], "politi")

    def test_user_only_sees_items_from_assigned_feeds(self):
        feeds = self.rss.list_feeds()
        first, second = feeds[0], feeds[1]
        self.rss.set_user_feeds(self.user_id, [first["id"]])

        first_item = {
            "dedupe_key": "a" * 64,
            "title": "Første melding",
            "summary": "Fra første feed",
            "link": "https://example.test/1",
            "published_at": "2026-08-17T12:00:00+00:00",
        }
        second_item = {
            "dedupe_key": "b" * 64,
            "title": "Anden melding",
            "summary": "Fra andet feed",
            "link": "https://example.test/2",
            "published_at": "2026-08-17T12:01:00+00:00",
        }
        self.rss.ingest_entries(first["id"], [first_item])
        self.rss.ingest_entries(second["id"], [second_item])

        rows = self.rss.list_items_for_user(self.user_id, 10)
        self.assertEqual([row["title"] for row in rows], ["Første melding"])
        self.assertIn(first["name"], rows[0]["feed_names"])

    def test_same_rss_item_is_deduplicated_across_multiple_feeds(self):
        first, second = self.rss.list_feeds()[:2]
        self.rss.set_user_feeds(self.user_id, [first["id"], second["id"]])
        item = {
            "dedupe_key": "c" * 64,
            "title": "Samme politiupdate",
            "summary": "Samme tekst",
            "link": "https://example.test/same",
            "published_at": "2026-08-17T13:00:00+00:00",
        }
        self.rss.ingest_entries(first["id"], [item])
        self.rss.ingest_entries(second["id"], [item])

        rows = self.rss.list_items_for_user(self.user_id, 10)
        self.assertEqual(len(rows), 1)
        self.assertIn(first["name"], rows[0]["feed_names"])
        self.assertIn(second["name"], rows[0]["feed_names"])

    def test_rss_push_is_opt_in_per_user(self):
        feed = self.rss.list_feeds()[0]
        self.rss.set_user_feeds(self.user_id, [feed["id"]])
        self.assertFalse(self.rss.user_push_enabled(self.user_id))
        self.assertEqual(self.rss.push_users_for_feed(feed["id"]), [])
        self.rss.set_user_push(self.user_id, True)
        self.assertEqual(self.rss.push_users_for_feed(feed["id"]), [self.user_id])

    def test_parser_handles_rss_and_strips_markup(self):
        payload = b"""<?xml version='1.0' encoding='utf-8'?>
        <rss version='2.0'><channel><item>
          <guid>abc-123</guid><title>Politi &amp; trafik</title>
          <description><![CDATA[<p>Vejen er <strong>spærret</strong>.</p>]]></description>
          <link>https://example.test/update</link>
          <pubDate>Mon, 17 Aug 2026 17:45:00 +0200</pubDate>
        </item></channel></rss>"""
        rows = parse_feed_xml(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Politi & trafik")
        self.assertEqual(rows[0]["summary"], "Vejen er spærret.")
        self.assertTrue(rows[0]["published_at"].endswith("+00:00"))
        self.assertEqual(len(rows[0]["dedupe_key"]), 64)

    def test_custom_feed_url_requires_public_https(self):
        self.assertEqual(
            validate_feed_url("https://example.com/feed.xml", resolve=False),
            "https://example.com/feed.xml",
        )
        with self.assertRaises(ValueError):
            validate_feed_url("http://example.com/feed.xml", resolve=False)
        with self.assertRaises(ValueError):
            validate_feed_url("https://127.0.0.1/feed.xml", resolve=True)

    def test_clean_text_is_bounded_and_plain(self):
        self.assertEqual(clean_text("<b>Hej</b>   verden", 20), "Hej verden")


if __name__ == "__main__":
    unittest.main()

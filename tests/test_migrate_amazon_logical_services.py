import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import migrate_amazon_logical_services as mals  # noqa: E402
import filter_integration  # noqa: E402


def _gti(label: str) -> str:
    """Deterministic, real-UUID-shaped amzn1.dv.gti.* value for a readable label.

    CONTENT_GTI_RX in migrate_amazon_logical_services.py requires a strict
    36-char UUID shape (matches production GTIs); a plain slug like
    "content-tennis" silently fails to extract, which would make these tests
    pass for the wrong reason.
    """
    return f"amzn1.dv.gti.{uuid.uuid5(uuid.NAMESPACE_DNS, label)}"


def _row(gti, channel_id, channel_name, is_stale=0):
    """Build a fake amazon_channels sqlite3.Row-like dict accessible by column name."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (gti TEXT, channel_id TEXT, channel_name TEXT, is_stale INTEGER)")
    conn.execute("INSERT INTO t VALUES (?,?,?,?)", (gti, channel_id, channel_name, is_stale))
    row = conn.execute("SELECT * FROM t").fetchone()
    conn.close()
    return row


class ResolveChannelAmbiguityTest(unittest.TestCase):
    """
    Regression coverage for the cross-feed misclassification bug: a live user
    reported that with only "Prime Exclusive" and a free-content sub-filter
    enabled, tuning a cricket match played Willow's subscription deeplink
    instead. Root cause: when a playable's own broadcast GTI wasn't yet
    scraped, resolve_channel() fell back to the event's shared content GTI --
    but for multi-feed events that content GTI's classification could belong
    to any sibling feed (e.g. a Willow-gated broadcast), not the one being
    resolved. content_is_ambiguous must block that fallback.
    """

    def test_ambiguous_content_gti_blocks_fallback_to_sibling_feed_classification(self):
        by_gti = {
            "content-gti": _row("content-gti", "aiv_willow", "Willow TV"),
        }
        # This playable's own broadcast GTI was never scraped (not in by_gti).
        result = mals.resolve_channel(
            by_gti,
            broadcast_gti="broadcast-b-unscraped",
            content_gti="content-gti",
            content_is_ambiguous=True,
        )
        self.assertIsNone(result, "must not borrow a sibling feed's classification")

    def test_unambiguous_content_gti_fallback_still_works(self):
        # Regression guard: single-feed events (e.g. Tennis Channel per-match feeds,
        # per the module docstring) must keep working when the broadcast page 404s
        # but the content GTI has valid channel metadata.
        by_gti = {
            "content-gti": _row("content-gti", "aiv_tennis_channel", "Tennis Channel"),
        }
        result = mals.resolve_channel(
            by_gti,
            broadcast_gti="broadcast-unscraped",
            content_gti="content-gti",
            content_is_ambiguous=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["channel_id"], "aiv_tennis_channel")

    def test_own_broadcast_gti_always_wins_even_when_ambiguous(self):
        # A playable whose own broadcast GTI *was* scraped must use it regardless
        # of ambiguity -- ambiguity only disables the content-GTI fallback.
        by_gti = {
            "broadcast-a": _row("broadcast-a", "aiv_prime", "Prime Exclusive"),
            "content-gti": _row("content-gti", "aiv_willow", "Willow TV"),
        }
        result = mals.resolve_channel(
            by_gti,
            broadcast_gti="broadcast-a",
            content_gti="content-gti",
            content_is_ambiguous=True,
        )
        self.assertEqual(result["channel_id"], "aiv_prime")

    def test_prefers_non_stale_broadcast_over_stale(self):
        by_gti = {
            "broadcast-a": _row("broadcast-a", "aiv_dazn", "DAZN", is_stale=1),
        }
        result = mals.resolve_channel(by_gti, "broadcast-a", None, content_is_ambiguous=False)
        self.assertEqual(result["channel_id"], "aiv_dazn")  # last resort still returns it


class MigrateEndToEndTest(unittest.TestCase):
    """
    Full reproduction of the reported bug against a real sqlite DB, exercising
    migrate() end-to-end rather than just resolve_channel() in isolation.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "fruit_events.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE playables (
                rowid INTEGER PRIMARY KEY,
                event_id TEXT, provider TEXT, logical_service TEXT,
                deeplink_play TEXT, deeplink_open TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE amazon_channels (gti TEXT, channel_id TEXT, channel_name TEXT, is_stale INTEGER DEFAULT 0)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert_playable(self, event_id, broadcast_gti, content_gti):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO playables (event_id, provider, logical_service, deeplink_play, deeplink_open) "
            "VALUES (?, 'aiv', '', ?, '')",
            (
                event_id,
                f"aiv://aiv/detail?gti={content_gti}&broadcast={broadcast_gti}",
            ),
        )
        conn.commit()
        conn.close()

    def _insert_amazon_channel(self, gti, channel_id, channel_name):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO amazon_channels (gti, channel_id, channel_name) VALUES (?, ?, ?)",
            (gti, channel_id, channel_name),
        )
        conn.commit()
        conn.close()

    def _logical_service_for(self, broadcast_gti):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT logical_service FROM playables WHERE deeplink_play LIKE ?",
            (f"%{broadcast_gti}%",),
        ).fetchone()
        conn.close()
        return row["logical_service"] if row else None

    def test_prime_free_feed_no_longer_inherits_sibling_willow_classification(self):
        content_gti = _gti("content-cricket-match")
        prime_broadcast = _gti("broadcast-prime")
        willow_broadcast = _gti("broadcast-willow")

        # Two feeds of the SAME cricket match: one Prime/free, one Willow-gated.
        self._insert_playable("event-1", prime_broadcast, content_gti)
        self._insert_playable("event-1", willow_broadcast, content_gti)

        # Willow's own broadcast GTI was scraped successfully.
        self._insert_amazon_channel(willow_broadcast, "aiv_willow", "Willow TV")
        # The Prime feed's own broadcast GTI was NOT scraped yet, but the shared
        # content GTI happens to be in amazon_channels too (e.g. amazon2.py's
        # broad GTI extraction queued it as its own scrape target and it landed
        # on the Willow-branded page). This is exactly the trap: the content GTI
        # is not feed-specific.
        self._insert_amazon_channel(content_gti, "aiv_willow", "Willow TV")

        mals.migrate(self.db_path)

        self.assertEqual(self._logical_service_for(willow_broadcast), "aiv_willow")
        # The critical assertion: the Prime/free feed must NOT be mislabeled as
        # Willow just because it shares a content GTI with a Willow-gated sibling.
        prime_result = self._logical_service_for(prime_broadcast)
        self.assertNotEqual(
            prime_result, "aiv_willow",
            "Prime feed must not inherit its Willow sibling's classification",
        )
        self.assertEqual(prime_result, "aiv_aggregator")

    def test_existing_sibling_classification_is_repaired_on_upgrade(self):
        content_gti = _gti("content-existing-bad-classification")
        prime_broadcast = _gti("broadcast-existing-prime")
        willow_broadcast = _gti("broadcast-existing-willow")

        self._insert_playable("event-upgrade", prime_broadcast, content_gti)
        self._insert_playable("event-upgrade", willow_broadcast, content_gti)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE playables SET logical_service='aiv_willow' WHERE event_id=?",
            ("event-upgrade",),
        )
        conn.commit()
        conn.close()

        self._insert_amazon_channel(willow_broadcast, "aiv_willow", "Willow TV")
        self._insert_amazon_channel(content_gti, "aiv_willow", "Willow TV")

        mals.migrate(self.db_path)

        self.assertEqual(self._logical_service_for(willow_broadcast), "aiv_willow")
        self.assertEqual(self._logical_service_for(prime_broadcast), "aiv_aggregator")

    def test_single_feed_event_still_resolves_via_content_gti_fallback(self):
        # Regression guard: unambiguous (single-feed) events must be unaffected.
        content_gti = _gti("content-tennis")
        broadcast_gti = _gti("broadcast-tennis-unscraped")
        self._insert_playable("event-2", broadcast_gti, content_gti)
        self._insert_amazon_channel(content_gti, "aiv_tennis_channel", "Tennis Channel")

        mals.migrate(self.db_path)

        self.assertEqual(self._logical_service_for(broadcast_gti), "aiv_tennis_channel")

    def test_own_broadcast_classification_wins_over_ambiguous_sibling(self):
        # A feed whose own broadcast GTI resolves correctly must keep that
        # classification even though it belongs to a multi-feed (ambiguous) event.
        content_gti = _gti("content-cricket-2")
        prime_broadcast = _gti("broadcast-prime-2")
        willow_broadcast = _gti("broadcast-willow-2")

        self._insert_playable("event-3", prime_broadcast, content_gti)
        self._insert_playable("event-3", willow_broadcast, content_gti)
        self._insert_amazon_channel(prime_broadcast, "aiv_prime", "Prime Exclusive")
        self._insert_amazon_channel(willow_broadcast, "aiv_willow", "Willow TV")

        mals.migrate(self.db_path)

        self.assertEqual(self._logical_service_for(prime_broadcast), "aiv_prime")
        self.assertEqual(self._logical_service_for(willow_broadcast), "aiv_willow")


class DownstreamFilteringTest(unittest.TestCase):
    """
    Closes the loop on the user's actual report: a mislabeled feed doesn't just
    get the wrong logical_service internally -- it makes it PAST the enabled
    filter and gets played, because filter_integration.get_filtered_playables
    trusts playables.logical_service. Proves the end state after the fix: the
    unclassified sibling feed is excluded rather than falsely let through as
    something it isn't (safe-by-default instead of confidently wrong).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "fruit_events.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE playables (
                playable_id TEXT, event_id TEXT, provider TEXT, logical_service TEXT,
                deeplink_play TEXT, deeplink_open TEXT, playable_url TEXT, title TEXT,
                content_id TEXT, priority INTEGER, service_name TEXT, espn_graph_id TEXT,
                locale TEXT
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _insert(self, playable_id, logical_service, priority=1):
        self.conn.execute(
            "INSERT INTO playables (playable_id, event_id, provider, logical_service, "
            "deeplink_play, deeplink_open, playable_url, title, content_id, priority, "
            "service_name, espn_graph_id) VALUES (?, 'event-1', 'aiv', ?, ?, '', '', '', '', ?, '', '')",
            (playable_id, logical_service, f"aiv://aiv/detail?gti=x&broadcast={playable_id}", priority),
        )
        self.conn.commit()

    def test_unclassified_sibling_is_excluded_not_falsely_allowed(self):
        self._insert("prime-feed", "aiv_aggregator", priority=1)  # post-fix: left unclassified
        self._insert("willow-feed", "aiv_willow", priority=2)

        results = filter_integration.get_filtered_playables(
            self.conn,
            event_id="event-1",
            enabled_services=["aiv_prime", "aiv_free"],  # user's actual reported selection
            amazon_master_enabled=True,
        )

        selected_ids = [p["playable_id"] for p in results]
        self.assertNotIn("willow-feed", selected_ids, "Willow must never be selected when not enabled")
        self.assertNotIn(
            "prime-feed", selected_ids,
            "Unclassified feed is correctly excluded rather than guessed into the enabled set",
        )
        self.assertEqual(selected_ids, [])

    def test_correctly_classified_prime_feed_is_selected_and_willow_excluded(self):
        # The success path: once amazon2.py has actually scraped the Prime feed's
        # own broadcast GTI (the fix doesn't change this case at all).
        self._insert("prime-feed", "aiv_prime", priority=1)
        self._insert("willow-feed", "aiv_willow", priority=2)

        results = filter_integration.get_filtered_playables(
            self.conn,
            event_id="event-1",
            enabled_services=["aiv_prime", "aiv_free"],
            amazon_master_enabled=True,
        )

        selected_ids = [p["playable_id"] for p in results]
        self.assertEqual(selected_ids, ["prime-feed"])


if __name__ == "__main__":
    unittest.main()

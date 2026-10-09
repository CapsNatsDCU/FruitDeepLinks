import sqlite3
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services.xtream_persistent import (  # noqa: E402
    StaleChannelLineup,
    ensure_schema,
    list_channels,
    reorder_channels,
)


class PersistentChannelReorderTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        for channel_id, number in [(1, "2"), (2, "7.5"), (3, "20")]:
            self.conn.execute(
                "INSERT INTO xtream_persistent_channels "
                "(id,stream_id,category_id,original_name,display_name,channel_number,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (channel_id, str(channel_id), "sports", f"Channel {channel_id}",
                 f"Channel {channel_id}", number, "2026-01-01", "2026-01-01"),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def order(self, ids, numbers=("2", "7.5", "20")):
        originals = dict(zip((1, 2, 3), numbers))
        return [{"id": channel_id, "channel_number": originals[channel_id]} for channel_id in ids]

    def test_drag_reassigns_existing_numbers_and_preserves_channel_identity(self):
        result = reorder_channels(self.conn, self.order((3, 1, 2)))
        self.assertEqual([(3, "2"), (1, "7.5"), (2, "20")],
                         [(channel["id"], channel["channel_number"]) for channel in result])
        self.assertEqual([(3, "3"), (1, "1"), (2, "2")],
                         [(channel["id"], channel["stream_id"]) for channel in result])

    def test_stale_and_incomplete_drag_does_not_change_numbers(self):
        for attempted in (self.order((2, 1)), self.order((2, 1, 3), ("2", "8", "20")),
                          self.order((1, 1, 3))):
            with self.assertRaises(StaleChannelLineup):
                reorder_channels(self.conn, attempted)
        self.assertEqual(["2", "7.5", "20"],
                         [channel["channel_number"] for channel in list_channels(self.conn)])


if __name__ == "__main__":
    unittest.main()

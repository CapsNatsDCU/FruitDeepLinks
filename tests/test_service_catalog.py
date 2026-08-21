import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import core.service_catalog as sc  # noqa: E402


class AmazonServiceCoverageTest(unittest.TestCase):
    """
    core/service_catalog.py is documented as the single source of truth for
    service codes (see CLAUDE.md), but DISPLAY_NAMES, INTERNAL_PRIORITY, and
    DEFAULT_USER_PRIORITY are three independently-maintained dicts with no
    cross-check between them. get_internal_priority()/get_default_user_priority()
    silently fall back to a generic default for a missing key, so a code can go
    unnoticed for a long time: aiv_wnba_league_pass and aiv_paramount_plus were
    both live in production (26 and 16 playables) with real display names and
    default priorities, but missing from INTERNAL_PRIORITY, discovered only by
    manually diffing live DB codes against the catalog during a debug session.
    Every aiv_* code should be fully covered by all three dicts.
    """

    def _aiv_keys(self, d):
        return {k for k in d if k.startswith("aiv")}

    def test_every_display_name_has_internal_priority(self):
        display = self._aiv_keys(sc.DISPLAY_NAMES)
        internal = self._aiv_keys(sc.INTERNAL_PRIORITY)
        missing = display - internal
        self.assertFalse(missing, f"aiv_* codes missing from INTERNAL_PRIORITY: {sorted(missing)}")

    def test_every_display_name_has_default_user_priority(self):
        display = self._aiv_keys(sc.DISPLAY_NAMES)
        default = self._aiv_keys(sc.DEFAULT_USER_PRIORITY)
        missing = display - default
        self.assertFalse(missing, f"aiv_* codes missing from DEFAULT_USER_PRIORITY: {sorted(missing)}")

    def test_every_internal_priority_has_display_name(self):
        internal = self._aiv_keys(sc.INTERNAL_PRIORITY)
        display = self._aiv_keys(sc.DISPLAY_NAMES)
        missing = internal - display
        self.assertFalse(missing, f"aiv_* codes in INTERNAL_PRIORITY with no DISPLAY_NAMES entry: {sorted(missing)}")

    def test_every_default_user_priority_has_display_name(self):
        default = self._aiv_keys(sc.DEFAULT_USER_PRIORITY)
        display = self._aiv_keys(sc.DISPLAY_NAMES)
        missing = default - display
        self.assertFalse(missing, f"aiv_* codes in DEFAULT_USER_PRIORITY with no DISPLAY_NAMES entry: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()

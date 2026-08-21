import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import core.service_catalog as sc  # noqa: E402
from adb_provider_mapper import ADB_PROVIDER_MAP, get_adb_provider_code  # noqa: E402

# Deliberately excluded from the 'aiv' ADB lane -- the confirmed-unwatchable
# bucket should never show up as selectable content there.
_INTENTIONALLY_UNMAPPED = {"aiv_unavailable_in_location"}


class AdbProviderCoverageTest(unittest.TestCase):
    """
    Every known Amazon sub-service in core/service_catalog.py should collapse
    to the shared 'aiv' ADB provider (per the module's own comment: "all
    Amazon content uses same ADB lanes"), or it silently falls through to an
    identity mapping instead and gets left out of fruit_build_adb_lanes.py's
    ADB lane entirely. aiv_bein (40 live playables) and aiv_mlb_network (7
    live playables) were missing this way, found only by manually diffing
    the catalog against this map during a debug session -- Fire TV/Android
    users were silently missing beIN Sports and MLB Network content via
    Amazon while it worked fine on the direct/lanes export paths.
    """

    def test_every_catalog_amazon_service_maps_to_aiv(self):
        catalog_aiv_codes = {k for k in sc.DISPLAY_NAMES if k.startswith("aiv")}
        expected = catalog_aiv_codes - _INTENTIONALLY_UNMAPPED
        unmapped = {code for code in expected if get_adb_provider_code(code) != "aiv"}
        self.assertFalse(
            unmapped,
            f"aiv_* codes not grouped into the shared 'aiv' ADB lane: {sorted(unmapped)}",
        )

    def test_unwatchable_bucket_is_deliberately_excluded(self):
        # Regression guard: don't let a future edit "helpfully" add this back.
        self.assertNotIn("aiv_unavailable_in_location", ADB_PROVIDER_MAP)


if __name__ == "__main__":
    unittest.main()

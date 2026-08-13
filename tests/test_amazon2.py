import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def _load_amazon2_module():
    module_name = "amazon2_under_test"
    script_path = Path(__file__).resolve().parents[1] / "bin" / "amazon2.py"

    fake_playwright = types.ModuleType("playwright")
    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.TimeoutError = TimeoutError
    fake_async_api.async_playwright = None
    fake_playwright.async_api = fake_async_api

    original_modules = {
        "playwright": sys.modules.get("playwright"),
        "playwright.async_api": sys.modules.get("playwright.async_api"),
    }
    sys.modules["playwright"] = fake_playwright
    sys.modules["playwright.async_api"] = fake_async_api

    try:
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


amazon2 = _load_amazon2_module()


class FakePage:
    def __init__(self, hrefs=None, error=None):
        self.hrefs = hrefs or []
        self.error = error

    async def eval_on_selector_all(self, selector, script):
        if self.error:
            raise self.error
        return list(self.hrefs)


class FakeLocator:
    def __init__(self, text=""):
        self.text = text

    async def count(self):
        return 1 if self.text else 0

    @property
    def first(self):
        return self

    async def inner_text(self):
        return self.text


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeScrapePage:
    def __init__(
        self,
        *,
        url,
        response_status=200,
        html="",
        body_text="",
        title_text="",
        entitlement_text="",
        hrefs=None,
        content_side_effects=None,
    ):
        self.url = url
        self._response_status = response_status
        self._html = html
        self._body_text = body_text
        self._title_text = title_text
        self._entitlement_text = entitlement_text
        self._hrefs = hrefs or []
        self._content_side_effects = list(content_side_effects or [])
        self.closed = False
        self.goto_calls = []
        self.wait_for_timeout_calls = []

    async def goto(self, url, wait_until, timeout):
        self.goto_calls.append((url, wait_until, timeout))
        return FakeResponse(self._response_status)

    async def wait_for_load_state(self, state, timeout):
        return None

    async def wait_for_timeout(self, timeout):
        self.wait_for_timeout_calls.append(timeout)

    async def content(self):
        if self._content_side_effects:
            effect = self._content_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return self._html

    async def inner_text(self, selector):
        if selector == "body":
            return self._body_text
        return ""

    async def title(self):
        return self._title_text

    def locator(self, selector):
        if selector in {
            '[data-automation-id="entitlement-message"]',
            '[data-testid="entitlement-message"]',
            "#entitlement-message",
        }:
            return FakeLocator(self._entitlement_text)
        return FakeLocator("")

    async def eval_on_selector_all(self, selector, script):
        return list(self._hrefs)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.init_scripts = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.contexts = []
        self.new_context_calls = []

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        page = self.pages.pop(0)
        ctx = FakeContext(page)
        self.contexts.append(ctx)
        return ctx


class Amazon2HelpersTest(unittest.TestCase):
    def test_parse_benefit_id_from_html_and_ignores_false_positive(self):
        html = """
        <a href="/gp/video/offers?benefitId=amzn1">bad</a>
        <script>window.payload = {"benefitId":"peacockus"};</script>
        """
        self.assertEqual(amazon2._parse_benefit_id(html), "peacockus")

    def test_extract_known_benefit_id_from_html_finds_embedded_marker(self):
        html = '<script>window.__data = {"imageUrl":"/maxliveeventsus/logos/channels-logo-white.png"};</script>'
        self.assertEqual(amazon2._extract_known_benefit_id_from_html(html), "maxliveeventsus")

    def test_extract_benefit_id_from_links_returns_first_match(self):
        page = FakePage(
            hrefs=[
                "/gp/video/detail/no-benefit-here",
                "/gp/video/offers?benefitId=amzn1.dv.spid.8cc2a36e-cd1b-d2cb-0e3b-b9ddce868f1d",
                "/gp/video/offers?benefitId=peacockus",
            ]
        )
        benefit_id = asyncio.run(amazon2._extract_benefit_id_from_links(page))
        self.assertEqual(
            benefit_id,
            "amzn1.dv.spid.8cc2a36e-cd1b-d2cb-0e3b-b9ddce868f1d",
        )

    def test_extract_benefit_id_from_links_swallows_page_errors(self):
        page = FakePage(error=RuntimeError("page eval failed"))
        benefit_id = asyncio.run(amazon2._extract_benefit_id_from_links(page))
        self.assertEqual(benefit_id, "")

    def test_normalize_prefers_known_benefit_map(self):
        name, channel_id, reason = amazon2._normalize(
            "peacockus",
            entitlement="Watch with Peacock",
        )
        self.assertEqual((name, channel_id, reason), ("Peacock", "aiv_peacock", ""))

    def test_normalize_can_infer_from_entitlement_when_benefit_unknown(self):
        name, channel_id, reason = amazon2._normalize(
            "unknown-benefit",
            entitlement="Subscribe to NBA League Pass to watch live",
        )
        self.assertEqual(name, "NBA League Pass")
        self.assertEqual(channel_id, "aiv_nba_league_pass")
        self.assertIn("inferred_from=entitlement", reason)

    def test_normalize_falls_back_to_slugified_entitlement(self):
        name, channel_id, reason = amazon2._normalize(
            "",
            entitlement="Regional Sports Add-On!",
        )
        self.assertEqual(name, "Regional Sports Add-On!")
        self.assertEqual(channel_id, "aiv_regional_sports_add_on")
        self.assertIn("fallback_to_entitlement", reason)

    def test_normalize_matches_paramount_plus_despite_trailing_symbol(self):
        # Regression: \bParamount\+\b never matched because '+' isn't a word character,
        # so \b can't form a boundary right after it (confirmed live 2026-08-13).
        name, channel_id, reason = amazon2._normalize(
            "",
            entitlement="Free trial of Paramount+",
        )
        self.assertEqual(name, "Paramount+")
        self.assertEqual(channel_id, "aiv_paramount_plus")

    def test_normalize_does_not_scan_page_text(self):
        # Regression: page-wide text scanning produced false positives across multiple
        # services (Prime, MLB Network -> Tennis Channel, Paramount+ -> WNBA League Pass),
        # confirmed live 2026-08-13, because Amazon's page includes unrelated content
        # (recommendation widgets) that matched these same patterns. _normalize must only
        # consider the narrow entitlement message, never page_text.
        self.assertNotIn("page_text", amazon2._normalize.__code__.co_varnames)

    def test_normalize_uses_confirmed_benefit_ids(self):
        for benefit_id, expected_channel_id in (
            ("foxoneus", "aiv_fox_one"),
            ("beinus", "aiv_bein"),
            ("mlbnetwork", "aiv_mlb_network"),
            ("cbsaacf", "aiv_paramount_plus"),
            ("cbsaapc", "aiv_paramount_plus"),
        ):
            with self.subTest(benefit_id=benefit_id):
                # Entitlement deliberately doesn't mention the service, to prove the
                # benefit_id -> BENEFIT_MAP lookup takes priority over any text inference.
                name, channel_id, reason = amazon2._normalize(
                    benefit_id,
                    entitlement="Watch for free",
                )
                self.assertEqual(channel_id, expected_channel_id)
                self.assertEqual(reason, "")

    def test_blank_unusable_page_detects_redirect_without_signals(self):
        is_blank, detail = amazon2._looks_blank_unusable_page(
            final_url="https://www.amazon.com/dp/B000TEST",
            benefit_id="",
            entitlement="",
            channel_id="",
            page_text="",
        )
        self.assertTrue(is_blank)
        self.assertEqual(detail, "retail_redirect_no_signals")

    def test_blank_unusable_page_allows_real_signal(self):
        is_blank, detail = amazon2._looks_blank_unusable_page(
            final_url="https://www.amazon.com/gp/video/offers/foo",
            benefit_id="peacockus",
            entitlement="",
            channel_id="aiv_peacock",
            page_text="",
        )
        self.assertFalse(is_blank)
        self.assertEqual(detail, "")

    def test_stale_404_detects_hard_status(self):
        is_stale, detail = amazon2._looks_stale_404(
            resp_status=404,
            title="Some title",
            page_text="Valid looking page text",
            benefit_id="peacockus",
            entitlement="Watch with Peacock",
            channel_id="aiv_peacock",
        )
        self.assertTrue(is_stale)
        self.assertEqual(detail, "http_status=404")

    def test_stale_404_requires_missing_signals_for_marker_only_detection(self):
        is_stale, detail = amazon2._looks_stale_404(
            resp_status=200,
            title="Dogs of Amazon",
            page_text="Sorry! We couldn't find that page",
            benefit_id="",
            entitlement="",
            channel_id="",
        )
        self.assertTrue(is_stale)
        self.assertEqual(detail, "visible_404_markers_no_signals")

    def test_stale_404_does_not_flag_marker_page_when_signals_exist(self):
        is_stale, detail = amazon2._looks_stale_404(
            resp_status=200,
            title="Dogs of Amazon",
            page_text="Sorry! We couldn't find that page",
            benefit_id="peacockus",
            entitlement="Watch with Peacock",
            channel_id="aiv_peacock",
        )
        self.assertFalse(is_stale)
        self.assertEqual(detail, "")

    def test_shell_page_detection_flags_continue_shopping_shell(self):
        is_shell, reason = amazon2._looks_shell_page(
            final_url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.123",
            title="Amazon.com",
            page_text="Click the button below to continue shopping",
            benefit_id="",
            entitlement="",
            channel_id="aiv_amazon_error",
        )
        self.assertTrue(is_shell)
        self.assertEqual(reason, "continue_shopping_shell")

    def test_unavailable_page_detection_flags_geo_message(self):
        is_unavailable, reason = amazon2._looks_unavailable_page(
            page_text="This video is currently unavailable to watch in your location",
            title="Watch Something",
        )
        self.assertTrue(is_unavailable)
        self.assertEqual(reason, "UNAVAILABLE_IN_LOCATION")

    def test_unavailable_page_detection_flags_regional_restrictions_from_buy_box(self):
        # Regression: user spot-checked a live aiv_amazon_error URL on 2026-08-13 and it
        # showed this exact banner in the structured buy-box-msg element (confirmed against
        # a screenshot), which none of the existing markers matched.
        is_unavailable, reason = amazon2._looks_unavailable_page(
            page_text="some other page text",
            title="",
            buy_box_message="UNAVAILABLE DUE TO REGIONAL RESTRICTIONS",
        )
        self.assertTrue(is_unavailable)
        self.assertEqual(reason, "UNAVAILABLE_IN_LOCATION")

    def test_unavailable_page_detection_ignores_faq_boilerplate_in_page_text(self):
        # Regression: an earlier version of this check scanned the whole page_text for
        # "unavailable due to regional restrictions" and produced false positives on ~73%
        # of a live scrape, because Amazon's live-sports FAQ widget quotes that exact phrase
        # as a generic example on virtually every page (confirmed live 2026-08-13 by fetching
        # a page with a known-good WNBA League Pass classification and finding the phrase
        # twice in FAQ text). The phrase must NOT trigger a match via page_text/title --
        # only via the scoped buy_box_message argument.
        is_unavailable, reason = amazon2._looks_unavailable_page(
            page_text=(
                'If you see "this video is unavailable" or "unavailable due to regional '
                'restrictions" that means the game isn\'t available in your area.'
            ),
            title="",
            buy_box_message="",
        )
        self.assertFalse(is_unavailable)
        self.assertEqual(reason, "")


class Amazon2ScrapeOneTest(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_one_returns_http_probe_result_without_browser(self):
        expected = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.12345678-1234-1234-1234-1234567890ab",
            url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.12345678-1234-1234-1234-1234567890ab",
            status="SUCCESS",
            channel_id="aiv_max",
            channel_name="Max",
            benefit_id="maxliveeventsus",
            entitlement_text="Subscribe for $18.49/month",
            failure_reason="",
            elapsed_ms=12,
        )
        browser = FakeBrowser([])

        with mock.patch.object(
            amazon2,
            "_probe_http_once",
            return_value=amazon2.HttpProbeResult(
                result=expected,
                needs_browser_fallback=False,
                fallback_reason="",
            ),
        ):
            result = await amazon2.scrape_one(
                playwright=None,
                browser=browser,
                gti=expected.gti,
                timeout_ms=3210,
                retries=0,
                unknown_seen=set(),
                progress_idx=1,
                total=1,
            )

        self.assertEqual(result, expected)
        self.assertEqual(browser.new_context_calls, [])

    async def test_scrape_one_success_from_benefit_id_in_url(self):
        gti = "amzn1.dv.gti.12345678-1234-1234-1234-1234567890ab"
        page = FakeScrapePage(
            url="https://www.amazon.com/gp/video/detail/foo?benefitId=peacockus",
            response_status=200,
            html="<html></html>",
            body_text="Watch live now",
            title_text="Amazon Video",
            entitlement_text="Watch with Peacock",
        )
        browser = FakeBrowser([page])
        with mock.patch.object(
            amazon2,
            "_probe_http_once",
            return_value=amazon2.HttpProbeResult(
                result=None,
                needs_browser_fallback=True,
                fallback_reason="test_forced_browser",
            ),
        ):
            result = await amazon2.scrape_one(
                playwright=None,
                browser=browser,
                gti=gti,
                timeout_ms=3210,
                retries=0,
                unknown_seen=set(),
                progress_idx=1,
                total=1,
            )

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.channel_id, "aiv_peacock")
        self.assertEqual(result.channel_name, "Peacock")
        self.assertEqual(result.benefit_id, "peacockus")
        self.assertEqual(result.failure_reason, "")
        self.assertTrue(page.closed)
        self.assertTrue(browser.contexts[0].closed)
        self.assertEqual(page.goto_calls[0][0], amazon2.gti_to_url(gti))

    async def test_scrape_one_retries_transient_content_error_then_succeeds(self):
        gti = "amzn1.dv.gti.12345678-1234-1234-1234-1234567890ab"
        page = FakeScrapePage(
            url="https://www.amazon.com/gp/video/detail/foo",
            response_status=200,
            body_text="Subscribe to NBA League Pass",
            title_text="Amazon Video",
            entitlement_text="Subscribe to NBA League Pass",
            content_side_effects=[
                RuntimeError("page is navigating and changing"),
                '<a href="/gp/video/offers?benefitId=amzn1.dv.channel.7a36cb2b-40e6-40c7-809f-a6cf9b9f0859">link</a>',
            ],
        )
        browser = FakeBrowser([page])
        with mock.patch.object(
            amazon2,
            "_probe_http_once",
            return_value=amazon2.HttpProbeResult(
                result=None,
                needs_browser_fallback=True,
                fallback_reason="test_forced_browser",
            ),
        ):
            result = await amazon2.scrape_one(
                playwright=None,
                browser=browser,
                gti=gti,
                timeout_ms=3210,
                retries=0,
                unknown_seen=set(),
                progress_idx=1,
                total=1,
            )

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.channel_id, "aiv_nba_league_pass")
        self.assertIn(500, page.wait_for_timeout_calls)

    async def test_scrape_one_marks_hard_404_as_stale(self):
        gti = "amzn1.dv.gti.12345678-1234-1234-1234-1234567890ab"
        page = FakeScrapePage(
            url="https://www.amazon.com/gp/video/detail/foo",
            response_status=404,
            html="<html>not found</html>",
            body_text="Sorry! We couldn't find that page",
            title_text="Dogs of Amazon",
            entitlement_text="",
        )
        browser = FakeBrowser([page])
        with mock.patch.object(
            amazon2,
            "_probe_http_once",
            return_value=amazon2.HttpProbeResult(
                result=None,
                needs_browser_fallback=True,
                fallback_reason="test_forced_browser",
            ),
        ):
            result = await amazon2.scrape_one(
                playwright=None,
                browser=browser,
                gti=gti,
                timeout_ms=3210,
                retries=0,
                unknown_seen=set(),
                progress_idx=1,
                total=1,
            )

        self.assertEqual(result.status, "STALE")
        self.assertEqual(result.failure_reason, "STALE_GTI_404")
        self.assertEqual(result.channel_id, "")
        self.assertEqual(result.channel_name, "")


class Amazon2UnavailableLogTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "fruit_events.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute(
            "CREATE TABLE playables (event_id INTEGER, deeplink_play TEXT, deeplink_open TEXT, provider TEXT)"
        )
        conn.execute("INSERT INTO events (id, title) VALUES (1, 'MLB: Boston Red Sox at Pittsburgh Pirates')")
        conn.execute(
            "INSERT INTO playables (event_id, deeplink_play, deeplink_open, provider) VALUES "
            "(1, 'aiv://aiv/detail?gti=amzn1.dv.gti.aaaa&broadcast=amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48', "
            "'', 'aiv')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_logs_only_unavailable_rows_with_title_lookup(self):
        results = [
            amazon2.ScrapeResult(
                gti="amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48",
                url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48",
                status="ERROR",
                channel_id="",
                channel_name="",
                benefit_id="",
                entitlement_text="",
                failure_reason="UNAVAILABLE_IN_LOCATION",
                elapsed_ms=100,
            ),
            amazon2.ScrapeResult(
                gti="amzn1.dv.gti.bbbb",
                url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.bbbb",
                status="SUCCESS",
                channel_id="aiv_dazn",
                channel_name="DAZN",
                benefit_id="daznus",
                entitlement_text="",
                failure_reason="",
                elapsed_ms=100,
            ),
        ]

        logged = amazon2.log_unavailable_results(self.db_path, results)
        self.assertEqual(logged, 1)

        log_path = Path(self._tmpdir.name) / "amazon_unavailable_log.csv"
        self.assertTrue(log_path.exists())

        import csv

        with open(log_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gti"], "amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48")
        self.assertEqual(rows[0]["title"], "MLB: Boston Red Sox at Pittsburgh Pirates")

    def test_no_unavailable_rows_means_no_file_created(self):
        results = [
            amazon2.ScrapeResult(
                gti="amzn1.dv.gti.bbbb",
                url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.bbbb",
                status="SUCCESS",
                channel_id="aiv_dazn",
                channel_name="DAZN",
                benefit_id="daznus",
                entitlement_text="",
                failure_reason="",
                elapsed_ms=100,
            ),
        ]
        logged = amazon2.log_unavailable_results(self.db_path, results)
        self.assertEqual(logged, 0)
        log_path = Path(self._tmpdir.name) / "amazon_unavailable_log.csv"
        self.assertFalse(log_path.exists())

    def test_appends_across_multiple_calls(self):
        result = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48",
            url="https://www.amazon.com/gp/video/detail/amzn1.dv.gti.35668709-6474-44f9-9459-b83349a85c48",
            status="ERROR",
            channel_id="",
            channel_name="",
            benefit_id="",
            entitlement_text="",
            failure_reason="UNAVAILABLE_IN_LOCATION",
            elapsed_ms=100,
        )
        amazon2.log_unavailable_results(self.db_path, [result])
        amazon2.log_unavailable_results(self.db_path, [result])

        import csv

        log_path = Path(self._tmpdir.name) / "amazon_unavailable_log.csv"
        with open(log_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)


class Amazon2UpsertResultsTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "fruit_events.db")
        conn = sqlite3.connect(self.db_path)
        amazon2._ensure_tables(conn)
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _row(self, gti):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM amazon_channels WHERE gti=?", (gti,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_success_writes_normally(self):
        result = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.aaaa", url="", status="SUCCESS",
            channel_id="aiv_dazn", channel_name="DAZN",
            benefit_id="daznus", entitlement_text="", failure_reason="", elapsed_ms=1,
        )
        n = amazon2.upsert_results(self.db_path, [result])
        self.assertEqual(n, 1)
        row = self._row("amzn1.dv.gti.aaaa")
        self.assertEqual(row["channel_id"], "aiv_dazn")

    def test_confirmed_unavailable_in_location_is_written(self):
        # Regression: this ERROR-status result must NOT be silently dropped -- it's a
        # deterministic per-title signal (scoped buy-box-msg element), not a transient
        # failure, and should get its own distinct, filterable logical_service.
        result = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.bbbb", url="", status="ERROR",
            channel_id=amazon2.UNAVAILABLE_IN_LOCATION_CHANNEL_ID,
            channel_name=amazon2.UNAVAILABLE_IN_LOCATION_CHANNEL_NAME,
            benefit_id="daznus", entitlement_text="", failure_reason="UNAVAILABLE_IN_LOCATION",
            elapsed_ms=1,
        )
        n = amazon2.upsert_results(self.db_path, [result])
        self.assertEqual(n, 1)
        row = self._row("amzn1.dv.gti.bbbb")
        self.assertEqual(row["channel_id"], "aiv_unavailable_in_location")
        self.assertEqual(row["is_stale"], 0)

    def test_confirmed_unavailable_does_not_overwrite_with_stale_flag(self):
        # A previously-good SUCCESS classification, later found region-blocked, should be
        # overwritten with the unavailable state (not left stuck on the old service) --
        # that's the whole point of writing it at all instead of skipping like other errors.
        good = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.cccc", url="", status="SUCCESS",
            channel_id="aiv_dazn", channel_name="DAZN",
            benefit_id="daznus", entitlement_text="", failure_reason="", elapsed_ms=1,
        )
        amazon2.upsert_results(self.db_path, [good])

        blocked = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.cccc", url="", status="ERROR",
            channel_id=amazon2.UNAVAILABLE_IN_LOCATION_CHANNEL_ID,
            channel_name=amazon2.UNAVAILABLE_IN_LOCATION_CHANNEL_NAME,
            benefit_id="daznus", entitlement_text="", failure_reason="UNAVAILABLE_IN_LOCATION",
            elapsed_ms=1,
        )
        amazon2.upsert_results(self.db_path, [blocked])

        row = self._row("amzn1.dv.gti.cccc")
        self.assertEqual(row["channel_id"], "aiv_unavailable_in_location")

    def test_generic_error_is_still_skipped(self):
        result = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.dddd", url="", status="ERROR",
            channel_id="", channel_name="",
            benefit_id="", entitlement_text="", failure_reason="http_error=ConnectionError",
            elapsed_ms=1,
        )
        n = amazon2.upsert_results(self.db_path, [result])
        self.assertEqual(n, 0)
        self.assertIsNone(self._row("amzn1.dv.gti.dddd"))

    def test_timeout_is_still_skipped(self):
        result = amazon2.ScrapeResult(
            gti="amzn1.dv.gti.eeee", url="", status="TIMEOUT",
            channel_id="", channel_name="",
            benefit_id="", entitlement_text="", failure_reason="TIMEOUT",
            elapsed_ms=1,
        )
        n = amazon2.upsert_results(self.db_path, [result])
        self.assertEqual(n, 0)
        self.assertIsNone(self._row("amzn1.dv.gti.eeee"))


if __name__ == "__main__":
    unittest.main()

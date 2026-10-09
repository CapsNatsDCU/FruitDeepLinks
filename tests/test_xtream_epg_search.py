"""Offline guide discovery stays exhaustive, paginated, and operator controlled."""
import unittest
from unittest.mock import patch

from tests import test_xtream_epg_lineup as fixtures
from server.app import create_app
from server.services import xtream_epg_index as index
from server.services.xtream_channel_cache import replace_snapshot
from server.services.xtream_persistent import get_channel
from tests import test_external_xmltv as external_fixtures


class EpgSearchTests(unittest.TestCase):
    setUp = fixtures.EpgLineupTests.setUp
    programme = fixtures.EpgLineupTests.programme
    import_xml = external_fixtures.ExternalXmltvTests.import_xml

    def test_pages_include_every_guide_once_beyond_previous_limit(self):
        index.replace_snapshot(self.conn, {
            f'guide{i:03}': {'names': [f'Sports station {i:03}'], 'programme_count': i}
            for i in range(123)
        })
        # A cached alias for an indexed guide must not duplicate its row.
        replace_snapshot(self.conn, [('10', 'Sports')], [
            ('10', '88', 'Sports station 000 HD', 'sports station 000 hd', None, 'guide000', 'ts')])
        client = create_app().test_client()
        with patch('server.routes.api.xtream._configured_client', side_effect=AssertionError('Offline search')):
            pages = [client.get(f'/api/xtream/epg/links/search?q=Sports&offset={n}').get_json()
                     for n in (0, 50, 100)]
        self.assertEqual([50, 50, 23], [len(p['candidates']) for p in pages])
        self.assertEqual([123] * 3, [p['total'] for p in pages])
        self.assertEqual([True, True, False], [p['has_more'] for p in pages])
        self.assertEqual(123, len({c['guide_id'] for p in pages for c in p['candidates']}))
        again = index.search_page(self.conn, 'Sports', offset=50)
        self.assertEqual(pages[1]['candidates'], again['candidates'])
        self.assertIsNone(get_channel(self.conn, self.channel['id'])['epg_source_id'])

    def test_broader_mode_reveals_weaker_matches_and_other_station_numbers(self):
        index.replace_snapshot(self.conn, {
            'sports': {'names': ['Monumental Network'], 'programme_count': 5},
            'fox5': {'names': ['FOX 5'], 'programme_count': 5},
            'fox50': {'names': ['FOX 50'], 'programme_count': 5},
        })
        self.assertEqual([], index.search(self.conn, 'Monumental Sports Washington'))
        self.assertIn('sports', [c['guide_id'] for c in index.search_page(
            self.conn, 'Monumental Sports Washington', mode='broad')['candidates']])
        self.assertNotIn('fox50', [c['guide_id'] for c in index.search(self.conn, 'FOX 5')])
        self.assertIn('fox50', [c['guide_id'] for c in index.search_page(
            self.conn, 'FOX 5', mode='broad')['candidates']])

    def test_browse_all_includes_unrelated_provider_external_and_cached_guides(self):
        self.import_xml('<tv><channel id="20367"><display-name>WTTGDT</display-name></channel></tv>')
        index.replace_snapshot(self.conn, {'ESPN.us': {'names': ['ESPNHD'], 'programme_count': 3}})
        replace_snapshot(self.conn, [('10', 'Sports')], [
            ('10', '88', 'NBC local', 'nbc local', None, 'NBC.us', 'ts')])
        client = create_app().test_client()
        with patch('server.routes.api.xtream._configured_client', side_effect=AssertionError('Offline search')), \
             patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            result = client.get('/api/xtream/epg/links/search?mode=all').get_json()
            station = client.get('/api/xtream/epg/links/search?q=xmltv:1:20367').get_json()
            substring = client.get('/api/xtream/epg/links/search?mode=all&q=TTG').get_json()
        self.assertEqual({'ESPN.us', 'NBC.us', 'xmltv:1:20367'}, {c['guide_id'] for c in result['candidates']})
        self.assertEqual(['ESPNHD', 'NBC local', 'WTTGDT'], [c['display_name'] for c in result['candidates']])
        self.assertEqual(['xmltv:1:20367'], [c['guide_id'] for c in station['candidates']])
        self.assertEqual(station['candidates'], substring['candidates'])
        self.assertEqual('ESPN.us', index.search(self.conn, 'ESPN')[0]['guide_id'])
        self.assertIsNone(get_channel(self.conn, self.channel['id'])['epg_source_id'])

    def test_api_rejects_invalid_modes_pages_and_queries(self):
        client = create_app().test_client()
        for args in ('mode=invalid', 'q=Sports&limit=0', 'q=Sports&limit=101',
                     'q=Sports&limit=abc', 'q=Sports&offset=-1', 'q=Sports&offset=1.5',
                     'mode=broad', 'mode=all&q=' + 'x' * 513):
            with self.subTest(args=args):
                self.assertEqual(400, client.get('/api/xtream/epg/links/search?' + args).status_code)

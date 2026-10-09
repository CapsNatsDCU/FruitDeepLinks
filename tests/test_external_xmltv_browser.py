"""Imported guide browsing, preview, and explicit assignment stay offline."""
import unittest
from unittest.mock import patch
from datetime import timedelta

from tests import test_external_xmltv as fixtures
from server.app import create_app
from server.services import external_xmltv
from server.services.xtream_persistent import create_channel, get_channel, render_xmltv, update_channel
from xtream_epg import cached_programmes
from xml.etree import ElementTree as ET


class ExternalBrowserTests(unittest.TestCase):
    setUp = fixtures.ExternalXmltvTests.setUp
    programme = fixtures.ExternalXmltvTests.programme
    import_xml = fixtures.ExternalXmltvTests.import_xml

    def import_station(self):
        self.import_xml('<tv><channel id="20367"><display-name>WTTGDT</display-name><display-name>FOX 5</display-name></channel>'
                        + self.programme(guide='20367') + '</tv>')

    def test_browse_pages_search_aliases_and_assigned_channels_without_network(self):
        self.import_xml('<tv>' + ''.join(f'<channel id="{i:03}"><display-name>Station {i:03}</display-name></channel>'
                                       for i in range(61)) + '</tv>')
        update_channel(self.conn, self.channel['id'], {'epg_source_id': 'xmltv:1:060'})
        client = create_app().test_client()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline browser')), \
             patch('server.routes.api.xtream._configured_client', side_effect=AssertionError('No provider')):
            first = client.get('/api/xtream/epg/external/stations').get_json()
            last = client.get('/api/xtream/epg/external/stations?offset=50').get_json()
            filtered = client.get('/api/xtream/epg/external/stations?q=060').get_json()
        self.assertEqual((61, 50, True), (first['total'], len(first['stations']), first['has_more']))
        self.assertEqual((11, False), (len(last['stations']), last['has_more']))
        self.assertEqual(61, len({s['guide_id'] for s in first['stations'] + last['stations']}))
        self.assertEqual('xmltv:1:060', filtered['stations'][0]['guide_id'])
        self.assertEqual(self.channel['id'], filtered['stations'][0]['assigned_channels'][0]['id'])
        for args in ('offset=-1', 'offset=no', 'q=' + 'x' * 513):
            self.assertEqual(400, client.get('/api/xtream/epg/external/stations?' + args).status_code)

    def test_preview_bounds_current_schedule_and_searches_alternate_display_name(self):
        programmes = []
        for i in range(25):
            start = self.start + timedelta(hours=i)
            stop = start + timedelta(minutes=30)
            programmes.append(f'<programme channel="20367" start="{start:%Y%m%d%H%M%S +0000}" stop="{stop:%Y%m%d%H%M%S +0000}"><title>Show {i} &amp; news</title><desc>Details &lt;safe&gt;</desc></programme>')
        self.import_xml('<tv><channel id="20367"><display-name>WTTGDT</display-name><display-name>FOX 5</display-name></channel>' + ''.join(programmes) + '</tv>')
        client = create_app().test_client()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline preview')):
            preview = client.get('/api/xtream/epg/external/station?guide_id=xmltv:1:20367').get_json()
            found = client.get('/api/xtream/epg/external/stations?q=fox%205').get_json()
            missing = client.get('/api/xtream/epg/external/station?guide_id=xmltv:1:missing')
        self.assertEqual(20, len(preview['programmes']))
        self.assertEqual('Show 0 & news', preview['programmes'][0]['title'])
        self.assertEqual('Details <safe>', preview['programmes'][0]['description'])
        self.assertEqual(self.start.isoformat(), preview['programmes'][0]['start'])
        self.assertEqual(1, found['total'])
        self.assertEqual(404, missing.status_code)
        self.assertIsNone(get_channel(self.conn, self.channel['id'])['epg_source_id'])

    def test_assign_replaces_only_chosen_channel_and_preserves_export_identity(self):
        self.import_station()
        other = create_channel(self.conn, {'stream_id': '88', 'name': 'Other'}, category_id='10', category_name='Sports',
                               channel_number='9001', epg_source_id='xmltv:1:20367')
        external_xmltv.apply_selected(self.conn)
        before = self.conn.execute('SELECT * FROM xtream_epg_programmes WHERE persistent_id=?', (other['id'],)).fetchall()
        client = create_app().test_client()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline assignment')), \
             patch('server.routes.api.xtream._configured_client', side_effect=AssertionError('No provider')), \
             patch('server.services.external_xmltv.apply_selected', wraps=external_xmltv.apply_selected) as apply:
            response = client.post('/api/xtream/epg/external/assign', json={
                'persistent_id': self.channel['id'], 'guide_id': 'xmltv:1:20367', 'expected_source_id': None})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        apply.assert_called_once()
        self.assertEqual(self.channel['id'], apply.call_args.kwargs['persistent_id'])
        changed = response.get_json()['channel']
        self.assertEqual('xmltv:1:20367', changed['epg_source_id'])
        for key in ('effective_guide_id', 'stream_id', 'channel_number', 'epg_channel_id'):
            self.assertEqual(self.channel[key], changed[key])
        self.assertEqual(1, response.get_json()['programmes'])
        self.assertEqual('Sports & News', cached_programmes(self.conn, changed)[0].findtext('title'))
        after = self.conn.execute('SELECT * FROM xtream_epg_programmes WHERE persistent_id=?', (other['id'],)).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        tree = ET.fromstring(render_xmltv(self.conn))
        self.assertIn(self.channel['effective_guide_id'], [p.get('channel') for p in tree.findall('programme')])

    def test_invalid_or_stale_assignment_leaves_selection_and_schedule_unchanged(self):
        self.import_station()
        client = create_app().test_client()
        for payload, code in (({}, 400), ([], 400), ({'persistent_id': True, 'guide_id': 'xmltv:1:20367'}, 400),
                              ({'persistent_id': self.channel['id'], 'guide_id': '20367'}, 400),
                              ({'persistent_id': 999, 'guide_id': 'xmltv:1:20367'}, 404),
                              ({'persistent_id': self.channel['id'], 'guide_id': 'xmltv:1:20367', 'expected_source_id': 'other'}, 409)):
            with self.subTest(payload=payload):
                self.assertEqual(code, client.post('/api/xtream/epg/external/assign', json=payload).status_code)
                self.assertIsNone(get_channel(self.conn, self.channel['id'])['epg_source_id'])
                self.assertEqual([], cached_programmes(self.conn, self.channel))

    def test_disabled_channel_can_be_assigned_without_fetching_or_enabling(self):
        self.import_station()
        update_channel(self.conn, self.channel['id'], {'enabled': False})
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            response = create_app().test_client().post('/api/xtream/epg/external/assign', json={
                'persistent_id': self.channel['id'], 'guide_id': 'xmltv:1:20367'})
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.get_json()['channel']['enabled'])
        self.assertEqual(0, response.get_json()['programmes'])
        self.assertEqual('xmltv:1:20367', get_channel(self.conn, self.channel['id'])['epg_source_id'])

    def test_no_import_has_empty_browser_and_missing_preview(self):
        client = create_app().test_client()
        self.assertEqual(0, client.get('/api/xtream/epg/external/stations').get_json()['total'])
        self.assertEqual(404, client.get('/api/xtream/epg/external/station?guide_id=xmltv:1:none').status_code)

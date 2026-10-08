import base64
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from fruit_build_lanes import create_lanes, ensure_lane_schema
from server.app import create_app
from server.services import channels_lineup
from server.services.xtream_persistent import ChannelNumberConflict, create_channel, get_channel, render_m3u, render_xmltv, update_channel
from xtream_accounts import load_accounts
from xtream_epg import _provider_xmltv_one, api_programme, refresh_epg, xml_time
from xtream_ingest import ensure_schema as ingest_schema
from xtream_ingest import XtreamError, run
from xtream_pool_schema import ensure_schema
from tests.test_xtream_pool import pool_environment
from tests.xtream_test_helpers import HealthyAccountClient
from xtream_pool import XtreamPool


class EpgLineupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fruit.db'
        self.env = {**pool_environment(), "FRUIT_DB_PATH": str(self.path), "SERVER_URL": "http://fruit.example:6655"}
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.accounts = load_accounts(self.conn)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.start, self.stop = self.now + timedelta(hours=1), self.now + timedelta(hours=2)
        self.channel = create_channel(self.conn, {"stream_id": "437219", "name": "ESPN & Friends", "epg_channel_id": "ESPN.us"}, category_id="10", category_name="Sports", channel_number="9000")
        self.client = Mock()
        self.client.config = self.accounts[0].config
        self.client.request_gate = None

    def response(self, xml):
        response = Mock()
        response.raw = io.BytesIO(xml.encode())
        self.client.session.get.return_value = response
        return response

    def programme(self, guide="ESPN.us", title="Sports &amp; News"):
        return f'<programme channel="{guide}" start="{self.start.strftime("%Y%m%d%H%M%S +0000")}" stop="{self.stop.strftime("%Y%m%d%H%M%S +0000")}"><title>{title}</title><sub-title>Live &amp; local</sub-title><desc>A &lt; B</desc><category>Sports</category><icon src="https://img.example/show.png"/><episode-num system="xmltv_ns">1.2.</episode-num><new/></programme>'

    def refresh_xml(self, **kw):
        self.response('<tv><channel id="ESPN.us"><display-name>Sports</display-name></channel>' + self.programme(**kw) + self.programme(guide="OTHER.us", title="Wrong station") + '</tv>')
        return refresh_epg(self.conn, self.client, self.accounts)

    def test_xmltv_skips_account_occupied_by_stream(self):
        pool = XtreamPool(self.path, self.env, client_factory=HealthyAccountClient)
        pool.check_accounts()
        lease = pool.acquire("437219", "recording")
        self.client.request_gate = pool.gate
        try:
            with self.assertRaisesRegex(XtreamError, "account is occupied"):
                _provider_xmltv_one(self.client, {"ESPN.us"}, lease.account.config)
            self.client.session.get.assert_not_called()
        finally:
            lease.release()

    def test_xmltv_retries_alternate_host_before_another_account(self):
        config = replace(self.client.config, fallback_server_url="http://alternate.example")
        self.client.config = config
        self.client.session.get.side_effect = OSError("primary unavailable")
        fallback = Mock()
        response = Mock()
        response.raw = io.BytesIO(("<tv>" + self.programme() + "</tv>").encode())
        fallback.get.return_value = response
        with patch("xtream_epg.requests.Session", return_value=fallback):
            result = _provider_xmltv_one(self.client, {"ESPN.us"}, config)
        self.assertEqual(1, len(result["ESPN.us"]))
        self.assertEqual("http://alternate.example/xmltv.php", fallback.get.call_args.args[0])
        fallback.close.assert_called_once()

    def test_full_provider_metadata_explicit_identity_and_escaping(self):
        result = self.refresh_xml()
        self.assertEqual(1, result['programmes'])
        self.client.get_epg.assert_not_called()
        xml = render_xmltv(self.conn)
        tree = ET.fromstring(xml)
        programme = tree.find('programme')
        self.assertEqual('ESPN.us', tree.find('channel').get('id'))
        self.assertEqual('ESPN.us', programme.get('channel'))
        self.assertEqual('Sports & News', programme.findtext('title'))
        self.assertEqual('Live & local', programme.findtext('sub-title'))
        self.assertEqual('A < B', programme.findtext('desc'))
        self.assertEqual('1.2.', programme.findtext('episode-num'))
        self.assertIsNotNone(programme.find('new'))
        self.assertNotIn(b'Wrong station', xml)
        self.assertIn(b'&amp;', xml)
        self.assertEqual(self.start, xml_time(programme.get('start')))
        self.assertTrue(self.client.session.get.call_args.kwargs['stream'])
        self.client.session.get.return_value.close.assert_called_once()

    def test_xmltv_guide_link_index_suggests_without_assigning(self):
        self.refresh_xml()
        extra = create_channel(self.conn, {"stream_id": "88", "name": "Sports HD"},
                               category_id="10", category_name="Sports", channel_number="9001")
        client = create_app().test_client()
        with patch('server.routes.api.xtream._configured_client',
                   side_effect=AssertionError('Index reads must not contact provider')):
            links = client.get('/api/xtream/epg/links')
            matches = client.get('/api/xtream/epg/links/suggestions')
        self.assertEqual(200, links.status_code)
        self.assertEqual(1, links.get_json()['cache']['channel_count'])
        self.assertEqual(["Sports"], links.get_json()['links'][0]['display_names'])
        self.assertEqual(1, links.get_json()['links'][0]['programme_count'])
        self.assertEqual(200, matches.status_code)
        proposed = next(item for item in matches.get_json()['channels']
                        if item['persistent_id'] == extra['id'])
        self.assertEqual('ESPN.us', proposed['candidates'][0]['guide_id'])
        self.assertIsNone(get_channel(self.conn, extra['id'])['epg_channel_id'])
        self.assertEqual([], self.conn.execute('SELECT * FROM xtream_epg_programmes WHERE persistent_id=?',
                                               (extra['id'],)).fetchall())
        update_channel(self.conn, extra['id'], {'guide_id': 'ESPN.us'})
        self.refresh_xml()
        self.assertEqual('ESPN.us', get_channel(self.conn, extra['id'])['effective_guide_id'])
        self.assertEqual(1, self.conn.execute('SELECT COUNT(*) FROM xtream_epg_programmes WHERE persistent_id=?',
                                              (extra['id'],)).fetchone()[0])

    def test_explicit_xmltv_link_refresh_keeps_snapshot_on_feed_failure(self):
        client = create_app().test_client()
        self.response('<tv><channel id="ESPN.us"><display-name>Sports</display-name></channel>'
                      + self.programme() + '</tv>')
        with patch('server.routes.api.xtream._configured_client',
                   return_value=(self.client.config, self.client)):
            refreshed = client.post('/api/xtream/epg/links/refresh')
        self.assertEqual(200, refreshed.status_code, refreshed.get_data(as_text=True))
        before = client.get('/api/xtream/epg/links').get_json()
        self.assertEqual(1, before['links'][0]['programme_count'])
        self.client.session.get.side_effect = OSError('provider unavailable')
        with patch('server.routes.api.xtream._configured_client',
                   return_value=(self.client.config, self.client)):
            failed = client.post('/api/xtream/epg/links/refresh')
        self.assertEqual(502, failed.status_code)
        self.assertEqual(before, client.get('/api/xtream/epg/links').get_json())

    def test_xmltv_link_index_redacts_account_credentials_in_names(self):
        from xml.sax.saxutils import escape
        secret = self.accounts[0].config.password
        self.response('<tv><channel id="ESPN.us"><display-name>'
                      + escape('Sports ' + secret) + '</display-name></channel>'
                      + self.programme() + '</tv>')
        refresh_epg(self.conn, self.client, self.accounts)
        stored = self.conn.execute('SELECT display_names_json FROM xtream_epg_link_index WHERE guide_id=?',
                                   ('ESPN.us',)).fetchone()[0]
        self.assertNotIn(secret, stored)
        self.assertIn('[REDACTED]', stored)

    def test_xmltv_failure_uses_next_account_for_programmes(self):
        self.client.metadata_configs = tuple(account.config for account in self.accounts[:2])
        self.client.session.get.side_effect = OSError("private-password/0")
        fallback = Mock()
        response = Mock()
        response.raw = io.BytesIO(('<tv>' + self.programme() + '</tv>').encode())
        fallback.get.return_value = response
        with patch('xtream_epg.requests.Session', return_value=fallback):
            result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(1, result['programmes'])
        self.assertEqual('Sports & News', ET.fromstring(render_xmltv(self.conn)).findtext('programme/title'))
        self.assertEqual(self.accounts[1].config.username, fallback.get.call_args.kwargs['params']['username'])
        response.close.assert_called_once()
        fallback.close.assert_called_once()

    def test_guide_override_keeps_original_epg_source_id(self):
        update_channel(self.conn, self.channel['id'], {'guide_id': 'custom.espn'})
        self.refresh_xml()
        tree = ET.fromstring(render_xmltv(self.conn))
        self.assertEqual('custom.espn', tree.find('programme').get('channel'))
        self.assertIn('tvg-id="custom.espn"', render_m3u(self.conn, 'http://fruit'))
        self.assertEqual('ESPN.us', get_channel(self.conn, self.channel['id'])['epg_channel_id'])

    def test_setup_epg_search_uses_both_saved_caches_without_provider_calls(self):
        from server.services.xtream_channel_cache import replace_snapshot
        from server.services.xtream_epg_index import replace_snapshot as replace_links
        replace_snapshot(self.conn, [('10', 'Sports')], [
            ('10', '88', 'US: ESPN HD', 'us: espn hd', None, 'ESPN.us', 'ts'),
            ('10', '89', 'US: ESPN UHD', 'us: espn uhd', None, 'ESPN.us', 'ts'),
            ('10', '90', 'US: NBC HD', 'us: nbc hd', None, 'NBC.us', 'ts'),
        ])
        client = create_app().test_client()
        with patch('server.routes.api.xtream._configured_client',
                   side_effect=AssertionError('Searching EPG links must not call provider')):
            before = client.get('/api/xtream/epg/links/search?q=ESPN').get_json()
            self.assertEqual(1, len(before['candidates']))
            self.assertIsNone(before['candidates'][0]['provider_programmes'])
            replace_links(self.conn, {'ESPN.us': {'names': ['ESPN'], 'programme_count': 12}})
            after = client.get('/api/xtream/epg/links/search?q=US:%20ESPN%20HD').get_json()
            self.assertEqual(['ESPN.us'], [row['guide_id'] for row in after['candidates']])
            self.assertEqual(12, after['candidates'][0]['provider_programmes'])
            self.assertEqual(400, client.get('/api/xtream/epg/links/search?q=').status_code)
        self.assertIsNone(get_channel(self.conn, self.channel['id'])['epg_source_id'])

    def test_alternative_epg_source_replaces_broken_native_link_and_clears_old_schedule(self):
        self.refresh_xml()
        original = get_channel(self.conn, self.channel['id'])
        response = create_app().test_client().patch(
            f'/api/xtream/persistent-channels/{self.channel["id"]}',
            json={'epg_source_id': 'ESPN.alternate'})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertEqual(0, self.conn.execute('SELECT COUNT(*) FROM xtream_epg_programmes').fetchone()[0])
        changed = get_channel(self.conn, self.channel['id'])
        self.assertEqual(original['epg_channel_id'], changed['epg_channel_id'])
        self.assertEqual(original['effective_guide_id'], changed['effective_guide_id'])
        self.response('<tv>' + self.programme(guide='ESPN.alternate', title='Alternative schedule') + '</tv>')
        refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual('Alternative schedule', ET.fromstring(render_xmltv(self.conn)).findtext('programme/title'))
        self.client.get_epg.assert_not_called()
        # An unavailable alternative must not silently use the original stream's guide.
        self.response('<tv/>')
        refresh_epg(self.conn, self.client, self.accounts)
        self.client.get_epg.assert_not_called()
        update_channel(self.conn, self.channel['id'], {'epg_source_id': ''})
        self.refresh_xml()
        self.assertEqual('Sports & News', ET.fromstring(render_xmltv(self.conn)).findtext('programme/title'))

    def test_short_station_search_includes_long_names_and_excludes_other_numbers(self):
        from server.services.xtream_epg_index import replace_snapshot, search
        replace_snapshot(self.conn, {
            'WTTG.us': {'names': ['US: FOX 5 LOCAL WASHINGTON DC HD'], 'programme_count': 20},
            'WUTV.us': {'names': ['US: FOX 29'], 'programme_count': 20},
            'Fox.us': {'names': ['FOX'], 'programme_count': 20},
        })
        matches = search(self.conn, 'FOX 5')
        self.assertEqual('WTTG.us', matches[0]['guide_id'])
        self.assertNotIn('WUTV.us', [item['guide_id'] for item in matches])

    def test_missing_guide_id_uses_only_explicit_stream_epg_and_decodes_base64(self):
        channel = create_channel(self.conn, {'stream_id': '88', 'name': 'Local'}, category_id='10', category_name='News', channel_number='11')
        self.response('<tv/>')
        self.client.get_epg.side_effect = lambda sid: [{'title': base64.b64encode(b'Local News & Weather').decode(),
                                                       'start_timestamp': str(self.start.timestamp()), 'stop_timestamp': str(self.stop.timestamp()),
                                                       'channel_id': sid}] if sid == '88' else []
        result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(1, result['programmes'])
        tree = ET.fromstring(render_xmltv(self.conn))
        self.assertEqual(channel['effective_guide_id'], tree.find('programme').get('channel'))
        self.assertEqual('Local News & Weather', tree.findtext('programme/title'))

    def test_malformed_and_missing_epg_do_not_invent_programmes(self):
        self.response('not XML private-password/0')
        self.client.get_epg.return_value = [{"title": "Wrong identity", "channel_id": "other", "start": self.start.isoformat(), "end": self.stop.isoformat()},
                                           {"title": "Bad times", "start": "never", "end": "tomorrow"}]
        result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(0, result['programmes'])
        self.assertEqual(1, result['failed'])
        self.assertIsNone(ET.fromstring(render_xmltv(self.conn)).find('programme'))
        self.client.get_epg.return_value = []
        self.assertEqual(0, refresh_epg(self.conn, self.client, self.accounts)['failed'])

    def test_xmltv_without_usable_title_falls_back_to_valid_stream_epg(self):
        self.response('<tv>' + self.programme().replace('<title>Sports &amp; News</title>', '') + '</tv>')
        self.client.get_epg.return_value = [{'title':'Fallback News', 'channel_id':'ESPN.us',
                                            'start':self.start.isoformat(), 'end':self.stop.isoformat()}]
        result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(1, result['programmes'])
        self.assertEqual('Fallback News', ET.fromstring(render_xmltv(self.conn)).findtext('programme/title'))

    def test_provider_outage_retains_unexpired_cache_but_stream_change_does_not_reuse_wrong_guide(self):
        self.refresh_xml()
        self.client.session.get.side_effect = OSError('private-password/0')
        self.client.get_epg.side_effect = OSError('private-user-0')
        self.assertEqual(1, refresh_epg(self.conn, self.client, self.accounts)['failed'])
        self.assertIsNotNone(ET.fromstring(render_xmltv(self.conn)).find('programme'))
        self.conn.execute('UPDATE xtream_persistent_channels SET stream_id=?', ('changed',))
        self.conn.commit()
        self.assertIsNone(ET.fromstring(render_xmltv(self.conn)).find('programme'))

    def test_timezone_and_dst_conversion(self):
        p = api_programme({'title':'Morning', 'start':'2026-03-08 03:30:00', 'end':'2026-03-08 04:30:00'}, self.channel, 'America/New_York')
        self.assertEqual('20260308073000 +0000', p.get('start'))
        p = api_programme({'title':'Fall', 'start':'2026-11-01T01:30:00-04:00', 'end':'2026-11-01T01:30:00-05:00'}, self.channel, 'UTC')
        self.assertEqual('20261101053000 +0000', p.get('start'))
        self.assertEqual('20261101063000 +0000', p.get('stop'))

    def add_lane(self):
        ingest_schema(self.conn)
        ensure_lane_schema(self.conn)
        create_lanes(self.conn, 2)
        self.conn.execute("INSERT INTO events(id,title,synopsis,hero_image_url) VALUES('game','Real game','Provider event','https://img.example/game.png')")
        self.conn.execute("INSERT INTO lane_events(lane_id,event_id,is_placeholder,start_utc,end_utc,chosen_provider) VALUES(1,'game',0,?,?,'xtream')", (self.start.isoformat(), self.stop.isoformat()))
        self.conn.commit()

    def test_unified_lineup_has_stable_numbers_matching_guide_and_local_urls(self):
        self.refresh_xml()
        self.add_lane()
        m3u = channels_lineup.m3u(self.conn, 'http://fruit.example:6655')
        xml = channels_lineup.xmltv(self.conn)
        numbers = re.findall(r'channel-number="([^"]+)"', m3u)
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual('9000', numbers[0])
        self.assertNotEqual('9000', numbers[1])
        self.assertEqual('9001', numbers[2])
        ids = re.findall(r'tvg-id="([^"]+)"', m3u)
        tree = ET.fromstring(xml)
        self.assertEqual(set(ids), {e.get('id') for e in tree.findall('channel')})
        self.assertEqual({'ESPN.us', 'lane.1'}, {e.get('channel') for e in tree.findall('programme')})
        self.assertEqual(3, len(set(re.findall(r'channel-id="([^"]+)"', m3u))))
        self.assertEqual(m3u, channels_lineup.m3u(self.conn, 'http://fruit.example:6655'))
        update_channel(self.conn, self.channel['id'], {'enabled': False})
        changed = channels_lineup.m3u(self.conn, 'http://fruit.example:6655')
        self.assertEqual(numbers[1:], re.findall(r'channel-number="([^"]+)"', changed))
        self.assertTrue(all(line.startswith('http://fruit.example:6655/') for line in m3u.splitlines() if line and not line.startswith('#')))
        self.assertNotIn('provider.example', m3u)
        self.assertEqual('channel', tree[0].tag)
        self.assertEqual('programme', tree[-1].tag)

    def test_persistent_guide_id_collision_with_lane_is_remapped_in_both_outputs(self):
        update_channel(self.conn, self.channel['id'], {'guide_id': 'lane.1'})
        self.refresh_xml()
        self.add_lane()
        m3u = channels_lineup.m3u(self.conn, 'http://fruit')
        tree = ET.fromstring(channels_lineup.xmltv(self.conn))
        expected_id = re.findall(r'tvg-id="([^"]+)"', m3u)[0]
        self.assertTrue(expected_id.startswith('xtream.guide.'))
        self.assertEqual(3, len({e.get('id') for e in tree.findall('channel')}))
        self.assertEqual(expected_id, next(e for e in tree.findall('programme') if e.findtext('title') == 'Sports & News').get('channel'))

    def test_shared_reserved_guide_id_is_stable_before_lane_exists_and_keeps_other_channel_epg(self):
        other = create_channel(self.conn, {'stream_id':'99', 'name':'Another feed', 'epg_channel_id':'ESPN.us'},
                               category_id='10', category_name='Sports', channel_number='12', guide_id='lane.1')
        update_channel(self.conn, self.channel['id'], {'guide_id':'lane.1'})
        self.refresh_xml()
        self.conn.execute('DELETE FROM xtream_epg_programmes WHERE persistent_id=?', (self.channel['id'],))
        self.conn.commit()
        before = channels_lineup.m3u(self.conn, 'http://fruit')
        ids = re.findall(r'tvg-id="([^"]+)"', before)
        self.assertEqual(1, len(set(ids)))
        self.assertTrue(ids[0].startswith('xtream.guide.'))
        tree = ET.fromstring(channels_lineup.xmltv(self.conn))
        self.assertEqual(1, len(tree.findall('channel')))
        self.assertEqual(ids[0], tree.find('programme').get('channel'))
        self.add_lane()
        self.assertEqual(ids, re.findall(r'tvg-id="([^"]+)"', channels_lineup.m3u(self.conn, 'http://fruit'))[:2])

    def test_persistent_creation_cannot_take_a_saved_lane_number(self):
        self.add_lane()
        numbers = re.findall(r'channel-number="([^"]+)"', channels_lineup.m3u(self.conn, 'http://fruit'))
        with self.assertRaises(ChannelNumberConflict):
            create_channel(self.conn, {'stream_id':'99','name':'New'}, category_id='10', category_name='Sports', channel_number=numbers[1])

    def test_static_only_and_failed_dynamic_refresh_preserve_persistent_channel_and_refresh_epg(self):
        for selected in ('', '20'):
            with self.subTest(selected=selected):
                metadata = Mock()
                metadata.get_live_streams.return_value = [{'stream_id':'437219', 'name':'ESPN & Friends', 'epg_channel_id':'ESPN.us'}]
                # Three account checks followed by one catalogue/guide client.
                clients = [HealthyAccountClient(a.config) for a in self.accounts] + [metadata]
                with patch('xtream_ingest.fetch_snapshot', side_effect=XtreamError('category failed')), \
                     patch('xtream_epg.refresh_epg', return_value={'programmes':1,'failed':0}) as refresh:
                    result = run(self.path, {**self.env,'XTREAM_CATEGORY_IDS':selected}, client_factory=Mock(side_effect=clients))
                self.assertEqual('available', get_channel(self.conn, self.channel['id'])['availability_status'])
                self.assertEqual(1, result['persistent_epg']['programmes'])
                metadata.get_live_streams.assert_called_once_with('10')
                metadata.session.close.assert_called_once()
                refresh.assert_called_once()
                self.assertEqual(3, len(metadata.metadata_configs))
                if selected:
                    self.assertIn('dynamic_error', result)

    def test_dynamic_failure_log_shows_cause_without_credentials(self):
        metadata = Mock()
        metadata.get_live_streams.return_value = [{'stream_id':'437219', 'name':'ESPN & Friends'}]
        clients = [HealthyAccountClient(a.config) for a in self.accounts] + [metadata]
        output = io.StringIO()
        with patch('xtream_ingest.fetch_snapshot', side_effect=XtreamError('HTTP 403 private-password/0')), \
             patch('xtream_epg.refresh_epg', return_value={'programmes':0,'failed':0}), \
             redirect_stdout(output):
            result = run(self.path, {**self.env,'XTREAM_CATEGORY_IDS':'20'}, client_factory=Mock(side_effect=clients))
        self.assertIn('HTTP 403 [REDACTED]', output.getvalue())
        self.assertNotIn('private-password/0', output.getvalue())
        self.assertEqual('HTTP 403 [REDACTED]', result['dynamic_error'])

    def test_secrets_in_provider_metadata_do_not_escape_exports_or_status(self):
        self.refresh_xml(title='private-user-0 private-password/0')
        self.conn.execute('UPDATE xtream_persistent_channels SET icon=?', ('http://provider.example/live/private-user-0/private-password%2F0/icon',))
        self.conn.commit()
        output = render_m3u(self.conn, 'http://fruit') + render_xmltv(self.conn).decode()
        for secret in ('private-user-0', 'private-password/0', 'private-password%2F0'):
            self.assertNotIn(secret, output)

    def test_existing_data_survives_repeated_migrations_and_exports_work_without_lanes(self):
        before = self.conn.execute('SELECT channel_number,guide_id,display_name FROM xtream_persistent_channels').fetchall()
        ensure_schema(self.conn)
        ensure_schema(self.conn)
        self.assertEqual(before, self.conn.execute('SELECT channel_number,guide_id,display_name FROM xtream_persistent_channels').fetchall())
        client = create_app().test_client()
        self.assertEqual(200, client.get('/m3u/channels').status_code)
        self.assertEqual(200, client.get('/xmltv/channels').status_code)
        self.assertIn('ESPN &amp; Friends', client.get('/xmltv/channels').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

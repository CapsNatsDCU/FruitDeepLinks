"""Multi-feed isolation, migration, explicit management, and guide preference."""
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone
from tests import test_external_xmltv as fixtures
from server.services import external_xmltv as xmltv, xtream_epg_index as index
from server.services.xtream_persistent import create_channel, update_channel, get_channel
from server.app import create_app
from xtream_epg import cached_programmes


class MultipleSourcesTests(unittest.TestCase):
    setUp = fixtures.ExternalXmltvTests.setUp
    programme = fixtures.ExternalXmltvTests.programme

    def source(self, name='Cable', url='http://guide.example/cable.xml'):
        return xmltv.save_source(self.conn, name, url)['id']

    def refresh(self, source, title='Cable schedule', name='ESPN', raw='ESPN.us'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [('<tv><channel id="' + raw + '"><display-name>' + name + '</display-name></channel>' + self.programme(guide=raw,title=title) + '</tv>').encode()]
        with patch('server.services.external_xmltv.requests.get', return_value=response):
            return xmltv.refresh(self.conn, source_id=source)

    def test_same_station_ids_are_isolated_in_preview_assignment_and_export(self):
        one = self.source()
        two = self.source('Satellite','http://guide.example/satellite.xml')
        self.refresh(one)
        self.refresh(two, 'Satellite schedule')
        ids = {s['guide_id'] for s in xmltv.entries(self.conn)}
        self.assertEqual({'xmltv:1:ESPN.us','xmltv:2:ESPN.us'}, ids)
        self.assertEqual('Cable schedule', xmltv.station_preview(self.conn,'xmltv:1:ESPN.us')['programmes'][0]['title'])
        self.assertEqual('Satellite schedule', xmltv.station_preview(self.conn,'xmltv:2:ESPN.us')['programmes'][0]['title'])
        update_channel(self.conn,self.channel['id'],{'epg_source_id':'xmltv:2:ESPN.us'})
        xmltv.apply_selected(self.conn)
        channel = get_channel(self.conn,self.channel['id'])
        programme = cached_programmes(self.conn,channel)[0]
        self.assertEqual('Satellite schedule', programme.findtext('title'))
        self.assertEqual(self.channel['effective_guide_id'],programme.get('channel'))
        filtered = xmltv.browse(self.conn,source_id=two)
        self.assertEqual(1,filtered['total'])
        self.assertEqual('Satellite',filtered['stations'][0]['source_name'])
        self.assertEqual(self.channel['id'],filtered['stations'][0]['assigned_channels'][0]['id'])
        self.assertEqual(2,index.status(self.conn)['channel_count'])

    def test_failed_refresh_preserves_both_sources_and_retry_is_independent(self):
        one,two = self.source(),self.source('Satellite','http://guide.example/satellite.xml')
        self.refresh(one); self.refresh(two,'Satellite schedule')
        response = MagicMock(); response.__enter__.return_value = response
        response.iter_content.return_value = [b'<html>bad</html>']
        with patch('server.services.external_xmltv.requests.get',return_value=response):
            with self.assertRaises(ValueError): xmltv.refresh(self.conn,source_id=two)
        self.assertIsNone(xmltv.status(self.conn,one)['last_error'])
        self.assertTrue(xmltv.status(self.conn,two)['last_error'])
        self.assertEqual('Satellite schedule',xmltv.programmes(self.conn,'xmltv:2:ESPN.us')[0].findtext('title'))
        self.refresh(two,'Updated satellite')
        self.assertIsNone(xmltv.status(self.conn,two)['last_error'])
        self.assertEqual('Cable schedule',xmltv.programmes(self.conn,'xmltv:1:ESPN.us')[0].findtext('title'))

    def test_legacy_snapshot_and_assignments_migrate_once_without_changing_exports(self):
        # Reproduce the deployed single-source schema, including a raw ID with a colon.
        self.conn.executescript('''CREATE TABLE external_xmltv_state(id INTEGER PRIMARY KEY CHECK(id=1),url TEXT NOT NULL,refreshed_at TEXT NOT NULL,checked_at TEXT NOT NULL,channel_count INTEGER NOT NULL,programme_count INTEGER NOT NULL,last_error TEXT);
            CREATE TABLE external_xmltv_channels(guide_id TEXT PRIMARY KEY,names_json TEXT NOT NULL,programme_count INTEGER NOT NULL);
            CREATE TABLE external_xmltv_programmes(guide_id TEXT NOT NULL,start_utc TEXT NOT NULL,stop_utc TEXT NOT NULL,programme_xml TEXT NOT NULL,PRIMARY KEY(guide_id,start_utc,stop_utc));''')
        stamp = self.now.isoformat()
        self.conn.execute('INSERT INTO external_xmltv_state VALUES(1,?,?,?,?,?,NULL)',('http://guide.example/old.xml',stamp,stamp,1,1))
        self.conn.execute('INSERT INTO external_xmltv_channels VALUES(?,?,?)',('2:station','["Legacy station"]',1))
        self.conn.execute('INSERT INTO external_xmltv_programmes VALUES(?,?,?,?)',('2:station',self.start.strftime('%Y%m%d%H%M%S +0000'),self.stop.strftime('%Y%m%d%H%M%S +0000'),self.programme(guide='2:station')))
        self.conn.commit()
        update_channel(self.conn,self.channel['id'],{'epg_source_id':'xmltv:2:station'})
        xmltv.ensure_schema(self.conn); xmltv.ensure_schema(self.conn)
        channel = get_channel(self.conn,self.channel['id'])
        self.assertEqual('xmltv:1:2:station',channel['epg_source_id'])
        self.assertEqual(self.channel['effective_guide_id'],channel['effective_guide_id'])
        self.assertEqual(1,len(xmltv.sources(self.conn)))
        self.assertEqual(1,xmltv.apply_selected(self.conn))
        self.assertEqual('Sports & News',cached_programmes(self.conn,channel)[0].findtext('title'))
        self.assertEqual(2,self.source())

    def test_management_is_offline_and_assigned_source_cannot_be_removed_or_replaced(self):
        one=self.source(); self.refresh(one)
        update_channel(self.conn,self.channel['id'],{'enabled':False,'epg_source_id':'xmltv:1:ESPN.us'})
        client=create_app().test_client()
        with patch('requests.sessions.Session.request',side_effect=AssertionError('Offline management')):
            added=client.post('/api/xtream/epg/external/sources',json={'name':'Second','url':'http://guide.example/second.xml'})
            self.assertEqual(201,added.status_code)
            self.assertEqual(400,client.delete('/api/xtream/epg/external/sources/1').status_code)
            self.assertEqual(400,client.patch('/api/xtream/epg/external/sources/1',json={'name':'Rename','url':'http://different.example/new.xml'}).status_code)
            self.assertEqual(200,client.patch('/api/xtream/epg/external/sources/1',json={'name':'Renamed','url':'http://guide.example/cable.xml'}).status_code)
            self.assertEqual(200,client.delete('/api/xtream/epg/external/sources/2').status_code)
            third=client.post('/api/xtream/epg/external/sources',json={'name':'Third','url':'http://guide.example/third.xml'}).get_json()['source']
            self.assertEqual(3,third['id'])  # Deleted identities must never be recycled.
            self.assertEqual(404,client.get('/api/xtream/epg/external/stations?source_id=999').status_code)
            self.assertEqual('Renamed',client.get('/api/xtream/epg/external/stations?source_id=1').get_json()['stations'][0]['source_name'])
        self.assertEqual('xmltv:1:ESPN.us',get_channel(self.conn,self.channel['id'])['epg_source_id'])

    def test_source_refresh_applies_only_its_selected_channels(self):
        one,two = self.source(),self.source('Satellite','http://guide.example/satellite.xml')
        self.refresh(one);self.refresh(two)
        update_channel(self.conn,self.channel['id'],{'epg_source_id':'xmltv:1:ESPN.us'})
        other=create_channel(self.conn,{'stream_id':'88','name':'Other'},category_id='10',category_name='Sports',channel_number='9001',epg_source_id='xmltv:2:ESPN.us')
        xmltv.apply_selected(self.conn)
        before=[tuple(r) for r in self.conn.execute('SELECT * FROM xtream_epg_programmes WHERE persistent_id=?',(self.channel['id'],))]
        self.refresh(two,'Updated satellite')
        self.assertEqual(1,xmltv.apply_selected(self.conn,source_id=two))
        after=[tuple(r) for r in self.conn.execute('SELECT * FROM xtream_epg_programmes WHERE persistent_id=?',(self.channel['id'],))]
        self.assertEqual(before,after)
        self.assertEqual('Updated satellite',cached_programmes(self.conn,other)[0].findtext('title'))

    def test_regular_refresh_checks_each_due_source_and_continues_after_failure(self):
        one,two = self.source(),self.source('Satellite','http://guide.example/satellite.xml')
        self.refresh(one);self.refresh(two)
        old=(datetime.now(timezone.utc)-timedelta(hours=7)).isoformat()
        self.conn.execute('UPDATE external_xmltv_state SET checked_at=?',(old,)); self.conn.commit()
        with patch.object(xmltv,'refresh',side_effect=[ValueError('bad feed'),{}]) as refresh:
            xmltv.refresh_if_due(self.conn)
        self.assertEqual([one,two],[call.kwargs['source_id'] for call in refresh.call_args_list])

    def test_search_filters_provider_imports_and_named_sources_before_pagination(self):
        one,two=self.source(),self.source('Satellite','http://guide.example/satellite.xml')
        self.refresh(one);self.refresh(two)
        index.replace_snapshot(self.conn,{f'provider{i}':{'names':[f'ESPN guide {i:03}'],'programme_count':5} for i in range(60)})
        from server.services.xtream_channel_cache import replace_snapshot
        replace_snapshot(self.conn,[('10','Sports')],[('10','123','ESPN cache alias','espn cache alias',None,'cached-guide','ts')])
        client=create_app().test_client()
        with patch('requests.sessions.Session.request',side_effect=AssertionError('Filters must stay offline')):
            responses={source:client.get('/api/xtream/epg/links/search?mode=all&source='+source).get_json() for source in ('all','provider','external','xmltv:1','xmltv:2')}
            page=client.get('/api/xtream/epg/links/search?mode=all&source=provider&offset=50').get_json()
            for invalid in ('missing','xmltv:0','xmltv:999','xmltv:-1','xmltv:abc'):
                self.assertEqual(400,client.get('/api/xtream/epg/links/search?mode=all&source='+invalid).status_code)
        self.assertEqual(63,responses['all']['total'])
        self.assertEqual(61,responses['provider']['total'])
        self.assertEqual(11,len(page['candidates']))
        self.assertEqual(2,responses['external']['total'])
        self.assertEqual(['xmltv:1:ESPN.us'],[c['guide_id'] for c in responses['xmltv:1']['candidates']])
        self.assertEqual(['xmltv:2:ESPN.us'],[c['guide_id'] for c in responses['xmltv:2']['candidates']])
        self.assertFalse(any(c['guide_id'].startswith('xmltv:') for c in responses['provider']['candidates']+page['candidates']))
        self.assertIsNone(get_channel(self.conn,self.channel['id'])['epg_source_id'])

    def test_external_preference_reorders_only_matching_candidates_and_preserves_selection(self):
        one=self.source();self.refresh(one,name='ESPN Sports')
        index.replace_snapshot(self.conn,{'provider':{'names':['ESPN'],'programme_count':5}})
        client=create_app().test_client()
        with patch('requests.sessions.Session.request',side_effect=AssertionError('Offline suggestion')):
            normal=client.get('/api/xtream/epg/links/search?q=ESPN').get_json()['candidates']
            preferred=client.get('/api/xtream/epg/links/search?q=ESPN&prefer_external=true').get_json()['candidates']
        self.assertEqual('provider',normal[0]['guide_id'])
        self.assertEqual('xmltv:1:ESPN.us',preferred[0]['guide_id'])
        self.assertEqual({c['guide_id'] for c in normal},{c['guide_id'] for c in preferred})
        self.assertIsNone(get_channel(self.conn,self.channel['id'])['epg_source_id'])

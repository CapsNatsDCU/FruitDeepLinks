import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from tests import test_xtream_epg_lineup as epg_fixture
from server.app import create_app
from server.services import team_channel_epg as team_epg
from server.services.xtream_persistent import PersistentChannelError, get_channel, render_xmltv, update_channel
from sports_schedule_audit import ensure_schema
from xtream_epg import cached_programmes, refresh_epg


class TeamChannelGuideTests(unittest.TestCase):
    setUp = epg_fixture.EpgLineupTests.setUp

    def seed(self, *, start=None, stop=None, status='scheduled', league='nhl', teams=None, event_id='game1'):
        ensure_schema(self.conn)
        teams = teams or ['Washington Capitals', 'Pittsburgh Penguins']
        self.conn.execute('''INSERT OR REPLACE INTO sports_schedule_reference_events
            (source,league_key,source_event_id,title,start_utc,end_utc,participants_json,event_status,first_seen_utc,last_seen_utc)
            VALUES ('fixture',?,?,?,?,?,?,?,?,?)''',
            (league, event_id, ' vs. '.join(teams), (start or self.start).isoformat(),
             (stop.isoformat() if stop else None), json.dumps(teams), status, self.now.isoformat(), self.now.isoformat()))
        self.conn.execute('''INSERT OR REPLACE INTO sports_schedule_league_state
            (league_key,source,status,last_success_utc) VALUES (?,'fixture','ok',?)''', (league, self.now.isoformat()))
        self.conn.commit()

    def select(self, **changes):
        return update_channel(self.conn, self.channel['id'], {'guide_mode':'team',
            'team_schedule_key':'nhl|washington capitals', **changes})

    def test_padded_matchup_continuous_gaps_and_export_identity(self):
        self.seed(stop=self.stop)
        channel = self.select(team_pre_minutes=15, team_post_minutes=20)
        result = team_epg.guide(self.conn, channel, now=self.now)
        programmes = result['programmes']
        game = next(p for p in programmes if p.findtext('category') == 'Sports')
        self.assertEqual(game.findtext('title'), 'Washington Capitals vs. Pittsburgh Penguins')
        self.assertEqual(game.get('start'), (self.start-timedelta(minutes=15)).strftime('%Y%m%d%H%M%S +0000'))
        self.assertEqual(game.get('stop'), (self.stop+timedelta(minutes=20)).strftime('%Y%m%d%H%M%S +0000'))
        self.assertTrue(any(p.findtext('title') == 'No game scheduled' for p in programmes))
        self.assertTrue(all(a.get('stop') == b.get('start') for a,b in zip(programmes,programmes[1:])))
        tree = ET.fromstring(render_xmltv(self.conn))
        self.assertTrue(all(p.get('channel') == channel['effective_guide_id'] for p in tree.findall('programme')))
        self.assertIn(game.findtext('title'), [p.findtext('title') for p in tree.findall('programme')])

    def test_estimated_duration_cancelled_and_overlap(self):
        self.seed()
        self.seed(start=self.start+timedelta(hours=2), event_id='game2', teams=['Washington Capitals','New York Rangers'])
        self.seed(start=self.start+timedelta(days=1), status='postponed', event_id='game3')
        result = team_epg.guide(self.conn,self.select(team_duration_minutes=120),now=self.now)
        games = [p for p in result['programmes'] if p.findtext('category') == 'Sports']
        self.assertEqual(len(games),1)
        self.assertIn('New York Rangers', games[0].findtext('title'))
        self.assertIn('estimated',games[0].findtext('desc'))
        self.assertEqual(games[0].get('stop'),(self.start+timedelta(hours=4,minutes=30)).strftime('%Y%m%d%H%M%S +0000'))

    def test_exact_team_and_league_scope(self):
        self.seed(teams=['Washington Capitals Junior','Penguins'])
        self.seed(league='nba',teams=['Washington Capitals','Opponent'],event_id='otherleague')
        # The saved roster supplies the requested identity even with no upcoming game.
        self.seed(start=self.now-timedelta(days=10), event_id='old')
        result=team_epg.guide(self.conn,self.select(),now=self.now)
        self.assertTrue(all(p.findtext('title') == 'No game scheduled' for p in result['programmes']))

    def test_failed_stale_and_missing_schedules_never_claim_no_game(self):
        self.seed()
        channel=self.select()
        for status,last_success in [('failed',self.now),('ok',self.now-timedelta(hours=73))]:
            self.conn.execute('UPDATE sports_schedule_league_state SET status=?,last_success_utc=?',(status,last_success.isoformat()))
            result=team_epg.guide(self.conn,channel,now=self.now)
            self.assertEqual(result['schedule_status'],'unavailable')
            self.assertTrue(all(p.findtext('title') == 'Schedule unavailable' for p in result['programmes']))
        self.conn.execute('DELETE FROM sports_schedule_league_state')
        self.assertEqual(team_epg.guide(self.conn,channel,now=self.now)['schedule_status'],'unavailable')

    def test_config_validation_atomic_and_tuple_connections(self):
        self.seed()
        for values in [{'team_schedule_key':'bad'}, {'team_pre_minutes':-1}, {'team_duration_minutes':True}, {'guide_mode':'bad'}]:
            with self.assertRaises(PersistentChannelError): self.select(**values)
            self.assertEqual(get_channel(self.conn,self.channel['id'])['guide_mode'],'standard')
        self.conn.row_factory=None
        self.assertIn('nhl|washington capitals',[t['key'] for t in team_epg.team_options(self.conn)])
        self.assertEqual(self.select()['guide_mode'],'team')

    def test_switching_back_restores_standard_guide_mapping_and_cache(self):
        self.seed()
        from xtream_epg import ensure_schema as epg_schema
        epg_schema(self.conn)
        self.conn.execute('INSERT INTO xtream_epg_programmes VALUES(?,?,?,?,?,?)',
            (self.channel['id'],self.channel['stream_id'],self.channel['effective_guide_id'],
             self.start.strftime('%Y%m%d%H%M%S +0000'),self.stop.strftime('%Y%m%d%H%M%S +0000'),epg_fixture.EpgLineupTests.programme(self)))
        self.conn.commit()
        self.assertTrue(any(p.findtext('category') == 'Sports' for p in cached_programmes(self.conn,self.select())))
        reverted=update_channel(self.conn,self.channel['id'],{'guide_mode':'standard'})
        self.assertEqual(cached_programmes(self.conn,reverted)[0].findtext('title'),'Sports & News')
        self.assertEqual(reverted['effective_guide_id'],self.channel['effective_guide_id'])

    def test_team_only_refresh_and_preview_make_no_provider_requests(self):
        self.seed()
        self.select()
        app=create_app(); client=app.test_client()
        with patch('server.services.external_xmltv.refresh_if_due',side_effect=AssertionError('network')), \
             patch('xtream_pool.XtreamPool.check_accounts',side_effect=AssertionError('network')), \
             patch('requests.sessions.Session.request',side_effect=AssertionError('network')):
            self.assertGreater(refresh_epg(self.conn,None)['programmes'],0)
            response=client.post('/api/xtream/epg/refresh')
            self.assertEqual(response.status_code,200,response.get_json())
            options=client.get('/api/xtream/persistent-channels/team-schedule/teams')
            self.assertEqual(options.status_code,200)
            status=client.get('/api/xtream/epg/status').get_json()['channels']
            self.assertEqual(status[0]['schedule_status'],'ready')
            preview=client.get('/api/xtream/persistent-channels/team-schedule/preview?team_schedule_key=nhl%7Cwashington+capitals')
            self.assertEqual(preview.status_code,200,preview.get_json())
            self.assertEqual(preview.get_json()['schedule_status'],'ready')
            bad=client.get('/api/xtream/persistent-channels/team-schedule/preview?team_pre_minutes=1.5')
            self.assertEqual(bad.status_code,400)
            export=client.get('/xmltv/persistent')
            self.assertEqual(export.status_code,200)
            self.assertIn('Washington Capitals',export.get_data(as_text=True))

    def catalog_team(self, league, name, aliases=()):
        from sports_metadata import ensure_schema as metadata_schema, _upsert_named
        metadata_schema(self.conn)
        league_id = _upsert_named(self.conn, 'leagues', league)
        team_id = _upsert_named(self.conn, 'teams', name, league_id=league_id, aliases=aliases)
        self.conn.commit()
        return team_id

    def test_catalog_teams_remain_selectable_when_only_ufl_downloaded(self):
        self.seed(league='ufl', teams=['DC Defenders', 'Birmingham Stallions'])
        self.catalog_team('NHL', 'Washington Capitals', aliases=['Caps'])
        self.catalog_team('National Football League', 'Washington Commanders')
        self.catalog_team('NBA', 'Washington Wizards')
        self.catalog_team('Premier League - Lebanon', 'Unrelated')
        options = team_epg.team_options(self.conn)
        self.assertTrue({'NHL','NFL','NBA','UFL'}.issubset({t['league'] for t in options}))
        self.assertFalse(any(t['team'] == 'Unrelated' for t in options))
        config = team_epg.validate_config(self.conn, {'guide_mode':'team','team_schedule_key':'nhl|washington capitals'})
        result = team_epg.guide(self.conn, config, now=self.now)
        self.assertEqual(result['schedule_status'], 'unavailable')
        self.assertTrue(all(p.findtext('title') == 'Schedule unavailable' for p in result['programmes']))

    def test_catalog_alias_matches_schedule_but_unmatched_identity_stays_unavailable(self):
        self.catalog_team('NHL','Washington Capitals', aliases=['Caps'])
        self.seed(teams=['Caps','Pittsburgh Penguins'])
        options=team_epg.team_options(self.conn)
        self.assertFalse(any(t['key'] == 'nhl|caps' for t in options))
        generated=team_epg.guide(self.conn,self.select(),now=self.now)
        self.assertEqual(generated['schedule_status'],'ready')
        self.assertTrue(any(p.findtext('category') == 'Sports' for p in generated['programmes']))
        self.catalog_team('NHL','Washington Wizards')
        unknown=team_epg.guide(self.conn,{'team_schedule_key':'nhl|washington wizards'},now=self.now)
        self.assertEqual(unknown['schedule_status'],'unavailable')

    def test_create_team_mode_through_api_and_distinct_export_ids(self):
        self.seed()
        from server.services.xtream_channel_cache import replace_snapshot
        replace_snapshot(self.conn, [('10','Sports')], [
            ('10','new','Penguins channel','penguins channel',None,'ESPN.us','ts')])
        self.select()
        response=create_app().test_client().post('/api/xtream/persistent-channels',json={
            'category_id':'10','stream_id':'new','channel_number':'9001',
            'guide_mode':'team','team_schedule_key':'nhl|pittsburgh penguins',
            'team_pre_minutes':0,'team_post_minutes':0,'team_duration_minutes':150})
        self.assertEqual(response.status_code,201,response.get_json())
        channel=response.get_json()['channel']
        self.assertEqual(channel['team_duration_minutes'],150)
        original=get_channel(self.conn,self.channel['id'])
        self.assertNotEqual(channel['effective_guide_id'],original['effective_guide_id'])
        tree=ET.fromstring(render_xmltv(self.conn))
        self.assertEqual(len(tree.findall('channel')),2)
        self.assertEqual({p.get('channel') for p in tree.findall('programme')},
                         {original['effective_guide_id'],channel['effective_guide_id']})

    def test_mixed_refresh_keeps_provider_channel_and_skips_team_api(self):
        self.seed()
        self.select()
        from server.services.xtream_persistent import create_channel
        standard=create_channel(self.conn,{'stream_id':'other','name':'Other','epg_channel_id':'Other.us'},category_id='10',category_name='Sports',channel_number='9001')
        with patch('xtream_epg.provider_xmltv',return_value={}), patch('server.services.external_xmltv.refresh_if_due'):
            self.client.get_epg.return_value=[]
            result=refresh_epg(self.conn,self.client,self.accounts)
        self.assertEqual(result['channels'],2)
        self.client.get_epg.assert_called_once_with(standard['stream_id'])


if __name__ == '__main__': unittest.main()

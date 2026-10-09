import io
import unittest
from unittest.mock import patch, MagicMock

from tests import test_xtream_epg_lineup as _fixtures
from server.services import external_xmltv, xtream_epg_index
from server.services.xtream_persistent import update_channel
from xtream_epg import cached_programmes, refresh_epg


class ExternalXmltvTests(unittest.TestCase):
    setUp = _fixtures.EpgLineupTests.setUp
    programme = _fixtures.EpgLineupTests.programme
    def import_xml(self, xml):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [xml.encode()]
        with patch('server.services.external_xmltv.requests.get', return_value=response):
            return external_xmltv.refresh(self.conn, 'http://guide.example/guide.xml', self.accounts)

    def feed(self):
        return '<tv><channel id="ESPN.us"><display-name>ESPN external</display-name></channel>' + self.programme(title="External schedule") + '</tv>'

    def test_external_guide_has_separate_identity_and_preserves_export(self):
        self.import_xml(self.feed())
        xtream_epg_index.replace_snapshot(self.conn, {'ESPN.us': {'names':['ESPN provider'], 'programme_count':3}})
        candidates = xtream_epg_index.search(self.conn, 'ESPN')
        self.assertEqual({c['guide_id'] for c in candidates}, {'ESPN.us', 'xmltv:ESPN.us'})
        selected = update_channel(self.conn, self.channel['id'], {'epg_source_id':'xmltv:ESPN.us'})
        self.assertEqual(external_xmltv.apply_selected(self.conn, self.accounts), 1)
        programmes = cached_programmes(self.conn, selected)
        self.assertEqual(programmes[0].get('channel'), self.channel['effective_guide_id'])
        self.assertEqual(programmes[0].findtext('title'), 'External schedule')
        result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(result['programmes'], 1)
        self.client.session.get.assert_not_called()
        self.client.get_epg.assert_not_called()

    def test_malformed_download_retains_snapshot_and_config(self):
        before = self.import_xml(self.feed())
        with self.assertRaisesRegex(ValueError, 'previous snapshot kept'):
            self.import_xml(self.feed()[:-5])
        after = external_xmltv.status(self.conn)
        self.assertEqual(after['refreshed_at'], before['refreshed_at'])
        self.assertEqual(after['programme_count'], 1)
        self.assertTrue(after['last_error'])
        self.assertEqual(len(external_xmltv.programmes(self.conn, 'xmltv:ESPN.us')), 1)

    def test_invalid_html_and_credential_urls_not_imported(self):
        with self.assertRaises(ValueError):
            self.import_xml('<html><title>Login</title></html>')
        self.assertFalse(external_xmltv.status(self.conn))
        for url in ('file:///etc/passwd', 'http://user:secret@host/guide.xml', 'http://host/xml?token=secret'):
            with self.assertRaises(ValueError):
                external_xmltv.validate_url(url)

    def test_oversize_document_cannot_replace_snapshot(self):
        before = self.import_xml(self.feed())
        with patch('server.services.external_xmltv.MAX_BYTES', 10):
            with self.assertRaises(ValueError):
                self.import_xml(self.feed())
        self.assertEqual(external_xmltv.status(self.conn)['refreshed_at'], before['refreshed_at'])

    def test_new_lineup_cannot_replace_selected_numeric_ids(self):
        self.import_xml(self.feed())
        update_channel(self.conn, self.channel['id'], {'epg_source_id':'xmltv:ESPN.us'})
        with self.assertRaisesRegex(ValueError, 'Clear the selected'):
            external_xmltv.refresh(self.conn, 'http://different.example/guide.xml')

    def test_unknown_or_empty_external_link_does_not_fallback_to_provider(self):
        self.import_xml(self.feed())
        selected = update_channel(self.conn, self.channel['id'], {'epg_source_id':'xmltv:missing'})
        result = refresh_epg(self.conn, self.client, self.accounts)
        self.assertEqual(result['failed'], 1)
        self.assertFalse(cached_programmes(self.conn, selected))
        self.client.session.get.assert_not_called()
        self.client.get_epg.assert_not_called()

    def test_import_route_does_not_contact_provider(self):
        from server.app import create_app
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [self.feed().encode()]
        with patch('server.services.external_xmltv.requests.get', return_value=response):
            result = create_app().test_client().post('/api/xtream/epg/external', json={'url':'http://guide.example/guide.xml'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['cache']['channel_count'], 1)


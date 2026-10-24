import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bot

class CollectorTests(unittest.TestCase):
    def test_numeric_values_and_unknowns(self):
        for value,expected in [('4.4K',4400),('458.2K',458200),('1.2M',1200000),('1,200+',1200),('100',100),('No data',None),('',None),('1,2',None)]:self.assertEqual(bot.parse_number(value),expected)
    def test_filters_and_unbounded_deep_pagination(self):
        from urllib.parse import parse_qs,urlparse
        for number in [1,2,19,21,101]:
            query=parse_qs(urlparse(bot.page_url(number)).query)
            self.assertEqual(query,{'page':[str(number)],'page_size':['50'],'playing_min':['100'],'visits_max':['500000']})
        self.assertEqual(bot.select_pages(19,2,0),[1,19]);self.assertEqual(bot.select_pages(21,2,0),[1,21]);self.assertEqual(bot.select_pages(20,3,20),[1,20,2])
    def test_only_real_roblox_links(self):
        self.assertEqual(bot.place_from_url('https://www.roblox.com/games/123/Test'),123)
        for url in ['https://evil.test/games/123','https://www.roblox.com.evil.test/games/123','https://www.roblox.com/search/experiences?keyword=test']:self.assertIsNone(bot.place_from_url(url))
    def test_identity_cache_does_not_prevent_recollection(self):
        candidate={'placeId':456,'active':100,'visits':500000}
        with patch.object(bot,'request_json',return_value={'universeId':123}) as request:
            cache={};self.assertEqual(bot.resolve_candidate(candidate,cache)['universeId'],123)
            self.assertEqual(bot.resolve_candidate(candidate,cache)['universeId'],123);self.assertEqual(request.call_count,1)
    def test_delivery_requests_fresh_oidc_identity_each_time(self):
        with patch.object(bot,'identity_token',return_value='not-a-real-token'),patch.object(bot,'request_json',return_value={'status':'Queued'}) as request:
            for _ in range(2):self.assertEqual(bot.deliver({'page':1})['status'],'Queued')
            self.assertEqual(request.call_count,2)
    def test_atomic_cursor_does_not_contain_credentials_or_seen_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scan_cursor.json';bot.atomic_save(path,{'next_page':2,'status':'Partial'})
            self.assertEqual(json.loads(path.read_text())['status'],'Partial')
            self.assertNotIn('seen',path.read_text());self.assertNotIn('token',path.read_text())

if __name__=='__main__':unittest.main()

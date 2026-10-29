import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import bot

class CollectorTests(unittest.TestCase):
    def run_batch(self, directory, pages, fail_page=None, budget_page=None, delivery=None):
        state_path=Path(directory)/'cursor.json';cache_path=Path(directory)/'identity.json'
        page=Mock();page.goto.side_effect=lambda url,**kwargs:Mock(status=200)
        browser=Mock();browser.new_page.return_value=page
        runtime=Mock();runtime.chromium.launch.return_value=browser
        manager=Mock();manager.__enter__=Mock(return_value=runtime);manager.__exit__=Mock(return_value=False)
        visited=[]
        def rows(_page):
            from urllib.parse import parse_qs,urlparse
            number=int(parse_qs(urlparse(page.goto.call_args.args[0]).query)['page'][0]);visited.append(number)
            if number==fail_page:raise bot.CollectionError('source failure')
            if number==budget_page:raise bot.BudgetReached('time budget')
            return [{'placeId':number*100+i,'name':f'Game {number}-{i}'} for i in range(pages.get(number,2))]
        module=types.ModuleType('playwright.sync_api');module.sync_playwright=Mock(return_value=manager)
        with patch.dict('sys.modules',{'playwright.sync_api':module}),patch.object(bot,'STATE_FILE',state_path),patch.object(bot,'IDENTITY_FILE',cache_path),patch.object(bot,'DRY_RUN',False),patch.object(bot,'select_pages',side_effect=lambda n:real_select(n,3,0)),patch.object(bot,'get_game_rows',side_effect=rows),patch.object(bot,'parse_row',side_effect=lambda row,headers:row),patch.object(bot,'resolve_candidate',side_effect=lambda c,cache:{**c,'universeId':c['placeId']+1}),patch.object(bot,'deliver',side_effect=delivery or (lambda payload:{'status':'Queued','queued':len(payload['candidates'])})):
            try:bot.main()
            except bot.CollectionError:
                if fail_page is None and delivery is None:raise
        return visited,json.loads(state_path.read_text())

    def table_page(self, rows, empty_labels=(), game_rows=()):
        page=Mock()
        def locator(selector):
            result=Mock()
            if selector=='table tbody tr.ant-table-row':result.all.return_value=list(game_rows)
            elif selector=='table tbody tr':result.all.return_value=list(rows)
            elif selector=='.ant-table-placeholder,.ant-empty-description':result.all_text_contents.return_value=list(empty_labels)
            else:raise AssertionError(selector)
            return result
        page.locator.side_effect=locator
        return page
    def table_row(self, classes='', cells=7):
        row=Mock();row.get_attribute.return_value=classes;row.locator.return_value.count.return_value=cells
        return row
    def test_empty_page_ignores_ant_design_measurement_row(self):
        sizing=self.table_row('ant-table-measure-row',7)
        placeholder=self.table_row('ant-table-placeholder',1)
        page=self.table_page([sizing,placeholder],['No dataNo data','No data'])
        self.assertEqual(bot.get_game_rows(page),[])
    def test_layout_row_alone_is_not_a_reliable_empty_page(self):
        page=self.table_page([self.table_row('ant-table-measure-row',7)])
        with self.assertRaisesRegex(bot.CollectionError,'reliable empty-page marker'):bot.get_game_rows(page)
    def test_legacy_game_rows_are_retained_without_layout_rows(self):
        game=self.table_row('legacy-game-row',7)
        page=self.table_page([self.table_row('ant-table-measure-row',7),game])
        self.assertEqual(bot.get_game_rows(page),[game])
    def test_current_game_rows_are_unchanged(self):
        game=self.table_row('ant-table-row',7)
        self.assertEqual(bot.get_game_rows(self.table_page([],game_rows=[game])),[game])
    def test_original_title_exclusions_are_preserved(self):
        for name in ['MODDED Game','Reuploaded place','NSFW','Test release']:
            self.assertTrue(bot.is_excluded(name))
        self.assertFalse(bot.is_excluded('My Small Business'));self.assertFalse(bot.is_excluded('Modern Age'))
    def test_numeric_values_and_unknowns(self):
        for value,expected in [('4.4K',4400),('458.2K',458200),('1.2M',1200000),('1,200+',1200),('100',100),('No data',None),('',None),('1,2',None)]:self.assertEqual(bot.parse_number(value),expected)
    def test_filters_and_unbounded_deep_pagination(self):
        from urllib.parse import parse_qs,urlparse
        for number in [1,2,19,21,101]:
            query=parse_qs(urlparse(bot.page_url(number)).query)
            self.assertEqual(query,{'page':[str(number)],'page_size':['50'],'playing_min':['100'],'visits_max':['500000']})
        self.assertEqual(bot.select_pages(19,3,0),[19,20,21]);self.assertEqual(bot.select_pages(21,2,0),[21,22]);self.assertEqual(bot.select_pages(20,3,20),[20,1,2])
        self.assertEqual([bot.select_pages(n,3,12) for n in [1,4,7,10,13]],[[1,2,3],[4,5,6],[7,8,9],[10,11,12],[1,2,3]])
    def test_rate_limit_respects_retry_after_before_one_retry(self):
        from urllib.error import HTTPError
        from io import BytesIO
        response=BytesIO(b'{"universeId":123}')
        with patch.object(bot,'urlopen',side_effect=[HTTPError('https://apis.roblox.com',429,'Limited',{'Retry-After':'5'},None),response]),patch.object(bot.time,'sleep') as sleep:
            self.assertEqual(bot.request_json('https://apis.roblox.com/universes/v1/places/456/universe')['universeId'],123)
            sleep.assert_called_once_with(5)
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

    def test_consecutive_runs_continue_after_acknowledgment_and_confirmed_end(self):
        with tempfile.TemporaryDirectory() as directory:
            visited,state=self.run_batch(directory,{})
            self.assertEqual(visited,[1,2,3]);self.assertEqual(state['next_page'],4)
            visited,state=self.run_batch(directory,{})
            self.assertEqual(visited,[4,5,6]);self.assertEqual(state['next_page'],7)
            visited,state=self.run_batch(directory,{7:0})
            self.assertEqual(visited,[7]);self.assertTrue(state['scan_complete']);self.assertEqual(state['next_page'],1)
            visited,state=self.run_batch(directory,{})
            self.assertEqual(visited,[1,2,3]);self.assertEqual(state['next_page'],4)

    def test_failed_page_and_budget_resume_without_skipping_acknowledged_pages(self):
        for kind in ['failure','budget']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                kwargs={'fail_page':2} if kind=='failure' else {'budget_page':2}
                visited,state=self.run_batch(directory,{},**kwargs)
                self.assertEqual(state['next_page'],2);self.assertEqual(state['pages_read'],[1])
                self.assertEqual(state['status'],'Partial' if kind=='failure' else 'Time budget reached')
                visited,state=self.run_batch(directory,{})
                self.assertEqual(visited,[2,3,4]);self.assertEqual(state['next_page'],5)

    def test_no_cursor_advance_after_unconfirmed_delivery(self):
        for status in ['Paused','Unknown']:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as directory:
                bot.atomic_save(Path(directory)/'cursor.json',{'version':2,'next_page':10})
                visited,state=self.run_batch(directory,{},delivery=lambda payload:{'status':status})
                self.assertEqual(visited,[10]);self.assertEqual(state['next_page'],10)

    def test_budget_bounds_requests_and_prevents_retry_sleep_over_deadline(self):
        from urllib.error import HTTPError
        from io import BytesIO
        with patch.object(bot,'_deadline',110),patch.object(bot.time,'monotonic',return_value=100),patch.object(bot,'urlopen',return_value=BytesIO(b'{}')) as request:
            bot.request_json('https://apis.roblox.com')
            self.assertEqual(request.call_args.kwargs['timeout'],10)
        with patch.object(bot,'_deadline',110),patch.object(bot.time,'monotonic',return_value=100),patch.object(bot,'urlopen',side_effect=HTTPError('https://apis.roblox.com',429,'Limited',{'Retry-After':'10'},None)),patch.object(bot.time,'sleep') as sleep:
            with self.assertRaises(bot.BudgetReached):bot.request_json('https://apis.roblox.com')
            sleep.assert_not_called()

real_select=bot.select_pages

if __name__=='__main__':unittest.main()


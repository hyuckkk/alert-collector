"""Offline tests of the actual collector entry point; no live HTTP or snapshots."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('discover', ROOT / 'scripts/discover.py')
discover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discover)


class ArtistDiscovery(unittest.TestCase):
    def test_real_entry_point_all_queries_and_repeat_urls(self):
        queries = json.loads((ROOT / 'watch/queries.json').read_text())
        calls = []
        def search(engine, url, query):
            calls.append((engine, query))
            return [{'title': query, 'url': 'https://example.test/' + str(queries['job3'].index(query)),
                     'engine': engine, 'query': query}] if query in queries['job3'] else []
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(discover, 'OUT', out), patch.object(discover, 'search', search), patch.object(discover.time, 'sleep'):
                self.assertEqual(discover.main(), 0)
                first = json.loads((out / 'job3.json').read_text())
                self.assertEqual(first['new_this_run'], len(queries['job3']))
                for group in queries.values():
                    if not isinstance(group, list):
                        continue
                    for query in group:
                        self.assertIn(('daum_web', query), calls)
                        self.assertIn(('gnews', query), calls)
                for artist in ['박효신', '이해원', '브랜든 최', '송소희', '이적', '최예나', '아일릿']:
                    self.assertTrue(any(artist in hit['query'] for hit in first['hits']))
                discover.main()
                self.assertEqual(json.loads((out / 'job3.json').read_text())['new_this_run'], 0)

    def test_source_candidate_extraction_and_no_host_spoofing(self):
        urls = ['https://weverse.io/illit/notice/29946',
                'https://m.weverse.io/illit/notice/29946?hl=ko',
                'https://news.coupang.com/archives/56473/',
                'https://festival.example.ac.kr/lineup/2026',
                'https://x.com/YENA_OFFICIAL/status/123456',
                'https://www.instagram.com/reel/example/',
                'https://ticket.yes24.com/Perf/1234',
                'https://nol.yanolja.com/ticket/products/25004131',
                'https://ticketlink.co.kr/product/1234',
                'https://booking.naver.com/booking/1234',
                'https://ticket.melon.com/performance/index.htm']
        bad = ['https://weverse.io.evil.test/illit/notice/29946',
               'https://evil.test/weverse.io/illit/notice/29946',
               'https://weverse.io/illit/notice', 'https://search.naver.com/search.naver']
        html = ''.join('<a href="' + url + '">Official concert candidate</a>' for url in urls + bad)
        with patch.object(discover, 'get', return_value=html):
            hits = discover.search('naver_web', 'https://example.test', 'ILLIT concert')
        self.assertEqual({h['url'] for h in hits}, set(urls))

    def test_naver_rotation_preserves_query_positions(self):
        queries = json.loads((ROOT / 'watch/queries.json').read_text())
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            for hour in range(3):
                class Clock:
                    @staticmethod
                    def now(tz):
                        return datetime(2026, 10, 26, hour, tzinfo=tz)
                with patch.object(discover, 'OUT', Path(directory)), patch.object(discover, 'datetime', Clock), patch.object(discover.time, 'sleep'), patch.object(discover, 'search', side_effect=lambda e, u, q: calls.append((e,q)) or []):
                    discover.main()
        for query in queries['job3']:
            self.assertEqual(calls.count(('naver_web', query)), 1)


if __name__ == '__main__':
    unittest.main()

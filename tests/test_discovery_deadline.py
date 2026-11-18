import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('discover', ROOT/'scripts/discover.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

class DiscoveryTests(unittest.TestCase):
    def test_all_queries_and_topics_preserved(self):
        now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        qs = {'job1':['a']*50, 'job3':['yena','illit'], 'job16':['ski']}
        tasks = d.build_tasks(qs, now)
        self.assertEqual([t[0] for t in tasks[:3]], ['job1','job3','job16'])
        for job, queries in qs.items():
            for query in queries:
                self.assertTrue(any(t[:3]==(job,query,'daum_web') for t in tasks))

    def test_partial_preserves_timestamp_and_dedup(self):
        now = datetime.now(d.KST)
        stamp = now.isoformat()
        old = {'job3':{'updated':'old', 'hits':[{'url':'known','first_seen':stamp}]}}
        counts={'job3':{'ok':0,'failed':1,'skipped':1}}
        with tempfile.TemporaryDirectory() as td, patch.object(d,'OUT',Path(td)):
            d.save_results({},old,{'job3':[]},{'job3':['blocked']},counts,{'job3':4},now,True)
            result=json.loads((Path(td)/'job3.json').read_text())
            self.assertEqual(result['updated'],'old')
            self.assertEqual(result['coverage']['pending'],2)
            self.assertEqual(result['status'],'failed')
            counts['job3']['ok']=1
            hits={'job3':[{'url':'known'},{'url':'fresh'},{'url':'fresh'}]}
            d.save_results({},old,hits,{'job3':[]},counts,{'job3':4},now,True)
            result=json.loads((Path(td)/'job3.json').read_text())
            self.assertEqual(result['new_this_run'],1)
            self.assertEqual(len(result['hits']),2)
            self.assertEqual(result['status'],'partial')

    def test_naver_circuit_stops_after_first_403(self):
        import queue
        output=queue.Queue()
        tasks=[('job3','q','naver_web','https://example.test')]*5
        with patch.object(d,'search',side_effect=RuntimeError('403 Forbidden')) as search, patch.object(d.time,'sleep'):
            d.search_lane(tasks,output,1,True)
        self.assertEqual(search.call_count,1)
        statuses=[output.get()[1][2] for _ in tasks]
        self.assertEqual(statuses.count('skipped'),4)

    def test_hung_transport_terminates_and_saves_other_lane(self):
        # Use the real spawn supervisor with a deterministic transport stub.
        # No HTTP, secrets, live snapshots or Telegram calls.
        source=(ROOT/'scripts/discover.py').read_text()
        stub='''
def search(engine, url, query):
    if engine.startswith('naver'):
        time.sleep(60)
    return [dict(url='https://example.test/'+query, title=query, engine=engine, query=query)]
'''
        source=source.replace("if __name__ == '__main__':",stub+"\nif __name__ == '__main__':")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'scripts').mkdir();(root/'watch').mkdir()
            (root/'scripts/discover.py').write_text(source)
            (root/'watch/queries.json').write_text(json.dumps({'job3':['yena','illit'],'job1':['family']}))
            start=time.monotonic()
            p=subprocess.run([sys.executable,str(root/'scripts/discover.py')],env={**os.environ,'DISCOVER_BUDGET_SECONDS':'2'},capture_output=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertLess(time.monotonic()-start,8)
            result=json.loads((root/'snapshots/discover/job3.json').read_text())
            self.assertGreater(len(result['hits']),0)
            self.assertGreater(result['coverage']['pending'],0)
            self.assertEqual(result['status'],'partial')
            self.assertTrue(result['run_finished'])

if __name__=='__main__':
    unittest.main()

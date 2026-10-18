from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import bot
from collector import Collector
import dashboard
import ops
from production import SignalApp
from publication import Publications
from service_state import ServiceState
from signals import SCORING_VERSION

ROOT=Path(__file__).resolve().parents[1]


def data(title='A model release'):
    now=time.time()
    return dict(generated_at=now,collected_at=now,scoring_version=SCORING_VERSION,
        items=[dict(id='test',title=title,url='https://example.com/story',published_at=now-100,summary='Summary',
                    source_name='Test',coverage=1,score=50,raw_score=None,metrics=[])],statuses=[],sources=[])


def request(app,path='/',method='GET',token=None,origin=None,etag=None):
    route,_,query=path.partition('?')
    env=dict(PATH_INFO=route,QUERY_STRING=query,REQUEST_METHOD=method,CONTENT_LENGTH='0',wsgi_input=io.BytesIO())
    if token:env['HTTP_AUTHORIZATION']='Bearer '+token
    if origin:env['HTTP_ORIGIN']=origin
    if etag:env['HTTP_IF_NONE_MATCH']=etag
    captured={}
    def start(status,headers):captured.update(status=int(status.split()[0]),headers=dict(headers))
    captured['body']=b''.join(app(env,start));return captured


class Production(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=ServiceState(self.root/'service.sqlite3');self.publications=Publications(self.root/'published')
        self.payload=data();self.publications.publish(self.payload)
        self.app=SignalApp(self.state,self.publications,admin_token='test-only-key',public_url='https://signal.example')
    def tearDown(self):self.temp.cleanup()

    def test_wsgi_reads_never_collect_and_requests_are_conditional(self):
        with patch('bot.RedditRSS.fetch',side_effect=AssertionError('Web must never fetch')):
            for path in ('/','/api/news','/api/rss?sort=day','/api/status','/metrics'):
                response=request(self.app,path);self.assertEqual(response['status'],200)
            first=request(self.app,'/api/news');second=request(self.app,'/api/news',etag=first['headers']['ETag'])
            self.assertEqual(second['status'],304);self.assertEqual(second['body'],b'')
            self.assertEqual(request(self.app,'/',method='HEAD')['body'],b'')
            self.assertEqual(request(self.app,'/api/rss?sort=wrong')['status'],400)

    def test_management_requires_secret_valid_origin_and_durable_queue(self):
        self.assertEqual(request(self.app,'/api/refresh',method='POST')['status'],401)
        self.assertEqual(request(self.app,'/api/refresh',method='POST',token='wrong')['status'],401)
        self.assertEqual(request(self.app,'/api/refresh',method='POST',token='test-only-key',origin='https://evil.example')['status'],403)
        result=request(self.app,'/api/refresh',method='POST',token='test-only-key',origin='https://signal.example')
        self.assertEqual(result['status'],202)
        other=SignalApp(ServiceState(self.root/'service.sqlite3'),self.publications,admin_token='test-only-key')
        self.assertTrue(json.loads(request(other,'/api/status')['body'])['refreshing'])
        public=json.loads(request(other,'/api/news')['body'])
        self.assertNotIn('test-only-key',json.dumps(public))

    def test_queue_coalesces_across_web_workers_and_recovers_after_crash(self):
        states=[ServiceState(self.root/'service.sqlite3') for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:jobs=list(pool.map(lambda state:state.enqueue(),states))
        self.assertEqual(len({job['job_id'] for job in jobs}),1)
        job=self.state.claim();self.assertIsNone(states[1].claim())
        self.state.recover();reclaimed=self.state.claim();self.assertEqual(reclaimed['id'],job['id'])
        self.state.finish(job['id'])
        self.assertEqual(self.state.enqueue()['status'],'cached')
        self.assertEqual(self.state.status()['collections_succeeded'],1)

    def test_readiness_cold_start_stale_worker_and_stale_data(self):
        self.assertEqual(request(self.app,'/health/live')['status'],200)
        self.assertEqual(request(self.app,'/health/ready')['status'],503)
        self.state.heartbeat('collector',scoring_version=SCORING_VERSION)
        self.assertEqual(request(self.app,'/health/ready')['status'],200)
        self.state.set('heartbeat:collector',dict(at=time.time()-100,status='running'))
        self.assertEqual(request(self.app,'/health/ready')['status'],503)
        empty=SignalApp(self.state,Publications(self.root/'empty'),admin_token='x')
        self.assertEqual(request(empty,'/')['status'],503)
        self.assertEqual(request(empty,'/health/live')['status'],200)

    def test_generation_switch_is_atomic_and_preserves_previous_on_failure(self):
        first=self.publications.load()['manifest']['generation']
        with patch('dashboard.render_html',side_effect=RuntimeError('Build failed')):
            with self.assertRaises(RuntimeError):self.publications.publish(data('Broken'))
        self.assertEqual(self.publications.load()['manifest']['generation'],first)
        self.publications.publish(data('Updated'))
        self.assertEqual(self.publications.load()['data']['items'][0]['title'],'Updated')
        self.assertNotEqual(self.publications.load()['manifest']['generation'],first)

    def test_embedded_data_cannot_break_out_of_script(self):
        self.publications.publish(data('</script><script>attack()</script>'))
        html=request(self.app,'/')['body'].decode()
        self.assertNotIn('</script><script>attack()',html)
        self.assertIn('\\u003c/script\\u003e',html)
        self.assertIn("script-src 'self' 'sha256-",request(self.app,'/')['headers']['Content-Security-Policy'])

    def test_failed_collection_keeps_published_generation(self):
        schedule=Mock();schedule.collect.side_effect=RuntimeError('Source down')
        collector=Collector(self.state,self.publications,schedule=schedule)
        self.state.enqueue();job=self.state.claim();generation=self.publications.load()['manifest']['generation']
        with self.assertLogs('collector',level='ERROR'):self.assertFalse(collector.collect(job))
        self.assertEqual(self.publications.load()['manifest']['generation'],generation)
        self.assertEqual(self.state.status()['collections_failed'],1)


@contextmanager
def isolated_workspace():
    before=Path.cwd()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);shutil.copyfile(ROOT/'config.json',root/'config.json');shutil.copytree(ROOT/'web',root/'web')
        os.chdir(root)
        try:yield root
        finally:os.chdir(before)


class Recovery(unittest.TestCase):
    def test_live_backup_includes_committed_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'live.sqlite3';target=Path(directory)/'backup.sqlite3'
            with sqlite3.connect(source) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE records (value TEXT)')
                db.execute("INSERT INTO records VALUES ('committed')");db.commit()
                self.assertGreater(Path(str(source)+'-wal').stat().st_size,0)
                ops.copy_database(source,target)
                with sqlite3.connect(target) as restored:
                    self.assertEqual(restored.execute('SELECT value FROM records').fetchone()[0],'committed')
                    self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_collection_success_and_backup_restore_keep_archive_and_first_seen(self):
        with isolated_workspace() as root:
            state=ServiceState();pub=Publications()
            post=dict(id='one',source='site',source_id='techcrunch',source_name='TechCrunch AI',category='news',
                      title='New AI model release',url='https://example.com/story',article_url='https://example.com/story',
                      created_utc=time.time()-100,selftext='A model release',links=[],score=None,feed_rank=0,filter_mode='all')
            cfg=json.loads(Path('config.json').read_text());cfg['feeds']=[s for s in cfg['feeds'] if s['id']=='techcrunch'];cfg['rss_groups']=[]
            Path('config.json').write_text(json.dumps(cfg))
            from http_cache import HTTPCache
            client=Mock();client.http=HTTPCache();client.get.return_value=[post];client.enrich.side_effect=lambda posts,*a,**k:posts
            client.source_status.return_value=dict(name='Test',status='ok',count=1,checked_at=time.time())
            collector=Collector(state,pub,client);state.enqueue();job=state.claim()
            with patch('source_schedule.random.uniform',return_value=0):self.assertTrue(collector.collect(job))
            self.assertEqual(pub.load()['data']['scoring_version'],SCORING_VERSION)
            with sqlite3.connect('data/news.sqlite3') as db:first=db.execute('SELECT first_seen FROM news').fetchone()[0]
            snapshots=Path('data/feed-snapshot.json').stat().st_mtime
            with patch('builtins.print'):ops.backup(root/'backups')
            backup=next((root/'backups').iterdir())
            Path('data').rename('previous-data')
            with patch('builtins.print'):ops.import_data(backup,include_control=True)
            with sqlite3.connect('data/news.sqlite3') as db:
                self.assertEqual(db.execute('SELECT first_seen FROM news').fetchone()[0],first)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(Path('data/feed-snapshot.json').stat().st_mtime,snapshots)
            self.assertEqual(ServiceState().status()['collections_succeeded'],1)

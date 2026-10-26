import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from io import BytesIO
from urllib.error import HTTPError
import bot
from http_cache import HTTPCache
from service_state import ServiceState
from source_schedule import SourceSchedule

RSS=b'''<rss version="2.0"><channel><item><title>LLM API release</title><link>https://example.test/news</link><pubDate>Sun, 18 Oct 2026 00:00:00 GMT</pubDate><description>AI news</description></item></channel></rss>'''


class Response:
    headers={};status=200
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,limit):return RSS[:limit]
    def geturl(self):return 'https://example.test/feed'


class Scheduling(unittest.TestCase):
    def setUp(self):
        from db_test import use_schema;use_schema(self)
        self.before=Path.cwd();self.temp=tempfile.TemporaryDirectory();os.chdir(self.temp.name)
        self.clock=10000
        self.clock_patch=patch('source_schedule.time.time',side_effect=lambda:self.clock);self.clock_patch.start()
        self.random_patch=patch('source_schedule.random.uniform',return_value=0);self.random_patch.start()
        self.cfg=dict(timezone='UTC',rss_groups=[],rss_limit=100,subreddits={},
            feeds=[dict(id='a',name='A',url='https://same.test/a',format='feed',category='news'),
                   dict(id='b',name='B',url='https://same.test/b',format='feed',category='news')],
            collection=dict(rss_seconds=100,request_gap_seconds=10,startup_spread_seconds=1),
            cache=dict(min_host_interval_seconds=0,rss_ttl_seconds=50,backoff_jitter_ratio=0))
        self.state=ServiceState();self.client=bot.RedditRSS();self.schedule=SourceSchedule(self.state,self.client)
        self.stop=threading.Event()
    def tearDown(self):
        self.random_patch.stop();self.clock_patch.stop();os.chdir(self.before);self.temp.cleanup()

    def test_one_source_per_job_global_gap_and_schedule_survive_restart(self):
        with patch('bot.urlopen',return_value=Response()) as network:
            first=self.schedule.collect(self.cfg,self.stop)
            self.assertTrue(first['changed']);self.assertEqual(network.call_count,1)
            self.assertFalse(self.schedule.collect(self.cfg,self.stop)['changed'])
            self.clock+=9
            restarted=SourceSchedule(self.state,self.client)
            self.assertFalse(restarted.collect(self.cfg,self.stop)['changed'])
            self.clock+=1
            second=restarted.collect(self.cfg,self.stop)
            self.assertEqual(network.call_count,2);self.assertEqual(len(second['posts']),2)
            self.assertGreater(restarted.next_due(self.cfg),self.clock)

    def test_shared_host_429_blocks_other_source_and_manual_refresh(self):
        limited=HTTPError('https://same.test/a',429,'limited',{'Retry-After':'7200'},BytesIO())
        with patch('bot.urlopen',side_effect=limited) as network:
            first=self.schedule.collect(self.cfg,self.stop)
            self.assertEqual(first['statuses'][0]['http_status'],429)
            self.clock+=100
            restarted=SourceSchedule(self.state,bot.RedditRSS())
            self.assertFalse(restarted.collect(self.cfg,self.stop)['changed'])
            self.assertGreaterEqual(restarted.next_due(self.cfg),17200)
            self.assertEqual(network.call_count,1)

    def test_host_spacing_is_applied_without_sleeping_in_collector(self):
        self.cfg['cache']['host_min_intervals']={'same.test':60}
        with patch('bot.urlopen',return_value=Response()) as network,patch('http_cache.time.sleep') as sleep:
            self.schedule.collect(self.cfg,self.stop);self.clock+=10
            self.assertFalse(self.schedule.collect(self.cfg,self.stop)['changed'])
            self.clock+=50
            self.schedule.collect(self.cfg,self.stop)
            self.assertEqual(network.call_count,2);sleep.assert_not_called()

    def test_failure_preserves_previous_posts_and_their_success_timestamp(self):
        self.cfg['feeds']=self.cfg['feeds'][:1]
        def enriched(posts,*a,**kw):return [dict(p,article_text='Saved full article',text_origin='article') for p in posts]
        with patch('bot.urlopen',return_value=Response()),patch.object(self.client,'enrich',side_effect=enriched):
            first=self.schedule.collect(self.cfg,self.stop)
        self.clock+=100
        with patch('bot.urlopen',side_effect=HTTPError('https://same.test/a',429,'limited',{},BytesIO())):
            second=self.schedule.collect(self.cfg,self.stop)
        self.assertEqual(first['posts'],second['posts'])
        self.assertEqual(first['collected_at'],second['collected_at'])
        self.assertEqual(second['statuses'][0]['status'],'stale')
        self.assertEqual(second['posts'][0]['article_text'],'Saved full article')

    def test_source_specific_interval_and_disabled_source(self):
        self.cfg['feeds'][0]['poll_seconds']=500
        with patch('bot.urlopen',return_value=Response()):self.schedule.collect(self.cfg,self.stop)
        self.cfg['feeds'][1]['enabled']=False
        self.assertEqual(self.schedule.next_due(self.cfg),10500)
        self.assertEqual(len(self.schedule.snapshot()['statuses']),1)

    def test_bootstrap_uses_existing_cache_age_and_keeps_existing_cooldown(self):
        self.client.http.configure(self.cfg['cache'])
        with patch('http_cache.urlopen',return_value=Response()):self.client.http.get('https://same.test/a',ttl=50)
        limited=HTTPError('https://same.test/b',429,'limited',{'Retry-After':'7200'},BytesIO())
        with patch('http_cache.urlopen',side_effect=limited):
            try:self.client.http.get('https://same.test/b')
            except RuntimeError:pass
        self.assertGreaterEqual(self.schedule.next_due(self.cfg),17200)

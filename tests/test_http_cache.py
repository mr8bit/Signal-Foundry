from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from io import BytesIO
import json
import socket
import ssl
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import bot
from http_cache import HTTPCache,CacheError,failure_message


class Response:
    def __init__(self,body=b'valid',headers=None,url='https://cache.test/feed'):
        self.body=body;self.headers=headers or {};self.url=url
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,limit):return self.body[:limit]
    def geturl(self):return self.url


class Cache(unittest.TestCase):
    def setUp(self):
        from db_test import use_schema;use_schema(self)
        self.temp=tempfile.TemporaryDirectory();self.path=self.temp.name+'/http.db'
        self.cache=HTTPCache(self.path,{'min_host_interval_seconds':0})
    def tearDown(self):self.temp.cleanup()

    def test_failure_diagnostics_distinguish_transport_and_invalid_content(self):
        for error,phrase in ((URLError(socket.gaierror(-2,'unknown host')),'DNS resolution'),
                             (URLError(TimeoutError('timed out')),'Connection, TLS'),
                             (URLError(ssl.SSLCertVerificationError('invalid certificate')),'TLS certificate')):
            self.assertIn(phrase,failure_message(error,'connection'))
        self.assertIn('Response read timed out',failure_message(TimeoutError(),'body'))
        self.assertIn('Content validation failed',failure_message(RuntimeError('Expected RSS 2.0 or Atom feed'),'validation'))

    def test_failed_source_reports_persistent_http_status_and_retry_without_new_request(self):
        client=bot.RedditRSS(cache_path=self.path,cache_policy={'min_host_interval_seconds':0})
        with patch('http_cache.time.time',return_value=1000),patch('bot.urlopen',side_effect=HTTPError('https://cache.test/a',429,'limited',{'Retry-After':'7200'},BytesIO())):
            with self.assertRaises(bot.FeedBackoff) as error:client.get('https://cache.test/a')
        with patch('bot.urlopen') as network:
            failure=client.source_failure('Source','https://cache.test/a',error.exception)
            self.assertEqual(failure['http_status'],429);self.assertEqual(failure['retry_at'],8200)
            network.assert_not_called()

    def test_cache_survives_clients_and_process_restarts(self):
        with patch('http_cache.time.time',return_value=1000):
            first=self.cache.get('https://cache.test/feed',loader=lambda *a,**k:Response(),ttl=100)
        other=HTTPCache(self.path,{'min_host_interval_seconds':0})
        with patch('http_cache.time.time',return_value=1050),patch('http_cache.urlopen') as network:
            result=other.get('https://cache.test/feed',ttl=100)
            self.assertEqual(result.body,first.body);self.assertEqual(result.state,'hit');network.assert_not_called()

    def test_expired_entry_uses_validators_then_caches_304(self):
        with patch('http_cache.time.time',return_value=1000):
            self.cache.get('https://cache.test/feed',ttl=10,loader=lambda *a,**k:Response(headers={'ETag':'v1','Last-Modified':'Mon, 01 Jan 2024 00:00:00 GMT'}))
        def unchanged(request,**kwargs):
            self.assertEqual(request.get_header('If-none-match'),'v1')
            self.assertIn('2024',request.get_header('If-modified-since'))
            raise HTTPError(request.full_url,304,'unchanged',{},BytesIO())
        with patch('http_cache.time.time',return_value=1011):
            result=self.cache.get('https://cache.test/feed',ttl=10,loader=unchanged)
            self.assertEqual(result.state,'revalidated');self.assertEqual(result.validated_at,1011)
        with patch('http_cache.time.time',return_value=1015),patch('http_cache.urlopen') as network:
            self.assertEqual(self.cache.get('https://cache.test/feed',ttl=10).state,'hit');network.assert_not_called()

    def test_stale_data_keeps_observation_time_and_error_cooldown_survives_restart(self):
        with patch('http_cache.time.time',return_value=1000):
            self.cache.get('https://cache.test/feed',ttl=10,loader=lambda *a,**k:Response())
        with patch('http_cache.time.time',return_value=1011),patch('http_cache.urlopen',side_effect=URLError('timeout')):
            result=self.cache.get('https://cache.test/feed',ttl=10)
            self.assertTrue(result.stale);self.assertEqual(result.validated_at,1000)
        other=HTTPCache(self.path,{'min_host_interval_seconds':0})
        with patch('http_cache.time.time',return_value=1012),patch('http_cache.urlopen') as network:
            self.assertTrue(other.get('https://cache.test/feed',ttl=10).stale);network.assert_not_called()

    def test_429_blocks_other_urls_on_same_host_after_restart(self):
        with patch('http_cache.time.time',return_value=1000),patch('http_cache.urlopen',side_effect=HTTPError('https://cache.test/a',429,'limited',{'Retry-After':'7200'},BytesIO())):
            with self.assertRaises(CacheError) as error:self.cache.get('https://cache.test/a')
            self.assertEqual(error.exception.retry_after,7200)
        other=HTTPCache(self.path,{'min_host_interval_seconds':0})
        with patch('http_cache.time.time',return_value=1001),patch('http_cache.urlopen') as network:
            with self.assertRaises(CacheError) as error:other.get('https://cache.test/b')
            self.assertEqual(error.exception.status,429);network.assert_not_called()

    def test_retry_after_http_date(self):
        with patch('http_cache.time.time',return_value=1000),patch('http_cache.urlopen',side_effect=HTTPError('https://cache.test/a',429,'limited',{'Retry-After':formatdate(8200,usegmt=True)},BytesIO())):
            with self.assertRaises(CacheError) as error:self.cache.get('https://cache.test/a')
            self.assertEqual(error.exception.retry_after,7200)

    def test_429_backoff_grows_across_urls_and_resets_after_success(self):
        self.cache.configure(dict(rate_limit_backoff_seconds=100,max_backoff_seconds=1000,backoff_jitter_ratio=0))
        def limited(request,**kwargs):raise HTTPError(request.full_url,429,'limited',{},BytesIO())
        with patch('http_cache.time.time',return_value=1000):
            with self.assertRaises(CacheError) as first:self.cache.get('https://cache.test/a',loader=limited)
            self.assertEqual(first.exception.retry_after,100)
        other=HTTPCache(self.path,self.cache.policy)
        with patch('http_cache.time.time',return_value=1101):
            with self.assertRaises(CacheError) as second:other.get('https://cache.test/b',loader=limited)
            self.assertEqual(second.exception.retry_after,200)
        with patch('http_cache.time.time',return_value=1302):
            other.get('https://cache.test/b',loader=lambda *a,**k:Response())
            with self.assertRaises(CacheError) as reset:other.get('https://cache.test/c',loader=limited)
            self.assertEqual(reset.exception.retry_after,100)

    def test_503_retry_after_is_respected_even_above_local_backoff_cap(self):
        self.cache.configure(dict(max_backoff_seconds=100,backoff_jitter_ratio=0))
        with patch('http_cache.time.time',return_value=1000),patch('http_cache.urlopen',side_effect=HTTPError('https://cache.test/a',503,'busy',{'Retry-After':'7200'},BytesIO())):
            with self.assertRaises(CacheError) as error:self.cache.get('https://cache.test/a')
            self.assertEqual(error.exception.retry_after,7200)

    def test_network_failure_backoff_grows_and_jitter_only_delays(self):
        self.cache.configure(dict(error_backoff_seconds=100,backoff_jitter_ratio=0.2))
        with patch('http_cache.random.uniform',return_value=0.2),patch('http_cache.time.time',return_value=1000),patch('http_cache.urlopen',side_effect=URLError('offline')):
            with self.assertRaises(CacheError) as first:self.cache.get('https://cache.test/a')
            self.assertEqual(first.exception.retry_after,120)
        with patch('http_cache.random.uniform',return_value=0.2),patch('http_cache.time.time',return_value=1121),patch('http_cache.urlopen',side_effect=URLError('offline')):
            with self.assertRaises(CacheError) as second:self.cache.get('https://cache.test/a')
            self.assertEqual(second.exception.retry_after,240)

    def test_concurrent_clients_fetch_same_url_only_once(self):
        clients=[HTTPCache(self.path,{'min_host_interval_seconds':0}) for _ in range(6)]
        calls=[];lock=threading.Lock()
        def loader(*args,**kwargs):
            with lock:calls.append(1)
            time.sleep(.03);return Response()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda client:client.get('https://cache.test/feed',loader=loader),clients))
        self.assertEqual(len(calls),1);self.assertEqual(sum(r.state=='miss' for r in results),1)

    def test_invalid_response_does_not_replace_previous_good_body(self):
        def validate(data):
            if data!=b'valid':raise RuntimeError('Block page')
        with patch('http_cache.time.time',return_value=1000):
            self.cache.get('https://cache.test/feed',ttl=10,validator=validate,loader=lambda *a,**k:Response())
        with patch('http_cache.time.time',return_value=1011):
            result=self.cache.get('https://cache.test/feed',ttl=10,validator=validate,loader=lambda *a,**k:Response(b'<html>blocked</html>'))
            self.assertEqual(result.body,b'valid');self.assertTrue(result.stale)
            failure=self.cache.read('https://cache.test/feed')
            self.assertEqual(failure['status'],200)
            self.assertIn('Content validation failed',failure['error'])

    def test_very_old_stale_data_is_not_returned(self):
        self.cache.configure({'stale_if_error_seconds':20})
        with patch('http_cache.time.time',return_value=1000):
            self.cache.get('https://cache.test/feed',ttl=10,loader=lambda *a,**k:Response())
        with patch('http_cache.time.time',return_value=1031),patch('http_cache.urlopen',side_effect=URLError('timeout')):
            with self.assertRaises(CacheError):self.cache.get('https://cache.test/feed',ttl=10)

    def test_domain_gap_is_shared_between_clients(self):
        self.cache.configure({'min_host_interval_seconds':2})
        clock=[1000]
        def sleep(seconds):clock[0]+=seconds
        with patch('http_cache.time.time',side_effect=lambda:clock[0]),patch('http_cache.time.sleep',side_effect=sleep) as wait:
            self.cache.get('https://cache.test/a',loader=lambda *a,**k:Response())
            other=HTTPCache(self.path,{'min_host_interval_seconds':2})
            other.get('https://cache.test/b',loader=lambda *a,**k:Response())
            wait.assert_called_once_with(2)

    def test_batch_pacing_includes_url_and_host_cooldowns(self):
        limited=HTTPError('https://cache.test/b',429,'limited',{'Retry-After':'7200'},BytesIO())
        with patch('http_cache.time.time',return_value=1000),patch('http_cache.urlopen',side_effect=limited):
            with self.assertRaises(CacheError):self.cache.get('https://cache.test/b')
        urls=['https://cache.test/a','https://cache.test/b','https://other.test/c']
        self.assertEqual(self.cache.not_before_many(urls),dict(zip(urls,[8200,8200,0])))
        self.assertEqual(self.cache.not_before_many([]),{})

    def test_trending_cache_hit_does_not_invent_new_observation(self):
        source=dict(id='hf_models',name='Models',category='model',format='hf_models',url='https://huggingface.co/api/models')
        client=bot.RedditRSS(cache_path=self.path,cache_policy={'min_host_interval_seconds':0})
        raw=json.dumps([dict(id='org/model',likes=10)]).encode()
        with patch('http_cache.time.time',return_value=1000),patch('bot.urlopen',return_value=Response(raw,url=source['url'])):
            first=client.get(source['url'],source)[0]
        with patch('http_cache.time.time',return_value=1010),patch('bot.urlopen') as network:
            second=client.get(source['url'],source)[0]
            self.assertEqual(first['observed_at'],second['observed_at']);self.assertEqual(second['observed_at'],1000);network.assert_not_called()


if __name__=='__main__':unittest.main()

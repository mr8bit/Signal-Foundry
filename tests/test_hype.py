import json
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

import bot
import signals
from trends import parse_trends

CFG=json.loads(Path('config.json').read_text())
SOURCE=next(s for s in CFG['feeds'] if s['id']=='replicate_hype')
NOW=1792324800


def row(origin='huggingface',**changes):
    host={'github':'github.com','huggingface':'huggingface.co','replicate':'replicate.com','reddit':'www.reddit.com'}[origin]
    value=dict(id='123',source=origin,username='org',name='Qwen-agent',description='AI model and local agent',
               stars=300,url=f'https://{host}/org/Qwen-agent',created_at='2026-10-18T10:00:00Z')
    if origin=='reddit':value.update(name='[R] Qwen model release',url='https://www.reddit.com/r/MachineLearning/comments/abc/title')
    return value|changes


def parse(*rows):
    return parse_trends(json.dumps(dict(success=True,result=rows)).encode(),SOURCE,NOW)


class Hype(unittest.TestCase):
    def test_units_and_categories_are_kept_separate(self):
        posts=parse(*(row(origin) for origin in ('github','huggingface','replicate','reddit')))
        self.assertEqual([p['category'] for p in posts],['repository','model','model','community'])
        self.assertEqual([p['metrics'][0]['kind'] for p in posts],['stars','likes','runs','upvotes'])
        self.assertTrue(all(p['source_id']=='replicate_hype' for p in posts))
        self.assertFalse(any(m['kind']=='stars_day' for p in posts for m in p['metrics']))
        self.assertFalse(signals.popularity(posts[2])[1])  # runs are displayed, not treated as likes
        self.assertIsNone(bot.reject(posts[3],CFG,NOW))

    def test_update_date_is_not_model_creation_or_collection_time(self):
        p=parse(row())[0]
        self.assertEqual(p['date_kind'],'updated')
        self.assertNotEqual(p['created_utc'],NOW)
        self.assertIsNone(p['original_published_at'])
        self.assertEqual(signals.reference_time(p),(p['created_utc'],'update'))
        item=signals.build_items([p],CFG,NOW)[0]
        self.assertIn('Model update',item['reasons'])
        self.assertEqual(item['column'],'models')
        self.assertEqual(item['news_at'],p['created_utc'])

    def test_dedup_does_not_invent_independent_coverage(self):
        hype=parse(row())[0]
        native=dict(hype,source_id='hf_models',source_name='Hugging Face · Models',hype_origin=None)
        result=signals.build_items([hype,native],CFG,NOW)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['coverage'],1)
        self.assertEqual(set(result[0]['sources']),{'Hugging Face · Models','Replicate Hype'})

    def test_reddit_still_requires_threshold_keywords_and_flair(self):
        p=parse(row('reddit',stars=1))[0]
        self.assertEqual(bot.reject(p,CFG,NOW),'score')
        p=parse(row('reddit',url='https://www.reddit.com/r/LocalLLaMA/comments/abc/title'))[0]
        self.assertEqual(bot.reject(p,CFG,NOW),'flair')
        p=parse(row('reddit',name='[D] Which model is best?'))[0]
        self.assertIsNotNone(bot.reject(p,CFG,NOW))

    def test_reddit_uses_the_original_post_identity_for_deduplication(self):
        p=parse(row('reddit'))[0]
        direct=dict(p,source='rss',id='abc')
        self.assertIn('reddit:abc',bot.keys(p)&bot.keys(direct))
        self.assertEqual(len(signals.build_items([p,direct],CFG,NOW)),1)

    def test_block_pages_and_schema_errors_do_not_replace_cached_data(self):
        for data in (b'<html>blocked</html>',b'[]',b'{"success":false,"result":[]}',b'{"success":true,"result":{}}'):
            with self.assertRaises((RuntimeError,ValueError)):parse_trends(data,SOURCE,NOW)
        self.assertEqual(parse(),[])

    def test_bad_urls_dates_and_metrics_are_not_trusted(self):
        self.assertEqual(parse(row(url='javascript:alert(1)'),row(created_at='bad'),row(source='unknown'),
                               row(url='https://huggingface.co.evil.test/org/model'),row(created_at='2030-01-01T00:00:00Z')),[])
        for value in (True,-1,'100',None):self.assertEqual(parse(row(stars=value))[0]['metrics'],[])
        p=parse(row(description='<p>Hello</p><script>bad()</script>'))[0]
        self.assertEqual(p['selftext'],'Hello')
        self.assertEqual(len(parse(row(),row())),1)

    def test_http_cache_dispatches_hype_with_configured_ttl_and_validator(self):
        from http_cache import CachedResponse
        body=json.dumps(dict(success=True,result=[row()])).encode()
        with tempfile.TemporaryDirectory() as directory:
            client=bot.RedditRSS(cache_path=Path(directory)/'cache.sqlite3')
            response=CachedResponse(body,SOURCE['url'],NOW,NOW+1800,'hit')
            with patch.object(client.http,'get',return_value=response) as get, patch('trends.time.time',return_value=NOW):
                posts=client.get(SOURCE['url'],SOURCE)
                self.assertEqual(posts[0]['hype_origin'],'huggingface')
                self.assertEqual(get.call_args.kwargs['ttl'],1800)
                self.assertEqual(len(get.call_args.kwargs['validator'](body)),1)


if __name__=='__main__':unittest.main()

import json
import unittest
from datetime import datetime,timezone
from pathlib import Path
import bot
import signals
from trends import parse_trends

NOW=datetime(2026,10,18,12,tzinfo=timezone.utc).timestamp()
CFG=json.loads(Path('config.json').read_text())


def source(identifier):
    return next(s for s in CFG['feeds'] if s['id']==identifier)


class Trends(unittest.TestCase):
    def test_github_period_stars_are_separate_from_total(self):
        data=b'''<article class="Box-row"><h2><a href="/org/llm-agent"><svg><path/></svg><span>org / </span>llm-agent</a></h2><p>A local AI agent with tools</p><span itemprop="programmingLanguage">Python</span><a href="/org/llm-agent/stargazers">12,345</a><a href="/org/llm-agent/forks">456</a><span class="d-inline-block float-sm-right"><svg></svg>321 stars today</span></article>'''
        p=parse_trends(data,source('github_trending'),NOW)[0]
        self.assertEqual(p['title'],'org/llm-agent')
        self.assertEqual(p['selftext'],'A local AI agent with tools')
        self.assertEqual(p['language'],'Python')
        metrics={x['kind']:x['value'] for x in p['metrics']}
        self.assertEqual(metrics,{'stars_day':321,'stars':12345,'forks':456})
        self.assertIsNone(p['original_published_at'])
        self.assertEqual(p['date_kind'],'trend')
        self.assertEqual(p['created_utc'],NOW)
        self.assertIsNone(bot.reject(p,CFG,NOW))
        self.assertEqual(bot.priority(p),3)
        with self.assertRaises(RuntimeError):parse_trends(b'<html>Blocked</html>',source('github_trending'),NOW)

    def test_models_old_creation_date_does_not_become_new_release(self):
        rows=[dict(id='org/model',createdAt='2024-01-01T00:00:00Z',likes=42,downloads=1234,
                   pipeline_tag='text-generation',cardData={'license':'apache-2.0'},private=False)]
        p=parse_trends(json.dumps(rows).encode(),source('hf_models'),NOW)[0]
        self.assertLess(p['original_published_at'],NOW-365*86400)
        self.assertEqual(p['observed_at'],NOW)
        self.assertEqual(p['score'],None)
        self.assertEqual(p['content_kind'],'model')
        self.assertIn('apache-2.0',p['selftext'])
        self.assertEqual({m['kind']:m['value'] for m in p['metrics']},{'likes':42,'downloads_month':1234})
        item=signals.build_items([p],CFG,NOW)[0]
        self.assertEqual(item['column'],'models')
        self.assertEqual(item['topic'],'Модели')
        self.assertEqual(item['date_kind'],'trend')

    def test_papers_use_abstract_and_upvotes_not_provider_ai_summary(self):
        rows=[dict(paper=dict(id='2401.00001',title='A paper about AI',summary='Original abstract\\nwith results',
                            ai_summary='Unverified generated summary',publishedAt='2024-01-01T00:00:00Z',upvotes=7,
                            githubRepo='https://github.com/org/research'),numComments=2)]
        p=parse_trends(json.dumps(rows).encode(),source('hf_papers'),NOW)[0]
        self.assertEqual(p['selftext'],'Original abstract with results')
        self.assertIn('https://arxiv.org/abs/2401.00001',p['links'])
        self.assertIn('https://github.com/org/research',p['links'])
        self.assertEqual(bot.priority(p),4)
        self.assertEqual(signals.build_items([p],CFG,NOW)[0]['column'],'papers')

    def test_metrics_are_normalized_per_unit_downloads_do_not_inflate_score(self):
        rows=[dict(id='org/model',likes=10,downloads=1000000000)]
        p=parse_trends(json.dumps(rows).encode(),source('hf_models'),NOW)[0]
        points,available,_=signals.popularity(p)
        self.assertTrue(available);self.assertLess(points,4)
        p['metrics']=[dict(kind='downloads_month',value=1000000000,label='downloads')]
        self.assertFalse(signals.popularity(p)[1])
        p['metrics']=[dict(kind='stars_day',value=1000,label='stars/day')]
        self.assertAlmostEqual(signals.popularity(p)[0],10)
        p['metrics']=[dict(kind='upvotes',value=300,label='votes')]
        self.assertAlmostEqual(signals.popularity(p)[0],10)

    def test_trend_source_errors_and_optional_fields(self):
        with self.assertRaises(RuntimeError):parse_trends(b'{"error":"limited"}',source('hf_models'),NOW)
        self.assertEqual(parse_trends(b'[{"id":"org/private","private":true}]',source('hf_models'),NOW),[])
        p=parse_trends(b'[{"id":"org/minimal"}]',source('hf_models'),NOW)[0]
        self.assertEqual(p['metrics'],[])
        self.assertIsNone(p['original_published_at'])


if __name__=='__main__':unittest.main()

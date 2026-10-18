from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
import attention
import signals

CFG=json.loads(Path('config.json').read_text())
NOW=datetime(2026,10,18,12,tzinfo=timezone.utc).timestamp()


def hype_repo(stars,**changes):
    p=dict(id='h',source='site',source_id='replicate_hype',source_name='Replicate Hype',category='repository',
           content_kind='repository',hype_origin='github',title='owner/tool',url='https://github.com/Owner/Tool',
           article_url='https://github.com/Owner/Tool',selftext='',links=[],created_utc=NOW-3600,feed_rank=0,
           metrics=[dict(kind='stars',value=stars,label='total stars')])
    p.update(changes);return p


class FakeHTTP:
    def __init__(self,bodies):self.bodies,self.urls=bodies,[]
    def configure(self,policy):pass
    def get(self,url,headers=None,ttl=None,timeout=None,max_bytes=None,validator=None):
        self.urls.append(url)
        body=next(b for prefix,b in self.bodies.items() if url.startswith(prefix))
        if validator:validator(body)
        return type('Response',(),{'body':body})()


class Attention(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=attention.Store(Path(self.tmp.name)/'a.sqlite3')

    def tearDown(self):self.tmp.cleanup()

    def test_totals_become_growth_per_day_from_stored_readings(self):
        self.store.record(attention.passive([hype_repo(1000)]),NOW-12*3600)
        self.store.record(attention.passive([hype_repo(1500)]),NOW)
        readings=self.store.snapshot(NOW)['github.com/owner/tool']
        self.assertEqual(readings['github_stars'],1500);self.assertAlmostEqual(readings['github_stars_day'],1000)
        # Readings closer than six hours apart do not claim a rate.
        self.store.record([('github.com/a/b','github_stars',10)],NOW-3600);self.store.record([('github.com/a/b','github_stars',90)],NOW)
        self.assertNotIn('github_stars_day',self.store.snapshot(NOW)['github.com/a/b'])

    def test_passive_readings_cover_feed_comments_and_hn_discussion(self):
        article=dict(hype_repo(0),source_id='ars',content_kind=None,hype_origin=None,url='https://www.arstechnica.com/ai/story/',
                     article_url='https://www.arstechnica.com/ai/story/',metrics=[dict(kind='comments',value=132,label='comments')])
        hn=dict(article,source_id='hn',metrics=[],selftext='Article URL: https://arstechnica.com/ai/story\nPoints: 233\nComments: 75',
                links=['https://news.ycombinator.com/item?id=1'])
        rows={(k,c):v for k,c,v in attention.passive([article,hn])}
        self.assertEqual(rows[('arstechnica.com/ai/story','comments')],132)
        self.assertEqual(rows[('arstechnica.com/ai/story','hn_points')],233)
        self.assertEqual(rows[('arstechnica.com/ai/story','hn_comments')],75)

    def test_google_news_counts_outlets_reporting_the_same_story(self):
        body=b'''<rss><channel>
          <item><title>DeepSeek releases V4.1 Flash with faster agents - Reuters</title><source>Reuters</source></item>
          <item><title>DeepSeek V4.1 Flash release beats rivals - Bloomberg</title><source>Bloomberg</source></item>
          <item><title>DeepSeek releases V4.1 Flash with faster agents - Reuters</title><source>Reuters</source></item>
          <item><title>Stock markets close higher - CNBC</title><source>CNBC</source></item></channel></rss>'''
        self.assertEqual(attention.parse_gnews(body,'DeepSeek releases V4.1 Flash, beating rivals on agents'),2)
        # The article's own outlet is not coverage of it.
        self.assertEqual(attention.parse_gnews(body,'DeepSeek releases V4.1 Flash, beating rivals on agents','www.reuters.com'),1)
        self.assertIsNone(attention.story_query('AI news'))
        retold=b'''<rss><channel>
          <item><title>Meta removes language that raised scraping and AI training concerns - TechCrunch</title><source>TechCrunch</source></item>
          <item><title>Meta removes controversial scraping language from its commercial terms - Crypto Briefing</title><source>Crypto Briefing</source></item>
          <item><title>Meta posts record quarterly revenue - CNBC</title><source>CNBC</source></item></channel></rss>'''
        self.assertEqual(attention.parse_gnews(retold,'Meta removes language that may have allowed it to scrape news sites'),2)

    def test_most_read_list_is_found_after_its_heading(self):
        page=b'''<nav><ul><li><a href="/about">About</a></li></ul></nav><h2>Most<br>Popular</h2><div><ol>
          <li><a href="/ai/1013195/ai-agents-blocked-web">One</a></li><li><a href="https://www.theverge.com/tech/1013453/tiktok-bans">Two</a></li>
          <li><a href="/tech/1013320/bose-earbuds-review">Three</a></li><li><a href="https://elsewhere.com/x/very-long-path">X</a></li></ol></div>'''
        links=attention.parse_most_read(page,'https://www.theverge.com/ai-artificial-intelligence')
        self.assertEqual(links,['https://www.theverge.com/ai/1013195/ai-agents-blocked-web','https://www.theverge.com/tech/1013453/tiktok-bans',
                                'https://www.theverge.com/tech/1013320/bose-earbuds-review'])
        with self.assertRaises(RuntimeError):attention.parse_most_read(b'<h2>Latest</h2><ul><li><a href="/a">a</a></li></ul>','https://x.com/')

    def test_refresh_spends_budget_on_lookups_and_respects_ttl(self):
        article=dict(hype_repo(0),source_id='techcrunch',category='news',content_kind=None,hype_origin=None,metrics=[],
                     title='DeepSeek releases V4.1 Flash with faster agents',url='https://techcrunch.com/2026/10/17/deepseek/',
                     article_url='https://techcrunch.com/2026/10/17/deepseek/')
        http=FakeHTTP({'https://hn.algolia.com/':json.dumps({'hits':[{'url':'https://techcrunch.com/2026/10/17/deepseek','points':321,'num_comments':40}]}).encode(),
                       'https://news.google.com/':b'<rss><channel><item><title>DeepSeek releases V4.1 Flash with faster agents - Reuters</title><source>Reuters</source></item></channel></rss>',
                       'https://api.github.com/':json.dumps({'stargazers_count':4200}).encode()})
        cfg=dict(CFG,attention=dict(CFG.get('attention',{}),most_read={}))
        result=attention.refresh([article,hype_repo(4000)],cfg,NOW,store=self.store,http=http)
        readings=self.store.snapshot(NOW)
        self.assertEqual(readings['techcrunch.com/2026/10/17/deepseek']['hn_points'],321)
        self.assertEqual(readings['techcrunch.com/2026/10/17/deepseek']['news_outlets'],1)
        self.assertEqual(readings['github.com/owner/tool']['github_stars'],4200)
        self.assertEqual(result['lookups']['gnews'],1)
        calls=len(http.urls)
        attention.refresh([article],cfg,NOW+60,store=self.store,http=http)
        self.assertEqual(len(http.urls),calls)
        # A fresh story is checked again within the hour; an older one waits for the regular TTL.
        attention.refresh([article],cfg,NOW+2800,store=self.store,http=http)
        self.assertEqual(len(http.urls),calls+2)
        old=dict(article,created_utc=NOW-20*3600)
        attention.refresh([old],cfg,NOW+2800+2800,store=self.store,http=http)
        self.assertEqual(len(http.urls),calls+2)

    def test_attention_combines_families_without_double_counting(self):
        repo=hype_repo(5000)
        parts=lambda readings:next(c for c in signals.score(repo,[repo],NOW,CFG,readings=readings)[1] if c['id']=='attention')
        self.assertEqual(parts({})['points'],0)
        growth=parts({'github.com/owner/tool':{'github_stars_day':1000}})
        self.assertAlmostEqual(growth['points'],25)
        self.assertEqual(growth['channels'][0]['family'],'platform')
        # Two channels of one family keep the best; another family adds the remaining share.
        both=parts({'github.com/owner/tool':{'github_stars_day':30,'comments':30}})
        alone=[parts({'github.com/owner/tool':{channel:30}})['points'] for channel in ('github_stars_day','comments')]
        self.assertEqual(both['points'],max(alone))
        one=parts({'github.com/owner/tool':{'github_stars_day':30}})
        discussed=parts({'github.com/owner/tool':{'github_stars_day':30},'github.com/Owner/Tool':{'hn_points':300}})
        self.assertAlmostEqual(discussed['points'],25)
        self.assertEqual(discussed['momentum'],discussed['points'])


if __name__=='__main__':unittest.main()

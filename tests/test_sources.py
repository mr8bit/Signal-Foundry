from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import unittest
from unittest.mock import patch
import bot
from sources import article_url, parse_site_feed, parse_newsroom

CFG = json.loads(Path('config.json').read_text())
SOURCE = dict(id='test',name='Test AI',url='https://example.com/feed',category='news',filter_mode='keywords')
NOW = datetime(2026,10,18,12,tzinfo=timezone.utc).timestamp()
RSS = b'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
<title>New AI model release</title><link>https://example.com/story?utm_source=rss</link>
<pubDate>Sun, 18 Oct 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>Benchmark details &amp; measurements.</p><a href="https://github.com/org/tool">Code</a><script>bad()</script>]]></description>
</item><item><title>Undated</title><link>https://example.com/undated</link></item></channel></rss>'''
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>New research paper</title>
<link rel="self" href="https://example.com/api"/><link href="/article"/>
<published>2026-10-18T09:00:00Z</published><updated>2026-10-19T00:00:00Z</updated>
<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Research <b>results</b></p><a href="/paper">Paper</a></div></content>
</entry></feed>'''


class Sources(unittest.TestCase):
    def test_rss_html_dates_and_undated_entries(self):
        posts = parse_site_feed(RSS,SOURCE)
        self.assertEqual(len(posts),1)
        p=posts[0]
        self.assertEqual(p['article_url'],'https://example.com/story')
        self.assertIn('measurements',p['selftext'])
        self.assertNotIn('bad()',p['selftext'])
        self.assertIn('https://github.com/org/tool',p['links'])
        self.assertIsNone(bot.reject(p,CFG,NOW))
        self.assertEqual(bot.reject(dict(p,created_utc=NOW-49*3600),CFG,NOW),'age')

    def test_google_news_search_feed_names_the_real_outlet(self):
        feed=b'''<rss version="2.0"><channel><item><title>Gartner says agent costs will rise fivefold - VentureBeat</title>
<link>https://news.google.com/rss/articles/CBMiabc?oc=5</link><pubDate>Fri, 16 Oct 2026 19:00:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiabc?oc=5"&gt;Gartner says agent costs&lt;/a&gt; VentureBeat</description>
<source url="https://venturebeat.com">VentureBeat</source></item></channel></rss>'''
        [item]=parse_site_feed(feed,SOURCE)
        self.assertEqual(item['title'],'Gartner says agent costs will rise fivefold')
        self.assertEqual((item['publisher_host'],item['selftext'],item['links'],item['source_name']),('venturebeat.com','',[],'VentureBeat'))
        import signals
        self.assertEqual(signals.publisher(item),'venturebeat.com')
        # An ordinary feed item is untouched.
        self.assertNotIn('publisher_host',parse_site_feed(RSS,SOURCE)[0])

    def test_atom_xhtml_relative_links_and_publication_date(self):
        p=parse_site_feed(ATOM,SOURCE)[0]
        self.assertEqual(p['url'],'https://example.com/article')
        self.assertEqual(p['selftext'],'Research results\nPaper')
        self.assertIn('https://example.com/paper',p['links'])
        self.assertEqual(p['created_utc'],datetime(2026,10,18,9,tzinfo=timezone.utc).timestamp())

    def test_site_filters_and_format(self):
        p=parse_site_feed(RSS,SOURCE)[0]
        self.assertEqual(bot.reject(dict(p,title='Gardening tips',selftext='Plants'),CFG,NOW),'no keyword')
        self.assertIsNone(bot.reject(dict(p,title='News',filter_mode='all'),CFG,NOW))
        self.assertEqual(bot.reject(dict(p,title='AI sponsored giveaway'),CFG,NOW),'excluded keyword')
        rendered=bot.render(p)
        self.assertIn('Read article',rendered)
        self.assertIn('Test AI',rendered)
        self.assertNotIn('r/',rendered)
        self.assertNotIn('reddit.com',rendered)

    def test_url_tracking_dedup_and_lab_preference(self):
        self.assertEqual(article_url('https://example.com/story/?utm_medium=x&id=5#top'),'https://example.com/story?id=5')
        p=parse_site_feed(RSS,SOURCE)[0]
        q=dict(p,id='other',article_url='https://example.com/story?utm_campaign=abc',category='lab')
        groups=bot.select([p,q],CFG,NOW)
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0][0]['category'],'lab')
        different=dict(p,id='third',title='Different AI release',article_url='https://other.com/new',url='https://other.com/new',links=[])
        self.assertNotEqual(bot.keys(different),bot.keys(p))

    def test_anthropic_newsroom(self):
        data=b'''<a href="/news/test"><time>Oct 18, 2026</time><span class="PublicationList__subject">Announcements</span><span class="PublicationList__title">Claude release</span></a>
        <a href="/claude-demo"><h4>New Claude</h4><time>Oct 17, 2026</time><p>Model details</p></a>'''
        source=dict(SOURCE,id='anthropic',url='https://www.anthropic.com/news')
        posts=parse_newsroom(data,source)
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0]['title'],'Claude release')
        self.assertEqual(posts[1]['selftext'],'Model details')
        self.assertEqual(posts[1]['url'],'https://www.anthropic.com/claude-demo')
        with self.assertRaises(RuntimeError):
            parse_newsroom(b'<html>Changed</html>',source)

    def test_source_failure_does_not_stop_collection(self):
        cfg=copy.deepcopy(CFG)
        cfg['rss_groups']=[]
        cfg['feeds']=[SOURCE,dict(SOURCE,id='other',name='Other')]
        client=bot.RedditRSS()
        with patch.object(client,'get',side_effect=[RuntimeError('HTTP 403'),parse_site_feed(RSS,SOURCE)]),patch('bot.time.sleep'):
            posts=client.fetch(cfg,NOW)
        self.assertEqual(len(posts),1)
        self.assertEqual(len(client.errors),1)
        self.assertEqual(client.statuses[1]['status'],'ok')


if __name__=='__main__':
    unittest.main()

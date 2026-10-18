import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
import articles
import bot

TEXT = 'A new model is available. It processes requests using less memory, with benchmark results and deployment instructions. '
PAGE = ('<html><nav><p>Navigation</p></nav><div class="entry-content wp-block-post-content">'
        '<p>'+TEXT+'<a href="https://example.test">Read more</a>.</p>'
        '<div class="newsletter"><p>Subscribe for promotions</p></div>'
        '<aside><p>Advertisement</p></aside><script>evil()</script>'
        '<p>'+TEXT+'<br/>More details.</p></div><footer><p>Copyright footer</p></footer></html>').encode()


class Articles(unittest.TestCase):
    def test_body_excludes_non_article_and_preserves_paragraphs(self):
        text = articles.parse_article(PAGE)
        self.assertIn('Read more',text)
        self.assertIn('\n\n',text)
        for noise in ('Navigation','Subscribe','Advertisement','evil','Copyright'):
            self.assertNotIn(noise,text)
        with self.assertRaises(RuntimeError):
            articles.parse_article(b'<html><p>Blocked</p></html>')

    def test_disk_cache_survives_new_fetcher(self):
        url='https://techcrunch.com/2026/10/17/example/'
        response=MagicMock()
        response.__enter__.return_value=response
        response.geturl.return_value=url
        response.read.return_value=PAGE
        with tempfile.TemporaryDirectory() as directory:
            with patch('articles.urlopen',return_value=response) as fetch:
                first=articles.ArticleFetcher(directory).get(url,'test-agent')
                self.assertEqual(fetch.call_count,1)
            with patch('articles.urlopen') as fetch:
                second=articles.ArticleFetcher(directory).get(url,'test-agent')
                fetch.assert_not_called()
            self.assertEqual(first,second)

    def test_failure_keeps_rss_and_render_prefers_article(self):
        cfg=json.loads(Path('config.json').read_text())
        now=1792324800
        p=dict(id='test',source='site',source_id='techcrunch',source_name='TechCrunch AI',category='news',
               title='AI model release',created_utc=now-100,article_url='https://techcrunch.com/example',url='https://techcrunch.com/example',
               selftext='Short RSS teaser',filter_mode='keywords',links=[],score=None,feed_rank=0)
        client=bot.RedditRSS()
        with patch.object(client.articles,'get',side_effect=RuntimeError('HTTP 403')):
            result=client.enrich([dict(p)],cfg,now)[0]
            self.assertEqual(result['selftext'],'Short RSS teaser')
            self.assertNotIn('article_text',result)
        with patch.object(client.articles,'get',return_value=TEXT*3):
            result=client.enrich([dict(p)],cfg,now)[0]
            self.assertEqual(result['text_origin'],'article')
            self.assertIn('benchmark results',bot.render(result))
            self.assertNotIn('Short RSS teaser',bot.render(result))

    def test_network_failure_and_source_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher=articles.ArticleFetcher(directory)
            with patch('articles.urlopen',side_effect=URLError('timeout')) as fetch:
                with self.assertRaises(RuntimeError):
                    fetcher.get('https://techcrunch.com/story','test')
                with self.assertRaisesRegex(RuntimeError,'cooling'):
                    fetcher.get('https://techcrunch.com/story','test')
                self.assertEqual(fetch.call_count,1)
            with self.assertRaisesRegex(RuntimeError,'not enabled'):
                fetcher.get('https://other.test/story','test')

    def test_rss_full_content_is_preferred_over_teaser(self):
        from sources import parse_site_feed
        xml=b'''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>AI release</title><link>https://example.test/article</link><pubDate>Sat, 17 Oct 2026 12:00:00 GMT</pubDate><description>Short teaser</description><content:encoded><![CDATA[<p>Longer article body with results.</p>]]></content:encoded></item></channel></rss>'''
        source=dict(id='test',name='Test',url='https://example.test/feed',category='news')
        p=parse_site_feed(xml,source)[0]
        self.assertEqual(p['selftext'],'Longer article body with results.')


if __name__=='__main__':
    unittest.main()

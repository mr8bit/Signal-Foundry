import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bot

CFG = json.loads(Path('config.json').read_text())
NOW = datetime.fromisoformat('2026-10-18T12:00:00+02:00').timestamp()


def post(**changes):
    p = dict(id='one', subreddit='LocalLLaMA', title='Qwen released open weights benchmark',
             score=350, created_utc=NOW - 3600, link_flair_text='New Model',
             url='https://huggingface.co/Qwen/Model', selftext='Benchmark 50 tok/s, 24 GB VRAM',
             permalink='/r/LocalLLaMA/comments/one/test/')
    p.update(changes)
    return p


class Rules(unittest.TestCase):
    def test_thresholds(self):
        for sub, threshold in CFG['subreddits'].items():
            title = '[R] Qwen released benchmark'
            self.assertIsNone(bot.reject(post(subreddit=sub, score=threshold, title=title), CFG, NOW))
            self.assertEqual(bot.reject(post(subreddit=sub, score=threshold-1, title=title), CFG, NOW), 'score')

    def test_age_windows(self):
        p = post(created_utc=NOW - 47 * 3600)
        self.assertIsNone(bot.reject(p, CFG, NOW))
        monday = datetime.fromisoformat('2026-10-19T12:00:00+02:00').timestamp()
        self.assertEqual(bot.reject(post(created_utc=monday - 25*3600), CFG, monday), 'age')
        self.assertEqual(bot.reject(post(created_utc=NOW-49*3600), CFG, NOW), 'age')

    def test_flairs_tags_and_gates(self):
        self.assertEqual(bot.reject(post(link_flair_text='Discussion'), CFG, NOW), 'flair')
        self.assertEqual(bot.reject(post(subreddit='MachineLearning', title='[D] Qwen release'), CFG, NOW), 'excluded keyword')
        self.assertEqual(bot.reject(post(subreddit='ClaudeAI', title='New API pricing'), CFG, NOW), 'model/release gate')
        self.assertIsNone(bot.reject(post(subreddit='ClaudeAI', title='Claude API pricing'), CFG, NOW))

    def test_words(self):
        for term in CFG['exclude']:
            self.assertEqual(bot.reject(post(title='Qwen released ' + term), CFG, NOW), 'excluded keyword')
        self.assertFalse(bot.matches('dramatic capital landscape', ['AMA', 'API', 'MCP']))
        self.assertTrue(bot.matches('Qwen3 and GPT-5 agents', ['Qwen']))

    def test_dedup_highest_and_transitive(self):
        a = post(id='a', url='https://huggingface.co/a/model?download=true', selftext='', score=400)
        b = post(id='b', url='https://github.com/org/repo/tree/main', selftext='', score=450)
        c = post(id='c', url='https://huggingface.co/a/model', selftext='https://github.com/org/repo', score=500)
        groups = bot.select([a,b,c], CFG, NOW)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0]['id'], 'c')
        self.assertIn('reddit:a', groups[0][1])
        self.assertEqual(bot.canonical('https://github.com/Org/Repo.git?x=1'), 'github.com/org/repo')

    def test_priority(self):
        self.assertEqual(bot.priority(post()), 1)
        self.assertEqual(bot.priority(post(title='Local Llama 50 tok/s', url='', selftext='24 GB VRAM')), 2)
        self.assertEqual(bot.priority(post(title='New MCP agent tool', url='https://github.com/org/tool', selftext='')), 3)
        self.assertEqual(bot.priority(post(title='New arXiv paper', url='', selftext='')), 4)

    def test_render_escapes(self):
        self.assertIn('&lt;script&gt;', bot.render(post(title='<script>')))
        self.assertNotIn('<script>', bot.render(post(title='<script>')))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = bot.database(self.temp.name + '/state.db')
        self.env = patch.dict('os.environ', TELEGRAM_BOT_TOKEN='test', TELEGRAM_CHAT_ID='test')
        self.env.start()

    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.temp.cleanup()

    def test_sent_survives_reopen_and_spacing(self):
        groups = [(post(), bot.keys(post()))]
        with patch('bot.request_json', return_value={'ok': True, 'result': {'message_id': 12}}) as send:
            bot.publish(self.db, groups, CFG)
            self.db.close()
            self.db = bot.database(self.temp.name + '/state.db')
            bot.publish(self.db, groups, CFG)
            other = post(id='two', url='https://huggingface.co/another/model')
            bot.publish(self.db, [(other, bot.keys(other))], CFG)
            self.assertEqual(send.call_count, 1)

    def test_timeout_does_not_retry_ambiguously(self):
        groups = [(post(), bot.keys(post()))]
        with patch('bot.request_json', side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError):
                bot.publish(self.db, groups, CFG)
        with patch('bot.request_json') as send:
            with self.assertRaisesRegex(RuntimeError, 'pending'):
                bot.publish(self.db, groups, CFG)
            send.assert_not_called()

    def test_later_duplicate_adds_aliases(self):
        original = post()
        with patch('bot.request_json', return_value={'ok': True, 'result': {'message_id': 12}}) as send:
            bot.publish(self.db, [(original, bot.keys(original))], CFG)
            duplicate = post(id='duplicate', selftext='https://github.com/org/new-tool')
            bot.publish(self.db, [(duplicate, bot.keys(duplicate))], CFG)
            third = post(id='third', url='https://github.com/org/new-tool', selftext='')
            bot.publish(self.db, [(third, bot.keys(third))], CFG)
            self.assertEqual(send.call_count, 1)
            self.assertIsNotNone(self.db.execute('SELECT 1 FROM aliases WHERE key=?', ('reddit:third',)).fetchone())

    def test_fetch_weekend_uses_top_week(self):
        cfg = copy.deepcopy(CFG)
        cfg['rss_groups'] = [['LocalLLaMA']]
        cfg['feeds'] = []
        with patch.object(bot.RedditRSS, 'get', return_value=[post(source='rss', score=None)]) as get:
            result = bot.RedditRSS().fetch(cfg, NOW)
            self.assertEqual(len(result), 1)
            self.assertIn('/top/.rss?', get.call_args.args[0])
            self.assertIn('t=week', get.call_args.args[0])


class RSS(unittest.TestCase):
    def sample(self):
        return bot.parse_feed(Path('tests/fixtures/reddit.atom').read_bytes())[0]

    def test_parse_links_text_date(self):
        p = self.sample()
        self.assertEqual(p['id'], 'rssdemo')
        self.assertEqual(p['source'], 'rss')
        self.assertIsNone(p['score'])
        self.assertEqual(len(p['links']), 2)
        self.assertNotIn('submitted by', p['selftext'])
        self.assertNotIn('[comments]', p['selftext'])
        self.assertNotIn('<p>', p['selftext'])
        self.assertIn('github.com/example/inference', bot.keys(p))
        self.assertIsNone(bot.reject(p, CFG, NOW))

    def test_rss_still_filters_keywords_age_and_tags(self):
        p = self.sample()
        self.assertEqual(bot.reject(dict(p, title='Qwen help'), CFG, NOW), 'excluded keyword')
        self.assertEqual(bot.reject(dict(p, created_utc=NOW-49*3600), CFG, NOW), 'age')
        self.assertEqual(bot.reject(dict(p, subreddit='MachineLearning'), CFG, NOW), 'tag')

    def test_dedupe_without_score(self):
        p = self.sample()
        q = dict(p, id='other', feed_rank=4)
        groups = bot.select([q,p], CFG, NOW)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0]['id'], p['id'])

    def test_news_format_and_untrusted_html(self):
        rendered = bot.render(self.sample())
        self.assertIn('Релиз модели', rendered)
        self.assertIn('<b>Qwen', rendered)
        self.assertIn('Hugging Face</a>', rendered)
        self.assertIn('GitHub</a>', rendered)
        self.assertNotIn('↑', rendered)
        self.assertNotIn('submitted by', rendered)
        parser = bot.FeedHTML()
        parser.feed('<script>bad</script><p>Safe &amp; useful</p>')
        self.assertEqual(parser.text(), 'Safe & useful')
        self.assertNotIn('javascript:', bot.render(dict(self.sample(), url='javascript:bad', links=[])))

    def test_malformed_feed(self):
        for data in (b'<html>blocked</html>', b'not XML'):
            with self.assertRaises(RuntimeError):
                bot.parse_feed(data)

    def test_ttl_cache_and_conditional_revalidation(self):
        from urllib.error import HTTPError
        from io import BytesIO
        from unittest.mock import MagicMock
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            client = bot.RedditRSS(cache_path=directory+'/http.db',cache_policy={'min_host_interval_seconds':0})
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = Path('tests/fixtures/reddit.atom').read_bytes()
            response.headers = {'ETag': 'v1'}
            with patch('http_cache.time.time',return_value=1000),patch('bot.urlopen', return_value=response):
                first = client.get('https://example.test/feed')
            with patch('http_cache.time.time',return_value=1100),patch('bot.urlopen') as get:
                self.assertEqual(client.get('https://example.test/feed'),first)
                get.assert_not_called()
            with patch('http_cache.time.time',return_value=2000),patch('bot.urlopen', side_effect=HTTPError('https://example.test/feed',304,'unchanged',{},BytesIO())) as get:
                self.assertEqual(client.get('https://example.test/feed'), first)
                self.assertEqual(get.call_args.args[0].get_header('If-none-match'), 'v1')
                self.assertEqual(client.last_cache_info.state,'revalidated')


if __name__ == '__main__':
    unittest.main()

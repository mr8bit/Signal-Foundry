from datetime import datetime,timezone
import copy
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
import bot
import dashboard
import digest
from cloud_news import cloud_decision,model_keys,prepare_cloud_posts
from sources import parse_site_feed

CFG=json.loads(Path('config.json').read_text())
NOW=datetime(2026,10,18,12,tzinfo=timezone.utc).timestamp()
BY_RULE={s['cloud_rule']:s for s in CFG['feeds'] if s.get('cloud_rule')}


def post(title,rule=None,**changes):
    source=BY_RULE[rule] if rule else next(s for s in CFG['feeds'] if s['id']=='techcrunch')
    p=dict(id=source['id']+title,source='site',source_id=source['id'],source_name=source['name'],category=source['category'],
           title=title,selftext='',links=[],url='https://example.com/'+source['id'],article_url='https://example.com/'+source['id'],
           created_utc=NOW-3600,feed_rank=0,score=None,filter_mode=source.get('filter_mode','all'))
    p.update({k:source[k] for k in ('cloud_rule','cloud_provider','cloud_label') if k in source});p.update(changes)
    return p


class CloudNews(unittest.TestCase):
    def test_all_twelve_feeds_have_distinct_rules_and_polite_intervals(self):
        self.assertEqual(len(BY_RULE),12)
        self.assertEqual(len({s['url'] for s in BY_RULE.values()}),12)
        self.assertTrue(all(s['enabled'] and s['poll_seconds']>=1800 for s in BY_RULE.values()))

    def test_source_gates_and_global_exclusions(self):
        denied=[('How Acme built AI agents with Bedrock','aws_ml'),('Introducing Bedrock in new regions','aws_whats_new'),
                ('Amazon Bedrock is now available in Europe (Spain) Region','aws_whats_new'),
                ('Introducing Azure AI strategy for tomorrow','azure_blog'),('Webinar: Introducing AI SDK','vercel'),
                ('Amazon Bedrock achieves ISO 27001 compliance','aws_whats_new'),
                ('[Preview] New OpenAI model','azure_updates'),('Introducing a new storage tier','aws_news'),
                ('Amazon S3 is now available on the Vercel Marketplace and v0 by Vercel','aws_news'),
                ('AWS cost cut of 50% for S3 storage','aws_news')]
        for title,rule in denied:
            self.assertEqual(cloud_decision(post(title,rule),CFG).mode,'drop',title)
        self.assertEqual(cloud_decision(post("What's new — October",'foundry'),CFG).mode,'standalone')

    def test_sdk_only_stable_major_ai_package(self):
        for title in ('ai@6.0.1','ai@6.1.0','ai@7.0.0-beta.1','@ai-sdk/openai@3.0.0'):
            self.assertEqual(cloud_decision(post(title,'vercel_sdk'),CFG).mode,'drop')
        self.assertEqual(cloud_decision(post('ai@7.0.0','vercel_sdk'),CFG).mode,'standalone')
        six=post('ai@6.0.0','vercel_sdk',article_url='https://github.com/vercel/ai/releases/tag/ai@6.0.0')
        seven=post('ai@7.0.0','vercel_sdk',article_url='https://github.com/vercel/ai/releases/tag/ai@7.0.0')
        self.assertEqual(len(bot.select([six,seven],CFG,NOW)),2)

    def test_owned_models_platforms_hardware_and_quantified_price_cuts(self):
        allowed=[('Introducing Amazon Nova 3','aws_ml','model'),('Cloudflare releases Clef 2','cloudflare','model'),
                 ('Amazon Bedrock AgentCore is now available','aws_ml','agent_platform'),
                 ('Introducing new Trainium3 AI instances','aws_news','hardware'),
                 ('Introducing new TPU AI hardware','gcloud_blog','hardware'),
                 ('Bedrock cuts inference prices by 50%','aws_whats_new','pricing')]
        for title,rule,kind in allowed:
            d=cloud_decision(post(title,rule),CFG)
            self.assertEqual((d.mode,d.kind),('standalone',kind),title)
        self.assertEqual(cloud_decision(post('Bedrock cuts prices','aws_whats_new',selftext='A model reaches 90% on the benchmark.'),CFG).mode,'drop')

    def test_ga_and_model_notes_without_preview(self):
        self.assertEqual(cloud_decision(post('Gemini 4 Pro generally available','gemini_enterprise'),CFG).mode,'availability')
        self.assertEqual(cloud_decision(post('Gemini 4 Pro preview','gemini_enterprise'),CFG).mode,'drop')
        self.assertEqual(cloud_decision(post('October 18, 2026','vertex_notes',selftext='Gemini 4 Pro is now generally available in Vertex AI.'),CFG).mode,'availability')
        self.assertEqual(cloud_decision(post('[Launched] OpenAI GPT-5 model now available','azure_updates'),CFG).mode,'availability')
        self.assertEqual(cloud_decision(post('Beam model by Reflection is now available on Amazon SageMaker JumpStart and AWS Trainium','aws_news'),CFG).mode,'availability')

    def test_exact_version_and_variant_model_matching(self):
        self.assertEqual(model_keys('Claude Sonnet 4.5'),model_keys('claude-4.5-sonnet'))
        self.assertNotEqual(model_keys('Gemini 3 Pro'),model_keys('Gemini 3 Flash'))
        self.assertNotEqual(model_keys('GPT-5.1'),model_keys('GPT-5.2'))
        self.assertEqual(model_keys('FLUX.3'),model_keys('FLUX-3'))
        self.assertNotEqual(model_keys('Gemini 4 Argon'),model_keys('Gemini 4 Unrelated'))
        self.assertNotEqual(model_keys('DeepSeek-V4.1-Flash-1017'),model_keys('DeepSeek-V4.1-Flash'))

    def test_availability_attaches_without_a_new_story_or_changed_release_date(self):
        release=post('Qwen 3.5 released open weights',url='https://huggingface.co/Qwen/Qwen3.5',content_kind='model',
                     source_id='hf_models',source_name='Hugging Face Models',category='model',filter_mode='all')
        update=post('Qwen 3.5 now available on AI Gateway','vercel',created_utc=NOW-60)
        original=copy.deepcopy([release,update])
        data=dashboard.dataset([release,update],CFG,[],NOW)
        self.assertEqual(len(data['items']),1)
        self.assertEqual(data['items'][0]['published_at'],release['created_utc'])
        self.assertEqual(data['items'][0]['cloud_availability'][0]['label'],'AI Gateway')
        self.assertEqual(len(data['signal_rankings']['models']),1)
        self.assertEqual(len(data['signal_rankings']['cloud']),1)
        self.assertEqual([release,update],original)
        groups=bot.select([release,update],CFG,NOW)
        self.assertEqual(len(groups),1);self.assertIn('Available on:',bot.render(groups[0][0]))

    def test_unmatched_updates_are_pending_not_standalone_posts(self):
        update=post('Claude Sonnet 4.5 now available on AI Gateway','vercel')
        wrong=post('Claude Opus 4.5 released')
        stories,pending=prepare_cloud_posts([wrong,update],CFG,NOW)
        self.assertEqual(len(stories),1);self.assertEqual(len(pending),1)
        self.assertEqual(bot.select([update],CFG,NOW),[])

    def test_old_model_newly_trending_can_keep_earlier_cloud_availability(self):
        release=post('Qwen 3.5',source_id='hf_models',content_kind='model',category='model',filter_mode='all',
                     original_published_at=NOW-60*86400,created_utc=NOW-60)
        update=post('Qwen 3.5 now available on AI Gateway','vercel',created_utc=NOW-30*86400)
        stories,pending=prepare_cloud_posts([release,update],CFG,NOW)
        self.assertFalse(pending);self.assertEqual(len(stories[0]['cloud_availability']),1)

    def test_known_huggingface_model_link_matches_an_unrecognised_model_name(self):
        url='https://huggingface.co/org/UnusualName'
        release=post('org/UnusualName',source_id='hf_models',content_kind='model',category='model',url=url)
        update=post('New models now available on AI Gateway','vercel',links=[url])
        stories,pending=prepare_cloud_posts([release,update],CFG,NOW)
        self.assertFalse(pending);self.assertEqual(len(stories[0]['cloud_availability']),1)

    def test_disabled_sources_never_attach_and_formats_keep_where_to_get(self):
        release=post('Claude Sonnet 4.5 released')
        update=post('Claude Sonnet 4.5 now available on AI Gateway','vercel')
        data=dashboard.dataset([release,update],CFG,[],NOW);item=data['items'][0]
        self.assertIn('Available on:',digest.markdown(data,now=NOW))
        self.assertIn('cloud_availability',digest.signals_json(data,now=NOW))
        self.assertIn('Available on:',digest.static_card(item))
        with patch('dashboard.time.time',return_value=NOW):
            rss=ET.fromstring(dashboard.rss(data));self.assertIsNotNone(rss.find('./channel/item/{urn:ai-signal:feed:1}cloud_availability'))
        cfg=copy.deepcopy(CFG)
        next(s for s in cfg['feeds'] if s['id']=='vercel')['enabled']=False
        self.assertEqual(dashboard.dataset([release,update],cfg,[],NOW)['items'][0]['cloud_availability'],[])

    def test_feed_parser_preserves_cloud_policy_metadata(self):
        xml=b'<rss version="2.0"><channel><item><title>ai@7.0.0</title><link>https://github.com/vercel/ai/releases/tag/ai@7.0.0</link><pubDate>Sun, 18 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
        p=parse_site_feed(xml,BY_RULE['vercel_sdk'])[0]
        self.assertEqual(p['cloud_rule'],'vercel_sdk');self.assertIsNone(bot.reject(p,CFG,NOW))

    def test_release_note_date_title_uses_source_headline_without_changing_date(self):
        xml=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>October 18, 2026</title><link href="https://cloud.google.com/notes#oct18"/><published>2026-10-18T10:00:00Z</published><content type="html">&lt;p&gt;Feature&lt;/p&gt;&lt;p&gt;Introducing Gemini Enterprise, now generally available.&lt;/p&gt;</content></entry></feed>'''
        p=parse_site_feed(xml,BY_RULE['gemini_enterprise'])[0]
        self.assertEqual(p['original_title'],'October 18, 2026')
        self.assertIn('Introducing Gemini Enterprise',p['title'])
        self.assertEqual(p['created_utc'],NOW-7200)
        self.assertEqual(cloud_decision(p,CFG).mode,'standalone')

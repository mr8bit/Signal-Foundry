import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dashboard
import signals
import xml.etree.ElementTree as ET

CFG=json.loads(Path('config.json').read_text())
NOW=datetime(2026,10,18,12,tzinfo=timezone.utc).timestamp()


def post(**changes):
    p=dict(id='one',source='site',source_id='techcrunch',source_name='TechCrunch AI',category='news',filter_mode='keywords',
           title='Qwen released open weights benchmark',url='https://example.com/release',article_url='https://example.com/release',
           selftext='50 tok/s on 24 GB VRAM. https://huggingface.co/qwen/model',links=['https://huggingface.co/qwen/model'],
           created_utc=NOW-3600,score=None,feed_rank=0)
    p.update(changes);return p


class Signals(unittest.TestCase):
    def test_enterprise_classification_requires_explicit_business_signals(self):
        for title in ('New enterprise AI agents','LLM API adds SSO and RBAC','On-prem model deployment',
                      'Amazon Bedrock launches new agents','Корпоративная платформа AI для компаний'):
            self.assertTrue(signals.enterprise_reasons(post(title=title,selftext='')),title)
        for title in ('Qwen released open weights','A faster LLM API','Deploy a model on your GPU',
                      'A research paper by Salesforce'):
            self.assertFalse(signals.enterprise_reasons(post(title=title,selftext='')),title)
        self.assertFalse(signals.enterprise_reasons(post(title='LLM API release',selftext='x'*1200+' enterprise')))

    def test_enterprise_body_cues_need_strength_and_case_studies_need_official_sources(self):
        teaser='CUDA C++ is a mature, enterprise-grade toolchain, and more.'
        self.assertFalse(signals.enterprise_reasons(post(title='Introducing CUDA Rust',selftext=teaser+'\n'+teaser)))
        self.assertTrue(signals.enterprise_reasons(post(title='Agents for teams',selftext='Enterprise work spans tools. Most enterprises lack context.')))
        self.assertTrue(signals.enterprise_reasons(post(title='Admin update',selftext='Available for ChatGPT Business workspaces.')))
        case=post(title='Stampli cuts launch hours by 68% using Codex',category='lab',source_id='openai')
        self.assertEqual(signals.enterprise_matches(case),{'Внедрение в компании':'case'})
        self.assertTrue(signals.enterprise_reasons(post(title='Samsung brings ChatGPT to employees',category='lab')))
        self.assertFalse(signals.enterprise_reasons(dict(case,category='community',source='rss')))
        self.assertFalse(signals.enterprise_reasons(post(title='How to build an agent with Claude',category='lab')))

    def test_enterprise_ranking_uses_business_evidence_for_relevance(self):
        lab=lambda id,title,**x:post(**dict(dict(id=id,title=title,selftext='',links=[],article_url='https://openai.com/'+id,
                                     category='lab',source_id='openai',source_name='OpenAI News',filter_mode='all'),**x))
        product=lab('a','ChatGPT Business adds premium seats')
        body=lab('c','Agents for teams',selftext='Enterprise work spans tools. Most enterprises lack context.')
        news=post(id='n',title='Wayfair boosts catalog accuracy with OpenAI',selftext='',links=[],
                  article_url='https://theregister.com/wayfair',source_id='register',source_name='The Register',category='news')
        first=lab('b','Proaction boosts sales with Codex',created_utc=NOW-3*86400)
        second=lab('d','How Cooley is accelerating IPO work with ChatGPT',created_utc=NOW-86400)
        later=lab('e','Lowe’s answers questions with OpenAI',created_utc=NOW-20*86400)
        # Daily stream: the case from day -3 keeps full weight, days -2 and -1 are the series.
        third=lab('f','Wayfair cuts support time with OpenAI',created_utc=NOW-2*86400)
        ranked=dashboard.dataset([product,body,news,first,second,later,third],CFG,[],NOW)['signal_rankings']['enterprise']
        relevance={x['title']:next(c['points'] for c in x['components'] if c['id']=='relevance') for x in ranked}
        self.assertEqual(relevance,{product['title']:30,news['title']:24,first['title']:18,later['title']:18,
                                    second['title']:12,third['title']:12,body['title']:18})
        reasons={x['title']:x['reasons'][0] for x in ranked}
        self.assertEqual(reasons[first['title']],'Кейс клиента в блоге вендора')
        self.assertEqual(reasons[second['title']],'Серия кейсов OpenAI News: 3-й за 7 дней')
        self.assertEqual(ranked[0]['reasons'][0],'Корпоративная тема в заголовке')

    def test_vendor_case_series_keeps_one_full_weight_story_per_week(self):
        cases=[post(id=str(d),title=f'Company{d} cuts costs with Codex',category='lab',source_id='openai',created_utc=NOW-d*86400)
               for d in range(20)]
        series=signals.enterprise_series(cases)
        full=sorted(round((NOW-t)/86400) for t in series['openai'][1])
        self.assertEqual(full,[3,11,19])
        self.assertEqual(sum(signals.enterprise_relevance([c],series)[0]==18 for c in cases),3)

    def test_case_studies_need_an_outcome_and_vendor_product_titles_are_not_cases(self):
        for title in ('Introducing agentic video understanding with Gemini','Intelligent transcription with Gemini 3.5'):
            self.assertNotIn('Внедрение в компании',signals.enterprise_reasons(post(title=title,category='lab')),title)
        case=post(title='The Den frees up 10 hours a week with ChatGPT Work',category='lab',source_id='openai',filter_mode='all')
        self.assertEqual(signals.enterprise_relevance([case])[0],18)

    def test_enterprise_is_an_overlapping_collection_and_keeps_explanations(self):
        corporate=post(id='business',title='Enterprise LLM API adds SSO',selftext='',links=[],
                       article_url='https://example.com/business')
        generic=post(id='generic',title='Qwen model release',selftext='',links=[],
                     article_url='https://example.com/generic')
        data=dashboard.dataset([corporate,generic],CFG,[],NOW)
        ranked=data['signal_rankings']['enterprise']
        self.assertEqual(len(ranked),1);self.assertEqual(ranked[0]['title'],corporate['title'])
        self.assertIn('enterprise',ranked[0]['signal_categories'])
        self.assertIn('media',ranked[0]['signal_categories'])
        self.assertIn('Безопасность и управление',ranked[0]['enterprise_reasons'])
        self.assertNotIn('enterprise',ranked[0]['signal_metrics'])

    def test_category_rankings_keep_membership_and_metrics_after_dedupe(self):
        model=post(source_id='hf_models',category='model',content_kind='model',filter_mode='all',
                   provider_trending_score=123.5,observed_at=NOW-10,
                   metrics=[dict(kind='likes',value=900,label='лайков')])
        article=post(id='article',source_id='openai',source_name='OpenAI News',category='lab')
        media=post(id='media',source_id='techcrunch',category='news')
        items=signals.build_items([model,article,media],CFG,NOW)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['signal_categories'],['media','models'])
        self.assertEqual(items[0]['signal_metrics']['models']['value'],123.5)
        self.assertEqual(items[0]['signal_metrics']['models']['observed_at'],NOW-10)
        self.assertNotIn('media',items[0]['signal_metrics'])

    def test_native_metrics_do_not_substitute_lifetime_stars_or_downloads(self):
        repo=post(category='repository',metrics=[dict(kind='stars',value=100000,label='всего')])
        self.assertEqual(signals.ranking_metadata([repo]),(['repos'],{}))
        model=post(category='model',provider_trending_score=float('nan'),metrics=[
            dict(kind='downloads_month',value=1000000,label='скачиваний'),dict(kind='likes',value=0,label='лайков')])
        categories,metrics=signals.ranking_metadata([model])
        self.assertEqual(categories,['models']);self.assertEqual(metrics['models']['kind'],'likes')
        self.assertEqual(metrics['models']['value'],0)

    def test_category_collection_shows_model_card_instead_of_linking_newsletter(self):
        model=post(id='model',source_id='hf_models',source_name='Hugging Face Models',category='model',content_kind='model',
                   title='Qwen/Qwen3.8-27B',url='https://huggingface.co/qwen/model',
                   article_url='https://huggingface.co/qwen/model',filter_mode='all',provider_trending_score=80)
        newsletter=post(id='digest',source_id='latent_space',category='newsletter',title='Qwen release daily digest')
        media=post(id='media',category='news',title='Qwen released new model')
        data=dashboard.dataset([model,newsletter,media],CFG,[],NOW)
        self.assertEqual(len(data['items']),1)
        ranked=data['signal_rankings']['models']
        self.assertEqual(len(ranked),1);self.assertEqual(ranked[0]['title'],model['title'])
        self.assertEqual(ranked[0]['url'],model['url'])
        self.assertEqual(data['signal_rankings']['media'][0]['title'],media['title'])
        self.assertEqual(data['signal_rankings']['repos'],[])

    def test_components_are_bounded_and_missing_popularity_is_explicit(self):
        p=post();score,parts,reasons=signals.score(p,[p],NOW)
        self.assertTrue(0<=score<=100)
        self.assertEqual(score,round(sum(x['points'] for x in parts),1))
        raw=next(x for x in parts if x['id']=='engagement')
        self.assertFalse(raw['available']);self.assertEqual(raw['points'],0)
        self.assertIn('Есть численные показатели',reasons)
        for x in parts:self.assertTrue(0<=x['points']<=x['max'])

    def test_freshness_decays_and_real_hn_points_are_used(self):
        p=post();fresh=signals.score(p,[p],NOW)[0];old=signals.score(dict(p,created_utc=NOW-48*3600),[p],NOW)[0]
        self.assertGreater(fresh,old)
        hn=post(source_id='hn',selftext='Article URL: https://example.com\nPoints: 233\n# Comments: 75')
        self.assertEqual(signals.raw_points(hn),233)
        self.assertTrue(next(x for x in signals.score(hn,[hn],NOW)[1] if x['id']=='engagement')['available'])

    def test_coverage_does_not_count_reddit_subs_or_google_twice(self):
        p=post(source='rss',subreddit='LocalLLaMA',permalink='/r/LocalLLaMA/comments/one/test/')
        q=post(source='rss',subreddit='LocalLLM',permalink='/r/LocalLLM/comments/two/test/')
        parts=signals.score(p,[p,q],NOW)[1]
        self.assertEqual(next(x for x in parts if x['id']=='coverage')['points'],0)
        self.assertEqual(signals.publisher(post(source_id='google')),signals.publisher(post(source_id='deepmind')))
        official=post(source_id='openai',category='lab',article_url='https://openai.com/news/test')
        parts=signals.score(p,[p,official],NOW)[1]
        self.assertEqual(next(x for x in parts if x['id']=='coverage')['points'],7.5)
        self.assertEqual(next(x for x in parts if x['id']=='primary')['points'],5)

    def test_trend_freshness_uses_original_or_first_seen_date(self):
        old_paper=post(content_kind='paper',date_kind='trend',category='paper',original_published_at=NOW-400*86400,created_utc=NOW)
        new_paper=dict(old_paper,original_published_at=NOW-3600)
        fresh=lambda p:next(x for x in signals.score(p,[p],NOW,CFG)[1] if x['id']=='freshness')['points']
        self.assertLess(fresh(old_paper),0.1);self.assertGreater(fresh(new_paper),24)
        repo=post(content_kind='repository',date_kind='trend',category='repository',created_utc=NOW,first_seen=NOW-72*3600)
        self.assertAlmostEqual(fresh(repo),25/8,places=1)

    def test_items_expose_when_story_became_news(self):
        paper=post(content_kind='paper',date_kind='trend',category='paper',source_id='hf_papers',filter_mode='all',
                   original_published_at=NOW-400*86400,created_utc=NOW-600)
        self.assertEqual(signals.build_items([paper],CFG,NOW)[0]['news_at'],NOW-400*86400)

    def test_model_relevance_and_velocity_popularity(self):
        known=post(content_kind='model',date_kind='trend',title='Qwen/Qwen3.8-27B',provider_trending_score=1000,
                   metrics=[dict(kind='likes',value=5,label='лайков')])
        unknown=dict(known,title='someone/humanizer')
        parts=lambda p:{x['id']:x['points'] for x in signals.score(p,[p],NOW,CFG)[1]}
        self.assertEqual(parts(known)['relevance'],30);self.assertEqual(parts(unknown)['relevance'],24)
        self.assertEqual(parts(known)['engagement'],10)

    def test_news_release_title_and_cross_outlet_story_coverage(self):
        news=post(title='DeepSeek Just Released V4.1-Flash-1017, Scores 77.0 on DeepSWE',selftext='',links=[],
                  article_url='https://marktechpost.com/deepseek',source_id='marktechpost')
        self.assertEqual(signals.priority(news,CFG),1)
        model=post(id='m',title='deepseek-ai/DeepSeek-V4.1-Flash-1017',source_id='hf_models',category='model',content_kind='model',
                   date_kind='trend',url='https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017',
                   article_url='https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017',selftext='',links=[],filter_mode='all')
        verge=post(id='v',title='Meta removes language that may have allowed it to scrape news sites',selftext='AI training terms',links=[],
                   article_url='https://theverge.com/meta',source_id='verge')
        tc=post(id='t',title='Meta removes language that raised scraping and AI training concerns',selftext='',links=[],
                article_url='https://techcrunch.com/meta',source_id='techcrunch')
        other=post(id='o',title='Anthropic opens a new office in Tokyo',selftext='',links=[],article_url='https://wired.com/x',source_id='wired')
        sizes=post(id='s',title='Stepfun Step-5 released: 600B MoE with 1M context',selftext='',links=[],article_url='https://decoder.com/s',source_id='decoder')
        muse=post(id='mu',title='Meta Releases Muse Spark: 816B MoE with 1M context',selftext='',links=[],article_url='https://register.com/m',source_id='register')
        items={x['title']:x for x in signals.build_items([news,model,verge,tc,other,sizes,muse],CFG,NOW)}
        self.assertEqual(items[sizes['title']]['coverage'],1)
        self.assertEqual(items[news['title']]['coverage'],2)
        # The same news from two outlets is now one card; the other article is listed under "also".
        merged=next(x for x in items.values() if x['title'] in (verge['title'],tc['title']))
        self.assertNotIn(tc['title'] if merged['title']==verge['title'] else verge['title'],items)
        self.assertEqual(merged['coverage'],2)
        self.assertEqual(len(merged['also']),1)
        self.assertEqual(items[other['title']]['coverage'],1)

    def test_roundups_do_not_bridge_stories_and_artifacts_represent_groups(self):
        model=lambda id,repo:post(id=id,title=repo,source_id='hf_models',source_name='Hugging Face · Models',category='model',
                                  content_kind='model',date_kind='trend',filter_mode='all',feed_rank=7,url='https://huggingface.co/'+repo,
                                  article_url='https://huggingface.co/'+repo,selftext='',links=[],provider_trending_score=500)
        flux,nemotron=model('f','bfl/flux-klein'),model('n','nvidia/nemotron-bolt')
        roundup=post(id='r',title='[AINews] Episode 1',source_id='latent',source_name='Latent Space',category='newsletter',
                     filter_mode='all',feed_rank=0,article_url='https://latent.space/ep1',selftext='',
                     links=['https://huggingface.co/bfl/flux-klein','https://huggingface.co/nvidia/nemotron-bolt'])
        titles=sorted(x['title'] for x in signals.build_items([flux,nemotron,roundup],CFG,NOW))
        self.assertEqual(titles,['[AINews] Episode 1','bfl/flux-klein','nvidia/nemotron-bolt'])
        single=dict(roundup,links=['https://huggingface.co/bfl/flux-klein'])
        items=signals.build_items([flux,single],CFG,NOW)
        self.assertEqual([x['title'] for x in items],['bfl/flux-klein'])
        self.assertEqual(items[0]['coverage'],2)

    def test_similar_articles_merge_into_one_card_without_false_merges(self):
        n=[0]
        def article(title,source,hours=1,category='news'):
            n[0]+=1
            return post(id=f'a{n[0]}',title=title,source_id=source,source_name=source.title(),category=category,filter_mode='all',
                        article_url=f'https://{source}.example/{n[0]}',url=f'https://{source}.example/{n[0]}',selftext='',links=[],
                        created_utc=NOW-hours*3600)
        deepseek=[article('DeepSeek Just Released V4.1-Flash-1017, Scores 77.0 on DeepSWE','marktechpost',hours=5),
                  article('DeepSeek V4.1 Flash (1017) Now Leads Open-Weight Models','decoder',hours=3),
                  article('DeepSeek-V4.1-Flash-1017 just dropped — this pricing could bring','register',hours=2)]
        super_vl=article('NVIDIA Releases Nemotron 3.5 Super VL, a 120B Open Multimodal Agent Model','marktechpost')
        bolt=article('NVIDIA Nemotron Bolt 3.5 74B A9B releases Oct. 15th','decoder')
        same_blog=[article('[Launched] Generally Available: Enable and disable control of agents','openai',category='lab'),
                   article('[Launched] Generally Available: Publishing Microsoft Foundry agents','openai',category='lab')]
        items=signals.build_items(deepseek+[super_vl,bolt]+same_blog,CFG,NOW)
        titles=[x['title'] for x in items]
        merged=[x for x in items if x['title'].startswith('DeepSeek')]
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['coverage'],3);self.assertEqual(len(merged[0]['also']),2)
        self.assertIn(super_vl['title'],titles);self.assertIn(bolt['title'],titles)
        self.assertEqual(sum(t.startswith('[Launched]') for t in titles),2)
        # The id follows the first report: adding a later article keeps the saved card.
        first=signals.build_items(deepseek[:1],CFG,NOW)[0]['id']
        self.assertEqual(merged[0]['id'],first)

    def test_digests_never_lead_and_short_same_outlet_titles_stay_apart(self):
        digest_issue=post(id='d1',title="Import AI 465: Open vs closed gaps; Kimi K3; Demis' big policy plan",source_id='importai',
                          source_name='Import AI',category='newsletter',filter_mode='all',article_url='https://importai.example/465',
                          selftext='',links=[],created_utc=NOW-7200)
        story=post(id='d2',title='Kimi K3: The open-weights escalation',source_id='interconnects',source_name='Interconnects',
                   category='newsletter',filter_mode='all',article_url='https://interconnects.example/k3',selftext='',links=[],created_utc=NOW-3600)
        self.assertEqual(signals.cluster_lead([digest_issue,story]),story)
        a=post(id='o1',title='ChatGPT for research',source_id='openai',category='lab',article_url='https://openai.com/a',selftext='',links=[])
        b=post(id='o2',title='Research with ChatGPT',source_id='openai',category='lab',article_url='https://openai.com/b',selftext='',links=[])
        self.assertFalse(signals.similar_articles(a,b))

    def test_merge_uses_complete_linkage_and_keeps_artifacts_apart(self):
        a=post(id='x1',title='Meta removes language that may have allowed it to scrape news sites',source_id='verge',
               article_url='https://verge.example/1',selftext='AI',links=[],created_utc=NOW-7200)
        b=post(id='x2',title='Meta removes language that raised scraping and AI training concerns',source_id='techcrunch',
               article_url='https://tc.example/2',selftext='AI',links=[],created_utc=NOW-3600)
        stories=[(a,[a]),(b,[b])]
        self.assertEqual(signals.merge_similar([(p,m,{}) for p,m in stories]),[[0,1]])
        c=post(id='x3',title='Meta language model training concerns rise among news publishers',source_id='wired',
               article_url='https://wired.example/3',selftext='AI',links=[],created_utc=NOW-1800)
        clusters=signals.merge_similar([(p,[p],{}) for p in (a,b,c)])
        self.assertTrue(all(len(cl)<3 for cl in clusters))
        models=[post(id=f'm{i}',title=f'bfl/flux-klein{x}',content_kind='model',category='model',date_kind='trend',
                     source_id='hf_models',article_url=f'https://huggingface.co/bfl/flux-klein{x}',selftext='',links=[]) for i,x in enumerate(('','-comfy'))]
        self.assertEqual(len(signals.merge_similar([(p,[p],{}) for p in models])),2)

    def test_groups_preserve_sources_and_exclude_removed_sources(self):
        p=post();q=post(id='two',source_id='openai',source_name='OpenAI News',category='lab',article_url='https://openai.com/news/example')
        removed=post(id='habr',source_id='habr',links=[],article_url='https://habr.com/test')
        items=signals.build_items([p,q,removed],CFG,NOW)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['coverage'],2)
        self.assertEqual(items[0]['source_id'],'openai')
        self.assertEqual(len(items[0]['sources']),2)

    def test_archive_upsert_preserves_first_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/news.db'
            with patch('dashboard.time.time',return_value=NOW):dashboard.archive_posts([post()],path)
            with patch('dashboard.time.time',return_value=NOW+3600):rows=dashboard.archive_posts([post(selftext='Updated')],path)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['selftext'],'Updated')
            import sqlite3
            with sqlite3.connect(path) as db:
                first,last=db.execute('SELECT first_seen,last_seen FROM news').fetchone()
            self.assertEqual(first,NOW);self.assertEqual(last,NOW+3600)

    def test_rss_is_valid_escaped_and_has_stable_guid_without_fake_metrics(self):
        item=signals.build_items([post(title='Qwen release <script>& benchmark')],CFG,NOW)[0]
        data=dict(generated_at=NOW,items=[item],scoring_version='editorial-v1')
        with patch('dashboard.time.time',return_value=NOW):raw=dashboard.rss(data,'day')
        root=ET.fromstring(raw);entry=root.find('./channel/item')
        self.assertIsNotNone(entry)
        self.assertEqual(entry.findtext('title'),'Qwen release <script>& benchmark')
        self.assertEqual(entry.find('guid').get('isPermaLink'),'false')
        self.assertIsNone(entry.find('{urn:ai-signal:feed:1}raw_score'))
        self.assertIsNotNone(entry.find('{urn:ai-signal:feed:1}editorial_score'))
        with self.assertRaises(ValueError):dashboard.rss(data,'bad')

    def test_year_window_keeps_history_day_window_excludes_old_items(self):
        p=post(created_utc=NOW-100*3600)
        self.assertEqual(len(signals.build_items([p],CFG,NOW)),1)
        self.assertEqual(signals.build_items([p],CFG,NOW,max_hours=24),[])


if __name__=='__main__':unittest.main()

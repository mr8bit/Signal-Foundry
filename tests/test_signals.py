import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dashboard
import digest
import embeddings
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
        self.assertEqual(signals.enterprise_matches(case),{'Business adoption':'case'})
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
        self.assertEqual(reasons[first['title']],'Customer story on a vendor blog')
        self.assertEqual(reasons[second['title']],'Customer story series OpenAI News: 3 in 7 days')
        self.assertEqual(ranked[0]['reasons'][0],'Enterprise topic in the headline')

    def test_vendor_case_series_keeps_one_full_weight_story_per_week(self):
        cases=[post(id=str(d),title=f'Company{d} cuts costs with Codex',category='lab',source_id='openai',created_utc=NOW-d*86400)
               for d in range(20)]
        series=signals.enterprise_series(cases)
        full=sorted(round((NOW-t)/86400) for t in series['openai'][1])
        self.assertEqual(full,[3,11,19])
        self.assertEqual(sum(signals.enterprise_relevance([c],series)[0]==18 for c in cases),3)

    def test_case_studies_need_an_outcome_and_vendor_product_titles_are_not_cases(self):
        for title in ('Introducing agentic video understanding with Gemini','Intelligent transcription with Gemini 3.5'):
            self.assertNotIn('Business adoption',signals.enterprise_reasons(post(title=title,category='lab')),title)
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
        self.assertIn('Security and governance',ranked[0]['enterprise_reasons'])
        self.assertNotIn('enterprise',ranked[0]['signal_metrics'])

    def test_category_rankings_keep_membership_and_metrics_after_dedupe(self):
        model=post(source_id='hf_models',category='model',content_kind='model',filter_mode='all',
                   provider_trending_score=123.5,observed_at=NOW-10,
                   metrics=[dict(kind='likes',value=900,label='likes')])
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
            dict(kind='downloads_month',value=1000000,label='скачиваний'),dict(kind='likes',value=0,label='likes')])
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
        # No attention signal: 0 points, and the breakdown says so rather than inventing one.
        raw=next(x for x in parts if x['id']=='attention')
        self.assertEqual(raw['points'],0);self.assertEqual(raw['channels'],[])
        self.assertIn('Includes measurements',reasons)
        for x in parts:self.assertTrue(0<=x['points']<=x['max'])

    def test_freshness_decays_and_real_hn_points_are_used(self):
        p=post();fresh=signals.score(p,[p],NOW)[0];old=signals.score(dict(p,created_utc=NOW-48*3600),[p],NOW)[0]
        self.assertGreater(fresh,old)
        hn=post(source_id='hn',selftext='Article URL: https://example.com\nPoints: 233\n# Comments: 75')
        self.assertEqual(signals.raw_points(hn),233)
        attention=next(x for x in signals.score(hn,[hn],NOW)[1] if x['id']=='attention')
        self.assertGreater(attention['points'],0);self.assertEqual(attention['channels'][0]['channel'],'native')

    def test_popularity_is_a_percentile_within_the_source(self):
        repo=lambda i,stars:post(id=f'g{i}',source_id='github_trending',source_name='GitHub Trending',category='repository',
                                  content_kind='repository',observed_at=NOW-3600,metrics=[dict(kind='stars_day',value=stars,label='stars / day')])
        history=[repo(i,100*(i+1)) for i in range(20)]
        baseline=signals.popularity_baseline(history,NOW,CFG)
        best,middle=signals.popularity(history[-1],baseline)[0],signals.popularity(history[9],baseline)[0]
        self.assertGreater(best,9.5);self.assertAlmostEqual(middle,4.75)
        # Values older than the window and other sources do not enter the sample.
        stale=[dict(r,id='old'+r['id'],observed_at=NOW-8*86400) for r in history]
        self.assertEqual(len(signals.popularity_baseline(history+stale,NOW,CFG)[('github_trending','stars_day')]),20)
        # Thin history keeps the fixed saturation scale.
        self.assertAlmostEqual(signals.popularity(repo(0,1000),{})[0],10)

    def test_reddit_rss_rank_is_popularity_and_articles_get_momentum(self):
        top=post(source='rss',subreddit='LocalLLaMA',feed_rank=0);later=post(source='rss',subreddit='LocalLLaMA',feed_rank=30)
        self.assertEqual(signals.popularity(top)[:2],(8,True))
        self.assertLess(signals.popularity(later)[0],signals.popularity(top)[0])
        article=post(score=None)
        number,parts,_=signals.score(article,[article],NOW,CFG)
        attention=next(x for x in parts if x['id']=='attention')
        # The proxy ranks Trending Now only; the editorial score keeps 0 attention for an article without signals.
        self.assertEqual(attention['points'],0)
        weights=[post(id=f'w{i}',title='Qwen benchmark '+str(i),selftext='',links=[]) for i in range(12)]
        baseline=signals.popularity_baseline(weights+[article],NOW,CFG)
        _,parts,_=signals.score(article,[article],NOW,CFG,baseline=baseline)
        self.assertGreater(digest.momentum(dict(components=parts)),9)

    def test_trending_now_limits_one_source_in_the_top(self):
        item=lambda i,source,points:dict(id=str(i),source_id=source,score=points,published_at=NOW,
                                         components=[dict(id='attention',points=points,momentum=points)])
        rows=[item(i,'github',10-i*0.1) for i in range(6)]+[item(10,'verge',8),item(11,'decoder',1)]
        order=[r['id'] for r in digest.diversify(rows)]
        self.assertEqual(order[:4],['0','1','2','10'])
        # A weak story is not promoted above a strong one of a capped source.
        self.assertLess(order.index('3'),order.index('11'))
        self.assertEqual([r['id'] for r in digest.diversify(rows[:6])],[str(i) for i in range(6)])

    def test_coverage_does_not_count_reddit_subs_or_google_twice(self):
        p=post(source='rss',subreddit='LocalLLaMA',permalink='/r/LocalLLaMA/comments/one/test/')
        q=post(source='rss',subreddit='LocalLLM',permalink='/r/LocalLLM/comments/two/test/')
        attention=lambda members:next(x for x in signals.score(p,members,NOW)[1] if x['id']=='attention')['points']
        # A second subreddit is the same platform: it adds no reach.
        self.assertEqual(attention([p,q]),attention([p]))
        self.assertEqual(signals.publisher(post(source_id='google')),signals.publisher(post(source_id='deepmind')))
        official=post(source_id='openai',category='lab',article_url='https://openai.com/news/test')
        parts=signals.score(p,[p,official],NOW)[1]
        # Two independent platforms add reach (1/3) on top of the thread's own platform signal.
        self.assertGreater(next(x for x in parts if x['id']=='attention')['points'],attention([p]))
        self.assertEqual(next(x for x in parts if x['id']=='primary')['points'],5)

    def test_day_only_dates_use_the_first_sighting_that_day(self):
        midnight=NOW-NOW%86400
        dated=lambda created,seen:signals.reference_time(post(created_utc=created,first_seen=seen))[0]
        self.assertEqual(dated(midnight,midnight+20*3600),midnight+20*3600)
        # A precise time, or a day-only date first seen on a later day (archive backfill), is kept.
        self.assertEqual(dated(midnight+60,midnight+20*3600),midnight+60)
        self.assertEqual(dated(midnight,midnight+30*3600),midnight)

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
                   metrics=[dict(kind='likes',value=5,label='likes')])
        unknown=dict(known,title='someone/humanizer')
        parts=lambda p:{x['id']:x['points'] for x in signals.score(p,[p],NOW,CFG)[1]}
        self.assertEqual(parts(known)['relevance'],30);self.assertEqual(parts(unknown)['relevance'],18)
        self.assertEqual(parts(known)['attention'],25)
        # A quant or uncensored fine-tune of a tracked family is not its release.
        for title in ('DavidAU/Qwen3.8-Flash-Abliterated-GGUF','turboderp/GLM-5.3-Flash-exl3','unsloth/Qwen3.8-27B-GGUF','realrebelai/FLUX-3-Image-Klein_GGUFs'):
            self.assertEqual(parts(dict(known,title=title))['relevance'],18,title)
        self.assertEqual(parts(dict(known,title='nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-NVFP4'))['relevance'],30)

    def test_tutorials_and_passing_mentions_are_not_releases(self):
        body='We use open-source weights from https://huggingface.co/nvidia/sdm, released last week. Benchmark: 40 tok/s.'
        tutorial=post(title='How to Make Zero-Training Tabular Predictions with NVIDIA Structured Data Models',selftext=body,
                      links=['https://huggingface.co/nvidia/sdm'],source_id='marktechpost')
        # A publication's how-to is Industry, however many models and numbers it mentions.
        self.assertEqual(signals.priority(tutorial,CFG),5)
        self.assertEqual(signals.priority(dict(tutorial,title='A Coding Guide to Qwen3.8 released with open weights'),CFG),5)
        # A headline announcing the release still counts; a body mentioning one does not.
        self.assertEqual(signals.priority(dict(tutorial,title='NVIDIA released open weights for Structured Data Models'),CFG),1)
        self.assertEqual(signals.priority(dict(tutorial,title='Liquid AI Releases d1-ultra: A Multimodal Decision Model'),CFG),1)
        self.assertGreater(signals.priority(dict(tutorial,title='Structured data models are changing tabular ML'),CFG),1)

    def test_publication_levels_follow_the_news_not_the_wording(self):
        news=lambda title,**extra:post(**dict(title=title,selftext='',links=[],source_id='siliconangle',source_name='SiliconANGLE AI')|extra)
        level=lambda p,**extra:signals.story_level(p,[p],CFG,**extra)
        self.assertEqual(level(news('Microsoft reveals cutting-edge MAI-Base-2 model with 2T parameters')),(1,'Model release'))
        self.assertEqual(level(news("Reflection AI open-sources Beam's weights, post-training recipes")),(1,'Model release'))
        self.assertEqual(level(news('Google Open-Sources Harness2, a Recursive Agent Harnessing Framework'))[0]>1,True)
        # The same story retold as a release elsewhere is a release here too.
        beats=news('Deepseek V4.1 Flash 1017 beats larger rivals in agentic tasks',source_id='decoder')
        self.assertEqual(level(beats)[0],5)
        self.assertEqual(level(beats,retold=[news('DeepSeek releases updated V4.1-Flash model')])[0],1)
        thread=post(id='t',source='rss',subreddit='DeepSeek',title='Deepseek v4.1 1017 - Horse',source_id=None,source_name=None,
                    category='community',selftext='',links=[])
        self.assertEqual(level(thread,retold=[news('DeepSeek releases updated V4.1-Flash model')])[0],5)
        self.assertEqual(level(news('Anthropic Executive Says Open-Source AI Models Are Higher Risk'))[0],5)
        self.assertEqual(level(dict(thread,title='I let Claude hire its first employee'))[1],'Industry')
        self.assertEqual(level(news('China plans to tighten export controls on open-weight models')),(5,'Policy & business'))
        self.assertEqual(level(news('RapidCanvas raises $29M to develop custom AI applications'))[1],'Industry')
        self.assertEqual(level(news('Anthropic raises $5 billion at a $300 billion valuation'))[1],'Policy & business')
        tips=news('20 Jev Alternatives: The Decision Models Taking On TypeSafe in 2026',selftext='Scores 95% at 40 tok/s on 24 GB VRAM')
        basis=signals.editorial_basis(tips,[tips],CFG)[0]
        self.assertEqual((basis['relevance'],basis['evidence']),(6,0))
        self.assertEqual(signals.editorial_basis(news('China plans to tighten export controls on open-weight models'),[],CFG)[0]['relevance'],15)

    def test_russian_publications_are_filtered_and_rated_by_their_headline(self):
        import bot
        ru=lambda title:post(title=title,selftext='',links=[],source_id='cnews_ru',source_name='CNews',region='ru',filter_mode='ru_ai')
        self.assertIsNone(bot.reject(ru('Закон об ИИ поделил рынок на своих и остальных'),CFG,NOW))
        # A general feed's headline must name AI: "модель" alone is any model.
        self.assertEqual(bot.reject(ru('Минцифры планирует обязать 5G-смартфоны поддерживать новую модель шифрования'),CFG,NOW),'no keyword')
        self.assertEqual(bot.reject(ru('Иисус и иконы: епископ о вере'),CFG,NOW),'no keyword')
        level=lambda title:signals.story_level(ru(title),[],CFG)
        self.assertEqual(level('Закон об ИИ поделил рынок на своих и остальных'),(5,'Policy & business'))
        self.assertEqual(level('Сбер представил новую версию GigaChat'),(1,'Model release'))
        self.assertEqual(level('«Сбер» запустил GigaPlatform AI Studio для проверки ИИ-гипотез')[0],3)
        self.assertEqual(level('Сбербанк представил первый в России бенчмарк Cowork Bench для оценки ИИ-агентов')[0],3)
        self.assertEqual(level('ИИ начал выявлять питбайкеров на дорогах Самарской области'),(5,'Industry'))
        items=signals.build_items([ru('Закон об ИИ поделил рынок на своих и остальных')],CFG,NOW)
        self.assertIn('russia',items[0]['signal_categories'])

    def test_retold_russian_news_merges_by_names_in_any_alphabet(self):
        self.assertIn('минцифры',signals.story_names('Минцифры предложило блокировать VPN с помощью ИИ',title=True))
        self.assertNotIn('нейросети',signals.story_names('Нейросети нацелят против россиян',title=True))
        n=[0]
        def outlet(title,host):
            n[0]+=1
            return post(id=f'g{n[0]}',title=title,selftext='',links=[],source_id='gnews_ru_law',source_name=host,region='ru',
                        publisher_host=host,url=f'https://news.google.com/rss/articles/{n[0]}',article_url=f'https://news.google.com/rss/articles/{n[0]}',
                        created_utc=NOW-n[0]*600)
        posts=[outlet('В Минцифры предлагали находить и блокировать VPN с помощью ИИ','meduza.io'),
               outlet('Минцифры предложило использовать ИИ для блокировки VPN','verstka.media'),
               outlet('В Минцифры оценили идею блокировать VPN с помощью ИИ','ura.news')]
        same=[1.0]+[0.0]*(embeddings.DIMS-1);near=[0.8,0.6]+[0.0]*(embeddings.DIMS-2)
        vectors={embeddings.digest(embeddings.story_text(p)):(embeddings.quantize(v),1.0) for p,v in zip(posts,[same,same,near])}
        vectors={k:(v,sum(x*x for x in v)**0.5) for k,(v,_) in vectors.items()}
        # The third retelling is 0.8 from the first two: the same story, joined by a shared name ("минцифры", "vpn").
        self.assertEqual(len(signals.build_items(posts,CFG,NOW,vectors=vectors)),1)

    def test_model_name_and_self_link_are_not_evidence(self):
        model=post(content_kind='model',category='model',title='someone/Agens-32B-Preview',selftext='Parameters: 32,000,000',
                   url='https://huggingface.co/someone/Agens-32B-Preview',links=[],article_url='https://huggingface.co/someone/Agens-32B-Preview')
        parts=lambda p:{x['id']:x['points'] for x in signals.score(p,[p],NOW,CFG)[1]}
        self.assertEqual(parts(model)['evidence'],0)
        self.assertEqual(parts(dict(model,selftext='45 tok/s on one GPU',links=['https://github.com/someone/agens']))['evidence'],12)
        # A news headline reporting a size still counts as a measurement.
        self.assertEqual(parts(post(title='Lab releases a 32B model',selftext='',links=[]))['evidence'],7)
        # A Hype update fades like news (24 h), not like a model release (168 h).
        updated=dict(model,date_kind='updated',created_utc=NOW-24*3600)
        self.assertAlmostEqual(parts(updated)['freshness'],12.5,places=1)

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

    def test_fine_tunes_get_no_coverage_from_base_model_news(self):
        news=post(id='r',title='Qwen3.8 Flash Next is amazing for local use',
                  selftext='',links=[],article_url='https://localllm.example/1')
        model=lambda id,repo:post(id=id,title=repo,source_id='hf_models',category='model',content_kind='model',
                                  date_kind='trend',filter_mode='all',url='https://huggingface.co/'+repo,
                                  article_url='https://huggingface.co/'+repo,selftext='',links=[])
        tune=model('d','DavidAU/Qwen3.8-Flash-Next-Turbo-Brilliance-50x-Reasoning-Instruct-GGUF')
        base=model('q','Qwen/Qwen3.8-Flash-Next')
        items={x['title']:x for x in signals.build_items([news,tune,base],CFG,NOW)}
        self.assertEqual(items[tune['title']]['coverage'],1)
        self.assertEqual(items[base['title']]['coverage'],2)

    def test_cards_list_every_merged_and_related_report(self):
        model=post(id='m',title='deepseek-ai/DeepSeek-V4.1-Flash-1017',source_id='hf_models',source_name='Hugging Face · Models',
                   category='model',content_kind='model',date_kind='trend',filter_mode='all',selftext='',links=[],
                   url='https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017',article_url='https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017')
        thread=post(id='r',source='rss',subreddit='LocalLLaMA',title='DeepSeek V4.1 Flash 1017 weights are up',source_id=None,source_name=None,
                    permalink='/r/LocalLLaMA/comments/abc/deepseek/',selftext='',links=['https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017'],
                    url='https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017',article_url='')
        news=post(id='n',title='DeepSeek Just Released V4.1-Flash-1017, Scores 77.0 on DeepSWE',selftext='',links=[],
                  article_url='https://marktechpost.com/deepseek',url='https://marktechpost.com/deepseek',source_id='marktechpost',source_name='MarkTechPost')
        item=next(x for x in signals.build_items([model,thread,news],CFG,NOW) if x['title']==model['title'])
        links={l['url']:l['kind'] for l in item['story_links']}
        self.assertEqual(links['https://www.reddit.com/r/LocalLLaMA/comments/abc/deepseek/'],'merged')
        self.assertEqual(links['https://marktechpost.com/deepseek'],'related')
        self.assertNotIn(item['url'],links)

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

    def test_retold_stories_merge_or_add_coverage_across_outlets(self):
        n=[0]
        def article(title,source,text='',hours=1,category='news'):
            n[0]+=1
            return post(id=f'r{n[0]}',title=title,source_id=source,source_name=source.title(),category=category,filter_mode='all',
                        article_url=f'https://{source}.example/{n[0]}',url=f'https://{source}.example/{n[0]}',selftext=text,links=[],
                        created_utc=NOW-hours*3600)
        verge=article('ChatGPT can peek at your Chrome tabs now','verge','OpenAI is letting ChatGPT read your open Chrome tabs.',hours=20)
        decoder=article('ChatGPT becomes a full Chrome browser assistant that reads and acts on your open tabs','decoder',
                        'The ChatGPT extension for Chrome now reads open tabs and acts on them.',hours=4)
        junior=article('Anthropic has nearly stopped hiring junior staff because AI agents now work six times as many hours','decoder',
                       'Agents now log six times the hours of people, so Anthropic hires few junior engineers.',hours=6)
        less=article('Anthropic says its workforce has become less junior and is spending more time in meetings','techcrunch',
                     'Fewer junior engineers join Anthropic while agents log more hours, its workforce data shows.',hours=8)
        singapore=[article('Google DeepMind and Singapore Government collaborate on a scam detection model','deepmind',category='lab',hours=3),
                   article('Anthropic opens Singapore office and signs MOU with the Government of Singapore','anthropic',category='lab',hours=2)]
        filler=[article(f'Unrelated story number {i} about chips and courts','wired',hours=30+i) for i in range(6)]
        items={x['title']:x for x in signals.build_items([verge,decoder,junior,less,*singapore,*filler],CFG,NOW)}
        # Retold in other words, the Chrome story is one card; the junior story is one story told twice: coverage, not a merge.
        chrome=[x for x in items.values() if 'Chrome' in x['title']]
        self.assertEqual(len(chrome),1);self.assertEqual(chrome[0]['coverage'],2)
        self.assertEqual(items[junior['title']]['coverage'],2);self.assertEqual(items[less['title']]['coverage'],2)
        # Two companies in one country are two stories.
        self.assertTrue(all(items[s['title']]['coverage']==1 for s in singapore))

    def test_community_threads_fold_into_the_story_they_discuss(self):
        news=post(id='n',title='DeepSeek V4.1 Flash 1017 released with open weights',source_id='techcrunch',source_name='TechCrunch AI',
                  filter_mode='all',article_url='https://tc.example/deepseek',url='https://tc.example/deepseek',selftext='',links=[],created_utc=NOW-60*3600)
        thread=post(id='t',source='rss',subreddit='LocalLLaMA',title='Deepseek Medieval Village (v4.1 flash 1017 high)',source_id=None,
                    source_name=None,category='community',permalink='/r/LocalLLaMA/comments/t/',url='https://www.reddit.com/r/LocalLLaMA/comments/t/',
                    article_url='',selftext='',links=[],score=500,created_utc=NOW-2*3600)
        # Two days later the thread is a new report, not the same news (MERGE_WINDOW), yet still related (STORY_WINDOW).
        items={x['id']:x for x in signals.build_items([news,thread],CFG,NOW)}
        card=next(x for x in items.values() if x['title']==news['title'])
        reddit=next(x for x in items.values() if x['title']==thread['title'])
        self.assertEqual(reddit['discussion_of'],card['id']);self.assertEqual(reddit['platform'],'reddit')
        self.assertEqual([d['id'] for d in card['discussions']],[reddit['id']])
        self.assertNotIn('discussion_of',card)

    def test_one_version_with_other_product_words_is_another_products_coverage(self):
        model=lambda id,title:post(id=id,title=title,source_id='hf_models',source_name='Hugging Face · Models',category='model',
                                   content_kind='model',date_kind='trend',filter_mode='all',url=f'https://huggingface.co/{title}',
                                   article_url=f'https://huggingface.co/{title}',selftext='',links=[],created_utc=NOW-3600)
        parse,bolt,vl=model('p','nvidia/Nemotron-3.5-Parse'),model('b','nvidia/NVIDIA-Nemotron-3.5-Bolt-74B'),model('v','nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B')
        news=post(id='n',title='NVIDIA Releases Nemotron 3.5 Super VL, a 120B open model',filter_mode='all',links=[],
                  article_url='https://mtp.example/vl',url='https://mtp.example/vl',selftext='',created_utc=NOW-7200)
        thread=post(id='t',source='rss',subreddit='LocalLLaMA',title='Nemotron 3.5 Bolt - Horse',source_id=None,source_name=None,
                    category='community',permalink='/r/LocalLLaMA/comments/t/',url='https://www.reddit.com/r/LocalLLaMA/comments/t/',
                    article_url='',selftext='',links=[],created_utc=NOW-1800)
        products=signals.product_words([parse,bolt,vl])
        self.assertTrue({'parse','bolt','super','vl'}<=products)
        self.assertTrue(signals.variants(parse,news,products))
        # A thread using the model in its own words still covers it; "horse" names no product.
        self.assertFalse(signals.variants(bolt,thread,products))
        items={x['title']:x for x in signals.build_items([parse,bolt,vl,news,thread],CFG,NOW)}
        self.assertEqual(items[parse['title']]['related_sources'],[])

    def test_category_rankings_share_the_story_attention(self):
        row=lambda points,score:dict(id='x',score=score,published_at=NOW,components=[dict(id='attention',points=points,momentum=points),
                                                                                     dict(id='freshness',points=10)])
        [shared]=signals.share_attention([row(4.3,40)],[row(25,60.7)])
        self.assertEqual((shared['score'],shared['components'][0]['momentum']),(60.7,25))

    def test_official_posts_rank_at_least_as_tools_and_launches_higher(self):
        lab=lambda title,**extra:post(**dict(title=title,source_id='openai',source_name='OpenAI News',category='lab',filter_mode='all',
                                            selftext='',links=[])|extra)
        relevance=lambda p:signals.editorial_basis(p,[p],CFG)[0]['relevance']
        self.assertEqual(relevance(lab('Introducing dots')),24)
        self.assertEqual(relevance(lab('Our approach to frontier risk')),18)
        # Tutorials keep the ladder; a news report about the lab is not the lab's own post.
        self.assertEqual(relevance(lab('How to build an agent with the Responses API')),6)
        self.assertEqual(relevance(lab('Introducing dots',source_id='techcrunch',source_name='TechCrunch AI',category='news')),6)

    def test_shared_keys_merge_articles_only_within_days(self):
        issue=lambda id,days:post(id=id,title='[AINews] not much happened today',source_id='latent',source_name='Latent Space',
                                  category='newsletter',filter_mode='all',article_url=f'https://latent.example/{id}',url=f'https://latent.example/{id}',
                                  selftext='',links=[],created_utc=NOW-days*86400)
        items=signals.build_items([issue('a',1),issue('b',6)],CFG,NOW)
        self.assertEqual(len(items),2);self.assertEqual(len({x['id'] for x in items}),2)
        # A model card keeps a late thread linking to it: the link identifies the artifact.
        model=post(id='m',title='bfl/flux-klein',source_id='hf_models',category='model',content_kind='model',date_kind='trend',filter_mode='all',
                   url='https://huggingface.co/bfl/flux-klein',article_url='https://huggingface.co/bfl/flux-klein',selftext='',links=[],created_utc=NOW-3600)
        thread=post(id='t',source='rss',subreddit='StableDiffusion',title='FLUX Klein weights are out',source_id=None,source_name=None,
                    permalink='/r/StableDiffusion/comments/x/',url='https://huggingface.co/bfl/flux-klein',article_url='',selftext='',
                    links=['https://huggingface.co/bfl/flux-klein'],created_utc=NOW-4*86400)
        self.assertEqual(len(signals.build_items([model,thread],CFG,NOW)),1)

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

"""Explainable editorial ranking. No inferred popularity or growth metrics."""
from collections import defaultdict
from hashlib import sha256
import math
import re
from urllib.parse import urlsplit
import bot
from cloud_news import prepare_cloud_posts,availability_for

COMPONENTS = [('relevance','Соответствие теме',30),('freshness','Свежесть',25),
              ('evidence','Конкретика',15),('coverage','Охват площадок',15),
              ('primary','Первоисточник',5),('engagement','Популярность',10)]
LABELS = {1:'Релиз модели',2:'Локальный запуск',3:'Инструменты',4:'Исследования',5:'Индустрия'}
SCORING_VERSION='editorial-v3'
SIGNAL_CATEGORIES={'model':'models','repository':'repos','paper':'papers','news':'media','cloud':'cloud'}
RANKING_METRICS={'models':('trending','likes'),'repos':('stars_day',),'papers':('upvotes',)}
# Strong cues (products, controls, deployment models) count anywhere in the excerpt. The weak cue
# "enterprise/corporate" is vendor boilerplate in bodies, so there it needs a second occurrence.
# Customer stories count only in titles of lab and news posts: community "built X with Claude" is not adoption.
# They need an outcome verb ("Sophos cuts…with OpenAI") or "How X…with": "Introducing Y with Gemini" is a product.
CASE_VERBS=r'(?:cut|boost|save|scale|turn|build|answer|resolve|increase|accelerat|transform|reimagin|put|bring|help|free|power|sharpen|move|reduce|speed|automat|deploy|triple|double|grow|govern)\w*'
ENTERPRISE_RULES=[
    ('Корпоративное применение','weak',r'\b(?:enterprises?|b2b|business customers|business users|corporate customers|erp|crm|saas\w*|cios?)\b|корпоративн\w*|для бизнес\w*|для компани\w*'),
    ('Безопасность и управление','strong',r'\b(?:sso|scim|rbac|soc\s*2|data residency|data governance|ai governance|enterprise security|admin controls|spend controls)\b|управлени\w* доступ\w*|резидентност\w* данн\w*'),
    ('Частное развёртывание','strong',r'\b(?:on[- ]prem(?:ises|ise)?|private cloud|dedicated deployment|air[- ]gapped|sovereign (?:ai|cloud))\b|частн\w* облак\w*|изолированн\w* контур\w*'),
    ('Корпоративная AI-платформа','strong',r'\b(?:chatgpt (?:enterprise|business|work|team)|claude (?:enterprise|for work|team)|gemini enterprise|copilot studio|microsoft 365 copilot|m365 copilot|github copilot enterprise|watsonx|agentforce|sap joule|azure ai foundry|amazon bedrock|agentcore|microsoft foundry|azure openai|ai gateway|workers ai|vertex ai|databricks|snowflake cortex|servicenow)\b'),
    ('Внедрение в компании','case',r'^(?!(?:how to|introducing|announcing)\b)(?:how\b.*|.*\b'+CASE_VERBS+r'\b.*)\b(?:with|using)\s+(?:chatgpt|codex|claude|gemini|openai|copilot)\b|\bto (?:its |all )?(?:[\d,]+ )?employees\b'),
]
ENTERPRISE_RELEVANCE={'title':(30,'Корпоративная тема в заголовке'),'case':(24,'Пример внедрения в компании'),
                      'body':(18,'Корпоративные признаки в тексте')}
# A vendor's story about its own customer is self-reported; a steady stream of them is a content series.
VENDOR_CASE=(18,'Кейс клиента в блоге вендора')
SERIES_POINTS=12
SERIES_WINDOW=7*86400


def enterprise_matches(p):
    """{label: 'title'|'case'|'body'} for every rule the post satisfies."""
    title=p['title']
    # A bounded source excerpt avoids boilerplate at the end of full articles.
    body=(p.get('article_text') or p.get('selftext',''))[:1200]
    found={}
    for label,kind,pattern in ENTERPRISE_RULES:
        if kind=='case':
            if p.get('category') in ('lab','news') and re.search(pattern,title,re.I): found[label]='case'
        elif re.search(pattern,title,re.I): found[label]='title'
        # Feeds often repeat a (truncated) teaser before the full text: a mention is identified by its sentence
        # up to the match, so the copy is not counted twice.
        elif len({body[max(body.rfind('.',0,m.start()),body.rfind('\n',0,m.start()))+1:m.end()].strip().casefold()
                  for m in re.finditer(pattern,body,re.I)})>=(1 if kind=='strong' else 2):
            found[label]='body'
    return found


def enterprise_reasons(p):
    return list(enterprise_matches(p))


def enterprise_series(posts):
    """source_id -> (times of the vendor blog's customer stories, times that keep full weight).

    Chronologically, a story keeps full weight when no full-weight story of the same blog precedes it
    within SERIES_WINDOW, so a daily stream still surfaces one case per week."""
    times=defaultdict(list)
    for p in posts:
        if p.get('category')=='lab' and 'case' in enterprise_matches(p).values():
            times[source_id(p)].append(p['created_utc'])
    series={}
    for source,stamps in times.items():
        full,last=set(),None
        for t in sorted(set(stamps)):
            if last is None or t-last>SERIES_WINDOW: full.add(t);last=t
        series[source]=(stamps,full)
    return series


def enterprise_relevance(members,series=None):
    """(points, reason) from the strongest business evidence in the group; independent outlets outrank vendor case studies."""
    best=(0,'')
    for m in members:
        places=set(enterprise_matches(m).values())
        if m.get('category')=='lab' and 'case' in places:
            stamps,full=(series or {}).get(source_id(m),([],{m['created_utc']}))
            if m['created_utc'] in full:
                option=VENDOR_CASE
            else:
                n=1+sum(0<m['created_utc']-t<=SERIES_WINDOW for t in stamps)
                option=(SERIES_POINTS,f'Серия кейсов {source_name(m)}: {n}-й за 7 дней')
        else:
            option=next((ENTERPRISE_RELEVANCE[x] for x in ('title','case','body') if x in places),(0,''))
        best=max(best,option,key=lambda x:x[0])
    return best


def ranking_metadata(members):
    """Keep category membership and real platform metrics even when dedupe selects another source."""
    categories=set();metrics={}
    for p in members:
        if p.get('cloud_rule') or p.get('cloud_availability'):categories.add('cloud')
        if enterprise_reasons(p):categories.add('enterprise')
        category=SIGNAL_CATEGORIES.get(p.get('category'))
        if not category:continue
        categories.add(category)
        options=list(p.get('metrics',[]))
        if category=='models':
            options.append(dict(kind='trending',value=p.get('provider_trending_score'),label='trendingScore HF'))
        order=RANKING_METRICS.get(category,())
        for metric in options:
            value=metric.get('value');kind=metric.get('kind')
            if kind not in order or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:continue
            candidate=dict(kind=kind,value=value,label=metric['label'],
                           observed_at=p.get('observed_at',p['created_utc']),source=source_name(p))
            previous=metrics.get(category)
            if previous is None or (order.index(kind),-value)<(order.index(previous['kind']),-previous['value']):
                metrics[category]=candidate
    return sorted(categories),metrics


def publisher(p):
    if p.get('source') != 'site':
        return 'reddit.com'
    if p.get('cloud_provider'):
        return {'google':'google','aws':'amazon.com','microsoft':'microsoft.com','vercel':'vercel.com','cloudflare':'cloudflare.com'}.get(p['cloud_provider'],p['cloud_provider'])
    host = (urlsplit(p.get('article_url','')).hostname or '').lower().removeprefix('www.')
    if host in ('news.ycombinator.com','hnrss.org') or p.get('source_id')=='hn':
        return 'hackernews'
    if p.get('source_id') in ('google','deepmind'):
        return 'google'
    return host or p.get('source_id','unknown')


def source_id(p):
    return p.get('source_id') or 'reddit:' + p.get('subreddit','unknown')


def source_name(p):
    return p.get('source_name') or 'r/' + p.get('subreddit','unknown')


def raw_points(p):
    if isinstance(p.get('score'), (int,float)):
        return p['score']
    if p.get('source_id')=='hn':
        match=re.search(r'(?m)^Points:\s*(\d+)',p.get('selftext',''))
        if match: return int(match[1])
    return None


RELEASE=['release','released','releases','launch','launches','launched','introducing','introduces',
         'announces','unveils','open weights','open-source','new model']
# Hours until freshness halves. News is stale after a day; papers and models stay topical longer.
HALF_LIFE={'paper':72,'model':168}
STOP={'that','this','with','from','have','will','what','more','about','into','your','their','they','says',
      'said','after','over','than','just','been','could','would','when','where','which','while','here','there',
      'some','also','them','were','does','like','make','makes','gets','first','model','models'}
STORY_WINDOW=72*3600


def popularity(p):
    # Per-platform saturation targets keep different units separate.
    # HF trendingScore and stars_day are recent velocity; total likes are only a fallback.
    targets={'stars_day':1000,'stars_week':7000,'stars_month':30000,'trending':1000,'likes':1000,'upvotes':300}
    metrics=list(p.get('metrics',[]))
    if isinstance(p.get('provider_trending_score'),(int,float)):
        metrics.insert(0,dict(kind='trending',value=p['provider_trending_score'],label='trendingScore Hugging Face'))
    for metric in metrics:
        kind,value=metric['kind'],metric['value']
        if kind in targets:
            points=min(10,10*math.log1p(max(0,value))/math.log1p(targets[kind]))
            return points,True,f'{value} {metric["label"]}; порог насыщения {targets[kind]}'
    raw=raw_points(p)
    return (min(10,math.log10(raw+1)*2.5),True,f'{raw} очков') if raw is not None and raw>=0 else (0,False,'Нет данных')


def reference_time(p):
    """When the story became news: publication for models/papers, first sighting for trending repositories."""
    if p.get('date_kind')!='trend':
        return p['created_utc'],'публикация'
    if p.get('content_kind') in HALF_LIFE and p.get('original_published_at'):
        return p['original_published_at'],'статья опубликована' if p['content_kind']=='paper' else 'модель создана'
    return p.get('first_seen',p['created_utc']),'впервые в трендах'


def priority(p,cfg=None):
    """bot.priority plus named model releases, which news and lab titles announce without links or numbers."""
    base=bot.priority(p)
    models=(cfg or {}).get('models',[])
    if base>1 and not p.get('content_kind') and bot.matches(p['title'],models) and bot.matches(p['title'],RELEASE):
        return 1
    return base


def fingerprint(p):
    """Version tokens (v4.1, qwen3.8, 27b) and 5-letter stems of the title, for cross-outlet story matching."""
    words=[w.strip('.') for w in re.split(r'[^\w.]+',p['title'].casefold()) if w.strip('.')]
    # Bare integers and sizes (100, 27b, 1m) are shared by unrelated stories, so they are not versions.
    versions={w for w in words if re.search(r'\d',w) and re.search(r'[a-zа-я.]',w) and not re.fullmatch(r'\d+(?:\.\d+)?[bkmt]',w)}
    stems={w[:5] for w in words if len(w)>=4 and w not in STOP and not re.search(r'\d',w)}
    return versions,stems


def same_story(a,b):
    (va,sa),(vb,sb)=a,b
    if len(va&vb)>=2 or va&vb and sa&sb:
        return True
    shared=sa&sb
    return len(shared)>=3 and len(shared)/len(sa|sb)>=0.4


def score(p, members, now, cfg=None, related=(), focus=None, series=None):
    priority_=priority(p,cfg)
    stamp,stamp_kind=reference_time(p)
    age=max(0,(now-stamp)/3600)
    half_life=HALF_LIFE.get(p.get('content_kind'),24)
    text=p['title']+' '+p.get('article_text',p.get('selftext',''))
    measured=bool(re.search(r'\d[\d.,]*\s*(?:tok/s|tokens/s|GB|t/s|%|ms|VRAM)|(?<![$\w])\d+(?:\.\d+)?B\b|Параметров:\s*\d',text,re.I))
    repository=any(bot.canonical(url) for url in [p.get('url','')]+p.get('links',[]))
    research=bot.matches(text,['paper','arXiv','research','benchmark'])
    own={publisher(m) for m in members}
    publishers=own|{publisher(m) for m in related}
    engagement,available,metric_note=max((popularity(m) for m in members),key=lambda x:(x[0],x[1]))
    unknown_model=p.get('content_kind')=='model' and not bot.matches(p['title'],(cfg or {}).get('models',[]))
    values=dict(relevance=24 if unknown_model else (6-priority_)*6,freshness=25*2**(-age/half_life),
                evidence=7*measured+5*repository+3*research,
                coverage=min(15,max(0,len(publishers)-1)*7.5),
                primary=5 if any(m.get('category') in ('lab','cloud') for m in members) else 0,
                engagement=engagement)
    if focus=='enterprise':
        # Inside Enterprise the general ladder rates nearly everything "Индустрия"; rank by business evidence instead.
        values['relevance'],focus_reason=enterprise_relevance(members,series)
    extra=sorted(publishers-own)
    notes=dict(engagement=metric_note,
               freshness=f'{round(age)} ч назад ({stamp_kind}); полураспад {half_life} ч',
               coverage=('Тот же сюжет: '+', '.join(extra)) if extra else '')
    reasons=[]
    if focus=='enterprise': reasons.append(focus_reason)
    elif unknown_model: reasons.append('Модель в трендах')
    elif priority_<5: reasons.append(LABELS[priority_])
    if measured: reasons.append('Есть численные показатели')
    if repository: reasons.append('Ссылка на модель или репозиторий')
    if len(publishers)>1: reasons.append(f'{len(publishers)} разных площадки')
    if values['primary']: reasons.append('Есть официальный источник')
    if not reasons: reasons.append('Свежая тематическая новость')
    return round(sum(values.values()),1),[{ 'id':key,'label':label,'points':round(values[key],1),'max':maximum,
        'available':available if key=='engagement' else True,
        'explanation':notes.get(key,'')} for key,label,maximum in COMPONENTS],reasons


def related_stories(stories):
    """Other outlets carrying the same story within STORY_WINDOW; they raise coverage but are not merged."""
    prints=[fingerprint(p) for p,_ in stories]
    index=defaultdict(set)
    for i,(versions,stems) in enumerate(prints):
        for token in versions|stems: index[token].add(i)
    result=[]
    for i,(p,members) in enumerate(stories):
        versions,stems=prints[i]
        candidates=set().union(*(index[t] for t in versions|stems)) if versions|stems else set()
        own={publisher(m) for m in members}
        found=[]
        for j in candidates-{i}:
            q,others=stories[j]
            if abs(reference_time(p)[0]-reference_time(q)[0])<=STORY_WINDOW and same_story(prints[i],prints[j]):
                found+=[m for m in others if publisher(m) not in own]
        result.append(found)
    return result


# Merging similar articles is stricter than related_stories, which only credits coverage: a merged card hides
# the other articles, so "Nemotron 3.5 Super VL" must never absorb "Nemotron 3.5 Bolt".
MERGE_WINDOW=48*3600
GENERIC={'just','new','now','release','releases','released','introducing','introduces','launch','launches','launched',
         'announces','unveils','open','model','models','says','today','meet','welcome','with','and','the','for','its',
         'is','a','an','of','on','to','in'}
SIZE=re.compile(r'[a-z]?\d+(?:\.\d+)?[bkmt]')
SOURCE_ORDER={'lab':0,'cloud':0,'news':1,'newsletter':2}
# Issues covering several topics may join a story but never become its card.
DIGEST=re.compile(r';|^\[ainews\]|^import ai \d+|\b(?:weekly|roundup|recap|newsletter)\b',re.I)


def title_words(p):
    return [w.strip('.') for w in re.split(r'[^\w.]+',p['title'].casefold()) if w.strip('.')]


def is_version(w):
    return bool(re.search(r'\d',w)) and ('.' in w or bool(re.search(r'[a-z]',w))) and not SIZE.fullmatch(w) and not re.fullmatch(r'(?:19|20)\d\d',w)


def merge_tokens(words):
    """Versions, long numbers (1017) and 5-letter stems; sizes, years and short numbers are too common to identify a story."""
    return {w if re.search(r'\d',w) else w[:5] for w in words
            if is_version(w) or re.fullmatch(r'\d{4,}',w) and not re.fullmatch(r'(?:19|20)\d\d',w)
            or len(w)>=4 and w not in STOP and not re.search(r'\d',w)}


def qualifiers(words,anchor):
    """Name words within two positions of a version token: "nemotron 3.5 super vl" -> {nemotron, super, vl}."""
    return {x for i,w in enumerate(words) if w==anchor for x in words[max(0,i-2):i+3]
            if x!=anchor and x not in GENERIC and len(x)>1 and not re.search(r'\d',x)}


def similar_articles(a,b):
    wa,wb=title_words(a),title_words(b);ka,kb=merge_tokens(wa),merge_tokens(wb)
    if not ka or not kb:return False
    shared=ka&kb;jaccard=len(shared)/len(ka|kb)
    # Two posts of one outlet are usually different announcements ("[Launched] Generally Available: ...").
    if publisher(a)==publisher(b):return jaccard>=0.7 and len(shared)>=4
    anchors={w for w in wa if is_version(w)}&{w for w in wb if is_version(w)}
    if anchors:
        if len(shared)/min(len(ka),len(kb))<0.5:return False
        # Each side naming the version with a word the other title lacks means different products.
        return not any({x for x in qualifiers(wa,v) if x not in wb} and {x for x in qualifiers(wb,v) if x not in wa} for v in anchors)
    return len(shared)>=4 and jaccard>=0.4


def merge_similar(stories):
    """Cluster article stories (not models, repositories or papers) that report the same news.

    Complete linkage: a story joins a cluster only if it matches every story already in it, so a chain
    A~B~C never merges unrelated A and C. Stories are visited in publication order: the first report anchors."""
    order=sorted(range(len(stories)),key=lambda i:stories[i][0]['created_utc'])
    clusters=[]
    for i in order:
        p=stories[i][0]
        target=None
        if not p.get('content_kind'):
            target=next((c for c in clusters if not stories[c[0]][0].get('content_kind') and all(
                abs(reference_time(p)[0]-reference_time(stories[j][0])[0])<=MERGE_WINDOW and similar_articles(p,stories[j][0]) for j in c)),None)
        if target is None:clusters.append([i])
        else:target.append(i)
    return clusters


def cluster_lead(reps):
    """Card of a merged story: not a multi-topic digest; the official source, then a news outlet, then the earliest report."""
    return min(reps,key=lambda p:(bool(DIGEST.search(p['title'])),SOURCE_ORDER.get(p.get('category'),3),p['created_utc']))


ARTIFACT_KEYS=('github.com/','huggingface.co/')


def story_keys(p):
    """bot.keys for the dashboard. A post naming several artifacts (a roundup or newsletter) would bridge
    unrelated stories into one group, so it is grouped by its own identity; related_stories still credits
    the stories it shares. Telegram keeps bot.keys."""
    ks=bot.keys(p)
    external={k for k in ks if k.startswith(ARTIFACT_KEYS)}-{bot.canonical(p.get('url',''))}
    return ks-external if len(external)>=2 else ks


def representative(members,chosen):
    """The artifact itself (model, repository, paper), then an official lab post, then bot.select's choice.
    Feed ranks of different feeds are not comparable, so they must not decide between a model and a newsletter."""
    artifacts=[m for m in members if m.get('content_kind')]
    if artifacts:return chosen if chosen in artifacts else min(artifacts,key=lambda m:m.get('feed_rank',0))
    labs=[m for m in members if m.get('category')=='lab']
    return chosen if chosen in labs or not labs else labs[0]


def build_items(posts,cfg,now,max_hours=8760,focus=None):
    enabled={s['id'] for s in cfg.get('feeds',[]) if s.get('enabled',True)}
    posts=[p for p in posts if p.get('source')!='site' or p.get('source_id') in enabled]
    posts,_=prepare_cloud_posts(posts,cfg,now,max_hours)
    groups=bot.select(posts,cfg,now,max_age_hours=max_hours,key_fn=story_keys)
    index=defaultdict(list)
    for p in posts:
        if bot.reject(p,cfg,now,max_age_hours=max_hours): continue
        for key in story_keys(p): index[key].append(p)
    grouped=[]
    for p,aliases in groups:
        members={}
        for key in aliases:
            for m in index[key]: members[(source_id(m),m['id'])]=m
        members=list(members.values())
        grouped.append((representative(members,p),members,aliases))
    stories,merged=[],[]
    for cluster in merge_similar(grouped):
        parts=[grouped[i] for i in cluster]
        lead=cluster_lead([rep for rep,_,_ in parts])
        members=list({(source_id(m),m['id']):m for _,group,_ in parts for m in group}.values())
        # The id follows the first report, so saved cards survive later articles joining the story.
        first=min(parts,key=lambda part:part[0]['created_utc'])
        also=[dict(source=source_name(rep),title=rep['title'],url=rep.get('article_url') or rep.get('url'))
              for rep,_,_ in sorted(parts,key=lambda part:part[0]['created_utc']) if rep is not lead]
        stories.append((lead,members));merged.append((first[2],also,min(reference_time(rep)[0] for rep,_,_ in parts)))
    series=enterprise_series(posts) if focus=='enterprise' else None
    result=[]
    for (aliases,also,news_at),(p,members),related in zip(merged,stories,related_stories(stories)):
        number,components,reasons=score(p,members,now,cfg,related,focus,series)
        stable=next(iter(sorted(k for k in aliases if k.startswith(('github.com/','huggingface.co/')))),None)
        stable=stable or next(iter(sorted(k for k in aliases if k.startswith('article:'))),None) or sorted(aliases)[0]
        sid=source_id(p)
        url=p.get('article_url') or p.get('url')
        if p.get('source')=='rss' and 'hugging face' in p['title'].casefold():
            url=next((link for link in p.get('links',[]) if urlsplit(link).hostname=='huggingface.co'),url)
        discussion=p.get('discussion_url') or ('https://www.reddit.com'+p['permalink'] if p.get('permalink') else '')
        if not url or urlsplit(url).scheme not in ('http','https'): url=discussion
        category=p.get('category','community')
        column={'repository':'repos','model':'models','paper':'papers'}.get(category) or ('community' if category=='community' or sid=='hn' else 'labs' if category in ('lab','newsletter','cloud') else 'media')
        summary=bot.excerpt(p.get('article_text') or p.get('selftext',''),650)
        if sid=='hn' and p.get('selftext','').startswith('Article URL:'): summary=''
        signal_categories,signal_metrics=ranking_metadata(members)
        result.append(dict(id=sha256(stable.encode()).hexdigest()[:20],title=p['title'],
            summary=summary,
            description_kind=('Метаданные модели' if p.get('content_kind')=='model' else 'Аннотация статьи' if p.get('content_kind')=='paper' else 'Описание репозитория' if p.get('content_kind')=='repository' else 'Фрагмент статьи' if p.get('article_text') else 'Описание из RSS'),
            source_id=sid,source_name=source_name(p),category=category,column=column,
            signal_categories=signal_categories,signal_metrics=signal_metrics,
            cloud_availability=availability_for(members),
            enterprise_reasons=list(dict.fromkeys(reason for member in members for reason in enterprise_reasons(member))),
            published_at=p['created_utc'],score=number,components=components,reasons=reasons,
            priority=priority(p,cfg),topic='Модели' if p.get('content_kind')=='model' else LABELS[priority(p,cfg)],url=url,discussion_url=discussion,
            domain=urlsplit(url).hostname or '',coverage=len({publisher(m) for m in members+related}),
            sources=list(dict.fromkeys(source_name(m) for m in members)),
            related_sources=list(dict.fromkeys(source_name(m) for m in related)),
            also=also,
            # When the story became news (earliest report, model/paper creation, a repository's first day in trends):
            # every period tab filters by it.
            news_at=news_at,
            raw_score=raw_points(p),metrics=p.get('metrics',[]),date_kind=p.get('date_kind','published'),
            original_published_at=p.get('original_published_at'),provider_rank=p.get('feed_rank') if p.get('date_kind')=='trend' else None,saved=False))
    return sorted(result,key=lambda x:(-x['score'],-x['published_at']))

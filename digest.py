"""Machine-readable views of a published dataset: Markdown digest, compact JSON, llms.txt and static HTML.

Agents usually fetch a page without running JavaScript, so everything here is derived on the server
from the same dataset and ordering the dashboard uses."""
from collections import defaultdict
from datetime import datetime, timezone
import html
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit
from dashboard import PERIODS

CATEGORIES={'all':'Overall','models':'Models','repos':'GitHub','papers':'Research papers',
            'media':'Publications','enterprise':'Enterprise','cloud':'Cloud'}
PERIOD_LABELS={'trending':'12 hours','day':'24 hours','week':'7 days','month':'30 days','year':'365 days','now':'48 hours'}
SORTS=['trending','day','week','month','year','now']
# Period tabs, mirrored from web/signal-ranking.js: the window counts from when a story became news (news_at),
# and each tab sorts by one measure. 'now' (48 h, editorial) remains only for existing API clients.
WINDOWS={'trending':(12,'momentum'),'day':(24,'editorial'),'week':(168,'significance'),'month':(720,'significance'),
         'year':(8760,'significance'),'now':(48,'editorial')}
MEASURES={'momentum':('momentum',25),'editorial':('score',100),'significance':('significance',75)}
PERIOD_NOTES={'trending':'Trending Now: stories from the last 12 hours, ranked by momentum (popularity within the story\'s own source plus coverage, up to 25). Articles without a platform metric use the percentile of their editorial weight as popularity.',
              'day':'The leading stories of the last 24 hours by editorial score, including freshness, up to 100.',
              'week':'The most significant stories of the last seven days. Significance excludes freshness, up to 75.',
              'month':'The most significant stories of the last 30 days, excluding freshness, up to 75.',
              'year':'The most significant stories of the last 365 days, excluding freshness, up to 75.',
              'now':'Stories from the last 48 hours by editorial score (legacy API default).'}
# API default stays 48 hours for existing clients; the dashboard page itself opens on Trending Now (sort=trending).
DEFAULT_PERIOD='now'
DEFAULT_LIMIT=20
MAX_LIMIT=100


class QueryError(ValueError):
    pass


def query(params):
    """Validate ?sort=&category=&limit= shared by /signals.md and /api/signals."""
    first=lambda key,default:(params.get(key) or [default])[0]
    period,category=first('sort',DEFAULT_PERIOD),first('category','all')
    if period not in SORTS:raise QueryError('Unknown sort; use '+', '.join(SORTS))
    if category not in CATEGORIES:raise QueryError('Unknown category; use '+', '.join(CATEGORIES))
    try:limit=int(first('limit',DEFAULT_LIMIT))
    except ValueError:raise QueryError('limit must be an integer') from None
    if not 1<=limit<=MAX_LIMIT:raise QueryError(f'limit must be between 1 and {MAX_LIMIT}')
    return period,category,limit


def ranked(data,category='all',period=DEFAULT_PERIOD,now=None):
    """Order of web/signal-ranking.js: the period's measure, then editorial score, recency and id."""
    now=time.time() if now is None else now
    hours,order=WINDOWS[period]
    rows=data['items'] if category=='all' else (data.get('signal_rankings') or {}).get(category,[])
    rows=[p for p in rows if 0<=now-news_time(p)<=hours*3600]
    rows=sorted(rows,key=lambda p:(-measure(p,order),-p['score'],-p['published_at'],str(p['id'])))
    return diversify(rows,order)


# Every period: past SOURCE_CAP stories of one source, further ones from that source compete for the first
# SOURCE_WINDOW places with a fifth of the measure's scale taken off (5 of 25 momentum, 20 of 100 score,
# 15 of 75 significance), so one source cannot fill the top yet weak stories are not promoted.
# Mirrored in web/signal-ranking.js.
SOURCE_CAP,SOURCE_WINDOW,SOURCE_PENALTY=3,10,0.2


def diversify(rows,order='momentum'):
    rest,top,counts=list(rows),[],defaultdict(int)
    source=lambda p:p.get('source_id') or p.get('source_name')
    penalty=SOURCE_PENALTY*MEASURES[order][1]
    while rest and len(top)<SOURCE_WINDOW:
        # max keeps the first of equal values, so ties follow the existing order.
        best=max(range(len(rest)),key=lambda i:measure(rest[i],order)-(penalty if counts[source(rest[i])]>=SOURCE_CAP else 0))
        p=rest.pop(best);top.append(p);counts[source(p)]+=1
    return top+rest


def news_time(p):
    """When the story became news: publication, model/paper creation, first day of a repository in trends."""
    return p.get('news_at') or p['published_at']


def component(p,key):
    return next((c for c in p.get('components',[]) if c['id']==key),{})


def momentum_parts(p):
    """What momentum is made of, so an agent can reproduce it: popularity, its basis and coverage."""
    engagement=component(p,'engagement')
    platform=engagement.get('available',True)
    return dict(popularity=engagement.get('points',0) if platform else engagement.get('momentum_proxy',0),
                basis='platform metric' if platform else 'editorial weight percentile',
                coverage=component(p,'coverage').get('points',0),note=engagement.get('explanation',''))


def momentum(p):
    """Platform popularity plus cross-outlet coverage, both taken from the score breakdown (max 25).

    A story without a platform metric (an article) uses its momentum_proxy: editorial weight as a percentile
    of recent publications. Mirrored in web/signal-ranking.js."""
    engagement=component(p,'engagement')
    popular=engagement.get('points',0) if engagement.get('available',True) else engagement.get('momentum_proxy',0)
    return round(popular+component(p,'coverage').get('points',0),1)


def significance(p):
    """Editorial score without freshness (max 75): importance independent of age within a long period."""
    return round(p['score']-component(p,'freshness').get('points',0),1)


def measure(p,order):
    return momentum(p) if order=='momentum' else significance(p) if order=='significance' else p['score']


def trending(p,now):
    return 0<=now-news_time(p)<=WINDOWS['trending'][0]*3600


def iso(stamp):
    return datetime.fromtimestamp(stamp,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if stamp else None


def number(value):
    return f'{value:,.0f}'.replace(',',' ') if isinstance(value,(int,float)) and float(value).is_integer() else str(value)


def http_url(url):
    return url if urlsplit(url or '').scheme in ('http','https') else ''


def compact(p):
    """Fields an agent needs to cite and judge a story, without the full scoring payload."""
    return dict(id=p['id'],title=p['title'],url=http_url(p.get('url')),discussion_url=http_url(p.get('discussion_url')) or None,
                source=p.get('source_name'),sources=p.get('sources',[]),category=p.get('column'),topic=p.get('topic'),
                published_at=iso(p['published_at']),date_kind=p.get('date_kind','published'),
                original_published_at=iso(p.get('original_published_at')),score=p['score'],
                reasons=p.get('reasons',[]),summary=p.get('summary',''),
                metrics=[dict(kind=m['kind'],value=m['value'],label=m['label']) for m in p.get('metrics',[])],
                score_components={c['id']:(c['points'] if c.get('available',True) else None) for c in p.get('components',[])},
                momentum=momentum(p),momentum_parts=momentum_parts(p),significance=significance(p),news_at=iso(news_time(p)),
                enterprise_reasons=p.get('enterprise_reasons') or [],
                also=[dict(source=a['source'],title=a['title'],url=http_url(a.get('url'))) for a in p.get('also',[])],cloud_availability=p.get('cloud_availability',[]))


def signals_json(data,period=DEFAULT_PERIOD,category='all',limit=DEFAULT_LIMIT,now=None):
    rows=ranked(data,category,period,now)
    return json.dumps(dict(generated_at=iso(data['generated_at']),scoring_version=data['scoring_version'],
                           period=period,period_hours=WINDOWS[period][0],order=WINDOWS[period][1],category=category,total=len(rows),
                           items=[compact(p) for p in rows[:limit]]),ensure_ascii=False)


def link(params):
    return '?'+urlencode(params)


def md_text(value):
    # Titles go inside [...]: keep them on one line and neutralise Markdown link syntax.
    return re.sub(r'([\[\]\\*_`<>])',r'\\\1',re.sub(r'\s+',' ',str(value or '')).strip())


def md_url(url):
    return quote(http_url(url),safe=':/?#&=%+@,;~!$\'*')


def md_item(position,p,order='editorial'):
    name,top=MEASURES[order]
    value=f'{p["score"]}/100' if order=='editorial' else f'{name} {measure(p,order)}/{top}, score {p["score"]}/100'
    lines=[f'{position}. [{md_text(p["title"])}]({md_url(p.get("url"))}) — {value}']
    when=({'trend':'trending since ','updated':'updated '}.get(p.get('date_kind'),'published '))+iso(p['published_at'])
    if p.get('original_published_at'):when+=f'; original date {iso(p["original_published_at"])}'
    lines.append(f'   - Source: {md_text(p.get("source_name"))}; {when}')
    if order=='momentum':
        parts=momentum_parts(p)
        lines.append(f'   - Momentum: popularity {round(parts["popularity"],1)} ({parts["basis"]}; {md_text(parts["note"])}) + coverage {parts["coverage"]}')
    details=[f'topic: {md_text(p.get("topic"))}']
    if p.get('coverage',1)>1:details.append(f'platforms: {p["coverage"]} ({md_text(", ".join(p.get("sources",[])+p.get("related_sources",[])))})')
    if p.get('reasons'):details.append('why it ranks: '+md_text('; '.join(p['reasons'])))
    line='; '.join(details)
    lines.append('   - '+line[:1].upper()+line[1:])
    if p.get('metrics'):lines.append('   - Metrics: '+'; '.join(f'{number(m["value"])} {md_text(m["label"])}' for m in p['metrics']))
    if p.get('cloud_availability'):lines.append('   - Available on: '+', '.join(f'[{md_text(a["label"])}'+(' (preview)' if a.get('stage')=='preview' else '')+f']({md_url(a["url"])})' for a in p['cloud_availability']))
    if p.get('enterprise_reasons'):lines.append('   - Enterprise: '+md_text('; '.join(p['enterprise_reasons'])))
    if p.get('also'):lines.append('   - Also covered by: '+'; '.join(f'[{md_text(a["source"])}: {md_text(a["title"])}]({md_url(a.get("url"))})' for a in p['also']))
    if p.get('discussion_url') and p['discussion_url']!=p.get('url'):lines.append(f'   - Discussion: <{md_url(p["discussion_url"])}>')
    if p.get('summary'):
        summary=md_text(p['summary'])
        lines.append('   > '+(summary[:400].rsplit(' ',1)[0]+'…' if len(summary)>400 else summary))
    return '\n'.join(lines)


def markdown(data,period=DEFAULT_PERIOD,category='all',limit=DEFAULT_LIMIT,now=None,base_url=''):
    rows=ranked(data,category,period,now)
    params=dict(sort=period,category=category,limit=limit)
    heading=f'{CATEGORIES[category]} · Trending Now' if period=='trending' else f'{CATEGORIES[category]} over {PERIOD_LABELS[period]}'
    order=WINDOWS[period][1]
    out=[f'# Signal — AI news radar: {heading}','',
         f'Updated {iso(data["generated_at"])}. Scoring version: {data["scoring_version"]}. '
         f'Matching stories: {len(rows)}, shown: {min(limit,len(rows))}.','',
         f'Other formats: [JSON]({base_url}/api/signals{link(params)}) · [RSS]({base_url}/api/rss?sort={"day" if period in ("now","trending") else period}) · '
         f'[site guide for agents]({base_url}/llms.txt). Parameters: sort={"|".join(SORTS)}, '
         f'category={"|".join(CATEGORIES)}, limit=1..{MAX_LIMIT}.','',
         'The 0–100 editorial score combines relevance, freshness, evidence, coverage, primary sources and popularity. '
         'It does not verify claims.','',f'## {CATEGORIES[category]}','']
    out+=[PERIOD_NOTES[period]+' Periods use the story date: publication, model or paper creation, first appearance in trends, or the model update date supplied by Hype.','']
    out+= [md_item(i,p,order) for i,p in enumerate(rows[:limit],1)] or ['No stories in this period.']
    if category=='all':
        for key in [k for k in CATEGORIES if k!='all']:
            top=ranked(data,key,period,now)[:5]
            if not top:continue
            out+=['',f'## {CATEGORIES[key]}: top 5',f'Full list: [{base_url}/signals.md{link(dict(sort=period,category=key))}]({base_url}/signals.md{link(dict(sort=period,category=key))})','']
            out+=[md_item(i,p,order) for i,p in enumerate(top,1)]
    return '\n'.join(out)+'\n'


def llms_txt(data,base_url=''):
    counts=', '.join(f'{label.lower()} — {len((data.get("signal_rankings") or {}).get(key,[]))}'
                     for key,label in CATEGORIES.items() if key!='all')
    return f'''# Signal — AI news radar

> An editorial AI news radar covering models, GitHub, research, publications, labs, cloud, Reddit and Hacker News. Each story has an explainable 0–100 editorial score. Updated {iso(data["generated_at"])}; scoring version {data["scoring_version"]}.

The page includes static HTML and JavaScript enhancements. The views below use the same published dataset and ordering as the dashboard.

## Data

- [Markdown digest]({base_url}/signals.md): overall ranking and top five per category. The API defaults to 48 hours; use `sort=trending` for the dashboard default, ordered by momentum. Parameters: `sort` = {" | ".join(SORTS)}; `category` = {" | ".join(CATEGORIES)}; `limit` = 1..{MAX_LIMIT}.
- [Signals JSON]({base_url}/api/signals): same parameters; includes id, title, url, source, UTC ISO 8601 dates, scores, components, reasons, metrics and summary.
- [RSS]({base_url}/api/rss?sort=day): `sort` = {" | ".join(PERIODS)}.
- [Full dataset]({base_url}/api/news): archived stories and score breakdowns; can be several megabytes.

## Categories

{counts}.

## Reading scores

- Relevance: 30; freshness: 25; evidence: 15; independent platform coverage: 15; primary source: 5; platform popularity: 10 (percentile within the same source over 7 days).
- Missing metrics remain `null`, not zero. Platform units are kept separate.
- Trending Now (`sort=trending`) orders by momentum, up to 25: popularity plus coverage. `momentum_parts` in JSON gives both and the popularity basis: a percentile of the platform metric within the same source over 7 days, or, for articles without one, the percentile of editorial weight (relevance + evidence + primary source) among recent publications. In every period, after three stories of one source further ones compete for the top ten with a fifth of the measure's scale taken off (5 momentum, 20 score, 15 significance), so lists are not strictly sorted by their measure.
- Scores help prioritize news; they do not verify claims. Check the original `url`.
- Periods use the story date. Hype model dates represent updates, not original model creation.
'''


def robots_txt(base_url=''):
    return f'User-agent: *\nAllow: /\n\n# Machine-readable views: {base_url}/llms.txt\n'


# Static fragments mirror the markup of web/dashboard.js so the page is readable before (or without) JavaScript.

def e(value):
    return html.escape(str(value or ''),quote=True)


def static_signal(p,i,order='momentum'):
    name,top=MEASURES[order];value=measure(p,order)
    return (f'<article class="signal-card"><header class="signal-head"><span class="signal-rank">{i:02d}</span>'
            f'<span class="signal-meta"><span class="signal-source">{e(p.get("source_name"))}</span><time datetime="{iso(p["published_at"])}">{"updated " if p.get("date_kind")=="updated" else ""}{iso(p["published_at"])[:10]}</time></span></header>'
            f'<h3><a href="{e(http_url(p.get("url")))}">{e(p["title"])}</a></h3>'
            f'<p class="signal-summary">{e(p.get("summary"))}</p>'+static_also(p)+static_cloud(p)+
            f'<ul class="signal-reasons">{"".join(f"<li>{e(x)}</li>" for x in p.get("reasons",[])[:3])}</ul>'
            f'<footer class="signal-foot"><span class="signal-score {order}"><b class="score-value">{value}<small>/{top}</small></b>'
            f'<span class="score-bar"><i style="width:{max(0,min(100,100*value/top))}%"></i></span><span class="score-label">{name}'
            +(f' · score {p["score"]}/100' if order!='editorial' else '')+'</span></span></footer></article>')


def static_also(p):
    if not p.get('also'):return ''
    return '<p class="card-also"><span>Also covered by:</span> '+', '.join(f'<a href="{e(http_url(a.get("url")))}" title="{e(a["title"])}">{e(a["source"])}</a>' for a in p['also'])+'</p>'


def static_cloud(p):
    entries=p.get('cloud_availability',[])
    if not entries:return ''
    links=[f'<a href="{e(http_url(a["url"]))}">{e(a["label"])}'+(' (preview)' if a.get('stage')=='preview' else '')+'</a>' for a in entries[:6]]
    return '<div class="cloud-availability"><span>Available on:</span> '+ ' · '.join(links)+'</div>'


def static_card(p,order='momentum'):
    name,top=MEASURES[order]
    meta=[e(p.get('topic'))]+[f'{e(number(m["value"]))} {e(m["label"])}' for m in p.get('metrics',[])[:2]]
    return (f'<article class="news-card"><div class="card-head"><span class="source-label"><span class="source-name">{e(p.get("source_name"))}</span></span>'
            f'<time datetime="{iso(p["published_at"])}">{"updated " if p.get("date_kind")=="updated" else ""}{iso(p["published_at"])[:10]}</time></div>'
            f'<h3><a href="{e(http_url(p.get("url")))}">{e(p["title"])}</a></h3>'
            +(f'<p class="card-summary">{e(p["summary"])}</p>' if p.get('summary') else '')+static_also(p)+static_cloud(p)+
            f'<p class="card-meta">{" · ".join(meta)}</p><div class="card-footer"><span>{e(p.get("domain"))}</span>'
            f'<span class="score-link {order}" title="{name.capitalize()} of {top}"><b>{measure(p,order)}</b></span></div></article>')


def static_fragments(data,now=None,signals=4,feed=40):
    # Same view the page opens on (Trending Now); quiet hours fall back to the day, then the week.
    period=next((key for key in ('trending','day','week') if ranked(data,'all',key,now)),None)
    rows=ranked(data,'all',period,now) if period else sorted(data['items'],key=lambda p:-p['published_at'])
    order=WINDOWS[period][1] if period else 'editorial'
    top=rows[:10]
    ld=dict({'@context':'https://schema.org','@type':'ItemList','name':'Signal — top AI signals',
             'itemListOrder':'https://schema.org/ItemListOrderDescending',
             'itemListElement':[dict({'@type':'ListItem'},position=i,url=http_url(p.get('url')),name=p['title'])
                                for i,p in enumerate(top,1)]})
    # Inside <script> only "<" can end the block; escape it like the embedded dashboard data.
    ld_json=json.dumps(ld,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return dict(signals=''.join(static_signal(p,i,order) for i,p in enumerate(rows[:signals],1)),
                feed=''.join(static_card(p,order) for p in rows[:feed]),json_ld=ld_json)

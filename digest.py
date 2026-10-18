"""Machine-readable views of a published dataset: Markdown digest, compact JSON, llms.txt and static HTML.

Agents usually fetch a page without running JavaScript, so everything here is derived on the server
from the same dataset and ordering the dashboard uses."""
from datetime import datetime, timezone
import html
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit
from dashboard import PERIODS

CATEGORIES={'all':'Общий рейтинг','models':'Модели','repos':'GitHub','papers':'Научные статьи',
            'media':'Издания','enterprise':'Enterprise','cloud':'Облака'}
PERIOD_LABELS={'trending':'12 часов','day':'24 часа','week':'7 дней','month':'30 дней','year':'365 дней','now':'48 часов'}
SORTS=['trending','day','week','month','year','now']
# Period tabs, mirrored from web/signal-ranking.js: the window counts from when a story became news (news_at),
# and each tab sorts by one measure. 'now' (48 h, editorial) remains only for existing API clients.
WINDOWS={'trending':(12,'momentum'),'day':(24,'editorial'),'week':(168,'significance'),'month':(720,'significance'),
         'year':(8760,'significance'),'now':(48,'editorial')}
MEASURES={'momentum':('импульс',25),'editorial':('балл',100),'significance':('значимость',75)}
PERIOD_NOTES={'trending':'Trending Now: что появилось за последние 12 часов и сильнее всего обсуждается. Порядок — импульс: популярность на платформе + охват площадок, до 25.',
              'day':'Главное за 24 часа по редакционному баллу (со свежестью), до 100.',
              'week':'Самое значимое за 7 дней. Значимость — редакционный балл без свежести, до 75.',
              'month':'Самое значимое за 30 дней по значимости (балл без свежести, до 75).',
              'year':'Самое значимое за 365 дней по значимости (балл без свежести, до 75).',
              'now':'Записи за 48 часов по редакционному баллу (устаревший параметр API).'}
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
    return sorted(rows,key=lambda p:(-measure(p,order),-p['score'],-p['published_at'],str(p['id'])))


def news_time(p):
    """When the story became news: publication, model/paper creation, first day of a repository in trends."""
    return p.get('news_at') or p['published_at']


def component(p,key):
    return next((c for c in p.get('components',[]) if c['id']==key),{})


def momentum(p):
    """Platform popularity plus cross-outlet coverage, both taken from the score breakdown (max 25)."""
    engagement=component(p,'engagement')
    return round((engagement.get('points',0) if engagement.get('available',True) else 0)+component(p,'coverage').get('points',0),1)


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
                momentum=momentum(p),significance=significance(p),news_at=iso(news_time(p)),
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
    value=f'{p["score"]}/100' if order=='editorial' else f'{name} {measure(p,order)}/{top}, балл {p["score"]}/100'
    lines=[f'{position}. [{md_text(p["title"])}]({md_url(p.get("url"))}) — {value}']
    when=('в трендах с ' if p.get('date_kind')=='trend' else 'опубликовано ')+iso(p['published_at'])
    if p.get('original_published_at'):when+=f'; исходная дата {iso(p["original_published_at"])}'
    lines.append(f'   - Источник: {md_text(p.get("source_name"))}; {when}')
    details=[f'тема: {md_text(p.get("topic"))}']
    if p.get('coverage',1)>1:details.append(f'площадок: {p["coverage"]} ({md_text(", ".join(p.get("sources",[])+p.get("related_sources",[])))})')
    if p.get('reasons'):details.append('почему в топе: '+md_text('; '.join(p['reasons'])))
    line='; '.join(details)
    lines.append('   - '+line[:1].upper()+line[1:])
    if p.get('metrics'):lines.append('   - Метрики: '+'; '.join(f'{number(m["value"])} {md_text(m["label"])}' for m in p['metrics']))
    if p.get('cloud_availability'):lines.append('   - Где взять: '+', '.join(f'[{md_text(a["label"])}'+(' (preview)' if a.get('stage')=='preview' else '')+f']({md_url(a["url"])})' for a in p['cloud_availability']))
    if p.get('enterprise_reasons'):lines.append('   - Enterprise: '+md_text('; '.join(p['enterprise_reasons'])))
    if p.get('also'):lines.append('   - Также пишут: '+'; '.join(f'[{md_text(a["source"])}: {md_text(a["title"])}]({md_url(a.get("url"))})' for a in p['also']))
    if p.get('discussion_url') and p['discussion_url']!=p.get('url'):lines.append(f'   - Обсуждение: <{md_url(p["discussion_url"])}>')
    if p.get('summary'):
        summary=md_text(p['summary'])
        lines.append('   > '+(summary[:400].rsplit(' ',1)[0]+'…' if len(summary)>400 else summary))
    return '\n'.join(lines)


def markdown(data,period=DEFAULT_PERIOD,category='all',limit=DEFAULT_LIMIT,now=None,base_url=''):
    rows=ranked(data,category,period,now)
    params=dict(sort=period,category=category,limit=limit)
    heading=f'{CATEGORIES[category]} · Trending Now' if period=='trending' else f'{CATEGORIES[category]} за {PERIOD_LABELS[period]}'
    order=WINDOWS[period][1]
    out=[f'# Signal — AI news radar: {heading}','',
         f'Обновлено {iso(data["generated_at"])}. Формула рейтинга: {data["scoring_version"]}. '
         f'Записей в выборке: {len(rows)}, показано: {min(limit,len(rows))}.','',
         f'Другие форматы: [JSON]({base_url}/api/signals{link(params)}) · [RSS]({base_url}/api/rss?sort={"day" if period in ("now","trending") else period}) · '
         f'[описание сайта для агентов]({base_url}/llms.txt). Параметры: sort={"|".join(SORTS)}, '
         f'category={"|".join(CATEGORIES)}, limit=1..{MAX_LIMIT}.','',
         'Балл — редакционная оценка 0–100 (тема, свежесть, конкретика, охват площадок, первоисточник, популярность), '
         'а не проверка достоверности.','',f'## {CATEGORIES[category]}','']
    out+=[PERIOD_NOTES[period]+' Период считается от момента, когда запись стала новостью (публикация, создание модели или статьи, первый день репозитория в трендах).','']
    out+= [md_item(i,p,order) for i,p in enumerate(rows[:limit],1)] or ['Нет записей за этот период.']
    if category=='all':
        for key in [k for k in CATEGORIES if k!='all']:
            top=ranked(data,key,period,now)[:5]
            if not top:continue
            out+=['',f'## {CATEGORIES[key]}: топ-5',f'Полный список: [{base_url}/signals.md{link(dict(sort=period,category=key))}]({base_url}/signals.md{link(dict(sort=period,category=key))})','']
            out+=[md_item(i,p,order) for i,p in enumerate(top,1)]
    return '\n'.join(out)+'\n'


def llms_txt(data,base_url=''):
    counts=', '.join(f'{label.lower()} — {len((data.get("signal_rankings") or {}).get(key,[]))}'
                     for key,label in CATEGORIES.items() if key!='all')
    return f'''# Signal — AI news radar

> Редакционный радар новостей об ИИ: релизы моделей, GitHub Trending, Hugging Face Models и Papers, издания, блоги лабораторий, Reddit и Hacker News. Каждая запись получает объяснимый балл 0–100. Данные обновлены {iso(data["generated_at"])}, формула {data["scoring_version"]}.

Сайт рендерится JavaScript-ом. Для чтения без браузера используйте форматы ниже: они строятся из той же публикации и в том же порядке, что и дашборд.

## Данные

- [Дайджест в Markdown]({base_url}/signals.md): общий топ и топ-5 каждой категории, по умолчанию за 48 часов; `sort=trending` — то, что дашборд показывает первым: в трендах сейчас, по импульсу. Параметры: `sort` = {" | ".join(SORTS)} (`trending` — что в трендах сейчас, по импульсу); `category` = {" | ".join(CATEGORIES)}; `limit` = 1..{MAX_LIMIT}.
- [Сигналы в JSON]({base_url}/api/signals): те же параметры; поля id, title, url, source, published_at (UTC ISO 8601), score, score_components, reasons, metrics, summary.
- [RSS]({base_url}/api/rss?sort=day): `sort` = {" | ".join(PERIODS)}.
- [Полная публикация]({base_url}/api/news): весь архив с разбором балла, несколько мегабайт.

## Категории

{counts}.

## Как читать балл

- Тема (до 30), свежесть (до 25), конкретика (до 15), охват разных площадок (до 15), первоисточник (до 5), популярность на платформе (до 10).
- Отсутствующая метрика не превращается в ноль: в JSON её значение `null`.
- Балл помогает выбрать новости и не подтверждает их достоверность: проверяйте первоисточник по `url`.
'''


def robots_txt(base_url=''):
    return f'User-agent: *\nAllow: /\n\n# Machine-readable views: {base_url}/llms.txt\n'


# Static fragments mirror the markup of web/dashboard.js so the page is readable before (or without) JavaScript.

def e(value):
    return html.escape(str(value or ''),quote=True)


def static_signal(p,i,order='momentum'):
    name,top=MEASURES[order];value=measure(p,order)
    return (f'<article class="signal-card"><header class="signal-head"><span class="signal-rank">{i:02d}</span>'
            f'<span class="signal-meta"><span class="signal-source">{e(p.get("source_name"))}</span><time datetime="{iso(p["published_at"])}">{iso(p["published_at"])[:10]}</time></span></header>'
            f'<h3><a href="{e(http_url(p.get("url")))}">{e(p["title"])}</a></h3>'
            f'<p class="signal-summary">{e(p.get("summary"))}</p>'+static_also(p)+static_cloud(p)+
            f'<ul class="signal-reasons">{"".join(f"<li>{e(x)}</li>" for x in p.get("reasons",[])[:3])}</ul>'
            f'<footer class="signal-foot"><span class="signal-score {order}"><b class="score-value">{value}<small>/{top}</small></b>'
            f'<span class="score-bar"><i style="width:{max(0,min(100,100*value/top))}%"></i></span><span class="score-label">{name}'
            +(f' · балл {p["score"]}/100' if order!='editorial' else '')+'</span></span></footer></article>')


def static_also(p):
    if not p.get('also'):return ''
    return '<p class="card-also"><span>Также пишут:</span> '+', '.join(f'<a href="{e(http_url(a.get("url")))}" title="{e(a["title"])}">{e(a["source"])}</a>' for a in p['also'])+'</p>'


def static_cloud(p):
    entries=p.get('cloud_availability',[])
    if not entries:return ''
    links=[f'<a href="{e(http_url(a["url"]))}">{e(a["label"])}'+(' (preview)' if a.get('stage')=='preview' else '')+'</a>' for a in entries[:6]]
    return '<div class="cloud-availability"><span>Где взять:</span> '+ ' · '.join(links)+'</div>'


def static_card(p,order='momentum'):
    name,top=MEASURES[order]
    meta=[e(p.get('topic'))]+[f'{e(number(m["value"]))} {e(m["label"])}' for m in p.get('metrics',[])[:2]]
    return (f'<article class="news-card"><div class="card-head"><span class="source-label"><span class="source-name">{e(p.get("source_name"))}</span></span>'
            f'<time datetime="{iso(p["published_at"])}">{iso(p["published_at"])[:10]}</time></div>'
            f'<h3><a href="{e(http_url(p.get("url")))}">{e(p["title"])}</a></h3>'
            +(f'<p class="card-summary">{e(p["summary"])}</p>' if p.get('summary') else '')+static_also(p)+static_cloud(p)+
            f'<p class="card-meta">{" · ".join(meta)}</p><div class="card-footer"><span>{e(p.get("domain"))}</span>'
            f'<span class="score-link {order}" title="{name.capitalize()} из {top}"><b>{measure(p,order)}</b></span></div></article>')


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

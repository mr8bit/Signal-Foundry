"""Archive collected posts and build the self-contained dashboard."""
import argparse
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path
import sqlite3
import time
import os
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from email.utils import formatdate
import bot
from cloud_news import prepare_cloud_posts
import attention
import embeddings
from signals import build_items, popularity_baseline, channel_baseline, share_attention, SCORING_VERSION, SIGNAL_CATEGORIES, enterprise_reasons

LOG=logging.getLogger('dashboard')
TELEMETRY='data/telemetry.json'


def record_telemetry(section,values):
    """Outcome of the last attention, embeddings and build run for the host's monitoring collector, which reads
    files and databases but not logs. Only the collector process writes it."""
    try:current=json.loads(Path(TELEMETRY).read_text())
    except (OSError,ValueError):current={}
    current[section]=dict(values,at=time.time())
    write_atomic(TELEMETRY,json.dumps(current,ensure_ascii=False))


PERIODS={'now':48,'day':24,'week':168,'month':720,'year':8760}


def archive_posts(posts,path='data/news.sqlite3'):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS news (key TEXT PRIMARY KEY, payload TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL)')
        now=time.time()
        for p in posts:
            key=(p.get('source_id') or 'reddit:'+p.get('subreddit',''))+':'+p['id']
            db.execute('INSERT INTO news VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen',
                       (key,json.dumps(p,ensure_ascii=False),now,now))
        db.execute('DELETE FROM news WHERE last_seen < ?',(now-366*86400,))
        # first_seen gives trending repositories (no creation date) an honest freshness reference.
        return [json.loads(payload)|{'first_seen':first} for payload,first in db.execute('SELECT payload,first_seen FROM news')]


def dataset(posts,cfg,statuses,now=None,readings=None,vectors=None):
    now=now or time.time()
    enabled={s['id'] for s in cfg.get('feeds',[]) if s.get('enabled',True)}
    posts=[p for p in posts if p.get('source')!='site' or p.get('source_id') in enabled]
    posts,pending=prepare_cloud_posts(posts,cfg,now)
    # One popularity baseline for every collection, so a subset does not shrink a source's history.
    # Attention readings are only read here; the collector records them (attention.refresh).
    readings=attention.load(now) if readings is None else readings
    baseline=popularity_baseline(posts,now,cfg)|channel_baseline(readings)
    # Story vectors are only read here; build() embeds new stories before calling dataset.
    vectors=embeddings.Store().load() if vectors is None else vectors
    common=dict(baseline=baseline,readings=readings,vectors=vectors)
    items=build_items(posts,cfg,now,**common)
    # Rank each collection independently: a newsletter linking many models must
    # not replace model cards through the mixed feed's cross-source deduplication.
    rankings={category:build_items([p for p in posts if SIGNAL_CATEGORIES.get(p.get('category'))==category],cfg,now,**common)
              for category in ('models','repos','papers','media')}
    rankings['enterprise']=build_items([p for p in posts if enterprise_reasons(p)],cfg,now,focus='enterprise',**common)
    rankings['cloud']=build_items([p for p in posts if p.get('cloud_rule') or p.get('cloud_availability')],cfg,now,**common)
    rankings['russia']=build_items([p for p in posts if p.get('region')=='ru'],cfg,now,**common)
    rankings={category:share_attention(rows,items) for category,rows in rankings.items()}
    return dict(generated_at=now,items=items,signal_rankings=rankings,statuses=statuses,cloud_pending=pending,
        sources=[dict(id=s['id'],name=s['name'],category=s['category'],categories=s.get('categories',[s['category']])) for s in cfg.get('feeds',[]) if s.get('enabled',True)]+[
            dict(id='reddit:'+s,name='r/'+s,category='community') for s in cfg['subreddits']],
        oldest_at=min((p['published_at'] for p in items),default=now),
        scoring_version=SCORING_VERSION,summary_mode='extractive')


def rss(data,period='day',base_url='http://127.0.0.1:8765',limit=100):
    if period not in PERIODS: raise ValueError('Unknown period')
    content='http://purl.org/rss/1.0/modules/content/'
    meta='urn:ai-signal:feed:1'
    ET.register_namespace('content',content);ET.register_namespace('signal',meta)
    root=ET.Element('rss',version='2.0');channel=ET.SubElement(root,'channel')
    for key,value in [('title','AI Signal · '+period),('link',base_url+'/'),('description','AI news from Reddit, publications and research labs'),
                      ('lastBuildDate',formatdate(data['generated_at'],usegmt=True)),('ttl','15')]:
        ET.SubElement(channel,key).text=value
    ET.SubElement(channel,'{'+meta+'}scoring_version').text=data['scoring_version']
    now=time.time()
    # Same rule as the dashboard tabs: the period counts from when a story became news, not its last trend sighting.
    items=[p for p in data['items'] if 0<=now-p.get('news_at',p['published_at'])<=PERIODS[period]*3600][:limit]
    for rank,p in enumerate(items,1):
        item=ET.SubElement(channel,'item')
        for key,value in [('title',p['title']),('link',p['url']),('pubDate',formatdate(p['published_at'],usegmt=True)),('description',p['summary'])]:
            ET.SubElement(item,key).text=value
        ET.SubElement(item,'guid',isPermaLink='false').text='ai-signal:'+p['id']
        for key,value in [('source',p['source_name']),('rank',rank),('editorial_score',p['score']),('coverage',p['coverage'])]:
            ET.SubElement(item,'{'+meta+'}'+key).text=str(value)
        if p.get('raw_score') is not None:
            ET.SubElement(item,'{'+meta+'}raw_score').text=str(p['raw_score'])
        ET.SubElement(item,'{'+meta+'}date_kind').text=p.get('date_kind','published')
        if p.get('original_published_at') is not None:
            ET.SubElement(item,'{'+meta+'}original_published_at').text=formatdate(p['original_published_at'],usegmt=True)
        for cloud in p.get('cloud_availability',[]):
            ET.SubElement(item,'{'+meta+'}cloud_availability',provider=cloud['provider'],label=cloud['label'],url=cloud['url'],stage=cloud['stage'])
        for metric in p.get('metrics',[]):
            ET.SubElement(item,'{'+meta+'}metric',kind=metric['kind'],label=metric['label']).text=str(metric['value'])
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def write_atomic(path,value):
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='wb',dir=target.parent,prefix=target.name+'.',suffix='.tmp',delete=False) as temp:
        temp.write(value if isinstance(value,bytes) else value.encode('utf-8'))
        temp.flush();os.fsync(temp.fileno())
    Path(temp.name).replace(target)
    directory=os.open(target.parent,os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def script_source():
    return Path('web/signal-ranking.js').read_text()+'\n'+Path('web/dashboard.js').read_text()


def render_html(data):
    import digest  # digest imports PERIODS from this module
    raw=json.dumps(data,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    static=digest.static_fragments(data)
    values={'DASHBOARD_CSS':Path('web/dashboard.css').read_text(),'DASHBOARD_JS':script_source(),'DASHBOARD_DATA':raw,
            'JSON_LD':static['json_ld'],'STATIC_SIGNALS':static['signals'],'STATIC_FEED':static['feed']}
    # One pass over the template only: inserted story text can never be mistaken for another placeholder.
    return re.sub(r'/\*([A-Z_]+)\*/|<!--([A-Z_]+)-->',lambda m:values[m[1] or m[2]],Path('web/dashboard.html').read_text())


def write_dashboard(data):
    write_atomic(os.getenv('DASHBOARD_HTML','news-preview.html'),render_html(data))
    write_atomic('data/dashboard.json',json.dumps(data,ensure_ascii=False))
    write_atomic('data/feed.xml',rss(data,base_url=os.getenv('SIGNAL_PUBLIC_URL','http://127.0.0.1:8765')))
    import digest
    write_atomic('data/signals.md',digest.markdown(data))


def build(snapshot='data/feed-snapshot.json',limit=None):
    cfg=json.loads(Path('config.json').read_text())
    posts=json.loads(Path(snapshot).read_text())
    # Keep previously enriched article text; refresh is a separate explicit collection step.
    posts=archive_posts(posts)
    # Only stories the filters keep are compared; general feeds bring many more (CNews: 28 of 200 are about AI).
    now=time.time();started=time.monotonic();store=embeddings.Store()
    try:store.refresh([p for p in posts if not bot.reject(p,cfg,now,max_age_hours=8760)])
    except Exception as exc:
        LOG.exception('embeddings_refresh_failed');store.last=dict(error=exc.__class__.__name__)
    if store.last:record_telemetry('embeddings',dict(store.last,duration_seconds=time.monotonic()-started))
    status_path=Path('data/feed-status.json')
    statuses=json.loads(status_path.read_text()) if status_path.exists() else []
    ranked=time.monotonic()
    data=dataset(posts,cfg,statuses)
    record_telemetry('build',dict(duration_seconds=time.monotonic()-started,ranking_seconds=time.monotonic()-ranked,
                                  archived=len(posts),items=len(data['items'])))
    data['collected_at']=Path(snapshot).stat().st_mtime
    if limit is not None: data['items']=data['items'][:limit]
    write_dashboard(data)
    return data


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',default='data/feed-snapshot.json')
    args=parser.parse_args();data=build(args.snapshot)
    print(f'Dashboard: {len(data["items"])} stories; news-preview.html; data/feed.xml')

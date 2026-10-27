"""Persistent incremental source polling. One source per job, no refresh bypass."""
from datetime import datetime
import json
import logging
from pathlib import Path
import random
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import bot
import db

LOG=logging.getLogger('source_schedule')
DEFAULTS=dict(rss_seconds=1800,reddit_seconds=3600,trend_seconds=3600,papers_seconds=7200,
              slow_source_seconds=3600,request_gap_seconds=10,jitter_ratio=0.15,startup_spread_seconds=10)


def feed_targets(cfg,now=None):
    weekend=datetime.fromtimestamp(time.time() if now is None else now,ZoneInfo(cfg['timezone'])).weekday()>=5
    result=[]
    for index,group in enumerate(cfg['rss_groups']):
        url='https://www.reddit.com/r/'+'+'.join(group)+'/top/.rss?'+urlencode(dict(t='week' if weekend else 'day',limit=cfg['rss_limit']))
        result.append((f'Reddit group {index+1}',url,None))
    return result+[(s['name'],s['url'],s) for s in cfg['feeds'] if s.get('enabled',True)]


class SourceSchedule:
    def __init__(self,state,client):
        self.state=state;self.client=client;self.signature=None;self.targets=[]

    def interval(self,source,cfg):
        policy=DEFAULTS|cfg.get('collection',{})
        if source is None:return policy['reddit_seconds']
        if 'poll_seconds' in source:return source['poll_seconds']
        kind=source.get('format')
        if kind=='hf_papers':return policy['papers_seconds']
        if kind in ('hf_models','github_trending'):return policy['trend_seconds']
        if source.get('id')=='venturebeat' or source.get('category') in ('lab','newsletter'):return policy['slow_source_seconds']
        return policy['rss_seconds']

    def sync(self,cfg):
        targets=feed_targets(cfg);signature=json.dumps([cfg.get('collection'),cfg.get('cache'),targets],sort_keys=True)
        if signature==self.signature:return
        policy=DEFAULTS|cfg.get('collection',{})
        for key,value in policy.items():
            if key not in DEFAULTS or isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=value<float('inf'):
                raise ValueError('Invalid collection setting: '+key)
            if key.endswith('_seconds') and value<=0:raise ValueError(key+' must be positive')
        if policy['jitter_ratio']>1:raise ValueError('collection jitter must not exceed 1')
        self.client.http.configure(cfg.get('cache',{}));now=time.time()
        posts=json.loads(Path('data/feed-snapshot.json').read_text()) if Path('data/feed-snapshot.json').exists() else []
        statuses=json.loads(Path('data/feed-status.json').read_text()) if Path('data/feed-status.json').exists() else []
        previous={s['name']:s for s in statuses};known={s.casefold():s for s in cfg['subreddits']}
        for index,(name,url,source) in enumerate(targets):
            interval=self.interval(source,cfg)
            if not isinstance(interval,(int,float)) or isinstance(interval,bool) or not 0<interval<float('inf'):
                raise ValueError('Source poll_seconds must be positive')
            group={s.casefold() for s in cfg['rss_groups'][index]} if source is None else set()
            batch=[p for p in posts if (p.get('source_id')==source['id'] if source else p.get('subreddit','').casefold() in group)]
            status=previous.get(name,dict(status='scheduled',count=len(batch),error=''))|dict(name=name,url=url)
            row=self.client.http.read(url) or {}
            due=max(now+index*policy['startup_spread_seconds']+random.uniform(0,policy['startup_spread_seconds']),
                    row.get('validated_at',0)+interval,self.client.http.not_before(url))
            with db.connect() as conn:
                conn.execute('INSERT INTO source_schedule(url,name,payload,status,next_at,last_success) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(url) DO NOTHING',
                           (url,name,json.dumps(batch,ensure_ascii=False),json.dumps(status,ensure_ascii=False),due,status.get('checked_at') or 0))
        self.targets=targets;self.known=known;self.policy=policy;self.signature=signature

    def rows(self,full=True):
        columns='*' if full else 'url,next_at'
        with db.connect() as conn:
            return {row['url']:dict(row) for row in conn.execute('SELECT '+columns+' FROM source_schedule')}

    def row(self,url):
        with db.connect() as conn:
            return conn.execute('SELECT * FROM source_schedule WHERE url=%s',(url,)).fetchone()

    def pacing(self):
        urls=[url for _,url,_ in self.targets]
        return self.client.http.not_before_many(urls),self.state.get('collection_next_request',0)

    def due(self,row,not_before=None,next_request=None):
        if not_before is None:not_before=self.client.http.not_before(row['url'])
        if next_request is None:next_request=self.state.get('collection_next_request',0)
        return max(row['next_at'],not_before,next_request)

    def next_due(self,cfg):
        self.sync(cfg);rows=self.rows(full=False);not_before,next_request=self.pacing()
        return min((self.due(rows[url],not_before[url],next_request) for _,url,_ in self.targets),default=time.time()+60)

    def snapshot(self,changed=False):
        rows=self.rows();not_before,next_request=self.pacing();posts=[];statuses=[];last=0
        for name,url,_ in self.targets:
            row=rows[url];posts+=json.loads(row['payload']);status=json.loads(row['status'])
            status['next_fetch_at']=self.due(row,not_before[url],next_request);statuses.append(status);last=max(last,row['last_success'])
        return dict(posts=posts,statuses=statuses,changed=changed,collected_at=last)

    def collect(self,cfg,stop_event):
        self.sync(cfg);rows=self.rows(full=False);not_before,next_request=self.pacing();now=time.time()
        ready=[target for target in self.targets if self.due(rows[target[1]],not_before[target[1]],next_request)<=now]
        if not ready:return self.snapshot()
        name,url,source=min(ready,key=lambda target:rows[target[1]]['next_at'])
        row=self.row(url);batch=json.loads(row['payload']);last_success=row['last_success']
        LOG.info('source_started name=%s',name)
        try:
            if stop_event.is_set():raise RuntimeError('Collection cancelled')
            enriched={p['id']:p for p in batch if p.get('article_text')}
            batch=self.client.get(url,source)
            for p in batch:
                old=enriched.get(p['id'])
                if old and not p.get('article_text'):
                    p['article_text']=old['article_text'];p['text_origin']=old.get('text_origin','article')
                if 'subreddit' in p:p['subreddit']=self.known.get(p['subreddit'].casefold(),p['subreddit'])
            status=self.client.source_status(name,url,len(batch))
            if status['status']=='ok':
                batch=self.client.enrich(batch,cfg,time.time(),stop_event=stop_event)
                last_success=status.get('checked_at') or now
        except (RuntimeError,ValueError) as exc:
            if stop_event.is_set():raise RuntimeError('Collection cancelled') from None
            status=self.client.source_failure(name,url,exc)
            if batch:status.update(status='stale',count=len(batch),cache='snapshot',checked_at=last_success or None)
        if stop_event.is_set():raise RuntimeError('Collection cancelled')
        interval=self.interval(source,cfg)
        due=max(time.time()+interval*(1+random.uniform(0,self.policy['jitter_ratio'])),self.client.http.not_before(url))
        with db.connect() as conn:
            conn.execute('UPDATE source_schedule SET payload=%s,status=%s,next_at=%s,last_attempt=%s,last_success=%s WHERE url=%s',
                       (json.dumps(batch,ensure_ascii=False),json.dumps(status,ensure_ascii=False),due,now,last_success,url))
        self.state.set('collection_next_request',time.time()+self.policy['request_gap_seconds']*(1+random.uniform(0,self.policy['jitter_ratio'])))
        LOG.info('source_finished name=%s status=%s next_at=%.0f',name,status['status'],due)
        return self.snapshot(changed=True)

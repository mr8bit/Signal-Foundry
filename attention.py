"""Attention readings: every signal of interest in a story, stored over time.

Passive readings are metrics the collected sources already carry (GitHub Trending stars, HF likes and
trendingScore, HN points and comments, feed comment counts). Stored as a history they give growth per day.
Active readings are budgeted lookups: HN discussion of a URL, the HF and GitHub APIs for artifacts, Substack
reactions, Google News coverage of a headline and outlets' most-read lists.

The collector writes readings; the dashboard build only reads them and never touches the network."""
from collections import defaultdict
import html
import json
import logging
import math
import os
import re
import time
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET
import bot
import db
from http_cache import HTTPCache, CacheError

LOG=logging.getLogger('attention')
DAY=86400
# Growth per day needs two readings at least this far apart, within the lookback.
GROWTH_MIN_SECONDS,GROWTH_LOOKBACK=6*3600,48*3600
KEEP_SECONDS=30*DAY
# Totals that are only meaningful as growth; the rest are used as read.
GROWTH={'github_stars':'github_stars_day','hf_likes':'hf_likes_day','replicate_runs':'replicate_runs_day'}
DEFAULTS=dict(enabled=True,budget_seconds=150,lookup_hours=72,
              ttl_seconds=dict(hn=7200,hf=10800,github=21600,substack=10800,gnews=10800,most_read=3600),
              # A story found minutes after publication has no other outlets or HN thread yet; while it can still
              # gain most of its Trending Now momentum, its coverage and discussion are checked again sooner.
              fresh_hours=12,fresh_ttl_seconds=dict(hn=1800,gnews=2700),
              limits=dict(hn=150,hf=60,github=60,github_without_token=15,substack=20,gnews=15),
              gnews_categories=['news','lab','newsletter'],substack_sources=[],most_read={})


def norm_url(url):
    """Identity of an article URL across feeds: host without www, path without trailing slash, no query."""
    p=urlsplit(html.unescape(url or '').strip())
    host=(p.hostname or '').lower().removeprefix('www.')
    return host+(p.path.rstrip('/') or '/') if host else ''


def url_keys(p):
    """Keys under which URL-based readings (HN, comments, coverage, most read) of a post are stored."""
    keys={norm_url(u) for u in (p.get('article_url'),p.get('url')) if u}
    return {k for k in keys if k}


def artifact_key(url):
    key=bot.canonical(url or '')
    if key:return key
    p=urlsplit(url or '');parts=[x for x in p.path.split('/') if x]
    if (p.hostname or '').lower().removeprefix('www.')=='replicate.com' and len(parts)==2:
        return 'replicate.com/'+'/'.join(parts).lower()
    return None


class Store:
    def __init__(self,path=None):
        pass

    def record(self,rows,now=None):
        """Store readings; an unchanged value is written at most hourly so histories stay small."""
        now=time.time() if now is None else now
        rows=[(k,c,float(v)) for k,c,v in rows if k and isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0]
        if not rows:return 0
        with db.connect() as connection:
            pairs=tuple({(key,channel) for key,channel,value in rows})
            latest={}
            keys=[key for key,channel in pairs];channels=[channel for key,channel in pairs]
            for row in connection.execute('''SELECT DISTINCT ON (key,channel) key,channel,value,observed_at
                    FROM readings WHERE (key,channel) IN (SELECT * FROM unnest(%s::text[],%s::text[]))
                    ORDER BY key,channel,observed_at DESC''',(keys,channels)):
                latest[(row['key'],row['channel'])]=(row['value'],row['observed_at'])
            pending=[]
            for key,channel,value in rows:
                last=latest.get((key,channel))
                if last and last[0]==value and now-last[1]<3600:continue
                pending.append((key,channel,value,now));latest[(key,channel)]=(value,now)
            connection.cursor().executemany('INSERT INTO readings (key,channel,value,observed_at) VALUES (%s,%s,%s,%s)',pending)
            connection.execute('DELETE FROM readings WHERE observed_at<%s',(now-KEEP_SECONDS,))
        return len(pending)

    def due(self,key,kind,ttl,now):
        with db.connect() as connection:
            row=connection.execute('SELECT checked_at FROM checks WHERE key=%s AND kind=%s',(key,kind)).fetchone()
        return not row or now-row['checked_at']>=ttl

    def checked(self,key,kind,now):
        with db.connect() as connection:
            connection.execute('''INSERT INTO checks (key,kind,checked_at) VALUES (%s,%s,%s)
                ON CONFLICT (key,kind) DO UPDATE SET checked_at=EXCLUDED.checked_at''',(key,kind,now))

    def snapshot(self,now=None,window=7*DAY):
        """{key: {channel: value}} with the latest value per channel and growth per day for totals."""
        now=time.time() if now is None else now
        history=defaultdict(list)
        with db.connect() as connection:
            rows=connection.execute('SELECT key,channel,value,observed_at FROM readings WHERE observed_at>=%s ORDER BY observed_at',
                                    (now-max(window,GROWTH_LOOKBACK),))
            for row in rows:
                history[(row['key'],row['channel'])].append((row['observed_at'],row['value']))
        result=defaultdict(dict)
        for (key,channel),points in history.items():
            stamp,value=points[-1]
            if now-stamp<=window:result[key][channel]=value
            if channel in GROWTH:
                recent=[(t,v) for t,v in points if stamp-t<=GROWTH_LOOKBACK]
                first=recent[0]
                if stamp-first[0]>=GROWTH_MIN_SECONDS:
                    result[key][GROWTH[channel]]=max(0,(value-first[1])/((stamp-first[0])/DAY))
        return dict(result)


def load(now=None):
    """Readings for a build."""
    return Store().snapshot(now)


def passive(posts):
    """Readings the collected posts already carry, keyed by artifact or article URL."""
    rows=[]
    for p in posts:
        metric={m.get('kind'):m.get('value') for m in p.get('metrics',[])}
        artifact=artifact_key(p.get('url'))
        if artifact and artifact.startswith('github.com/') and p.get('source_id')=='github_trending':
            rows.append((artifact,'github_stars',metric.get('stars')))
        if artifact and artifact.startswith('huggingface.co/') and p.get('content_kind')=='model':
            rows.append((artifact,'hf_likes',metric.get('likes')))
            rows.append((artifact,'hf_trending',p.get('provider_trending_score')))
        if artifact and artifact.startswith('replicate.com/'):
            rows.append((artifact,'replicate_runs',metric.get('runs')))
        for key in url_keys(p):
            rows.append((key,'comments',metric.get('comments')))
        if p.get('source_id')=='hn':
            text=p.get('selftext','')
            points,comments=(re.search(rf'(?m)^{name}:\s*(\d+)',text) for name in ('Points','Comments'))
            # An hnrss item's URL is the article it discusses: that article gets the discussion.
            for key in {k for k in url_keys(p) if not k.startswith('news.ycombinator.com')}:
                if points:rows.append((key,'hn_points',int(points[1])))
                if comments:rows.append((key,'hn_comments',int(comments[1])))
    return [row for row in rows if row[2] is not None]


def story_query(title):
    """A Google News query from a headline: its distinctive words, newest two days."""
    words=[w for w in re.findall(r"[\w.+-]+",title) if len(w)>2 and w.casefold() not in QUERY_STOP][:8]
    return ' '.join(words)+' when:2d' if len(words)>=3 else None


QUERY_STOP={'the','and','for','with','from','that','this','are','its','new','how','why','what','your','you','has','have',
            'will','into','over','about','says','said','just','now','can','after','more','than','one','our','their',
            'для','как','что','при','это','его','она','они','все','так','уже','или','над','под','без','про','чем','где'}


def title_tokens(title):
    return {w.casefold()[:6] for w in re.findall(r'[\w.]+',title) if len(w)>3 and w.casefold() not in QUERY_STOP}


def parse_gnews(body,title,own_host=''):
    """Distinct other outlets in Google News results whose headline shares two fifths of ours (three words minimum).
    Outlets retell a headline ("removes language that raised scraping concerns" / "removes controversial scraping
    language"), so half was too strict. The article's own outlet and its reprints under the same name are not coverage."""
    own=re.sub(r'\W','',own_host.removeprefix('www.').rsplit('.',1)[0].casefold())
    try:root=ET.fromstring(body)
    except ET.ParseError:raise RuntimeError('Google News returned invalid XML') from None
    ours=title_tokens(title);outlets=set()
    for item in root.findall('./channel/item'):
        theirs=title_tokens(re.sub(r'\s+-\s+[^-]+$','',item.findtext('title','')))
        shared=ours&theirs
        if len(shared)>=3 and len(shared)>=0.4*min(len(ours),len(theirs)):
            name=re.sub(r'\W','',(item.findtext('source','') or '').casefold())
            if name and not (own and (own in name or name in own)):outlets.add(name)
    return len(outlets)


MOST_READ=re.compile(r'(?is)most(?:\s|<br\s*/?>)*(?:popular|read)')


def parse_most_read(body,base):
    """Article links, in order, from the first list after an outlet's "Most popular" / "Most read" heading."""
    text=body.decode('utf-8','ignore')
    for match in MOST_READ.finditer(text):
        lists=re.search(r'(?is)<(ol|ul)\b.*?</\1>',text[match.end():match.end()+20000])
        if not lists:continue
        links=[]
        for href in re.findall(r'<a\b[^>]*href="([^"#]+)"',lists.group(0)):
            url=urljoin(base,html.unescape(href))
            if url not in links and urlsplit(url).hostname==urlsplit(base).hostname and len(urlsplit(url).path)>12:links.append(url)
        if len(links)>=3:return links
    raise RuntimeError('No most-read list found')


class Lookups:
    def __init__(self,store,http,cfg,now,stop=None):
        self.store,self.http,self.now,self.stop=store,http,now,stop
        self.cfg=dict(DEFAULTS,**cfg.get('attention',{}))
        self.deadline=time.time()+self.cfg['budget_seconds']
        self.token=os.getenv('GITHUB_TOKEN','').strip()
        self.ua=os.getenv('ATTENTION_USER_AGENT','Signal-Foundry/1.0 (+https://github.com/mr8bit/Signal-Foundry)')
        self.counts=defaultdict(int);self.errors=defaultdict(int)

    def open(self):return time.time()<self.deadline and not (self.stop is not None and self.stop.is_set())

    def take(self,kind,key,published=None):
        limit=self.cfg['limits']['github_without_token'] if kind=='github' and not self.token else self.cfg['limits'].get(kind,50)
        fresh=published is not None and self.now-published<self.cfg['fresh_hours']*3600 and kind in self.cfg['fresh_ttl_seconds']
        ttl=self.cfg['fresh_ttl_seconds'][kind] if fresh else self.cfg['ttl_seconds'][kind]
        if not self.open() or self.counts[kind]>=limit or not self.store.due(key,kind,ttl,self.now):return False
        self.counts[kind]+=1;return True

    def fetch(self,kind,key,url,headers=None,ttl=None,validator=None):
        try:
            body=self.http.get(url,dict({'User-Agent':self.ua},**(headers or {})),ttl=ttl or self.cfg['ttl_seconds'][kind],
                               timeout=20,max_bytes=2_000_000,validator=validator).body
        except (CacheError,ValueError) as exc:
            self.errors[kind]+=1;LOG.info('attention_lookup_failed kind=%s error=%s',kind,exc);return None
        self.store.checked(key,kind,self.now)
        return body

    def hn(self,key,url):
        body=self.fetch('hn',key,'https://hn.algolia.com/api/v1/search?restrictSearchableAttributes=url&hitsPerPage=20&query='+quote(url,safe=''))
        if body is None:return []
        hits=[h for h in json.loads(body).get('hits',[]) if norm_url(h.get('url'))==key]
        best=max(hits,key=lambda h:h.get('points') or 0,default=None)
        return [(key,'hn_points',best.get('points') or 0),(key,'hn_comments',best.get('num_comments') or 0)] if best else [(key,'hn_points',0)]

    def hf(self,key):
        body=self.fetch('hf',key,'https://huggingface.co/api/models/'+key.removeprefix('huggingface.co/')+'?expand[]=trendingScore&expand[]=likes')
        if body is None:return []
        data=json.loads(body)
        return [(key,'hf_trending',data.get('trendingScore')),(key,'hf_likes',data.get('likes'))]

    def github(self,key):
        headers={'Accept':'application/vnd.github+json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        body=self.fetch('github',key,'https://api.github.com/repos/'+key.removeprefix('github.com/'),headers)
        return [(key,'github_stars',json.loads(body).get('stargazers_count'))] if body else []

    def substack(self,key,url):
        p=urlsplit(url);slug=[x for x in p.path.split('/') if x][-1:]
        if not slug:return []
        body=self.fetch('substack',key,f'https://{p.hostname}/api/v1/posts/{slug[0]}')
        if body is None:return []
        data=json.loads(body)
        return [(key,'reactions',(data.get('reaction_count') or 0)+(data.get('restacks') or 0)),(key,'comments',data.get('comment_count'))]

    def gnews(self,key,title,own_host=None,region=None):
        query=story_query(title)
        if not query:return []
        # Russian stories are covered by Russian outlets, which the US edition of Google News does not list.
        locale='hl=ru&gl=RU&ceid=RU:ru' if region=='ru' else 'hl=en-US&gl=US&ceid=US:en'
        body=self.fetch('gnews',key,f'https://news.google.com/rss/search?{locale}&q='+quote(query))
        return [(key,'news_outlets',parse_gnews(body,title,own_host or key.split('/',1)[0]))] if body else []

    def most_read(self,name,url):
        if not self.open() or not self.store.due(url,'most_read',self.cfg['ttl_seconds']['most_read'],self.now):return []
        body=self.fetch('most_read',url,url,{'Accept':'text/html'},validator=lambda b:parse_most_read(b,url))
        if body is None:return []
        links=parse_most_read(body,url)
        # The top of the list is worth most; value counts places from the bottom.
        return [(norm_url(link),'most_read',len(links)-i) for i,link in enumerate(links)]


def refresh(posts,cfg,now=None,stop=None,store=None,http=None):
    """Record passive readings, then spend the lookup budget on the newest stories first."""
    now=time.time() if now is None else now
    settings=dict(DEFAULTS,**cfg.get('attention',{}))
    store=store or Store()
    written=store.record(passive(posts),now)
    if not settings['enabled']:return dict(passive=written)
    http=http or HTTPCache();http.configure(cfg.get('cache',{}))
    look=Lookups(store,http,cfg,now,stop)
    recent=sorted((p for p in posts if 0<=now-p['created_utc']<=settings['lookup_hours']*3600),key=lambda p:-p['created_utc'])
    articles,artifacts={},{}
    for p in recent:
        artifact=artifact_key(p.get('url'))
        if artifact:artifacts.setdefault(artifact,p)
        url=p.get('article_url') or p.get('url')
        if p.get('source')=='site' and p.get('source_id')!='hn' and norm_url(url):articles.setdefault(norm_url(url),(url,p))
    # One pass per kind, scarce and high-value lookups first, so the time budget cannot starve a whole kind.
    rows=[]
    for name,url in settings['most_read'].items():rows+=look.most_read(name,url)
    for key,(url,p) in articles.items():
        if p.get('source_id') in settings['substack_sources'] and look.take('substack',key):rows+=look.substack(key,url)
    for key,(url,p) in articles.items():
        if p.get('category') in settings['gnews_categories'] and not p.get('content_kind') and look.take('gnews',key,p['created_utc']):rows+=look.gnews(key,p['title'],p.get('publisher_host'),p.get('region'))
    for key,p in artifacts.items():
        if key.startswith('huggingface.co/') and p.get('content_kind')=='model' and p.get('provider_trending_score') is None and look.take('hf',key):
            rows+=look.hf(key)
    for key,p in artifacts.items():
        if key.startswith('github.com/') and p.get('source_id')!='github_trending' and look.take('github',key):rows+=look.github(key)
    for key,(url,p) in articles.items():
        # A Google News redirect is never the URL submitted to HN, and Russian articles are rarely there.
        if not p.get('publisher_host') and p.get('region')!='ru' and look.take('hn',key,p['created_utc']):rows+=look.hn(key,url)
    written+=store.record(rows,now)
    LOG.info('attention_refresh readings=%s lookups=%s errors=%s',written,dict(look.counts),dict(look.errors))
    return dict(readings=written,lookups=dict(look.counts),errors=dict(look.errors))

#!/usr/bin/env python3
"""Reddit top listings → rule-based selection → Telegram. Python 3.11+."""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from sources import article_url, parse_site_feed, parse_newsroom
from articles import ArticleFetcher
from trends import parse_trends
from presentation import english_post
from http_cache import HTTPCache, CacheError
from cloud_news import cloud_decision,prepare_cloud_posts,availability_for

LOG = logging.getLogger('reposter')


def load_env(path='.env'):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def matches(text, terms):
    # Word boundaries prevent API matching 'capital', AMA matching 'drama', etc.
    return any(re.search(r'(?<!\w)' + re.escape(term) + r'(?![a-z])', text, re.I)
               for term in terms)


def canonical(url):
    p = urlsplit(html.unescape(url).rstrip('.,;!)]>'))
    host = (p.hostname or '').lower().removeprefix('www.')
    parts = [x for x in p.path.split('/') if x]
    if host == 'github.com' and len(parts) >= 2:
        return 'github.com/' + '/'.join(parts[:2]).removesuffix('.git').lower()
    if host == 'huggingface.co':
        if parts and parts[0] in ('blog', 'docs', 'learn', 'posts', 'collections', 'papers', 'organizations'):
            return None
        n = 3 if parts and parts[0] in ('datasets', 'spaces') else 2
        if len(parts) >= n:
            return 'huggingface.co/' + '/'.join(parts[:n]).lower()
    return None


def keys(post):
    urls = re.findall(r'https?://[^\s<>\"\']+', post.get('selftext', ''))
    urls.append(post.get('url', ''))
    urls.extend(post.get('links', []))
    identity = {'article:' + article_url(post['article_url'])} if post.get('source') == 'site' else {'reddit:' + post['id']}
    if post.get('source') == 'site':
        identity.add('title:' + re.sub(r'\W+', '', post['title'].casefold()))
    if post.get('cloud_rule')=='vercel_sdk':return identity
    return identity | {k for u in urls if (k := canonical(u))}


RELEASE_CUES=['release', 'released', 'releases', 'launch', 'launches', 'launched', 'introducing', 'introduces', 'unveils',
              'announces', 'open-source', 'open-sources', 'open weights', 'open model', 'new model']
# Tutorials explain how to use something; their bodies mention releases and benchmarks without being one.
# Russian AI news (filter_mode ru_ai): general and business feeds mention "модель" or "агент" in any topic, so the
# headline must name AI itself.
RU_AI=re.compile(r'(?<![\w-])(?:ИИ(?![а-яё])|AI(?![a-z])|искусственн\w* интеллект\w*|нейросет\w*|нейронн\w* сет\w*|GigaChat|ГигаЧат|'
                 r'YandexGPT|Алис[аы] AI|генеративн\w*|LLM|больш\w* языков\w* модел\w*|машинн\w* обучени\w*|Kandinsky|чат-бот\w*)',re.I)
TUTORIAL=re.compile(r'^\s*(?:how to\b|tutorial\b|guide\b|a (?:coding|step[- ]by[- ]step|hands[- ]on|practical|complete|beginner\W?s?|full)\b|'
                    r'step[- ]by[- ]step\b|building an?\b|build an?\b|implementing\b|getting started\b)',re.I)


def priority(p):
    level=classify(p)
    return max(level,3) if not p.get('content_kind') and TUTORIAL.search(p['title']) else level


def classify(p):
    cloud=cloud_decision(p)
    if cloud and cloud.mode=='standalone':return {'model':1,'hardware':2,'agent_platform':3,'pricing':3}.get(cloud.kind,5)
    if p.get('content_kind') in ('repository','model','paper'):
        return {'repository':3,'model':1,'paper':4}[p['content_kind']]
    text = p['title'] + ' ' + p.get('selftext', '')
    refs = keys(p)
    repo = any(x.startswith('github.com/') for x in refs)
    weights = any(x.startswith('huggingface.co/') for x in refs) or matches(text, ['weights', 'GGUF'])
    numbers = bool(re.search(r'\d[\d.,]*\s*(?:tok/s|tokens/s|GB|GB/s|t/s)', text, re.I))
    # An article body mentions releases in passing; for publications the headline must announce one.
    announcement = p['title'] if p.get('source') == 'site' else text
    if weights and matches(announcement, RELEASE_CUES) and (numbers or matches(text, ['benchmark'])):
        return 1
    if numbers and matches(text, ['VRAM', 'tok/s', 'tokens/s', 'GPU', 'local', 'ran', 'running']):
        return 2
    if repo and matches(text, ['agent', 'MCP', 'local', 'inference', 'ollama']):
        return 3
    if matches(text, ['paper', 'arXiv', 'research']):
        return 4
    return 5


def reject(p, cfg, now, max_age_hours=None):
    site = p.get('source') == 'site'
    sub = p.get('subreddit', '')
    if not site and sub not in cfg['subreddits']:
        return 'subreddit'
    hours = max_age_hours if max_age_hours is not None else (48 if datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).weekday() >= 5 else 24)
    if not 0 <= now - p['created_utc'] <= hours * 3600:
        return 'age'
    if site:
        if matches(p['title'], cfg.get('site_exclude', [])):
            return 'excluded keyword'
        cloud=cloud_decision(p,cfg)
        if cloud:return None if cloud.mode=='standalone' else 'cloud: '+cloud.reason
        if p.get('filter_mode') == 'ru_ai':
            return None if RU_AI.search(p['title']) else 'no keyword'
        if p.get('filter_mode') != 'all' and not matches(p['title'] + ' ' + p.get('selftext', '')[:1500], cfg.get('site_include', cfg['include'])):
            return 'no keyword'
        return None
    if p.get('stickied') or p.get('removed_by_category'):
        return 'removed/sticky'
    if p.get('source') != 'rss' and (p.get('score') or 0) < cfg['subreddits'][sub]:
        return 'score'
    title = p['title']
    if matches(title, cfg['exclude']):
        return 'excluded keyword'
    if not matches(title, cfg['include']):
        return 'no keyword'
    if p.get('source') != 'rss' and sub == 'LocalLLaMA' and (p.get('link_flair_text') or '').casefold() not in {x.casefold() for x in cfg['allowed_localllama_flairs']}:
        return 'flair'
    if sub == 'MachineLearning' and not re.search(r'\[(R|N|P)\]', title, re.I):
        return 'tag'
    if sub in cfg['gated_subreddits'] and not matches(title, cfg['models'] + ['release', 'launch', 'announces', 'released']):
        return 'model/release gate'
    # Industry drama is allowed only in the two designated top listings.
    if matches(title, ['drama', 'controversy', 'scandal', 'lawsuit', 'fired']) and sub not in ('LocalLLaMA', 'singularity'):
        return 'drama'
    return None


def select(posts, cfg, now, max_age_hours=None, key_fn=None):
    posts,_=prepare_cloud_posts(posts,cfg,now,max_age_hours if max_age_hours is not None else 8760)
    key_fn = key_fn or keys
    reasons = Counter()
    accepted = []
    for p in posts:
        reason = reject(p, cfg, now, max_age_hours=max_age_hours)
        if reason:
            reasons[reason] += 1
        else:
            accepted.append(p)
    # Connected components also merge A→HF, B→HF+GitHub, C→GitHub.
    groups = []
    for p in accepted:
        ks = key_fn(p)
        members = [p]
        remaining = []
        for group_keys, group_posts in groups:
            if ks & group_keys:
                ks |= group_keys
                members += group_posts
            else:
                remaining.append((group_keys, group_posts))
        remaining.append((ks, members))
        groups = remaining
    result = []
    for ks, members in groups:
        best = min(members, key=lambda x: (0 if x.get('category') == 'lab' else 1,
                   x.get('feed_rank', 0), -len(x.get('selftext', '')))) if all(x.get('source') in ('rss', 'site') for x in members) else max(members, key=lambda x: x.get('score') or 0)
        if availability_for(members):best=dict(best,cloud_availability=availability_for(members))
        result.append((best, ks))
    result.sort(key=lambda item: (priority(item[0]), item[0].get('feed_rank', 0), -(item[0].get('score') or 0)))
    LOG.info('Scanned=%s accepted=%s groups=%s rejected=%s', len(posts), len(accepted), len(result), dict(reasons))
    return result


def request_json(url, data=None, headers=None):
    req = Request(url, data=data, headers=headers or {})
    try:
        with urlopen(req, timeout=40) as response:
            return json.load(response)
    except HTTPError as exc:
        # Never log exception URLs: Telegram URLs contain the bot token.
        retry = exc.headers.get('Retry-After', '')
        raise RuntimeError(f'HTTP {exc.code}; Retry-After={retry or "unspecified"}') from None
    except (URLError, TimeoutError):
        raise RuntimeError('Network failure or timeout') from None


class FeedHTML(HTMLParser):
    """Extract readable text and links; never forward source HTML to Telegram."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = 0
        self.current_link = ''
        self.primary_link = ''

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('p', 'br', 'div', 'li', 'tr', 'blockquote'):
            self.parts.append('\n')
        if tag == 'a':
            href = dict(attrs).get('href', '')
            self.current_link = href
            if urlsplit(href).scheme in ('http', 'https'):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == 'a':
            self.current_link = ''
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li', 'blockquote'):
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.hidden:
            if text.strip().lower() == '[link]':
                self.primary_link = self.current_link
            self.parts.append(text)

    def text(self):
        text = ''.join(self.parts)
        text = re.split(r'\s*submitted by\s', text, flags=re.I)[0]
        text = re.sub(r'\[(?:link|comments)\]', '', text, flags=re.I)
        return '\n'.join(line for raw in text.splitlines() if (line := re.sub(r'[ \t]+', ' ', raw).strip()))


def external_link(url):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    return p.scheme in ('http', 'https') and not any(host == h or host.endswith('.' + h)
        for h in ('reddit.com', 'redd.it', 'redditmedia.com', 'redditstatic.com'))


def parse_feed(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise RuntimeError('Invalid RSS/Atom XML (possibly an HTML block page)') from None
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise RuntimeError('Expected a Reddit Atom feed')
    posts = []
    for rank, entry in enumerate(root.findall('a:entry', ns)):
        url = next((x.get('href', '') for x in entry.findall('a:link', ns)
                    if x.get('rel', 'alternate') == 'alternate'), '')
        path = urlsplit(url).path
        match = re.search(r'/r/([^/]+)/comments/([^/]+)', path, re.I)
        if not match:
            LOG.warning('Skipping feed entry without a Reddit post URL')
            continue
        stamp = entry.findtext('a:published', namespaces=ns) or entry.findtext('a:updated', namespaces=ns)
        try:
            date = datetime.fromisoformat((stamp or '').replace('Z', '+00:00'))
            if date.tzinfo is None:
                raise ValueError('Missing timezone')
        except ValueError:
            LOG.warning('Skipping feed entry with invalid date')
            continue
        content = entry.findtext('a:content', default='', namespaces=ns)
        parser = FeedHTML()
        parser.feed(content)
        links = list(dict.fromkeys(x for x in parser.links if external_link(x)))
        title = entry.findtext('a:title', default='', namespaces=ns).strip()
        if not title:
            continue
        posts.append(dict(id=match[2], subreddit=match[1], title=title,
                          created_utc=date.timestamp(), permalink=path, source='rss',
                          score=None, link_flair_text=None, selftext=parser.text(),
                          url=parser.primary_link if external_link(parser.primary_link) else (links[0] if links else url), links=links, feed_rank=rank))
    return posts


class FeedBackoff(RuntimeError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f'Feed rate limit; retry after {seconds} seconds')


class RedditRSS:
    def __init__(self, cache_path=None, cache_policy=None):
        self.http = HTTPCache(cache_path,cache_policy)
        self.errors = []
        self.statuses = []
        self.last_cache_info = None
        self.articles = ArticleFetcher(http_cache=self.http)

    def source_status(self,name,url,count):
        info=self.last_cache_info
        row=self.http.read(url) or {}
        return dict(name=name,url=url,count=count,status='stale' if info and info.stale else 'ok',
                    cache=info.state if info else 'unknown',checked_at=info.validated_at if info else None,
                    expires_at=info.expires_at if info else None,error=info.error if info else '',
                    http_status=row.get('status'),retry_at=row.get('retry_at',0))

    def source_failure(self,name,url,error):
        row=self.http.read(url) or {}
        with self.http.connect() as db:
            host=db.execute('SELECT * FROM hosts WHERE host=?',((urlsplit(url).hostname or '').lower(),)).fetchone()
        return dict(name=name,url=url,status='error',error=str(error),count=0,
                    http_status=row.get('status') or (host['status'] if host else None),
                    retry_at=max(row.get('retry_at',0),host['retry_at'] if host else 0))

    def enrich(self, posts, cfg, now, stop_event=None):
        enabled = {s['id'] for s in cfg.get('feeds', []) if s.get('enabled', True) and s.get('fetch_article')}
        for post in posts:
            if stop_event is not None and stop_event.is_set():raise RuntimeError('Collection cancelled')
            if post.get('source_id') not in enabled or reject(post, cfg, now):
                continue
            try:
                text = self.articles.get(post['article_url'], os.getenv('REDDIT_USER_AGENT', 'reddit-reposter/1.0'))
                if len(text) > len(post.get('selftext', '')):
                    post['article_text'] = text
                    post['text_origin'] = 'article'
            except RuntimeError as exc:
                LOG.warning('%s article %s: %s; keeping RSS description', post['source_name'], post['id'], exc)
        return posts

    def get(self, url, source=None, timeout=40):
        self.last_cache_info=None
        def parse(data,observed=None):
            if source and source.get('format') in ('github_trending','hf_models','hf_papers'):
                return parse_trends(data,source,observed)
            return (parse_newsroom(data,source) if source.get('format')=='html' else parse_site_feed(data,source)) if source else parse_feed(data)
        ttl_key={'github_trending':'github_trending_ttl_seconds','hf_models':'hf_models_ttl_seconds',
                 'hf_papers':'hf_papers_ttl_seconds','html':'html_ttl_seconds'}.get((source or {}).get('format'),'rss_ttl_seconds')
        ttl=(source or {}).get('cache_ttl_seconds',self.http.policy[ttl_key])
        headers={'User-Agent':os.getenv('REDDIT_USER_AGENT','reddit-reposter/1.0'),
                 'Accept':'application/json, application/atom+xml, application/rss+xml, application/xml, text/html;q=0.5'}
        try:
            response=self.http.get(url,headers,ttl=ttl,timeout=timeout,validator=parse,loader=urlopen)
        except CacheError as exc:
            if exc.status==429:raise FeedBackoff(exc.retry_after) from None
            raise RuntimeError(str(exc)) from None
        self.last_cache_info=response
        return parse(response.body,response.validated_at)

    def fetch(self, cfg, now, stop_event=None):
        self.http.configure(cfg.get('cache',{}))
        self.articles.ttl=self.http.policy['article_ttl_seconds']
        weekend = datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).weekday() >= 5
        posts = []
        self.errors = []
        self.statuses = []
        known = {s.casefold(): s for s in cfg['subreddits']}
        sources = []
        for index, group in enumerate(cfg['rss_groups']):
            url = 'https://www.reddit.com/r/' + '+'.join(group) + '/top/.rss?' + urlencode({
                't': 'week' if weekend else 'day', 'limit': cfg['rss_limit']})
            sources.append((f'Reddit group {index + 1}', url, None))
        sources.extend((s['name'], s['url'], s) for s in cfg.get('feeds', []) if s.get('enabled', True))
        for index, (name, url, source) in enumerate(sources):
            if stop_event is not None and stop_event.is_set():raise RuntimeError('Collection cancelled')
            if index and self.last_cache_info is not None and self.last_cache_info.state in ('miss','revalidated'):
                if stop_event is not None:
                    if stop_event.wait(cfg['feed_delay_seconds']):raise RuntimeError('Collection cancelled')
                else:time.sleep(cfg['feed_delay_seconds'])
            try:
                batch = self.get(url, source) if source else self.get(url)
            except (RuntimeError, ValueError) as exc:
                error = f'{name}: {exc}'
                self.errors.append(error)
                self.statuses.append(self.source_failure(name,url,exc))
                LOG.warning('%s', error)
                continue
            for post in batch:
                if 'subreddit' in post:
                    post['subreddit'] = known.get(post['subreddit'].casefold(), post['subreddit'])
            posts.extend(batch)
            status=self.source_status(name,url,len(batch))
            self.statuses.append(status)
            if status['status']=='stale':
                self.errors.append(name+': stale cached data')
                LOG.warning('%s: using stale cache (%s)',name,status['error'])
            LOG.info('%s: %s entries', name, len(batch))
        if not posts and self.errors:
            raise RuntimeError('All available feeds returned no entries; ' + '; '.join(self.errors))
        return self.enrich(posts, cfg, now, stop_event=stop_event)


def excerpt(text, limit=900):
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'(?m)^\s*#{1,6}\s*', '', text)
    text = re.sub(r'[*`_]{1,3}', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    # Keep complete sentences when the first paragraph contains a useful one.
    ends = list(re.finditer(r'[.!?](?:\s|$)', cut))
    if ends and ends[-1].start() > limit // 2:
        return cut[:ends[-1].start() + 1]
    return cut.rstrip(' ,;:') + '…'


def render(p):
    p=english_post(p)
    labels = {1: 'Model release', 2: 'On your hardware', 3: 'Tools', 4: 'Research', 5: 'AI news'}
    if p.get('content_kind')=='model': labels[1]='Models'
    title = re.sub(r'^\s*\[(?:R|N|P)\]\s*', '', p['title'], flags=re.I)
    title = html.escape(excerpt(title, 250))
    body = html.escape(excerpt(p.get('article_text') or p.get('selftext', '')))
    site = p.get('source') == 'site'
    url = p['article_url'] if site else 'https://www.reddit.com' + p['permalink']
    sources = list(dict.fromkeys([p.get('url', '')] + p.get('links', [])))
    sources = sorted((x for x in sources if external_link(x) and (not site or article_url(x) != article_url(url))), key=lambda x: not bool(canonical(x)))[:2]
    links = []
    for external in sources:
        host = (urlsplit(external).hostname or '').lower()
        label = 'Hugging Face' if host == 'huggingface.co' else 'GitHub' if host == 'github.com' else host.removeprefix('www.')
        links.append(f'<a href="{html.escape(external, quote=True)}">{html.escape(label)}</a>')
    links.insert(0, f'<a href="{html.escape(url, quote=True)}">{"Read article" if site else "Discussion"}</a>')
    if site and p.get('discussion_url'):
        links.append(f'<a href="{html.escape(p["discussion_url"], quote=True)}">HN discussion</a>')
    credit = p['source_name'] if site else 'r/' + p['subreddit']
    availability=availability_for([p])[:3]
    where=('\n\nAvailable on: '+', '.join(f'<a href="{html.escape(a["url"],quote=True)}">{html.escape(a["label"])}'+(' (preview)' if a.get('stage')=='preview' else '')+'</a>' for a in availability)) if availability else ''
    return (f'{labels[priority(p)]}\n\n<b>{title}</b>\n\n' +
            (body + '\n\n' if body else '') + ' · '.join(links) +
            where+f'\n\n{html.escape(credit)}')


def database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, delivery INTEGER NOT NULL);''')
    return db


def publish(db, groups, cfg):
    if db.execute("SELECT 1 FROM deliveries WHERE status='pending'").fetchone():
        raise RuntimeError('Uncertain pending delivery. Reconcile it with Telegram before retrying; see README.')
    sent = 0
    for p, ks in groups:
        existing = [row[0] for k in ks
                    if (row := db.execute('SELECT delivery FROM aliases WHERE key=?', (k,)).fetchone())]
        if existing:
            with db:
                db.executemany('INSERT OR IGNORE INTO aliases VALUES (?,?)', [(k, existing[0]) for k in ks])
            continue
        last = db.execute("SELECT MAX(created) FROM deliveries WHERE status='sent'").fetchone()[0]
        if last and time.time() - last < cfg['min_publish_seconds']:
            break
        # Durable intent prevents automatic duplicates after an ambiguous HTTP timeout.
        with db:
            delivery = db.execute("INSERT INTO deliveries(status,created) VALUES ('pending',?)", (time.time(),)).lastrowid
            db.executemany('INSERT INTO aliases VALUES (?,?)', [(k, delivery) for k in ks])
        result = request_json('https://api.telegram.org/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
                              json.dumps({'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': render(p),
                                          'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}}).encode(),
                              {'Content-Type': 'application/json'})
        if not result.get('ok'):
            raise RuntimeError('Telegram rejected delivery; pending intent retained for inspection')
        with db:
            db.execute("UPDATE deliveries SET status='sent',message_id=?,created=? WHERE id=?",
                       (result['result']['message_id'], time.time(), delivery))
        sent += 1
        LOG.info('Published post %s', p['id'])
        if sent >= cfg['max_posts_per_cycle']:
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--dry-run', action='store_true', help='Preview without Telegram calls or delivery state writes')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--fixture', help='Offline Atom XML or JSON post array; requires --dry-run')
    args = parser.parse_args()
    if args.fixture and not args.dry_run:
        parser.error('--fixture requires --dry-run')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    load_env()
    cfg = json.loads(Path(args.config).read_text())
    for name in ('poll_seconds', 'min_publish_seconds', 'max_posts_per_cycle', 'rss_limit', 'feed_delay_seconds'):
        if not isinstance(cfg[name], int) or cfg[name] <= 0:
            parser.error(f'{name} must be a positive integer')
    ZoneInfo(cfg['timezone'])
    if not 1 <= cfg['rss_limit'] <= 100:
        parser.error('rss_limit must be between 1 and 100')
    if not cfg.get('rss_groups') or any(not group or any(s not in cfg['subreddits'] for s in group)
                                         for group in cfg['rss_groups']):
        parser.error('rss_groups must contain known subreddits')
    reddit = RedditRSS()
    LOG.warning('RSS mode: score and flair checks unavailable; duplicate winner chosen by feed rank')
    db = None
    lock = None
    if not args.dry_run:
        for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
            if not os.environ.get(key):
                parser.error(f'Set {key} in .env')
        path = os.getenv('STATE_DB', 'data/state.sqlite3')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        lock = open(path + '.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another publisher is already running')
        db = database(path)
    while True:
        delay = cfg['poll_seconds']
        try:
            now = time.time()
            if args.fixture:
                raw = Path(args.fixture).read_bytes()
                posts = json.loads(raw) if args.fixture.endswith('.json') else parse_feed(raw)
            else:
                posts = reddit.fetch(cfg, now)
            groups = select(posts, cfg, now)
            if args.dry_run:
                for post, aliases in groups:
                    print(json.dumps({'id': post['id'], 'priority': priority(post), 'aliases': sorted(aliases), 'text': render(post)}, ensure_ascii=False))
            else:
                publish(db, groups, cfg)
        except FeedBackoff as exc:
            LOG.warning('%s', exc)
            if args.once or args.fixture:
                raise SystemExit(1) from None
            delay = max(delay, exc.seconds)
        except (RuntimeError, KeyError, ValueError, sqlite3.Error) as exc:
            LOG.error('%s', exc)
            if args.once or args.fixture:
                raise SystemExit(1) from None
        if args.once or args.fixture:
            break
        time.sleep(delay)


if __name__ == '__main__':
    main()

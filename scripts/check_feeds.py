#!/usr/bin/env python3
"""Check configured sources once and save an offline snapshot for preview."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot


def main():
    bot.load_env()
    cfg = json.loads(Path('config.json').read_text())
    window = 'week' if datetime.now(ZoneInfo(cfg['timezone'])).weekday() >= 5 else 'day'
    targets = [(s['name'], s['url'], s) for s in cfg.get('feeds', []) if s.get('enabled', True)]
    # Probe one Reddit group per host after site checks, avoiding parallel Reddit requests.
    def check(target):
        name, url, source = target
        try:
            client=bot.RedditRSS(cache_policy=cfg.get('cache'))
            posts = client.get(url, source)
            return client.source_status(name,url,len(posts)), posts
        except (RuntimeError, ValueError) as exc:
            return dict(name=name,url=url,status='error',count=0,error=str(exc)), []
    statuses, all_posts = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for status, posts in pool.map(check, targets):
            statuses.append(status)
            all_posts.extend(posts)
            print(status['name'], status['status'], status['count'], status.get('error', ''), flush=True)
    for index, group in enumerate(cfg['rss_groups']):
        if index:
            time.sleep(cfg['feed_delay_seconds'])
        url = 'https://www.reddit.com/r/' + '+'.join(group) + '/top/.rss?' + urlencode({'t':window,'limit':cfg['rss_limit']})
        status, posts = check((f'Reddit group {index+1}', url, None))
        statuses.append(status)
        all_posts.extend(posts)
        print(status['name'],status['status'],status['count'],status.get('error',''),flush=True)
    Path('data').mkdir(exist_ok=True)
    all_posts = bot.RedditRSS().enrich(all_posts, cfg, time.time())
    Path('data/feed-status.json').write_text(json.dumps(statuses,ensure_ascii=False,indent=2)+'\n')
    Path('data/feed-snapshot.json').write_text(json.dumps(all_posts,ensure_ascii=False,indent=2)+'\n')
    print(f'{sum(s["status"]=="ok" for s in statuses)}/{len(statuses)} sources parsed; {len(all_posts)} entries')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Inspect source cache state; optional probes keep normal TTLs and host cooldowns."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import bot
from source_schedule import feed_targets


def targets(cfg):
    return feed_targets(cfg)


def diagnose(cfg,probe=False,source=None,timeout=40):
    selected=[t for t in targets(cfg) if not source or source.casefold() in (t[0]+' '+t[1]).casefold()]
    def check(target):
        name,url,spec=target;client=bot.RedditRSS(cache_policy=cfg.get('cache'))
        before=client.http.read(url) or {};result=dict(name=name,url=url)
        if probe:
            started=time.monotonic()
            try:
                posts=client.get(url,spec,timeout=timeout)
                result.update(client.source_status(name,url,len(posts)))
            except (RuntimeError,ValueError) as exc:result.update(client.source_failure(name,url,exc))
            result['elapsed_seconds']=round(time.monotonic()-started,3)
        row=client.http.read(url) or {}
        with client.http.connect() as db:
            host=db.execute('SELECT * FROM hosts WHERE host=%s',((urlsplit(url).hostname or '').lower(),)).fetchone()
        retry=max(row.get('retry_at',0),host['retry_at'] if host else 0)
        active_host=bool(host and host['retry_at']>time.time())
        result.update(http_status=(row.get('status') if row.get('error') else None) or
                      (host['status'] if active_host else None) or row.get('status'),
                      last_http_status=row.get('status'),host_http_status=host['status'] if active_host else None,
                      cache_error=row.get('error') or (host['error'] if active_host else ''),
                      retry_at=retry or None,retry_in_seconds=max(0,round(retry-time.time())),
                      validated_at=row.get('validated_at') or None,has_cached_body=row.get('body') is not None,
                      cached_bytes=len(row['body']) if row.get('body') is not None else 0)
        if probe:result['cache_changed']=row.get('validated_at',0)!=before.get('validated_at',0) or row.get('retry_at',0)!=before.get('retry_at',0)
        return result
    # A small pool, plus cache host locks, keeps different hosts independent.
    with ThreadPoolExecutor(max_workers=3) as pool:return list(pool.map(check,selected))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',action='store_true',help='Check selected sources through the shared cache; never bypass cooldown')
    parser.add_argument('--source',help='Name or URL substring')
    parser.add_argument('--timeout',type=int,default=40)
    args=parser.parse_args()
    if not 1<=args.timeout<=60:parser.error('timeout must be between 1 and 60 seconds')
    bot.load_env();cfg=json.loads(Path('config.json').read_text())
    print(json.dumps(dict(checked_at=time.time(),probe=args.probe,timeout_seconds=args.timeout,
                         sources=diagnose(cfg,args.probe,args.source,args.timeout)),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

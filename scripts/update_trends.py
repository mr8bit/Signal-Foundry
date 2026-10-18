#!/usr/bin/env python3
"""Update discovery monitors while preserving the existing news snapshot."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bot
import dashboard


def main():
    bot.load_env();cfg=json.loads(Path('config.json').read_text())
    path=Path('data/feed-snapshot.json');posts=json.loads(path.read_text()) if path.exists() else []
    status_path=Path('data/feed-status.json');statuses=json.loads(status_path.read_text()) if status_path.exists() else []
    client=bot.RedditRSS(cache_policy=cfg.get('cache'))
    for source in cfg['feeds']:
        if source.get('format') not in ('github_trending','hf_models','hf_papers') or not source.get('enabled',True):continue
        statuses=[s for s in statuses if s['name']!=source['name']]
        try:
            batch=client.get(source['url'],source)
            posts=[p for p in posts if p.get('source_id')!=source['id']]+batch
            statuses.append(client.source_status(source['name'],source['url'],len(batch)))
            print(source['name'],len(batch),'entries',flush=True)
        except (RuntimeError,ValueError) as exc:
            statuses.append(dict(name=source['name'],url=source['url'],status='error',count=0,error=str(exc)))
            print(source['name'],str(exc),flush=True)
    dashboard.write_atomic(path,json.dumps(posts,ensure_ascii=False,indent=2))
    dashboard.write_atomic(status_path,json.dumps(statuses,ensure_ascii=False,indent=2))
    data=dashboard.build()
    print('Dashboard updated:',len(data['items']),'stories')


if __name__=='__main__':main()

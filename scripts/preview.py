#!/usr/bin/env python3
"""Build the dashboard, optionally collecting a fresh snapshot first."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bot
import dashboard


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,help='Limit archived stories shown; all by default')
    parser.add_argument('--snapshot',help='Use a saved snapshot without fetching feeds again')
    args=parser.parse_args()
    if args.limit is not None and args.limit<1:parser.error('--limit must be positive')
    bot.load_env()
    snapshot=args.snapshot or 'data/feed-snapshot.json'
    if not args.snapshot:
        cfg=json.loads(Path('config.json').read_text());client=bot.RedditRSS()
        posts=client.fetch(cfg,time.time())
        dashboard.write_atomic(snapshot,json.dumps(posts,ensure_ascii=False))
        dashboard.write_atomic('data/feed-status.json',json.dumps(client.statuses,ensure_ascii=False))
    data=dashboard.build(snapshot,limit=args.limit)
    print(f'Dashboard updated: {len(data["items"])} archived stories; news-preview.html')


if __name__=='__main__':main()

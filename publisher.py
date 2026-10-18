#!/usr/bin/env python3
"""Optional Telegram process; consumes the collector's snapshot without fetching sites."""
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import bot
from publication import Publications
from service_state import ServiceState
from signals import source_name

LOG=logging.getLogger('publisher')


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s')
    bot.load_env()
    for key in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'):
        if not os.getenv(key):raise SystemExit(f'Set {key} before enabling the telegram profile')
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    state=ServiceState();publications=Publications();path=os.getenv('STATE_DB','data/state.sqlite3')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path+'.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another Telegram publisher is running') from None
        db=bot.database(path)
        while not stop.is_set():
            try:
                state.heartbeat('publisher')
                snapshot=Path('data/feed-snapshot.json')
                if not snapshot.exists() or time.time()-snapshot.stat().st_mtime>3600:
                    state.heartbeat('publisher',status='waiting_for_fresh_snapshot')
                else:
                    cfg=json.loads(Path('config.json').read_text())
                    enabled={s['id'] for s in cfg['feeds'] if s.get('enabled',True)}
                    posts=[p for p in json.loads(snapshot.read_text()) if p.get('source')!='site' or p.get('source_id') in enabled]
                    groups=bot.select(posts,cfg,time.time())
                    data=publications.load()['data']
                    ranks={(p['source_name'],p['title']):p['score'] for p in data['items']}
                    groups.sort(key=lambda pair:-ranks.get((source_name(pair[0]),pair[0]['title']),0))
                    bot.publish(db,groups,cfg)
            except Exception:
                LOG.exception('publication_failed')
                state.heartbeat('publisher',status='blocked' if db.execute("SELECT 1 FROM deliveries WHERE status='pending'").fetchone() else 'error')
            stop.wait(15)
        state.heartbeat('publisher',status='stopped');db.close()


if __name__=='__main__':main()

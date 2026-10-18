#!/usr/bin/env python3
"""The single source-fetching process. Web workers only read published generations."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import bot
import dashboard
from publication import Publications
from service_state import ServiceState
from signals import SCORING_VERSION
from source_schedule import SourceSchedule

LOG=logging.getLogger('collector')


class Collector:
    def __init__(self,state=None,publications=None,client=None,schedule=None):
        self.state=state or ServiceState();self.publications=publications or Publications()
        self.client=client or bot.RedditRSS();self.stop=threading.Event()
        self.schedule=schedule or SourceSchedule(self.state,self.client)

    def publish_snapshot(self):
        data=dashboard.build()
        manifest=self.publications.publish(data)
        self.state.set('publication',manifest)
        return data

    def collect(self,job):
        LOG.info('collection_started job=%s',job['id'])
        try:
            cfg=json.loads(Path('config.json').read_text())
            result=self.schedule.collect(cfg,self.stop)
            if self.stop.is_set():raise RuntimeError('Collection cancelled')
            if result['changed']:
                if not result['posts']:raise RuntimeError('Waiting for first successful source')
                dashboard.write_atomic('data/feed-snapshot.json',json.dumps(result['posts'],ensure_ascii=False))
                stamp=result['collected_at']
                if stamp:os.utime('data/feed-snapshot.json',(stamp,stamp))
                dashboard.write_atomic('data/feed-status.json',json.dumps(result['statuses'],ensure_ascii=False))
                data=self.publish_snapshot()
            else:
                data=self.publications.load()['data']
            self.state.finish(job['id'])
            LOG.info('collection_finished job=%s items=%s sources_ok=%s scoring=%s',job['id'],len(data['items']),
                     sum(s['status']=='ok' for s in data['statuses']),SCORING_VERSION)
            return True
        except Exception:
            LOG.exception('collection_failed job=%s',job['id'])
            self.state.finish(job['id'],'Collection did not complete; the previous publication was preserved.')
            return False

    def heartbeats(self):
        while not self.stop.is_set():
            self.state.heartbeat('collector',scoring_version=SCORING_VERSION)
            self.stop.wait(5)

    def run(self,once=False):
        Path('data').mkdir(exist_ok=True)
        with Path('data/collector.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('A collector is already running') from None
            self.state.recover()
            self.state.heartbeat('collector',scoring_version=SCORING_VERSION)
            thread=threading.Thread(target=self.heartbeats,daemon=True);thread.start()
            try:
                cfg=json.loads(Path('config.json').read_text())
                self.schedule.sync(cfg)
                if Path('data/feed-snapshot.json').exists():
                    try:
                        dashboard.write_atomic('data/feed-status.json',json.dumps(self.schedule.snapshot()['statuses'],ensure_ascii=False))
                        self.publish_snapshot()
                    except Exception:LOG.exception('snapshot_bootstrap_failed')
                while not self.stop.is_set():
                    cfg=json.loads(Path('config.json').read_text())
                    if once or time.time()>=self.schedule.next_due(cfg):self.state.enqueue(cooldown=0)
                    job=self.state.claim()
                    if job:
                        success=self.collect(job)
                        if once:return 0 if success else 1
                    self.stop.wait(5)
            finally:
                self.stop.set();thread.join(timeout=6)
                self.state.heartbeat('collector',status='stopped',scoring_version=SCORING_VERSION)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s')
    bot.load_env();collector=Collector()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:collector.stop.set())
    raise SystemExit(collector.run(args.once) or 0)


if __name__=='__main__':main()

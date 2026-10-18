#!/usr/bin/env python3
"""Local dashboard + RSS API. Collection never sends Telegram messages."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, parse_qs
import bot
import dashboard
import digest

LOG=logging.getLogger('dashboard')
AGENT_LINKS='</llms.txt>; rel="help"; type="text/plain", </signals.md>; rel="alternate"; type="text/markdown", </api/signals>; rel="alternate"; type="application/json"'


class Application:
    def __init__(self):
        self.lock=threading.Lock()
        self.client=bot.RedditRSS()
        self.refreshing=False
        self.error=None
        self.last_refresh_at=0
        self.refresh_cooldown=60
        self.data=None
        self.reload()

    def reload(self):
        self.data=dashboard.build()

    def refresh(self):
        if not self.lock.acquire(blocking=False): return False
        if time.time()-self.last_refresh_at<self.refresh_cooldown:
            self.lock.release()
            return False
        self.last_refresh_at=time.time()
        self.refreshing=True
        self.error=None
        def work():
            try:
                cfg=json.loads(Path('config.json').read_text())
                posts=self.client.fetch(cfg,time.time())
                # Publish files atomically so browsers never read half a JSON document.
                for path,value in [('data/feed-snapshot.json',posts),('data/feed-status.json',self.client.statuses)]:
                    temp=Path(path+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False));temp.replace(path)
                self.reload()
            except Exception as exc:
                self.error='Could not update sources; the previous feed was preserved.'
                LOG.exception('Refresh failed: %s',type(exc).__name__)
            finally:
                self.refreshing=False
                self.lock.release()
        threading.Thread(target=work,daemon=True).start()
        return True


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def respond(self,code,body,content_type,headers=()):
            if isinstance(body,str): body=body.encode('utf-8')
            self.send_response(code)
            for name,value in headers: self.send_header(name,value)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-cache')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            if self.command!='HEAD': self.wfile.write(body)

        def do_GET(self):
            path=urlsplit(self.path)
            base=f'http://{self.server.server_address[0]}:{self.server.server_address[1]}'
            if path.path in ('/','/homepage/','/news-preview.html'):
                accept=self.headers.get('Accept','')
                if 'text/markdown' in accept and 'text/html' not in accept:
                    self.respond(200,digest.markdown(app.data,base_url=base),'text/markdown; charset=utf-8',[('Vary','Accept')]);return
                self.respond(200,Path('news-preview.html').read_bytes(),'text/html; charset=utf-8',[('Vary','Accept'),('Link',AGENT_LINKS)])
            elif path.path=='/llms.txt':
                self.respond(200,digest.llms_txt(app.data,base),'text/plain; charset=utf-8')
            elif path.path=='/robots.txt':
                self.respond(200,digest.robots_txt(base),'text/plain; charset=utf-8')
            elif path.path in ('/signals.md','/index.md','/api/signals'):
                try: period,category,limit=digest.query(parse_qs(path.query))
                except digest.QueryError as exc:
                    self.respond(400,json.dumps(dict(error=str(exc))),'application/json; charset=utf-8');return
                if path.path=='/api/signals':
                    self.respond(200,digest.signals_json(app.data,period,category,limit),'application/json; charset=utf-8')
                else:
                    self.respond(200,digest.markdown(app.data,period,category,limit,base_url=base),'text/markdown; charset=utf-8')
            elif path.path in ('/api/rss','/data/feed.xml'):
                period=parse_qs(path.query).get('sort',['day'])[0]
                if period not in dashboard.PERIODS:
                    self.respond(400,'Unknown sort; use now, day, week, month or year','text/plain; charset=utf-8');return
                # Feed links use local base only; do not trust arbitrary Host headers.
                self.respond(200,dashboard.rss(app.data,period),'application/rss+xml; charset=utf-8')
            elif path.path=='/api/news':
                self.respond(200,json.dumps(app.data,ensure_ascii=False),'application/json; charset=utf-8')
            elif path.path=='/api/status':
                self.respond(200,json.dumps(dict(refreshing=app.refreshing,error=app.error,generated_at=app.data['generated_at'])),'application/json; charset=utf-8')
            else:
                self.respond(404,'Not found','text/plain; charset=utf-8')

        def do_POST(self):
            if urlsplit(self.path).path!='/api/refresh':
                self.respond(404,'Not found','text/plain; charset=utf-8');return
            origin=self.headers.get('Origin')
            if origin and origin!=f'http://{self.headers.get("Host")}':
                self.respond(403,'Cross-origin refresh is not allowed','text/plain; charset=utf-8');return
            started=app.refresh()
            result=dict(status='refreshing' if started or app.refreshing else 'cached',
                        retry_after=max(0,math.ceil(app.refresh_cooldown-(time.time()-app.last_refresh_at))))
            self.respond(202,json.dumps(result),'application/json; charset=utf-8')

        def log_message(self,format,*args):
            LOG.info(format,*args)
    return Handler


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--auto-refresh',action='store_true',help='Collect feeds every poll_seconds')
    args=parser.parse_args();logging.basicConfig(level=logging.INFO);bot.load_env()
    if not Path('data/feed-snapshot.json').exists():
        cfg=json.loads(Path('config.json').read_text());client=bot.RedditRSS();posts=client.fetch(cfg,time.time());Path('data').mkdir(exist_ok=True)
        Path('data/feed-snapshot.json').write_text(json.dumps(posts,ensure_ascii=False));Path('data/feed-status.json').write_text(json.dumps(client.statuses,ensure_ascii=False))
    app=Application()
    if args.auto_refresh:
        def periodic():
            while True:
                cfg=json.loads(Path('config.json').read_text());time.sleep(cfg['poll_seconds']);app.refresh()
        threading.Thread(target=periodic,daemon=True).start()
    server=ThreadingHTTPServer((args.host,args.port),handler(app))
    print(f'Dashboard: http://{args.host}:{args.port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__':main()

"""Read-only WSGI dashboard and durable management API. No source fetching here."""
import base64
from hashlib import sha256
import hmac
import json
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs,urlsplit
import dashboard
import digest
from publication import Publications
from service_state import ServiceState

LOG=logging.getLogger('web')
# Lets agents that only read headers find the machine-readable views of the dashboard.
AGENT_LINKS='</llms.txt>; rel="help"; type="text/plain", </signals.md>; rel="alternate"; type="text/markdown", </api/signals>; rel="alternate"; type="application/json"'


class SignalApp:
    def __init__(self,state=None,publications=None,admin_token=None,public_url=None):
        self.state=state or ServiceState();self.publications=publications or Publications()
        self.admin_token=os.getenv('SIGNAL_ADMIN_TOKEN','') if admin_token is None else admin_token
        self.public_url=(public_url or os.getenv('SIGNAL_PUBLIC_URL','http://127.0.0.1:8765')).rstrip('/')
        parsed=urlsplit(self.public_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname:raise ValueError('SIGNAL_PUBLIC_URL must be an HTTP(S) URL')
        self.max_age=int(os.getenv('SIGNAL_READY_MAX_AGE','7200'))
        self.script_hash=base64.b64encode(sha256(dashboard.script_source().encode()).digest()).decode()
        self._rss={};self._rss_lock=threading.Lock()

    def status(self):
        value=self.state.status()
        try:
            pub=self.publications.load();value.update(generated_at=pub['data']['generated_at'],publication=pub['manifest'])
        except (OSError,ValueError,KeyError):value.update(generated_at=None,publication=None)
        value['auth_required']=True
        # Do not expose process IDs or file-system details through a public API.
        for role in ('collector','publisher'):
            if value.get(role):value[role]={k:v for k,v in value[role].items() if k!='pid'}
        return value

    def response(self,start,status,body=b'',content_type='application/json; charset=utf-8',headers=None,environ=None,cache=True):
        body=body.encode() if isinstance(body,str) else body
        extra=list(headers or [])
        extra.extend([('Content-Type',content_type),('Cache-Control','no-cache'),('X-Content-Type-Options','nosniff'),
                      ('Referrer-Policy','strict-origin-when-cross-origin'),('X-Frame-Options','DENY'),
                      ('Content-Security-Policy',"default-src 'self'; script-src 'self' 'sha256-"+self.script_hash+"'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")])
        if cache and status==200:
            tag='"'+sha256(body).hexdigest()+'"';extra.append(('ETag',tag))
            if environ and environ.get('HTTP_IF_NONE_MATCH')==tag:
                start('304 Not Modified',extra);return [b'']
        extra.append(('Content-Length',str(len(body))))
        phrases={200:'OK',202:'Accepted',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',413:'Content Too Large',500:'Internal Server Error',503:'Service Unavailable'}
        start(f'{status} {phrases[status]}',extra)
        return [b'' if environ and environ.get('REQUEST_METHOD')=='HEAD' else body]

    def __call__(self,environ,start_response):
        try:return self.dispatch(environ,start_response)
        except Exception:
            LOG.exception('request_failed path=%s',environ.get('PATH_INFO','/'))
            return self.response(start_response,500,'{"error":"Internal service error"}',cache=False)

    def dispatch(self,env,start):
        path=env.get('PATH_INFO','/');method=env.get('REQUEST_METHOD','GET')
        if path=='/api/refresh':
            if method!='POST':return self.response(start,405,'{"error":"Use POST"}',headers=[('Allow','POST')],cache=False)
            try:length=int(env.get('CONTENT_LENGTH') or '0')
            except ValueError:return self.response(start,400,'{"error":"Invalid length"}',cache=False)
            if length>1024:return self.response(start,413,'{"error":"Request too large"}',cache=False)
            if not self.admin_token:return self.response(start,503,'{"error":"Management is disabled"}',cache=False)
            supplied=env.get('HTTP_AUTHORIZATION','')
            if not supplied.startswith('Bearer ') or not hmac.compare_digest(supplied[7:].encode(),self.admin_token.encode()):
                return self.response(start,401,'{"error":"Management key required","auth_required":true}',headers=[('WWW-Authenticate','Bearer')],cache=False)
            origin=env.get('HTTP_ORIGIN')
            if origin and origin!=self.public_url:
                return self.response(start,403,'{"error":"Origin is not allowed"}',cache=False)
            return self.response(start,202,json.dumps(self.state.enqueue()),cache=False)
        if method not in ('GET','HEAD'):
            return self.response(start,405,'{"error":"Method not allowed"}',headers=[('Allow','GET, HEAD')],cache=False)
        if path in ('/health/live','/healthz'):
            return self.response(start,200,'{"status":"alive"}',cache=False,environ=env)
        if path in ('/health/ready','/readyz'):
            status=self.status();heartbeat=status.get('collector') or {}
            ready=bool(status['publication'] and heartbeat.get('status')=='running' and time.time()-heartbeat.get('at',0)<30 and
                       time.time()-status['publication'].get('collected_at',0)<self.max_age)
            return self.response(start,200 if ready else 503,json.dumps(dict(status='ready' if ready else 'not_ready',scoring_version=(status['publication'] or {}).get('scoring_version'))),cache=False,environ=env)
        if path=='/api/status':return self.response(start,200,json.dumps(self.status(),ensure_ascii=False),cache=False,environ=env)
        if path=='/metrics':return self.response(start,200,self.metrics(),'text/plain; version=0.0.4; charset=utf-8',cache=False,environ=env)
        if path=='/robots.txt':return self.response(start,200,digest.robots_txt(self.public_url),'text/plain; charset=utf-8',environ=env)
        try:pub=self.publications.load()
        except (OSError,ValueError,KeyError):
            if path in ('/','/homepage/','/news-preview.html'):
                body='<html lang="ru"><meta charset="utf-8"><meta http-equiv="refresh" content="10"><title>Signal</title><body style="background:#101214;color:#eef1f2;font:18px system-ui;padding:8vw"><h1>Signal запускается</h1><p>Сборщик готовит первую ленту. Страница обновится автоматически.</p></body></html>'
                return self.response(start,503,body,'text/html; charset=utf-8',headers=[('Retry-After','10')],cache=False,environ=env)
            return self.response(start,503,'{"error":"First publication is not ready"}',headers=[('Retry-After','10')],cache=False,environ=env)
        if path in ('/','/homepage/','/news-preview.html'):
            accept=env.get('HTTP_ACCEPT','')
            if 'text/markdown' in accept and 'text/html' not in accept:
                return self.response(start,200,digest.markdown(pub['data'],base_url=self.public_url),'text/markdown; charset=utf-8',
                                     headers=[('Vary','Accept')],environ=env)
            return self.response(start,200,pub['html'],'text/html; charset=utf-8',headers=[('Vary','Accept'),('Link',AGENT_LINKS)],environ=env)
        if path=='/llms.txt':return self.response(start,200,digest.llms_txt(pub['data'],self.public_url),'text/plain; charset=utf-8',environ=env)
        if path in ('/signals.md','/index.md','/api/signals'):
            try:period,category,limit=digest.query(parse_qs(env.get('QUERY_STRING','')))
            except digest.QueryError as exc:return self.response(start,400,json.dumps(dict(error=str(exc))),cache=False,environ=env)
            if path=='/api/signals':return self.response(start,200,digest.signals_json(pub['data'],period,category,limit),environ=env)
            return self.response(start,200,digest.markdown(pub['data'],period,category,limit,base_url=self.public_url),
                                 'text/markdown; charset=utf-8',environ=env)
        if path=='/api/news':return self.response(start,200,pub['json'],environ=env)
        if path in ('/api/rss','/data/feed.xml'):
            period=parse_qs(env.get('QUERY_STRING','')).get('sort',['day'])[0]
            if period not in dashboard.PERIODS:return self.response(start,400,'{"error":"Unknown sort"}',cache=False,environ=env)
            key=(pub['manifest']['generation'],period,int(time.time()//30))
            with self._rss_lock:
                if key not in self._rss:self._rss={key:dashboard.rss(pub['data'],period,base_url=self.public_url)}
                body=self._rss[key]
            return self.response(start,200,body,'application/rss+xml; charset=utf-8',environ=env)
        return self.response(start,404,'{"error":"Not found"}',environ=env,cache=False)

    def metrics(self):
        status=self.status();pub=status.get('publication') or {};heartbeat=status.get('collector') or {}
        entries={'signal_collection_success_total':status['collections_succeeded'],'signal_collection_failure_total':status['collections_failed'],
                 'signal_last_success_timestamp_seconds':status['last_success'] or 0,'signal_collector_heartbeat_timestamp_seconds':heartbeat.get('at',0),
                 'signal_refresh_running':int(status['refreshing']),'signal_published_items':pub.get('items',0)}
        return ''.join(f'{name} {value}\n' for name,value in entries.items())


def create_app():return SignalApp()

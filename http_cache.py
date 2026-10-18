"""Shared application cache for public GETs, persistent backoff and host pacing."""
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import fcntl
from hashlib import sha256
from http.client import HTTPException
import math
import os
from pathlib import Path
import re
import random
import socket
import sqlite3
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_POLICY=dict(rss_ttl_seconds=900,html_ttl_seconds=1800,github_trending_ttl_seconds=1800,
                    hf_models_ttl_seconds=1800,hf_papers_ttl_seconds=3600,article_ttl_seconds=21600,
                    stale_if_error_seconds=86400,error_backoff_seconds=900,
                    rate_limit_backoff_seconds=1800,min_host_interval_seconds=2,
                    max_backoff_seconds=86400,backoff_jitter_ratio=0.2,host_min_intervals={})


def failure_message(exc,phase):
    reason=exc.reason if isinstance(exc,URLError) else exc
    if phase=='validation':
        detail=re.sub(r'https?://\S+','[URL]',' '.join(str(exc).split()))[:160]
        return f'Content validation failed ({type(exc).__name__}): {detail}; retry is cooling down'
    if isinstance(reason,ssl.SSLCertVerificationError):kind='TLS certificate verification failed'
    elif isinstance(reason,ssl.SSLError):kind='TLS handshake failed'
    elif isinstance(reason,socket.gaierror):kind='DNS resolution failed'
    elif isinstance(reason,TimeoutError):kind='Response read timed out' if phase=='body' else 'Connection, TLS or response headers timed out'
    elif isinstance(exc,CacheError):kind=str(exc)
    elif isinstance(reason,ConnectionRefusedError):kind='Connection refused'
    elif isinstance(reason,ConnectionResetError):kind='Connection reset'
    else:kind=f'Network failure ({type(reason).__name__})'
    return kind+'; retry is cooling down'


class CacheError(RuntimeError):
    def __init__(self,message,retry_after=0,status=None):
        self.retry_after=max(0,math.ceil(retry_after));self.status=status
        super().__init__(message)


@dataclass
class CachedResponse:
    body:bytes
    url:str
    validated_at:float
    expires_at:float
    state:str
    error:str=''

    @property
    def stale(self):return self.state=='stale'


class HTTPCache:
    def __init__(self,path=None,policy=None):
        self.path=Path(path or os.getenv('HTTP_CACHE_DB','data/http-cache.sqlite3'))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock_dir=Path(str(self.path)+'.locks');self.lock_dir.mkdir(exist_ok=True)
        self.policy=dict(DEFAULT_POLICY)
        self.configure(policy or {})
        self.last_result=None
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS responses (
                url TEXT PRIMARY KEY, body BLOB, response_url TEXT, etag TEXT, modified TEXT,
                validated_at REAL NOT NULL DEFAULT 0, expires_at REAL NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', status INTEGER);
                CREATE TABLE IF NOT EXISTS hosts (
                host TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', status INTEGER);''')
            db.execute('BEGIN IMMEDIATE')
            for table,column in [('responses','failures'),('hosts','rate_failures')]:
                if column not in {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')

    def configure(self,policy):
        for key,value in policy.items():
            if key not in DEFAULT_POLICY:raise ValueError(f'Unknown cache setting: {key}')
            if key=='host_min_intervals':
                if not isinstance(value,dict) or any(not isinstance(host,str) or isinstance(seconds,bool) or
                    not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<=seconds<=60 for host,seconds in value.items()):
                    raise ValueError('host_min_intervals must map host names to pauses between 0 and 60 seconds')
                continue
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                raise ValueError(f'{key} must be a nonnegative number')
            if key.endswith('_ttl_seconds') and value<=0:raise ValueError(f'{key} must be positive')
            if key=='min_host_interval_seconds' and value>60:raise ValueError('Host request interval cannot exceed 60 seconds')
            if key=='backoff_jitter_ratio' and value>1:raise ValueError('Backoff jitter must not exceed 1')
        self.policy.update(policy)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def locked(self,key):
        with (self.lock_dir/(sha256(key.encode()).hexdigest()+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def read(self,url):
        with self.connect() as db:
            row=db.execute('SELECT * FROM responses WHERE url=?',(url,)).fetchone()
        return dict(row) if row else None

    def not_before(self,url):
        host=(urlsplit(url).hostname or '').lower()
        with self.connect() as db:
            row=db.execute('SELECT retry_at FROM responses WHERE url=?',(url,)).fetchone()
            record=db.execute('SELECT next_at,retry_at FROM hosts WHERE host=?',(host,)).fetchone()
        return max(row['retry_at'] if row else 0,record['next_at'] if record else 0,record['retry_at'] if record else 0)

    def host_interval(self,host):
        matches=[(len(domain),seconds) for domain,seconds in self.policy['host_min_intervals'].items()
                 if host==domain or host.endswith('.'+domain)]
        return max(self.policy['min_host_interval_seconds'],max(matches)[1] if matches else 0)

    def reset_failures(self,db,host):
        db.execute("UPDATE hosts SET rate_failures=0,retry_at=0,error='',status=200 WHERE host=?",(host,))

    def result(self,row,state,error=''):
        response=CachedResponse(bytes(row['body']),row['response_url'] or row['url'],row['validated_at'],row['expires_at'],state,error)
        self.last_result=response
        return response

    def fallback(self,row,message,retry_at,status=None):
        now=time.time()
        if row and row['body'] is not None and now<=row['expires_at']+self.policy['stale_if_error_seconds']:
            return self.result(row,'stale',message)
        raise CacheError(message,max(0,retry_at-now),status)

    def fail(self,url,row,message,seconds,status=None,host=None):
        with self.connect() as db:
            failures=(row or {}).get('failures',0)+1
            count=failures
            if host and status==429:
                record=db.execute('SELECT rate_failures FROM hosts WHERE host=?',(host,)).fetchone()
                count=(record['rate_failures'] if record else 0)+1
            base=self.policy['rate_limit_backoff_seconds'] if status==429 else self.policy['error_backoff_seconds']
            delay=min(self.policy['max_backoff_seconds'],base*2**min(count-1,16)*(1+random.uniform(0,self.policy['backoff_jitter_ratio'])))
            retry_at=time.time()+max(seconds,delay)
            db.execute('INSERT INTO responses(url,retry_at,error,status) VALUES (?,?,?,?) '
                       'ON CONFLICT(url) DO UPDATE SET retry_at=excluded.retry_at,error=excluded.error,status=excluded.status',
                       (url,retry_at,message,status))
            db.execute('UPDATE responses SET failures=? WHERE url=?',(failures,url))
            if host and status in (403,429,503):
                db.execute('INSERT INTO hosts(host,retry_at,error,status) VALUES (?,?,?,?) '
                           'ON CONFLICT(host) DO UPDATE SET retry_at=MAX(hosts.retry_at,excluded.retry_at),error=excluded.error,status=excluded.status',
                           (host,retry_at,message,status))
                if status==429:db.execute('UPDATE hosts SET rate_failures=? WHERE host=?',(count,host))
        return self.fallback(row,message,retry_at,status)

    def get(self,url,headers=None,ttl=None,timeout=40,max_bytes=5_000_000,validator=None,loader=None,allowed_hosts=None):
        ttl=self.policy['rss_ttl_seconds'] if ttl is None else ttl
        if ttl<=0:raise ValueError('Cache TTL must be positive')
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname:raise ValueError('Expected a public HTTP(S) URL')
        host=parsed.hostname.lower()
        if allowed_hosts and host not in allowed_hosts:raise CacheError('Host is not enabled')
        loader=loader or urlopen
        self.last_result=None
        # All collectors use the same lock, including other Python processes/containers.
        with self.locked('url:'+url):
            row=self.read(url);now=time.time()
            if row and row['body'] is not None:
                row['expires_at']=min(row['expires_at'],row['validated_at']+ttl)
            if row and row['body'] is not None and now<row['expires_at']:
                return self.result(row,'hit')
            if row and now<row['retry_at']:
                return self.fallback(row,row['error'] or 'Source retry is cooling down',row['retry_at'],row['status'])
            with self.locked('host:'+host):
                with self.connect() as db:
                    record=db.execute('SELECT * FROM hosts WHERE host=?',(host,)).fetchone()
                if record:
                    if record['retry_at']>time.time():
                        return self.fallback(row,record['error'] or 'Host retry is cooling down',record['retry_at'],record['status'])
                    delay=record['next_at']-time.time()
                    if delay>0:time.sleep(min(delay,60))
                outgoing=dict(headers or {})
                if row:
                    if row['etag']:outgoing['If-None-Match']=row['etag']
                    if row['modified']:outgoing['If-Modified-Since']=row['modified']
                phase='connection'
                response_status=None
                try:
                    with loader(Request(url,headers=outgoing),timeout=timeout) as response:
                        response_status=getattr(response,'status',200)
                        if not isinstance(response_status,int):response_status=200
                        final_url=response.geturl()
                        if not isinstance(final_url,str):final_url=url
                        if allowed_hosts and (urlsplit(final_url).hostname or '').lower() not in allowed_hosts:
                            raise CacheError('Unexpected redirect')
                        phase='body'
                        body=response.read(max_bytes+1)
                        if len(body)>max_bytes:raise CacheError('Response exceeds size limit')
                        if validator:
                            phase='validation'
                            validator(body)
                        etag=response.headers.get('ETag');modified=response.headers.get('Last-Modified')
                        etag=etag if isinstance(etag,str) else None
                        modified=modified if isinstance(modified,str) else None
                    checked=time.time()
                    with self.connect() as db:
                        db.execute('''INSERT INTO responses(url,body,response_url,etag,modified,validated_at,expires_at,retry_at,error,status)
                            VALUES (?,?,?,?,?,?,?,0,'',200) ON CONFLICT(url) DO UPDATE SET body=excluded.body,response_url=excluded.response_url,
                            etag=excluded.etag,modified=excluded.modified,validated_at=excluded.validated_at,expires_at=excluded.expires_at,retry_at=0,error='',status=200,failures=0''',
                            (url,body,final_url,etag,modified,checked,checked+ttl))
                        self.reset_failures(db,host)
                    return self.result(self.read(url),'miss')
                except HTTPError as exc:
                    if exc.code==304 and row and row['body'] is not None:
                        checked=time.time()
                        with self.connect() as db:
                            db.execute("UPDATE responses SET validated_at=?,expires_at=?,retry_at=0,error='',status=304,failures=0 WHERE url=?",(checked,checked+ttl,url))
                            self.reset_failures(db,host)
                        return self.result(self.read(url),'revalidated')
                    seconds=self.policy['error_backoff_seconds']
                    if exc.code in (429,503):
                        if exc.code==429:seconds=self.policy['rate_limit_backoff_seconds']
                        retry=exc.headers.get('Retry-After','') if exc.headers else ''
                        try:seconds=max(seconds,int(retry))
                        except ValueError:
                            try:seconds=max(seconds,parsedate_to_datetime(retry).timestamp()-time.time())
                            except (TypeError,ValueError,OverflowError):pass
                    return self.fail(url,row,f'HTTP {exc.code}; retry is cooling down',seconds,exc.code,host)
                except (URLError,TimeoutError,OSError,HTTPException,RuntimeError,ValueError) as exc:
                    return self.fail(url,row,failure_message(exc,phase),self.policy['error_backoff_seconds'],response_status)
                finally:
                    # Count failed requests too. Preserve any 429 host cooldown.
                    with self.connect() as db:
                        db.execute('INSERT INTO hosts(host,next_at) VALUES (?,?) ON CONFLICT(host) DO UPDATE SET next_at=excluded.next_at',
                                   (host,time.time()+self.host_interval(host)))

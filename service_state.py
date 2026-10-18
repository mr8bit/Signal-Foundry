"""Durable refresh queue and service heartbeats shared across worker processes."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time


class ServiceState:
    def __init__(self,path=None):
        self.path=Path(path or os.getenv('SIGNAL_CONTROL_DB','data/service.sqlite3'))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY,requested REAL NOT NULL,
                started REAL,finished REAL,status TEXT NOT NULL,error TEXT);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,id);''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()

    def set(self,key,value):
        with self.connect() as db:
            db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key,json.dumps(value)))

    def get(self,key,default=None):
        with self.connect() as db:row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def heartbeat(self,role,status='running',**extra):
        self.set('heartbeat:'+role,dict(at=time.time(),pid=os.getpid(),status=status,**extra))

    def enqueue(self,cooldown=60):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            if active:return dict(status='refreshing',job_id=active['id'],retry_after=0)
            last=db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 1').fetchone()
            if last and now-last['requested']<cooldown:
                return dict(status='cached',job_id=last['id'],retry_after=max(1,int(cooldown-(now-last['requested']))))
            job=db.execute("INSERT INTO jobs(requested,status) VALUES (?,'queued')",(now,)).lastrowid
            return dict(status='refreshing',job_id=job,retry_after=0)

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET status='running',started=? WHERE id=?",(time.time(),row['id']))
            return dict(row)

    def recover(self):
        # Called only after acquiring the collector's exclusive process lock.
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued',started=NULL,error='Collector restarted' WHERE status='running'")

    def finish(self,job_id,error=None):
        now=time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,finished=?,error=? WHERE id=?',('failed' if error else 'done',now,error,job_id))
            db.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished<?",(now-30*86400,))
            key='collections_failed' if error else 'collections_succeeded'
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            count=json.loads(row[0]) if row else 0
            db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(count+1)))
        if not error:self.set('last_success',now)

    def status(self):
        with self.connect() as db:
            row=db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 1').fetchone()
        job=dict(row) if row else None
        return dict(refreshing=bool(job and job['status'] in ('queued','running')),job=job,
                    error=job.get('error') if job and job['status']=='failed' else None,
                    collector=self.get('heartbeat:collector'),publisher=self.get('heartbeat:publisher'),
                    last_success=self.get('last_success'),collections_succeeded=self.get('collections_succeeded',0),
                    collections_failed=self.get('collections_failed',0))

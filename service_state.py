"""Durable refresh queue and service heartbeats shared across worker processes."""
import json
import os
import time
import db


class ServiceState:
    def __init__(self,path=None):
        pass

    def connect(self):
        return db.connect()

    def set(self,key,value):
        with self.connect() as db:
            db.execute('INSERT INTO meta VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key,json.dumps(value)))

    def get(self,key,default=None):
        with self.connect() as db:row=db.execute('SELECT value FROM meta WHERE key=%s',(key,)).fetchone()
        return json.loads(row['value']) if row else default

    def heartbeat(self,role,status='running',**extra):
        self.set('heartbeat:'+role,dict(at=time.time(),pid=os.getpid(),status=status,**extra))

    def enqueue(self,cooldown=60):
        now=time.time()
        with db.advisory_lock('service:enqueue'):
            with self.connect() as conn:
                active=conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
                if active:return dict(status='refreshing',job_id=active['id'],retry_after=0)
                last=conn.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 1').fetchone()
                if last and now-last['requested']<cooldown:
                    return dict(status='cached',job_id=last['id'],retry_after=max(1,int(cooldown-(now-last['requested']))))
                job=conn.execute("INSERT INTO jobs(requested,status) VALUES (%s,'queued') RETURNING id",(now,)).fetchone()['id']
                return dict(status='refreshing',job_id=job,retry_after=0)

    def claim(self):
        with self.connect() as db:
            row=db.execute('''WITH next AS (SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                UPDATE jobs SET status='running',started=%s FROM next WHERE jobs.id=next.id RETURNING jobs.*''',
                           (time.time(),)).fetchone()
            return dict(row) if row else None

    def recover(self):
        # Called only after acquiring the collector's exclusive process lock.
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued',started=NULL,error='Collector restarted' WHERE status='running'")

    def finish(self,job_id,error=None):
        now=time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=%s,finished=%s,error=%s WHERE id=%s',('failed' if error else 'done',now,error,job_id))
            db.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished<%s",(now-30*86400,))
            key='collections_failed' if error else 'collections_succeeded'
            row=db.execute('SELECT value FROM meta WHERE key=%s',(key,)).fetchone()
            count=json.loads(row['value']) if row else 0
            db.execute('INSERT INTO meta VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(count+1)))
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

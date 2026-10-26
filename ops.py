#!/usr/bin/env python3
"""Operational commands. Secrets are written to .env and never printed."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import time
import db
from service_state import ServiceState

FILES=('feed-snapshot.json','feed-status.json')
TABLES={
 'news.sqlite3':{'news':('key','payload','first_seen','last_seen','position')},
 'http-cache.sqlite3':{
  'responses':('url','body','response_url','etag','modified','validated_at','expires_at','retry_at','error','status','failures'),
  'hosts':('host','next_at','retry_at','error','status','rate_failures')},
 'state.sqlite3':{'deliveries':('id','status','created','message_id'),'aliases':('key','delivery')},
 'service.sqlite3':{'meta':('key','value'),'jobs':('id','requested','started','finished','status','error'),
                    'source_schedule':('url','name','payload','status','next_at','last_attempt','last_success')},
 'attention.sqlite3':{'readings':('key','channel','value','observed_at'),'checks':('key','kind','checked_at')},
 'embeddings.sqlite3':{'vectors':('digest','model','vector')},
}


def init_env():
    path=Path('.env');password=secrets.token_urlsafe(48)
    if path.exists():
        lines=path.read_text().splitlines()
        if any(line.startswith('POSTGRES_PASSWORD=') and line.split('=',1)[1] for line in lines):
            print('.env already exists; preserved');return
        with path.open('a') as out:
            if lines and not path.read_text().endswith('\n'):out.write('\n')
            out.write('POSTGRES_PASSWORD='+password+'\n')
        print('Added POSTGRES_PASSWORD to existing .env; other settings preserved');return
    template=Path('.env.example').read_text()
    token=secrets.token_urlsafe(48)
    template=template.replace('SIGNAL_ADMIN_TOKEN=','SIGNAL_ADMIN_TOKEN='+token,1)
    template=template.replace('POSTGRES_PASSWORD=','POSTGRES_PASSWORD='+password,1)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as out:out.write(template)
    print('Created .env (0600); management and database keys generated, Telegram remains disabled')


def sqlite_rows(path,table,columns):
    if not path.exists():return []
    wal=Path(str(path)+'-wal')
    immutable=not wal.exists() or wal.stat().st_size==0
    uri=path.resolve().as_uri()+'?mode=ro'+('&immutable=1' if immutable else '')
    with sqlite3.connect(uri,uri=True) as source:
        selected=['rowid' if table=='news' and column=='position' else column for column in columns]
        query='SELECT '+','.join(selected)+' FROM '+table+(' ORDER BY rowid' if table=='news' else '')
        try:return source.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):return []
            raise


def migrate_sqlite(source,include_control=False):
    target=Path('data');target.mkdir(exist_ok=True)
    with db.connect() as destination:
        for filename,tables in TABLES.items():
            for table,columns in tables.items():
                if table=='jobs' and not include_control:continue
                rows=sqlite_rows(source/filename,table,columns)
                if not rows:continue
                placeholders=','.join(['%s']*len(columns))
                sql=f'INSERT INTO {table} ({",".join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
                destination.cursor().executemany(sql,rows)
                print(f'{table}: imported up to {len(rows)} rows')
        for table,column in (('jobs','id'),('deliveries','id'),('news','position')):
            destination.execute(f"SELECT setval(pg_get_serial_sequence('{table}','{column}'),GREATEST((SELECT COALESCE(MAX({column}),1) FROM {table}),1))")
    for name in FILES:
        src=source/name;dst=target/name
        if src.exists() and not dst.exists():shutil.copy2(src,dst)
    if (source/'articles').exists() and not (target/'articles').exists():shutil.copytree(source/'articles',target/'articles')
    if os.geteuid()==0:
        for path in [target,*target.rglob('*')]:os.chown(path,10001,10001)
    print('SQLite import finished; source files preserved')


def backup(root):
    target=root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ');target.mkdir(parents=True)
    subprocess.run(['pg_dump','--schema',db.schema(),'--format=custom','--file',str(target/'postgres.dump')],check=True)
    for name in FILES:
        source=Path('data')/name
        if source.exists():shutil.copy2(source,target/name)
    if Path('data/articles').exists():shutil.copytree('data/articles',target/'articles')
    shutil.copyfile('config.json',target/'config.json')
    (target/'manifest.json').write_text(json.dumps(dict(created_at=time.time(),format=2,files=[p.name for p in target.iterdir()]),indent=2))
    print(str(target))


def restore(source):
    Path('data').mkdir(exist_ok=True)
    db.init_schema()
    with db.connect() as conn:
        tables=('news','responses','hosts','meta','jobs','source_schedule','readings','checks','vectors','deliveries','aliases')
        if any(conn.execute('SELECT count(*) AS n FROM '+table).fetchone()['n'] for table in tables):
            raise RuntimeError('Restore requires an empty database')
    subprocess.run(['pg_restore','--dbname',db.settings()['dbname'],'--clean','--if-exists','--exit-on-error',
                    '--single-transaction',str(source/'postgres.dump')],check=True)
    for name in FILES:
        src=source/name;dst=Path('data')/name
        if src.exists():shutil.copy2(src,dst)
    print('PostgreSQL backup restored')


def health(role):
    value=ServiceState().get('heartbeat:'+role,{})
    good=time.time()-value.get('at',0)<(90 if role=='publisher' else 35) and value.get('status') not in ('stopped','error','blocked')
    raise SystemExit(0 if good else 1)


def main():
    parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init-env');p=commands.add_parser('health');p.add_argument('role',choices=['collector','publisher'])
    for name in ('import-data','migrate-sqlite'):
        p=commands.add_parser(name);p.add_argument('source',type=Path);p.add_argument('--include-control',action='store_true')
    p=commands.add_parser('backup');p.add_argument('destination',type=Path)
    p=commands.add_parser('restore');p.add_argument('source',type=Path)
    commands.add_parser('status')
    args=parser.parse_args()
    if args.command=='init-env':init_env()
    elif args.command=='health':health(args.role)
    elif args.command in ('import-data','migrate-sqlite'):migrate_sqlite(args.source,args.include_control)
    elif args.command=='backup':backup(args.destination)
    elif args.command=='restore':restore(args.source)
    elif args.command=='status':print(json.dumps(ServiceState().status(),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

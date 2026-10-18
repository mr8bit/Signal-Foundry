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
import sys
import time
import tempfile
from service_state import ServiceState

DATABASES=('news.sqlite3','http-cache.sqlite3','state.sqlite3','service.sqlite3')
FILES=('feed-snapshot.json','feed-status.json')


def copy_database(source,destination,offline=False):
    destination.parent.mkdir(parents=True,exist_ok=True)
    wal=Path(str(source)+'-wal')
    # An offline WAL-mode DB may still have the WAL header but no -wal/-shm files.
    # immutable makes that complete, checkpointed DB readable from a read-only import mount.
    # Live backups must retain SQLite locking even if a WAL does not exist yet.
    immutable=offline and (not wal.exists() or wal.stat().st_size==0)
    uri=source.resolve().as_uri()+'?mode=ro'+('&immutable=1' if immutable else '')
    with tempfile.NamedTemporaryFile(dir=destination.parent,prefix=destination.name+'.',suffix='.tmp',delete=False) as file:temp=Path(file.name)
    src=dst=None
    try:
        src=sqlite3.connect(uri,uri=True);dst=sqlite3.connect(temp)
        src.backup(dst);dst.execute('PRAGMA journal_mode=DELETE')
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity check failed')
        dst.close();dst=None;src.close();src=None
        temp.replace(destination)
    finally:
        if dst:dst.close()
        if src:src.close()
        temp.unlink(missing_ok=True)


def init_env():
    path=Path('.env')
    if path.exists():
        print('.env already exists; preserved');return
    template=Path('.env.example').read_text()
    token=secrets.token_urlsafe(48)
    template=template.replace('SIGNAL_ADMIN_TOKEN=','SIGNAL_ADMIN_TOKEN='+token,1)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as out:out.write(template)
    print('Created .env (0600); management key generated, Telegram remains disabled')


def import_data(source,include_control=False):
    target=Path('data');target.mkdir(exist_ok=True)
    for name in DATABASES:
        if name=='service.sqlite3' and not include_control:continue
        src=source/name;dst=target/name
        if src.exists() and (not dst.exists() or dst.stat().st_size==0):copy_database(src,dst,offline=True)
    for name in FILES:
        src=source/name;dst=target/name
        if src.exists() and not dst.exists():shutil.copy2(src,dst)
    if (source/'articles').exists() and not (target/'articles').exists():shutil.copytree(source/'articles',target/'articles')
    if os.geteuid()==0:
        for path in [target,*target.rglob('*')]:os.chown(path,10001,10001)
    print('Data imported; existing destination files preserved')


def backup(root):
    target=root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ');target.mkdir(parents=True)
    for name in DATABASES:
        source=Path('data')/name
        if source.exists():copy_database(source,target/name)
    for name in FILES:
        source=Path('data')/name
        if source.exists():shutil.copy2(source,target/name)
    if Path('data/articles').exists():shutil.copytree('data/articles',target/'articles')
    shutil.copyfile('config.json',target/'config.json')
    (target/'manifest.json').write_text(json.dumps(dict(created_at=time.time(),format=1,files=[p.name for p in target.iterdir()]),indent=2))
    print(str(target))


def health(role):
    value=ServiceState().get('heartbeat:'+role,{})
    good=time.time()-value.get('at',0)<(90 if role=='publisher' else 35) and value.get('status') not in ('stopped','error','blocked')
    raise SystemExit(0 if good else 1)


def main():
    parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init-env');p=commands.add_parser('health');p.add_argument('role',choices=['collector','publisher'])
    p=commands.add_parser('import-data');p.add_argument('source',type=Path);p.add_argument('--include-control',action='store_true')
    p=commands.add_parser('backup');p.add_argument('destination',type=Path)
    commands.add_parser('status')
    args=parser.parse_args()
    if args.command=='init-env':init_env()
    elif args.command=='health':health(args.role)
    elif args.command=='import-data':import_data(args.source,args.include_control)
    elif args.command=='backup':backup(args.destination)
    elif args.command=='status':print(json.dumps(ServiceState().status(),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

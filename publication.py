"""Publish immutable dashboard generations, switch readers with one atomic pointer."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import threading
import time
from uuid import uuid4
import dashboard


class Publications:
    def __init__(self,root=None):
        self.root=Path(root or os.getenv('SIGNAL_PUBLICATIONS','data/published'))
        self._lock=threading.Lock();self._cached=None

    def publish(self,data):
        self.root.mkdir(parents=True,exist_ok=True)
        identifier=f'{time.time_ns()}-{uuid4().hex[:8]}'
        folder=self.root/'generations'/identifier;folder.mkdir(parents=True)
        html=dashboard.render_html(data).encode()
        raw=json.dumps(data,ensure_ascii=False).encode()
        dashboard.write_atomic(folder/'index.html',html);dashboard.write_atomic(folder/'news.json',raw)
        base=os.getenv('SIGNAL_PUBLIC_URL','http://127.0.0.1:8765').rstrip('/')
        dashboard.write_atomic(folder/'feed.xml',dashboard.rss(data,base_url=base))
        manifest=dict(generation=identifier,generated_at=data['generated_at'],collected_at=data.get('collected_at',data['generated_at']),
                      scoring_version=data['scoring_version'],items=len(data['items']),html_etag=sha256(html).hexdigest(),
                      json_etag=sha256(raw).hexdigest())
        dashboard.write_atomic(self.root/'current.json',json.dumps(manifest))
        self.prune(identifier)
        return manifest

    def load(self):
        with self._lock:
            manifest=json.loads((self.root/'current.json').read_text())
            identifier=manifest['generation']
            if not identifier or any(c not in '0123456789abcdef-' for c in identifier):raise ValueError('Invalid publication generation')
            if self._cached is not None and self._cached['manifest']['generation']==identifier:return self._cached
            folder=self.root/'generations'/identifier
            raw=(folder/'news.json').read_bytes();html=(folder/'index.html').read_bytes()
            if sha256(raw).hexdigest()!=manifest['json_etag'] or sha256(html).hexdigest()!=manifest['html_etag']:
                raise ValueError('Publication checksum mismatch')
            value=dict(manifest=manifest,data=json.loads(raw),json=raw,html=html)
            self._cached=value
            return value

    def prune(self,current,keep=5):
        folders=sorted((self.root/'generations').iterdir(),reverse=True)
        for folder in folders[keep:]:
            if folder.is_dir() and folder.name!=current and time.time()-folder.stat().st_mtime>3600:
                shutil.rmtree(folder)

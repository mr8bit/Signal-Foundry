"""Story vectors from an optional OpenAI-compatible embeddings API (SIGNAL_EMBEDDINGS_URL/KEY/MODEL), cached in PostgreSQL.

A dashboard build embeds only stories it has not seen, at most PER_REFRESH of them, so a slow or failing API
delays matching by a cycle and never blocks a publication; stories without a vector fall back to TF-IDF.

Memory: a vector is cut to its first DIMS values (Matryoshka-trained embedders keep their ranking there) and
stored as int8, 256 bytes a story, so a year of the archive is a few megabytes. Stories are compared only
inside a 72-hour window of a few hundred, which brute force handles faster than a vector index would start."""
import array
import hashlib
import json
import logging
import math
from operator import mul
import os
import urllib.request

import db

LOG=logging.getLogger('embeddings')
DIMS=256
LEAD_CHARS=400
BATCH=16
PER_REFRESH=512
# Instruction-tuned embedders (Qwen3, Giga-Embeddings) separate same-event reports from same-topic ones better with it.
TASK='Instruct: Given a news headline and lead, retrieve reports about the same news event\nQuery: '


def settings():
    """(url, key, model), or None when the matcher stays TF-IDF."""
    url,key,model=(os.getenv('SIGNAL_EMBEDDINGS_'+name,'').strip() for name in ('URL','KEY','MODEL'))
    return (url.rstrip('/'),key,model) if url and model else None


def is_story(p):
    """Articles and discussions; models, repositories and papers match by their links instead."""
    return not p.get('content_kind')


def story_text(p):
    return (p['title']+'\n'+(p.get('article_text') or p.get('selftext',''))[:LEAD_CHARS]).strip()


def digest(text):
    """Vectors follow the text: a story whose article text is fetched later gets a new vector."""
    return hashlib.sha256(text.encode()).digest()[:16]


def quantize(values):
    head=values[:DIMS];scale=max(map(abs,head)) or 1
    return array.array('b',(round(x/scale*127) for x in head))


def cosine(a,b):
    """Cosine of two loaded vectors (int8 values, norm)."""
    return sum(map(mul,a[0],b[0]))/(a[1]*b[1])


def request(texts,config):
    url,key,model=config
    headers={'Content-Type':'application/json'}|({'Authorization':'Bearer '+key} if key else {})
    body=json.dumps({'model':model,'input':[TASK+t for t in texts]}).encode()
    with urllib.request.urlopen(urllib.request.Request(url+'/embeddings',body,headers),timeout=90) as response:
        data=json.load(response)['data']
    return [x['embedding'] for x in sorted(data,key=lambda x:x['index'])]


class Store:
    def __init__(self,path=None):
        # Outcome of the last refresh for monitoring: vectors added, still missing, the failure class if any.
        self.last={}

    def refresh(self,posts,config=None,embed=request):
        """Embed new stories, newest first, and drop vectors of stories that left the archive or of another model."""
        config=config or settings()
        if not config:return 0
        wanted={}
        for p in sorted(filter(is_story,posts),key=lambda p:-p['created_utc']):
            text=story_text(p);wanted.setdefault(digest(text),text)
        with db.connect() as connection:
            connection.execute('DELETE FROM vectors WHERE model!=%s',(config[2],))
            stored={bytes(row['digest']) for row in connection.execute('SELECT digest FROM vectors')}
            connection.cursor().executemany('DELETE FROM vectors WHERE digest=%s',((d,) for d in stored-wanted.keys()))
        missing=[d for d in wanted if d not in stored]
        added=0;error=''
        limit=min(len(missing),PER_REFRESH)
        for start in range(0,limit,BATCH):
            batch=missing[start:min(start+BATCH,limit)]
            try:vectors=embed([wanted[d] for d in batch],config)
            except Exception as exc:
                error=exc.__class__.__name__
                LOG.warning('embeddings_failed added=%s error=%s',added,error);break
            with db.connect() as connection:
                connection.cursor().executemany('''INSERT INTO vectors (digest,model,vector) VALUES (%s,%s,%s)
                    ON CONFLICT (digest,model) DO UPDATE SET vector=EXCLUDED.vector''',
                                       ((d,config[2],quantize(v).tobytes()) for d,v in zip(batch,vectors)))
            added+=len(batch)
        if added:LOG.info('embeddings_added count=%s pending=%s',added,len(missing)-added)
        self.last=dict(model=config[2],stories=len(wanted),added=added,pending=len(missing)-added,error=error)
        return added

    def load(self,config=None):
        """{digest: (int8 values, norm)} for the configured model; empty without one."""
        config=config or settings()
        if not config:return {}
        result={}
        with db.connect() as connection:
            for row in connection.execute('SELECT digest,vector FROM vectors WHERE model=%s',(config[2],)):
                values=array.array('b',row['vector'])
                result[bytes(row['digest'])]=(values,math.sqrt(sum(map(mul,values,values))) or 1)
        return result

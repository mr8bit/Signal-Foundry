"""Fetch article bodies for explicitly enabled sources, with RSS fallback."""
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from http_cache import HTTPCache, CacheError


class ArticleBody(HTMLParser):
    """Read paragraphs inside TechCrunch's article body, excluding site widgets."""
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    HIDDEN = {'script','style','nav','aside','figure','footer','form','button','iframe','svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.paragraphs, self.buffer = [], [], []
        self.found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else ('', False, False, False)
        classes = attrs.get('class', '').split()
        body = parent[1] or 'entry-content' in classes or 'wp-block-post-content' in classes
        self.found |= body
        hidden = parent[2] or tag in self.HIDDEN or bool(re.search(
            r'ad-unit|advert|newsletter|event|promo|related|social-share|author-bio', attrs.get('class',''), re.I))
        paragraph = parent[3] or tag == 'p'
        if tag == 'p' and body and not hidden:
            self.buffer = []
        if tag == 'br' and body and not hidden and paragraph:
            self.buffer.append(' ')
        if tag not in self.VOID:
            self.stack.append((tag, body, hidden, paragraph))

    def handle_data(self, data):
        if self.stack:
            _, body, hidden, paragraph = self.stack[-1]
            if body and paragraph and not hidden:
                self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == 'p' and self.stack:
            _, body, hidden, _ = self.stack[-1]
            if body and not hidden:
                text = re.sub(r'\s+', ' ', ''.join(self.buffer)).strip()
                if text:
                    self.paragraphs.append(text)
                self.buffer = []
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break

    def text(self):
        return '\n\n'.join(self.paragraphs)


def parse_article(data):
    parser = ArticleBody()
    parser.feed(data.decode('utf-8'))
    text = parser.text()
    if not parser.found or len(text) < 150:
        raise RuntimeError('Article body unavailable or too short')
    return text


class ArticleFetcher:
    def __init__(self, cache_dir='data/articles', ttl=None, http_cache=None):
        self.cache_dir = Path(cache_dir)
        self.http=http_cache or HTTPCache(None if str(cache_dir)=='data/articles' else self.cache_dir/'http-cache.sqlite3')
        self.ttl=ttl if ttl is not None else self.http.policy['article_ttl_seconds']

    def get(self, url, user_agent):
        if (urlsplit(url).hostname or '').lower() not in ('techcrunch.com','www.techcrunch.com'):
            raise RuntimeError('Article host is not enabled')
        path = self.cache_dir / (sha256(url.encode()).hexdigest() + '.json')
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if cached['url'] == url and time.time() - cached['fetched_at'] < self.ttl:
                    return cached['text']
            except (ValueError, KeyError, OSError):
                pass
        try:
            response=self.http.get(url,{'User-Agent':user_agent,'Accept':'text/html'},ttl=self.ttl,
                                   timeout=20,max_bytes=3_000_000,validator=parse_article,loader=urlopen,
                                   allowed_hosts=('techcrunch.com','www.techcrunch.com'))
            text=parse_article(response.body)
        except CacheError as exc:
            raise RuntimeError(str(exc)) from None
        self.cache_dir.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',dir=self.cache_dir,prefix=path.stem+'.',suffix='.tmp',delete=False) as temp:
            temp.write(json.dumps({'url':url,'fetched_at':response.validated_at,'text':text},ensure_ascii=False))
        Path(temp.name).replace(path)
        return text

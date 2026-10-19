"""RSS 2.0 / Atom normalization and Anthropic newsroom adapter."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET
from cloud_news import normalize_note


def article_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        return ''
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'ref', 'source')]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(query), ''))


class Content(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.hidden = base, [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('p', 'br', 'div', 'li', 'blockquote'):
            self.parts.append('\n')
        if tag == 'a':
            link = article_url(urljoin(self.base, dict(attrs).get('href', '')))
            if link:
                self.links.append(link)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden-1)
        elif tag in ('p', 'div', 'li', 'blockquote'):
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)

    def text(self):
        return '\n'.join(line for raw in ''.join(self.parts).splitlines()
                         if (line := re.sub(r'\s+', ' ', raw).strip()))


def timestamp(value):
    try:
        date = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        date = parsedate_to_datetime(value.strip())
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.timestamp()


def post(source, title, url, stamp, content, rank):
    url = article_url(urljoin(source['url'], url))
    if not url or not title.strip():
        return None
    try:
        created = timestamp(stamp)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None  # No invented publication date for undated entries.
    parser = Content(url)
    parser.feed(content)
    title_parser = Content(url)
    title_parser.feed(title)
    return dict(id=sha256(url.encode()).hexdigest()[:24], title=title_parser.text(),
                source='site', source_id=source['id'], source_name=source['name'],
                category=source['category'], filter_mode=source.get('filter_mode', 'keywords'),
                created_utc=created, url=url, article_url=url, selftext=parser.text(),
                links=list(dict.fromkeys(parser.links)), feed_rank=rank, score=None,
                **{key:source[key] for key in ('cloud_rule','cloud_provider','cloud_label','region') if key in source})


def parse_site_feed(data, source):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise RuntimeError('Invalid feed XML (possibly an HTML block page)') from None
    ns = {'a': 'http://www.w3.org/2005/Atom', 'content': 'http://purl.org/rss/1.0/modules/content/',
          'dc': 'http://purl.org/dc/elements/1.1/', 'slash': 'http://purl.org/rss/1.0/modules/slash/',
          'thr': 'http://purl.org/syndication/thread/1.0'}
    atom = root.tag == '{http://www.w3.org/2005/Atom}feed'
    if atom:
        entries = root.findall('a:entry', ns)
    elif root.tag == 'rss':
        entries = root.findall('./channel/item')
    else:
        raise RuntimeError('Expected RSS 2.0 or Atom feed')
    result = []
    for rank, entry in enumerate(entries):
        if atom:
            url = next((x.get('href', '') for x in entry.findall('a:link', ns)
                        if x.get('rel', 'alternate') == 'alternate'), '')
            title = entry.findtext('a:title', '', ns)
            stamp = entry.findtext('a:published', '', ns) or entry.findtext('a:updated', '', ns)
            node = entry.find('a:content', ns)
            if node is None:
                node = entry.find('a:summary', ns)
            if node is not None and len(node):
                for child in node.iter():
                    child.tag = child.tag.rsplit('}', 1)[-1]
                content = ET.tostring(node, encoding='unicode')
            else:
                content = '' if node is None else node.text or ''
        else:
            url = entry.findtext('link', '') or entry.findtext('guid', '')
            title = entry.findtext('title', '')
            stamp = entry.findtext('pubDate', '') or entry.findtext('dc:date', '', ns)
            content = entry.findtext('content:encoded', '', ns) or entry.findtext('description', '')
        item = post(source, title, url, stamp, content, rank)
        if item and not atom:
            item = google_news_item(item, entry.find('source'))
        if item:
            item=normalize_note(item)
            # WordPress (slash) and Atom threading (thr) feeds carry the article's comment count.
            comments=(entry.findtext('slash:comments', '', ns) or entry.findtext('thr:total', '', ns)).strip()
            if comments.isdigit():
                item['metrics']=[dict(kind='comments',value=int(comments),label='comments')]
            if source['id'] == 'hn':
                item['discussion_url'] = next((x for x in item['links'] if 'news.ycombinator.com/item?' in x), '')
            result.append(item)
    return result


def google_news_item(item, outlet):
    """A Google News search feed stands in for an outlet that blocks our feed requests (VentureBeat). Its links are
    Google redirects, so the outlet comes from the item's <source url>: it identifies the publisher for coverage and
    the card's domain. The title loses its " - Outlet" suffix; the description only repeats the title."""
    if (urlsplit(item['url']).hostname or '') != 'news.google.com' or outlet is None:
        return item
    host = (urlsplit(outlet.get('url', '')).hostname or '').lower().removeprefix('www.')
    name = (outlet.text or '').strip()
    title = item['title'][:-len(' - '+name)] if name and item['title'].endswith(' - '+name) else item['title']
    # The card names the outlet ("Meduza"), not the search that found it.
    return item | dict(title=title, selftext='', links=[], publisher_host=host or None, source_name=name or item['source_name'])


class Newsroom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.current, self.stack = [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            href = attrs.get('href', '')
            if href.startswith(('/news/', '/claude-')):
                self.current = {'url': href, 'title': [], 'date': [], 'body': []}
                self.stack = []
        if self.current is not None and tag not in ('img', 'br', 'input', 'meta', 'link', 'hr'):
            field = 'date' if tag == 'time' else 'title' if tag in ('h2', 'h3', 'h4') or '__title' in attrs.get('class', '') else 'body' if tag == 'p' else ''
            self.stack.append((tag, field))
            if tag == 'time' and attrs.get('datetime'):
                self.current['date'] = [attrs['datetime']]

    def handle_data(self, data):
        if self.current is not None:
            field = next((field for _, field in reversed(self.stack) if field), '')
            if field:
                self.current[field].append(data)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == 'a':
            self.items.append(self.current)
            self.current = None
            self.stack = []
        else:
            for i in range(len(self.stack)-1, -1, -1):
                if self.stack[i][0] == tag:
                    self.stack = self.stack[:i]
                    break


def parse_newsroom(data, source):
    parser = Newsroom()
    parser.feed(data.decode('utf-8'))
    result, seen = [], set()
    for rank, item in enumerate(parser.items):
        raw_date = ''.join(item['date']).strip()
        try:
            stamp = datetime.strptime(raw_date, '%b %d, %Y').replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            stamp = raw_date
        p = post(source, ''.join(item['title']), item['url'], stamp, ''.join(item['body']), rank)
        if p and p['url'] not in seen:
            seen.add(p['url'])
            result.append(p)
    if not result:
        raise RuntimeError('Anthropic newsroom markup changed or no dated articles found')
    return result

"""Read application state from PostgreSQL and publish node_exporter metrics on stdout."""
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

import db

PREFIX = 'signal_foundry_'


def label(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


def safe_error(value):
    value = re.sub(r'https?://\S+', '[URL removed]', str(value or ''))
    value = re.sub(r'(?i)(bearer|token|key)=?\s*[A-Za-z0-9._-]{16,}', r'\1 [removed]', value)
    return ''.join(c for c in value if c >= ' ')[:200]


class Metrics:
    def __init__(self):
        self.lines = []
        self.names = set()

    def add(self, name, value, labels=None, help='', kind='gauge'):
        name = PREFIX + name
        if name not in self.names:
            self.lines.extend([f'# HELP {name} {help or name}', f'# TYPE {name} {kind}'])
            self.names.add(name)
        suffix = '{' + ','.join(f'{k}="{label(v)}"' for k, v in (labels or {}).items()) + '}' if labels else ''
        self.lines.append(f'{name}{suffix} {float(value or 0):.6f}')

    def text(self):
        return '\n'.join(self.lines) + '\n'


def active_sources(config):
    sources = {f'Reddit group {i+1}': dict(kind='reddit', region='', category='community')
               for i in range(len(config.get('rss_groups', [])))}
    for feed in config.get('feeds', []):
        if feed.get('enabled', True):
            sources[feed['name']] = dict(kind=feed.get('format', 'feed'), region=feed.get('region', ''),
                                         category=feed.get('category', ''), id=feed['id'], url=feed['url'])
    return sources


LOOKUP_HOSTS = {'www.reddit.com', 'news.google.com', 'hn.algolia.com', 'api.github.com', 'huggingface.co', 'techcrunch.com'}


def active_hosts(config):
    urls = [feed['url'] for feed in config.get('feeds', []) if feed.get('enabled', True)]
    urls += list((config.get('attention') or {}).get('most_read', {}).values())
    return LOOKUP_HOSTS | {urlsplit(url).hostname for url in urls if urlsplit(url).hostname}


def service(m, sources, now):
    with db.connect() as connection:
        meta = {row['key']: json.loads(row['value']) for row in connection.execute('SELECT key,value FROM meta')}
        heartbeat = meta.get('heartbeat:collector') or {}
        m.add('collector_heartbeat_timestamp_seconds', heartbeat.get('at'), help='Last heartbeat of the collector process.')
        m.add('collector_running', int(heartbeat.get('status') == 'running'))
        publication = meta.get('publication') or {}
        m.add('publication_generated_timestamp_seconds', publication.get('generated_at'), help='When the published ranking was built.')
        m.add('publication_collected_timestamp_seconds', publication.get('collected_at'), help='Newest source data in the publication.')
        m.add('publication_items', publication.get('items'))
        m.add('last_success_timestamp_seconds', meta.get('last_success'))
        m.add('collections_succeeded_total', meta.get('collections_succeeded', 0), kind='counter')
        m.add('collections_failed_total', meta.get('collections_failed', 0), kind='counter')
        running = connection.execute("SELECT started FROM jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        m.add('job_running', int(bool(running)))
        m.add('job_running_age_seconds', now - running['started'] if running and running['started'] else 0)
        queued = connection.execute("SELECT count(*) AS n FROM jobs WHERE status='queued'").fetchone()['n']
        m.add('jobs_queued', queued)
        recent = connection.execute('SELECT finished-started AS duration,status FROM jobs WHERE finished>%s AND started IS NOT NULL', (now - 3600,)).fetchall()
        durations = sorted(row['duration'] for row in recent if row['duration'] is not None)
        m.add('jobs_finished_1h', len(recent), help='Collection jobs finished in the last hour; one source is polled per job.')
        m.add('jobs_failed_1h', sum(row['status'] == 'failed' for row in recent))
        m.add('job_duration_avg_1h_seconds', sum(durations) / len(durations) if durations else 0)
        m.add('job_duration_max_1h_seconds', durations[-1] if durations else 0)
        last = connection.execute("SELECT finished,finished-started AS duration FROM jobs WHERE status IN ('done','failed') ORDER BY finished DESC LIMIT 1").fetchone()
        m.add('job_last_finished_timestamp_seconds', last['finished'] if last else 0)
        m.add('job_last_duration_seconds', last['duration'] if last else 0)
        failed = connection.execute("SELECT finished,error FROM jobs WHERE status='failed' ORDER BY finished DESC LIMIT 1").fetchone()
        if failed:
            m.add('job_last_error_timestamp_seconds', failed['finished'], {'message': safe_error(failed['error'])})
        rows = {}
        for row in connection.execute('SELECT url,name,status,next_at,last_attempt,last_success FROM source_schedule ORDER BY last_attempt'):
            info = sources.get(row['name'])
            if info and info.get('url', row['url']) == row['url']:
                rows[row['name']] = (json.loads(row['status']), row['next_at'], row['last_attempt'], row['last_success'])
    m.add('sources_configured', len(sources))
    counts = {'ok': 0, 'stale': 0, 'error': 0, 'scheduled': 0}
    for name, info in sorted(sources.items()):
        status, next_at, attempt, success = rows.get(name, ({'status': 'scheduled'}, 0, 0, 0))
        state = status.get('status', 'scheduled')
        counts[state if state in counts else 'error'] += 1
        tags = {'source': name, 'kind': info['kind'], 'category': info['category'], 'region': info['region']}
        m.add('source_up', int(state == 'ok'), tags, help='1 when the last poll succeeded; stale means an old copy is served.')
        m.add('source_stale', int(state == 'stale'), tags)
        m.add('source_items', status.get('count', 0), tags, help='Entries in the last successful response.')
        m.add('source_http_status', status.get('http_status') or 0, tags)
        m.add('source_last_success_timestamp_seconds', success, tags)
        m.add('source_last_attempt_timestamp_seconds', attempt, tags)
        m.add('source_next_poll_timestamp_seconds', next_at, tags)
        if status.get('error'):
            m.add('source_error_info', attempt, dict(tags, message=safe_error(status['error'])),
                  help='Last poll error of a source; value is the attempt time.')
    for state, count in counts.items():
        m.add('sources', count, {'state': state})


def archive(m, sources, now):
    names = {info['id']: name for name, info in sources.items() if info.get('id')}
    with db.connect() as connection:
        m.add('archive_posts', connection.execute('SELECT count(*) AS n FROM news').fetchone()['n'])
        seen = {}
        for row in connection.execute('SELECT key FROM news WHERE first_seen>%s', (now - 86400,)):
            prefix = row['key'].rsplit(':', 1)[0]
            name = 'Reddit' if prefix.startswith('reddit:') else names.get(prefix, prefix)
            seen[name] = seen.get(name, 0) + 1
    for name, count in sorted(seen.items()):
        m.add('archive_new_posts_24h', count, {'source': name}, help='Posts first archived in the last 24 hours, before filters.')


def lookups(m, now, hosts):
    with db.connect() as connection:
        m.add('attention_readings', connection.execute('SELECT count(*) AS n FROM readings').fetchone()['n'])
        for row in connection.execute('SELECT kind,count(*) AS n FROM checks WHERE checked_at>%s GROUP BY kind', (now - 3600,)):
            m.add('attention_lookups_1h', row['n'], {'kind': row['kind']}, help='Distinct stories looked up in the last hour.')
        for row in connection.execute('SELECT channel,max(observed_at) AS last FROM readings GROUP BY channel'):
            m.add('attention_last_reading_timestamp_seconds', row['last'], {'channel': row['channel']})
        for row in connection.execute('SELECT model,count(*) AS n FROM vectors GROUP BY model'):
            m.add('embedding_vectors', row['n'], {'model': row['model']})
        for row in connection.execute('SELECT host,retry_at,status,rate_failures FROM hosts'):
            if row['host'] not in hosts:
                continue
            tags = {'host': row['host']}
            m.add('host_backoff', int(row['retry_at'] > now), tags, help='1 while requests to the host wait for a backoff to end.')
            m.add('host_backoff_until_timestamp_seconds', row['retry_at'] if row['retry_at'] > now else 0, tags)
            m.add('host_rate_failures', row['rate_failures'], tags)
            m.add('host_http_status', row['status'] or 0, tags)


def telemetry(m, data):
    path = data / 'telemetry.json'
    if not path.exists():
        return
    runs = json.loads(path.read_text())
    attention = runs.get('attention') or {}
    m.add('attention_run_timestamp_seconds', attention.get('at'))
    m.add('attention_run_duration_seconds', attention.get('duration_seconds'))
    m.add('attention_run_readings', attention.get('readings'))
    m.add('attention_run_failed', int(bool(attention.get('error'))))
    for kind, count in (attention.get('lookups') or {}).items():
        m.add('attention_run_lookups', count, {'kind': kind})
    for kind, count in (attention.get('errors') or {}).items():
        m.add('attention_run_errors', count, {'kind': kind})
    embedding = runs.get('embeddings') or {}
    m.add('embedding_run_timestamp_seconds', embedding.get('at'))
    m.add('embedding_run_duration_seconds', embedding.get('duration_seconds'))
    m.add('embedding_run_added', embedding.get('added'))
    m.add('embedding_run_pending', embedding.get('pending'), help='Kept stories still without a vector after the last run.')
    m.add('embedding_run_stories', embedding.get('stories'))
    m.add('embedding_run_failed', int(bool(embedding.get('error'))), {'error': safe_error(embedding.get('error'))})
    build = runs.get('build') or {}
    m.add('build_timestamp_seconds', build.get('at'))
    m.add('build_duration_seconds', build.get('duration_seconds'), help='Embeddings refresh plus ranking of every collection.')
    m.add('build_ranking_seconds', build.get('ranking_seconds'))


def publication(m, data, now):
    published = json.loads((data / 'dashboard.json').read_text())
    for category, rows in sorted((published.get('signal_rankings') or {}).items()):
        m.add('ranking_stories', len(rows), {'category': category})
    m.add('ranking_stories', len(published.get('items', [])), {'category': 'all'})
    columns = {}
    for item in published.get('items', []):
        if 0 <= now - item.get('news_at', item.get('published_at', 0)) <= 86400:
            columns[item.get('column', '')] = columns.get(item.get('column', ''), 0) + 1
    for column, count in sorted(columns.items()):
        m.add('stories_24h', count, {'column': column}, help='Stories that became news in the last 24 hours, by column.')


def snapshot(config, data=None, now=None):
    now = time.time() if now is None else now
    data = Path(data or 'data')
    sources = active_sources(config)
    m = Metrics()
    m.add('snapshot_timestamp_seconds', now, help='Time of the last successful snapshot.')
    service(m, sources, now)
    archive(m, sources, now)
    lookups(m, now, active_hosts(config))
    telemetry(m, data)
    publication(m, data, now)
    with db.connect() as connection:
        size = connection.execute('SELECT pg_database_size(current_database()) AS size').fetchone()['size']
    m.add('postgres_database_bytes', size, help='Total size of the application PostgreSQL database.')
    for name in ('dashboard.json', 'feed-snapshot.json'):
        if (data / name).exists():
            m.add('file_bytes', (data / name).stat().st_size, {'file': name})
    return m.text()


def main():
    config = json.loads(Path('config.json').read_text())
    sys.stdout.write(snapshot(config))


if __name__ == '__main__':
    main()

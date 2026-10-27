import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import db
import monitoring
from service_state import ServiceState


def values(text):
    result = {}
    for line in text.splitlines():
        if line and not line.startswith('#'):
            name, value = line.rsplit(' ', 1)
            result[name] = float(value)
    return result


class Monitoring(unittest.TestCase):
    def setUp(self):
        from db_test import use_schema
        use_schema(self)
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / 'data'
        self.data.mkdir()
        self.cfg = dict(
            rss_groups=[['LocalLLaMA']],
            feeds=[dict(id='a', name='A', url='https://same.test/a', format='feed', category='news')],
            attention=dict(most_read={'a': 'https://same.test/popular'}),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_reads_postgres_and_preserves_metric_families(self):
        state = ServiceState()
        state.set('heartbeat:collector', dict(at=1900, status='running'))
        state.set('publication', dict(generated_at=1500, collected_at=1400, items=2))
        state.set('last_success', 1800)
        state.set('collections_succeeded', 5)
        state.set('collections_failed', 1)
        with db.connect() as connection:
            connection.execute("INSERT INTO jobs(requested,started,status) VALUES (1900,1900,'running')")
            connection.execute("INSERT INTO jobs(requested,started,finished,status) VALUES (1700,1700,1800,'done')")
            connection.execute("INSERT INTO news(key,payload,first_seen,last_seen) VALUES ('a:one','{}',1900,1950)")
            connection.execute("INSERT INTO source_schedule(url,name,payload,status,next_at,last_attempt,last_success) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                               ('https://same.test/a', 'A', '[]', '{"status":"ok","count":3,"http_status":200}', 2100, 1900, 1950))
            connection.execute("INSERT INTO readings(key,channel,value,observed_at) VALUES ('key','hn_points',10,1900)")
            connection.execute("INSERT INTO checks(key,kind,checked_at) VALUES ('key','hn',1900)")
            connection.execute("INSERT INTO vectors(digest,model,vector) VALUES (%s,%s,%s)", (b'1' * 16, 'test-model', b'2' * 256))
            connection.execute("INSERT INTO hosts(host,retry_at,error,status,rate_failures) VALUES (%s,%s,%s,%s,%s)",
                               ('same.test', 2500, 'limited', 429, 2))
        (self.data / 'telemetry.json').write_text(json.dumps(dict(build=dict(at=1900, duration_seconds=3.5, ranking_seconds=2.5))))
        (self.data / 'dashboard.json').write_text(json.dumps(dict(
            generated_at=1500,
            items=[dict(column='media', news_at=1900), dict(column='media', news_at=1900)],
            signal_rankings=dict(media=[{}, {}]),
        )))
        (self.data / 'feed-snapshot.json').write_text('[]')

        text = monitoring.snapshot(self.cfg, self.data, now=2000)
        found = values(text)
        self.assertEqual(found['signal_foundry_publication_generated_timestamp_seconds'], 1500)
        self.assertEqual(found['signal_foundry_publication_collected_timestamp_seconds'], 1400)
        self.assertEqual(found['signal_foundry_publication_items'], 2)
        self.assertEqual(found['signal_foundry_collector_heartbeat_timestamp_seconds'], 1900)
        self.assertEqual(found['signal_foundry_job_running'], 1)
        self.assertEqual(found['signal_foundry_job_running_age_seconds'], 100)
        self.assertEqual(found['signal_foundry_jobs_finished_1h'], 1)
        self.assertEqual(found['signal_foundry_job_duration_avg_1h_seconds'], 100)
        self.assertEqual(found['signal_foundry_sources_configured'], 2)
        self.assertEqual(found['signal_foundry_archive_posts'], 1)
        self.assertEqual(found['signal_foundry_attention_readings'], 1)
        self.assertEqual(found['signal_foundry_build_duration_seconds'], 3.5)
        self.assertEqual(found['signal_foundry_ranking_stories{category="all"}'], 2)
        self.assertEqual(found['signal_foundry_stories_24h{column="media"}'], 2)
        self.assertGreater(found['signal_foundry_postgres_database_bytes'], 0)
        self.assertIn('signal_foundry_source_up{source="A",kind="feed",category="news",region=""} 1.000000', text)
        self.assertIn('signal_foundry_archive_new_posts_24h{source="A"} 1.000000', text)
        self.assertIn('signal_foundry_attention_lookups_1h{kind="hn"} 1.000000', text)
        self.assertIn('signal_foundry_embedding_vectors{model="test-model"} 1.000000', text)
        self.assertIn('signal_foundry_host_backoff{host="same.test"} 1.000000', text)
        self.assertIn('signal_foundry_file_bytes{file="dashboard.json"}', text)
        self.assertNotIn('.sqlite3', text)

    def test_host_wrapper_publishes_metrics_and_status(self):
        wrapper = runpy.run_path('deploy/signal_foundry_metrics.py')
        payload = '# HELP signal_foundry_test test\n# TYPE signal_foundry_test gauge\nsignal_foundry_test 1.000000\n'
        output = Path(self.temp.name)
        with patch('subprocess.run') as run:
            run.return_value.stdout = payload
            run.return_value.returncode = 0
            wrapper['collect']('/opt/signal-foundry', output)
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ['-T', 'worker', 'python', 'monitoring.py'])
        self.assertEqual(run.call_args.kwargs['cwd'], '/opt/signal-foundry')
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual((output / 'signal-foundry.prom').read_text(), payload)
        self.assertIn('signal_foundry_collector_success 1.000000', (output / 'signal-foundry-status.prom').read_text())

    def test_host_wrapper_failure_preserves_last_metrics_and_reports_failure(self):
        wrapper = runpy.run_path('deploy/signal_foundry_metrics.py')
        output = Path(self.temp.name)
        (output / 'signal-foundry.prom').write_text('previous metrics\n')
        with patch('subprocess.run', side_effect=OSError('docker unavailable')):
            with self.assertRaises(RuntimeError):
                wrapper['collect']('/opt/signal-foundry', output)
        self.assertEqual((output / 'signal-foundry.prom').read_text(), 'previous metrics\n')
        self.assertIn('signal_foundry_collector_success 0.000000', (output / 'signal-foundry-status.prom').read_text())


if __name__ == '__main__':
    unittest.main()

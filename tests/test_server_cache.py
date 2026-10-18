from http.server import ThreadingHTTPServer
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import server


class DashboardCache(unittest.TestCase):
    def app(self):
        data=dict(generated_at=1000,items=[],statuses=[],scoring_version='editorial-v2')
        with patch('server.dashboard.build',return_value=data),patch('server.bot.RedditRSS') as client:
            app=server.Application()
        return app,client.return_value

    def test_repeated_dashboard_api_reads_never_collect_sources(self):
        app,client=self.app()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.handler(app))
        worker=threading.Thread(target=lambda:http.serve_forever(poll_interval=.01),daemon=True);worker.start()
        try:
            for _ in range(3):
                for path in ('/api/news','/api/status','/api/rss?sort=day'):
                    with urlopen(f'http://127.0.0.1:{http.server_port}'+path,timeout=2) as response:
                        self.assertEqual(response.status,200);response.read()
            client.fetch.assert_not_called();client.get.assert_not_called()
        finally:
            http.shutdown();http.server_close();worker.join(timeout=2)

    def test_repeated_refresh_requests_are_coalesced_by_cooldown(self):
        app,client=self.app();client.fetch.return_value=[];client.statuses=[]
        with patch('server.time.time',return_value=1000),patch('server.Path') as paths,patch.object(app,'reload'),patch('server.threading.Thread',side_effect=lambda target,daemon:SimpleNamespace(start=target)):
            paths.return_value.read_text.return_value='{}'
            self.assertTrue(app.refresh())
            self.assertFalse(app.refresh())
            self.assertFalse(app.refreshing)
            self.assertEqual(client.fetch.call_count,1)


if __name__=='__main__':unittest.main()

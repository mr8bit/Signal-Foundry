from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
import dashboard
import digest
from production import SignalApp
from publication import Publications
from service_state import ServiceState
from signals import SCORING_VERSION

NOW=time.time()


def item(id,score,hours=1,column='media',**changes):
    p=dict(id=id,title=f'Story {id}',url=f'https://example.com/{id}',summary='Summary text',source_name='Example',
           sources=['Example'],related_sources=[],column=column,topic='Industry',published_at=NOW-hours*3600,score=score,
           reasons=['Recent relevant news'],metrics=[],coverage=1,domain='example.com',raw_score=None,
           components=[dict(id='attention',label='Attention',points=0,max=25,available=True,channels=[])])
    p.update(changes);return p


def data(items=None,rankings=None):
    items=items or [item('a',40),item('b',70),item('c',70,hours=3),item('old',99,hours=100)]
    return dict(generated_at=NOW,collected_at=NOW,scoring_version=SCORING_VERSION,items=items,statuses=[],sources=[],
                signal_rankings=rankings or {'enterprise':[item('e',30,enterprise_reasons=['Enterprise use'])]})


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__();self.skip=0;self.parts=[]
    def handle_starttag(self,tag,attrs):self.skip+=tag in ('script','style')
    def handle_endtag(self,tag):self.skip-=tag in ('script','style')
    def handle_data(self,text):
        if not self.skip:self.parts.append(text)


class Digest(unittest.TestCase):
    def test_ranking_matches_dashboard_order_and_period(self):
        self.assertEqual([p['id'] for p in digest.ranked(data(),'all','now',NOW)],['b','c','a'])
        self.assertEqual([p['id'] for p in digest.ranked(data(),'all','week',NOW)],['old','b','c','a'])
        self.assertEqual([p['id'] for p in digest.ranked(data(),'enterprise','now',NOW)],['e'])

    def test_tabs_match_dashboard_period_model(self):
        # Same fixture as tests/signal-ranking.test.cjs.
        H,D=3600,86400;now=100*D
        parts=lambda engagement,coverage,freshness:[dict(id='attention',points=(engagement or 0)+coverage,max=25,momentum=(engagement or 0)+coverage),
                                                   dict(id='freshness',points=freshness,max=25)]
        rows=[item('hot',40,published_at=now-3*H,news_at=now-3*H,components=parts(6,15,22)),
              item('quiet-new',70,published_at=now-2*H,news_at=now-2*H,components=parts(None,0,24)),
              item('afternoon',65,published_at=now-14*H,news_at=now-14*H,components=parts(9,7.5,17)),
              item('august-model',90,published_at=now-H,news_at=now-55*D,date_kind='trend',components=parts(10,0,0)),
              item('big-last-week',60,published_at=now-5*D,news_at=now-5*D,components=parts(10,15,0.4)),
              item('small-yesterday',50,published_at=now-20*H,news_at=now-20*H,components=parts(0,0,14))]
        ids=lambda period:[p['id'] for p in digest.ranked(data(rows),'all',period,now)]
        self.assertEqual(ids('trending'),['hot','quiet-new'])
        self.assertEqual(ids('day'),['quiet-new','afternoon','small-yesterday','hot'])
        self.assertEqual(ids('week'),['big-last-week','afternoon','quiet-new','small-yesterday','hot'])
        self.assertNotIn('august-model',ids('month'));self.assertIn('august-model',ids('year'))
        text=digest.markdown(data(rows),'trending',now=now)
        self.assertIn('— momentum 21/25, score 40/100',text)
        self.assertIn('— significance 59.6/75, score 60/100',digest.markdown(data(rows),'week',now=now))
        value=json.loads(digest.signals_json(data(rows),'week',now=now))
        self.assertEqual((value['order'],value['period_hours'],value['items'][0]['significance']),('significance',168,59.6))

    def test_trending_folds_threads_and_limits_reddit_as_a_whole(self):
        # Same fixture as tests/signal-ranking.test.cjs.
        H,D=3600,86400;now=100*D
        row=lambda id,hours,value,**extra:item(id,50,**dict(published_at=now-hours*H,news_at=now-hours*H,source_id=id,
            components=[dict(id='attention',points=value,max=25,momentum=value)])|extra)
        thread=lambda id,hours,value:row(id,hours,value,column='community',platform='reddit',discussion_of='release')
        threads=[thread('t1',2,25),thread('t2',3,20),thread('t-old',20,24)]
        rows=[row('release',30,4,discussions=[dict(id=t['id'],title=t['id'],source='r/x',url='https://x',news_at=t['news_at'],
                                                   momentum=t['components'][0]['momentum']) for t in threads]),*threads,
              *[row(f'r{i+1}',1,v,column='community',platform='reddit',source_id=f'reddit:sub{i}') for i,v in enumerate([19,18,17,16])],
              row('other',1,15)]
        order=digest.ranked(data(rows),'all','trending',now)
        self.assertEqual([p['id'] for p in order],['release','r1','r2','r3','other','r4'])
        self.assertEqual(digest.momentum(order[0]),25);self.assertEqual([t['id'] for t in order[0]['threads']],['t1','t2'])
        self.assertEqual([p['id'] for p in digest.ranked(data(rows),'all','day',now) if p['id'].startswith('t')],['t1','t2','t-old'])
        self.assertIn('Discussed now in: [r/x: t1]',digest.markdown(data(rows),'trending',now=now))

    def test_query_validation(self):
        self.assertEqual(digest.query({}),('now','all',20))
        self.assertEqual(digest.query({'sort':['week'],'category':['papers'],'limit':['5']}),('week','papers',5))
        self.assertEqual(digest.query({'sort':['trending']})[0],'trending')
        for params in ({'sort':['bad']},{'category':['bad']},{'limit':['0']},{'limit':['101']},{'limit':['x']}):
            with self.assertRaises(digest.QueryError):digest.query(params)

    def test_json_is_compact_and_missing_metrics_are_null(self):
        value=json.loads(digest.signals_json(data(),limit=2,now=NOW))
        self.assertEqual(value['total'],3);self.assertEqual([x['id'] for x in value['items']],['b','c'])
        self.assertRegex(value['items'][0]['published_at'],r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        # No attention signal is 0 with no channels listed, never an invented value.
        self.assertEqual(value['items'][0]['score_components']['attention'],0)
        self.assertEqual(value['items'][0]['attention']['channels'],[])

    def test_markdown_escapes_titles_and_unsafe_urls(self):
        hostile=item('x',90,title='Break ](javascript:alert(1)) *bold*\nnext',url='javascript:alert(1)')
        text=digest.markdown(data([hostile]),now=NOW)
        self.assertIn('[Break \\](javascript:alert(1)) \\*bold\\* next]()',text)
        self.assertNotIn('](javascript:',text.replace('\\](javascript:',''))
        self.assertIn('## Enterprise: top 5',text)
        merged=item('m',80,also=[dict(source='Verge',title='Same ](javascript:x) story',url='javascript:x')])
        line=next(l for l in digest.markdown(data([merged]),'week',now=NOW).splitlines() if 'Also covered by' in l)
        self.assertEqual(line,'   - Also covered by: [Verge: Same \\](javascript:x) story]()')
        self.assertEqual(json.loads(digest.signals_json(data([merged]),'week',now=NOW))['items'][0]['also'][0]['url'],'')
        self.assertIn('/api/signals?sort=now&category=all&limit=20',text)

    def test_html_is_readable_without_javascript_and_json_ld_cannot_break_out(self):
        story=item('s',80,title='Model </script><script>alert(1)</script> release')
        html=dashboard.render_html(data([story]))
        parser=VisibleText();parser.feed(html);visible=' '.join(parser.parts)
        self.assertIn('Model </script><script>alert(1)</script> release',visible)
        self.assertNotIn('<script>alert(1)',html)
        ld=re.search(r'<script type="application/ld\+json">(.*?)</script>',html,re.S)[1]
        self.assertEqual(json.loads(ld)['itemListElement'][0]['name'],story['title'])
        self.assertIn('rel="alternate" type="text/markdown"',html)
        self.assertNotIn('<!--STATIC_',html);self.assertNotIn('/*JSON_LD*/',html)

    def test_story_text_cannot_inject_template_placeholders(self):
        html=dashboard.render_html(data([item('p',80,title='/*DASHBOARD_JS*/ and <!--STATIC_FEED-->')]))
        self.assertEqual(html.count('function card(p)'),1)


def request(app,path,accept=None):
    route,_,query=path.partition('?')
    env=dict(PATH_INFO=route,QUERY_STRING=query,REQUEST_METHOD='GET',wsgi_input=io.BytesIO())
    if accept:env['HTTP_ACCEPT']=accept
    captured={}
    def start(status,headers):captured.update(status=int(status.split()[0]),headers=dict(headers))
    captured['body']=b''.join(app(env,start));return captured


class AgentRoutes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        publications=Publications(root/'published');publications.publish(data())
        self.app=SignalApp(ServiceState(root/'service.sqlite3'),publications,admin_token='x',public_url='https://signal.example')
    def tearDown(self):self.temp.cleanup()

    def test_machine_readable_routes(self):
        for path,kind in (('/llms.txt','text/plain'),('/robots.txt','text/plain'),('/signals.md','text/markdown'),
                          ('/index.md','text/markdown'),('/api/signals?category=enterprise','application/json')):
            response=request(self.app,path)
            self.assertEqual(response['status'],200,path);self.assertTrue(response['headers']['Content-Type'].startswith(kind),path)
        self.assertIn(b'https://signal.example/signals.md',request(self.app,'/llms.txt')['body'])
        self.assertEqual(json.loads(request(self.app,'/api/signals?category=enterprise')['body'])['items'][0]['id'],'e')
        self.assertEqual(request(self.app,'/api/signals?sort=bad')['status'],400)

    def test_homepage_points_agents_to_markdown(self):
        page=request(self.app,'/',accept='text/html,*/*')
        self.assertIn('/llms.txt',page['headers']['Link']);self.assertIn(b'Story b',page['body'])
        markdown=request(self.app,'/',accept='text/markdown')
        self.assertTrue(markdown['headers']['Content-Type'].startswith('text/markdown'))
        self.assertTrue(markdown['body'].startswith(b'# Signal'))


if __name__=='__main__':unittest.main()

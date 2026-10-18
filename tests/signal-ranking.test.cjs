const test=require('node:test');
const assert=require('node:assert/strict');
const ranking=require('../web/signal-ranking.js');
const item=(id,category,score,metric)=>({id,score,published_at:100,source_id:'same-provider',
  signal_categories:[category],signal_metrics:metric?{[category]:metric}:{}});

test('enterprise overlaps source categories and uses editorial ranking',()=>{
  const rows=[{...item('business','media',60),signal_categories:['media','enterprise']},
    item('ordinary','media',90),item('security','enterprise',80)];
  assert.deepEqual(ranking.rank(rows,'enterprise','native').map(x=>x.id),['security','business']);
  assert.equal(ranking.metrics.enterprise,undefined);
});

test('a single provider can occupy all positions; editorial score wins over likes',()=>{
  const rows=[item('low','models',10,{kind:'likes',value:100000}),item('high','models',90,{kind:'likes',value:1})];
  assert.deepEqual(ranking.rank(rows,'models').map(x=>x.id),['high','low']);
  assert.deepEqual(rows.map(x=>x.id),['low','high']); // no mutation of the feed
});
test('native model ranking keeps trendingScore and fallback likes in separate groups',()=>{
  const rows=[item('likes','models',90,{kind:'likes',value:1000000}),item('trend-low','models',30,{kind:'trending',value:2}),
    item('unknown','models',100),item('trend-high','models',10,{kind:'trending',value:50})];
  assert.deepEqual(ranking.rank(rows,'models','native').map(x=>x.id),['trend-high','trend-low','likes','unknown']);
});
test('GitHub uses daily gain, never lifetime stars, with missing metrics at the end',()=>{
  const rows=[item('total','repos',99,{kind:'stars',value:1000000}),item('small','repos',50,{kind:'stars_day',value:10}),
    item('large','repos',20,{kind:'stars_day',value:200}),item('zero','repos',10,{kind:'stars_day',value:0})];
  assert.deepEqual(ranking.rank(rows,'repos','native').map(x=>x.id),['large','small','zero','total']);
});
test('papers use upvotes and filter other categories; media falls back to editorial',()=>{
  const rows=[item('paper1','papers',80,{kind:'upvotes',value:4}),item('paper2','papers',40,{kind:'upvotes',value:20}),
    item('media1','media',30),item('media2','media',70)];
  assert.deepEqual(ranking.rank(rows,'papers','native').map(x=>x.id),['paper2','paper1']);
  assert.deepEqual(ranking.rank(rows,'media','native').map(x=>x.id),['media2','media1']);
  assert.equal(ranking.rank(rows,'models').length,0);
});
test('merged stories retain multiple categories; invalid or absent metrics stay missing',()=>{
  const merged={...item('merged','media',90),signal_categories:['media','models']};
  assert.equal(ranking.rank([merged],'models')[0],merged);
  for(const value of [null,NaN,Infinity,-1,'200']){
    assert.equal(ranking.metric(item('bad','models',10,{kind:'trending',value}),'models'),null);
  }
});
// Shared fixture with tests/test_digest.py: both implementations must select and order identically.
const H=3600,D=86400,NOW=100*D;
const parts=(engagement,coverage,freshness)=>[{id:'engagement',points:engagement,available:engagement!==null},{id:'coverage',points:coverage},{id:'freshness',points:freshness}];
const FIXTURE=[
  {id:'hot',score:40,published_at:NOW-3*H,news_at:NOW-3*H,components:parts(6,15,22)},
  {id:'quiet-new',score:70,published_at:NOW-2*H,news_at:NOW-2*H,components:parts(null,0,24)},
  {id:'afternoon',score:65,published_at:NOW-14*H,news_at:NOW-14*H,components:parts(9,7.5,17)},
  {id:'august-model',score:90,published_at:NOW-1*H,news_at:NOW-55*D,date_kind:'trend',components:parts(10,0,0)},
  {id:'big-last-week',score:60,published_at:NOW-5*D,news_at:NOW-5*D,components:parts(10,15,0.4)},
  {id:'small-yesterday',score:50,published_at:NOW-20*H,news_at:NOW-20*H,components:parts(0,0,14)}];
const ids=(period,order)=>ranking.rank(FIXTURE.filter(x=>ranking.inPeriod(x,period,NOW)),'all',order??ranking.PERIODS[period].order).map(x=>x.id);
test('tabs select by when a story became news and sort by their own measure',()=>{
  assert.deepEqual(ids('trending'),['hot','quiet-new']);
  assert.deepEqual(ids('day'),['quiet-new','afternoon','small-yesterday','hot']);
  assert.deepEqual(ids('week'),['big-last-week','afternoon','quiet-new','small-yesterday','hot']);
  assert.ok(ranking.inPeriod(FIXTURE[3],'year',NOW)&&!ranking.inPeriod(FIXTURE[3],'month',NOW));
});
test('the card headline is the measure the tab sorts by',()=>{
  assert.deepEqual(ranking.headline(FIXTURE[0],'trending'),{name:'Momentum',max:25,order:'momentum',value:21});
  assert.deepEqual(ranking.headline(FIXTURE[0],'day'),{name:'Score',max:100,order:'editorial',value:40});
  assert.deepEqual(ranking.headline(FIXTURE[4],'month'),{name:'Significance',max:75,order:'significance',value:59.6});
});
test('Trending Now: articles use momentum_proxy and one source cannot fill the top',()=>{
  const row=(id,source,points,extra={})=>({id,source_id:source,score:points,published_at:100,
    components:[{id:'engagement',points,available:true,...extra},{id:'coverage',points:0}]});
  const article=row('article','verge',0,{available:false,momentum_proxy:8});
  assert.equal(ranking.momentum(article),8);
  const rows=[0,1,2,3,4,5].map(i=>row('g'+i,'github',10-i*0.1)).concat([article,row('weak','decoder',1)]);
  const order=ranking.rank(rows,'all','momentum').map(x=>x.id);
  assert.deepEqual(order.slice(0,4),['g0','g1','g2','article']);
  assert.ok(order.indexOf('g3')<order.indexOf('weak'));
});

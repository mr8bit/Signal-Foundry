'use strict';
// The same functions run in the self-contained dashboard and offline Node checks.
(function(root){
  const metrics={models:['trending','likes'],repos:['stars_day'],papers:['upvotes']};
  const categories=['all','models','repos','papers','media','enterprise','cloud'];
  // Period tabs (mirrored in digest.py). Every tab selects by when a story became news (news_at: publication,
  // model/paper creation, a repository's first day in GitHub Trending), never by the last read of a trend list,
  // so an August model trending again today is not "today's" news. Each tab sorts and headlines one measure:
  //   trending  12 h   momentum     = platform popularity + cross-outlet coverage (max 25): what is discussed now
  //   day       24 h   editorial    = full editorial score with freshness (max 100): the day's main news
  //   week/month/year  significance = editorial score without freshness (max 75): what mattered in the period
  const PERIODS={trending:{hours:12,order:'momentum'},day:{hours:24,order:'editorial'},
                 week:{hours:168,order:'significance'},month:{hours:720,order:'significance'},year:{hours:8760,order:'significance'}};
  const MEASURES={momentum:{name:'Импульс',max:25},editorial:{name:'Балл',max:100},significance:{name:'Значимость',max:75}};
  function belongs(item,category){return category==='all'||(item.signal_categories||[item.column]).includes(category)}
  function metric(item,category){return item.signal_metrics?.[category]||null}
  function validMetric(item,category){const m=metric(item,category);return m&&metrics[category]?.includes(m.kind)&&typeof m.value==='number'&&Number.isFinite(m.value)&&m.value>=0?m:null}
  function component(item,id){return (item.components||[]).find(c=>c.id===id)}
  const round=value=>Math.round(value*10)/10;
  function momentum(item){
    const engagement=component(item,'engagement'),coverage=component(item,'coverage');
    return round((engagement?.available===false?0:engagement?.points||0)+(coverage?.points||0));
  }
  function significance(item){return round(item.score-(component(item,'freshness')?.points||0))}
  function measure(item,order){return order==='momentum'?momentum(item):order==='significance'?significance(item):item.score}
  function newsTime(item){return item.news_at??item.published_at}
  function inPeriod(item,period,now){const age=now-newsTime(item);return age>=0&&age<=(PERIODS[period]||PERIODS.day).hours*3600}
  function trending(item,now){return inPeriod(item,'trending',now)}
  function headline(item,period){const order=(PERIODS[period]||PERIODS.day).order;return {...MEASURES[order],order,value:measure(item,order)}}
  function editorial(a,b){return b.score-a.score||b.published_at-a.published_at||String(a.id).localeCompare(String(b.id))}
  function rank(rows,category='all',order='editorial'){
    if(!categories.includes(category))category='all';
    return rows.filter(item=>belongs(item,category)).sort((a,b)=>{
      if(order==='native'&&metrics[category]){
        const ma=validMetric(a,category),mb=validMetric(b,category);
        if(Boolean(ma)!==Boolean(mb))return ma?-1:1;
        if(ma&&mb){
          const bucket=metrics[category].indexOf(ma.kind)-metrics[category].indexOf(mb.kind);
          if(bucket)return bucket;
          if(ma.value!==mb.value)return mb.value-ma.value;
        }
      }
      if(order==='momentum'||order==='significance'){const d=measure(b,order)-measure(a,order);if(d)return d}
      return editorial(a,b);
    });
  }
  const api={categories,metrics,PERIODS,MEASURES,belongs,metric:validMetric,rank,momentum,significance,
    newsTime,inPeriod,trending,headline};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.SignalRanking=api;
})(globalThis);

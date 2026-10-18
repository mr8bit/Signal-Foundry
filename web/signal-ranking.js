'use strict';
// The same functions run in the self-contained dashboard and offline Node checks.
(function(root){
  const metrics={models:['trending','likes'],repos:['stars_day'],papers:['upvotes']};
  const categories=['all','models','repos','papers','media','enterprise','cloud'];
  // Period tabs (mirrored in digest.py). Every tab selects by when a story became news (news_at: publication,
  // model/paper creation, a repository's first day in GitHub Trending), never by the last read of a trend list,
  // so an August model trending again today is not "today's" news. Each tab sorts and headlines one measure:
  //   trending  12 h   momentum     = attention: platform, discussion and reach signals (max 25): what is discussed now
  //   day       24 h   editorial    = full editorial score with freshness (max 100): the day's main news
  //   week/month/year  significance = editorial score without freshness (max 75): what mattered in the period
  const PERIODS={trending:{hours:12,order:'momentum'},day:{hours:24,order:'editorial'},
                 week:{hours:168,order:'significance'},month:{hours:720,order:'significance'},year:{hours:8760,order:'significance'}};
  const MEASURES={momentum:{name:'Momentum',max:25},editorial:{name:'Score',max:100},significance:{name:'Significance',max:75}};
  function belongs(item,category){return category==='all'||(item.signal_categories||[item.column]).includes(category)}
  function metric(item,category){return item.signal_metrics?.[category]||null}
  function validMetric(item,category){const m=metric(item,category);return m&&metrics[category]?.includes(m.kind)&&typeof m.value==='number'&&Number.isFinite(m.value)&&m.value>=0?m:null}
  function component(item,id){return (item.components||[]).find(c=>c.id===id)}
  const round=value=>Math.round(value*10)/10;
  // Momentum is computed once, in signals.attention_parts: the story's attention (0–25), or its editorial-weight
  // proxy when it has no attention signal at all.
  function momentum(item){
    if(item.trend_momentum!==undefined)return item.trend_momentum;
    const attention=component(item,'attention');
    return round(attention?.momentum??attention?.points??0);
  }
  function significance(item){return round(item.score-(component(item,'freshness')?.points||0))}
  function measure(item,order){return order==='momentum'?momentum(item):order==='significance'?significance(item):item.score}
  function newsTime(item){return item.news_at??item.published_at}
  function inPeriod(item,period,now){const age=now-newsTime(item);return age>=0&&age<=(PERIODS[period]||PERIODS.day).hours*3600}
  function trending(item,now){return inPeriod(item,'trending',now)}
  // Stories of a period (mirrored in digest.py). Trending Now folds community threads into the card they discuss
  // (discussion_of, signals.fold_discussions): the card stays while any of its threads is fresh, at the highest
  // momentum of itself and those threads, which it lists as `threads`.
  function select(rows,period,now){
    if(period!=='trending')return rows.filter(item=>inPeriod(item,period,now));
    const ids=new Set(rows.map(item=>item.id));
    return rows.flatMap(item=>{
      if(item.discussion_of&&ids.has(item.discussion_of))return [];
      const threads=(item.discussions||[]).filter(d=>trending(d,now));
      if(!threads.length)return trending(item,now)?[item]:[];
      return [{...item,threads,trend_momentum:Math.max(momentum(item),...threads.map(d=>round(d.momentum)))}];
    });
  }
  function headline(item,period){const order=(PERIODS[period]||PERIODS.day).order;return {...MEASURES[order],order,value:measure(item,order)}}
  function editorial(a,b){return b.score-a.score||b.published_at-a.published_at||String(a.id).localeCompare(String(b.id))}
  function rank(rows,category='all',order='editorial'){
    if(!categories.includes(category))category='all';
    const sorted=rows.filter(item=>belongs(item,category)).sort((a,b)=>{
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
    return MEASURES[order]?diversify(sorted,order):sorted;
  }
  // Every period (as in digest.py): past SOURCE_CAP stories of one source, further ones compete for the first
  // SOURCE_WINDOW places with a fifth of the measure's scale taken off, so one source cannot fill the top
  // yet weak stories are not promoted. All subreddits are one source (platform). The platform-metric order is left as it is.
  const SOURCE_CAP=3,SOURCE_WINDOW=10,SOURCE_PENALTY=0.2;
  function diversify(rows,order='momentum'){
    const rest=rows.slice(),top=[],counts={},source=item=>item.platform||item.source_id||item.source_name;
    const penalty=SOURCE_PENALTY*MEASURES[order].max;
    while(rest.length&&top.length<SOURCE_WINDOW){
      let best=0,bestValue=-Infinity;
      rest.forEach((item,i)=>{
        const value=measure(item,order)-((counts[source(item)]||0)>=SOURCE_CAP?penalty:0);
        if(value>bestValue){best=i;bestValue=value}
      });
      const [item]=rest.splice(best,1);top.push(item);counts[source(item)]=(counts[source(item)]||0)+1;
    }
    return top.concat(rest);
  }
  const api={categories,metrics,PERIODS,MEASURES,belongs,metric:validMetric,rank,diversify,momentum,significance,
    newsTime,inPeriod,trending,select,headline};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.SignalRanking=api;
})(globalThis);

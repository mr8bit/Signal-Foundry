'use strict';
const DATA=JSON.parse(document.getElementById('dashboard-data').textContent);
const $=s=>document.querySelector(s);
const read=(key,fallback)=>{try{return JSON.parse(localStorage.getItem('signal:'+key))??fallback}catch{return fallback}};
const store=(key,value)=>{try{localStorage.setItem('signal:'+key,JSON.stringify(value))}catch{}};
const state={period:new URLSearchParams(location.search).get('sort')||read('period','trending'),topic:'all',signalCategory:new URLSearchParams(location.search).get('signals')||read('signalCategory','all'),signalOrder:read('signalOrder','editorial'),signalLimit:4,query:'',view:read('view','kanban'),saved:new Set(read('saved',[])),columns:{repos:'group:repos',models:'group:models',papers:'group:papers',community:'group:community',media:'group:media',labs:'group:labs'},shown:{repos:4,models:4,papers:4,community:8,media:8,labs:8}};
// Old links and saved settings with sort=now (the removed 48-hour tab) open Trending Now.
const HOURS=Object.fromEntries(Object.entries(SignalRanking.PERIODS).map(([key,value])=>[key,value.hours]));
const WINDOWS={trending:'12 hours',day:'24 hours',week:'7 days',month:'30 days',year:'365 days'};
const isTrending=()=>state.period==='trending';if(!HOURS[state.period])state.period='trending';
// Trending Now measures momentum (0–25); the other periods the editorial score (0–100).
const periodOrder=()=>SignalRanking.PERIODS[state.period].order;
const headline=p=>SignalRanking.headline(p,state.period);
if(!SignalRanking.categories.includes(state.signalCategory))state.signalCategory='all';
if(!['editorial','native'].includes(state.signalOrder))state.signalOrder='editorial';
const SIGNAL_LABELS={all:'Overall',models:'Models',repos:'GitHub',papers:'Research papers',media:'Publications',enterprise:'Enterprise',cloud:'Cloud'};
const GROUPS={repos:'GitHub Trending',models:'Hugging Face · Models',papers:'Hugging Face · Papers',community:'Community',media:'Media',labs:'Labs, cloud and authors'};
const e=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safe=url=>{try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?e(u.href):'#'}catch{return '#'}};
const now=()=>Date.now()/1000;
const age=stamp=>{const h=Math.max(0,(now()-stamp)/3600);return h<1?Math.max(1,Math.floor(h*60))+'m ago':h<24?Math.floor(h)+'h ago':Math.floor(h/24)+'d ago'};
const date=stamp=>new Date(stamp*1000).toLocaleString('en-US',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'});
const initial=name=>name.startsWith('r/')?'r/':name.split(/[\s·]+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),4500)}
function items(input=DATA.items){const q=state.query.toLocaleLowerCase();return input.filter(p=>{return SignalRanking.inPeriod(p,state.period,now())&&(state.topic==='all'||state.topic==='saved'&&state.saved.has(p.id)||['enterprise','cloud'].includes(state.topic)&&SignalRanking.belongs(p,state.topic)||String(p.priority)===state.topic)&&(!q||[p.title,p.summary,p.source_name,p.topic,...p.sources,...(p.also||[]).map(a=>a.source+' '+a.title)].join(' ').toLocaleLowerCase().includes(q))})}
function syncFilterUrl(){if(location.protocol==='file:')return;const url=new URL(location.href);url.searchParams.set('sort',state.period);url.searchParams.set('signals',state.signalCategory);history.replaceState(null,'',url)}
function sourceBadge(p){return `<span class="source-label"><span class="source-icon">${e(initial(p.source_name))}</span><span class="source-name">${e(p.source_name)}</span></span>`}
function saveButton(p){return `<button class="save-button ${state.saved.has(p.id)?'saved':''}" data-save="${p.id}" aria-label="${state.saved.has(p.id)?'Unsave story':'Save story'}" aria-pressed="${state.saved.has(p.id)}">${state.saved.has(p.id)?'★':'☆'}</button>`}
const compact=value=>new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1}).format(value);
const scoreBar=(value,max=100)=>`<span class="score-bar" aria-hidden="true"><i style="width:${Math.max(0,Math.min(100,100*value/max))}%"></i></span>`;
function stamp(p){return `<time datetime="${new Date(p.published_at*1000).toISOString()}" title="${date(p.published_at)}">${p.date_kind==='trend'?'trending · ':p.date_kind==='updated'?'updated · ':''}${age(p.published_at)}</time>`}
function cloudWhere(p){return p.cloud_availability?.length?`<div class="cloud-availability"><span>Available on:</span> ${p.cloud_availability.slice(0,6).map(a=>`<a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer" title="Announced: ${date(a.published_at)}">${e(a.label)}${a.stage==='preview'?' (preview)':''} ↗</a>`).join(' · ')}</div>`:''}
// Articles merged into this card (same news from other outlets), linked so nothing is hidden.
function alsoLine(p){return p.also?.length?`<p class="card-also"><span>Also covered by:</span> ${p.also.map(a=>`<a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer" title="${e(a.title)}">${e(a.source)}</a>`).join(', ')}</p>`:''}
function card(p){
  const meta=[`<span class="topic-label">${e(p.topic)}</span>`];
  if(SignalRanking.belongs(p,'enterprise'))meta.push('<span class="enterprise-label">Enterprise</span>');
  if(p.coverage>1)meta.push(`<span>${p.coverage} platforms</span>`);
  (p.metrics||[]).slice(0,2).forEach(m=>meta.push(`<span class="metric">${e(compact(m.value))} <span class="muted">${e(m.label)}</span></span>`));
  if(p.original_published_at)meta.push(`<span title="Original publication date">from ${new Date(p.original_published_at*1000).toLocaleDateString('en-US')}</span>`);
  if(p.raw_score!=null)meta.push(`<span title="Platform points">↑ ${e(p.raw_score)}</span>`);
  return `<article class="news-card" data-id="${p.id}"><div class="card-head">${sourceBadge(p)}${stamp(p)}</div><h3><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.title)}</a></h3>${p.summary?`<p class="card-summary">${e(p.summary)}</p>`:'<p class="card-summary no-summary">No feed description. Open the original source for details.</p>'}${alsoLine(p)}${cloudWhere(p)}<p class="card-meta">${meta.join('')}</p><div class="card-footer"><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.domain.replace(/^www\./,''))} ↗</a><div class="card-actions"><button class="score-link ${headline(p).order}" data-detail="${p.id}" aria-label="${headline(p).name}: ${headline(p).value} of ${headline(p).max}, score breakdown" title="${headline(p).name} of ${headline(p).max}">${headline(p).value}${scoreBar(headline(p).value,headline(p).max)}</button>${saveButton(p)}</div></div></article>`}
function signalCard(p,i){
  const metric=SignalRanking.metric(p,state.signalCategory);
  const h=headline(p);
  // "Link to a model or repository" says nothing on a card that is the model or repository itself.
  const own=['repos','models','papers'].includes(p.column);
  const reasons=(h.order!=='editorial'?[`<li class="date">Editorial score ${p.score}/100</li>`]:[]).concat(p.original_published_at?[`<li class="date">${p.category==='paper'?'Paper published':'Created'} ${new Date(p.original_published_at*1000).toLocaleDateString('en-US')}</li>`]:[]).concat(p.reasons.filter(x=>!(own&&x==='Model or repository link')).map(x=>`<li>${e(x)}</li>`)).slice(0,3);
  return `<article class="signal-card"><header class="signal-head"><span class="signal-rank">${String(i+1).padStart(2,'0')}</span><span class="signal-meta"><span class="signal-source">${e(p.source_name)}</span>${stamp(p)}</span></header><h3><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.title)}</a></h3><p class="signal-summary">${e(p.summary||'No feed description. Open the original source for details.')}</p>${alsoLine(p)}${metric?`<div class="signal-metric">${e(new Intl.NumberFormat('en-US',{maximumFractionDigits:2}).format(metric.value))} ${e(metric.label)}<small>Observed ${date(metric.observed_at)}</small></div>`:''}${cloudWhere(p)}<ul class="signal-reasons">${reasons.join('')}</ul><footer class="signal-foot"><button class="signal-score ${h.order}" data-detail="${p.id}" data-signal-kind="${state.signalCategory}" aria-label="${h.name} ${h.value} of ${h.max}, why it ranks"><span class="score-value">${h.value}<small>/${h.max}</small></span>${scoreBar(h.value,h.max)}<span class="score-label">${h.order==='editorial'?'Why it ranks':h.name} ↗</span></button></footer></article>`}
function signalRows(category,rows){return category==='all'?rows:items(DATA.signal_rankings?.[category]||rows)}
function nativeAllowed(){return Boolean(SignalRanking.metrics[state.signalCategory])}
// Trending Now is always ordered by momentum; other tabs by their measure or the chosen platform metric.
function effectiveOrder(){return !isTrending()&&nativeAllowed()&&state.signalOrder==='native'?'native':periodOrder()}
// Trending Now is always ordered by momentum; elsewhere by the editorial score or the chosen platform metric.
function topSignals(rows){
  const order=effectiveOrder();
  return SignalRanking.rank(signalRows(state.signalCategory,rows),state.signalCategory,order);
}
function renderSignals(rows){
  const ranked=topSignals(rows);
  const order=effectiveOrder();
  const select=$('#signal-order');select.value=order==='native'?'native':'editorial';select.disabled=isTrending();$('.signal-order').hidden=isTrending();select.querySelector('[value="native"]').disabled=!nativeAllowed();
  select.querySelector('[value="editorial"]').textContent={momentum:'Momentum',editorial:'Editorial score',significance:'Significance'}[periodOrder()];
  document.querySelectorAll('[data-signal-category]').forEach(button=>{
    const category=button.dataset.signalCategory,count=signalRows(category,rows).filter(p=>SignalRanking.belongs(p,category)).length;
    button.classList.toggle('active',category===state.signalCategory);button.setAttribute('aria-pressed',category===state.signalCategory);
    button.innerHTML=e(SIGNAL_LABELS[category])+` <span class="signal-category-count">${count}</span>`;
    button.classList.toggle('is-empty',count===0&&category!==state.signalCategory);
  });
  $('#signals').innerHTML=ranked.slice(0,state.signalLimit).map(signalCard).join('');
  $('#signals-empty').hidden=ranked.length>0;
  $('#signals-more').hidden=ranked.length<=state.signalLimit;
  $('#signals-more').textContent=`Show more · ${Math.max(0,ranked.length-state.signalLimit)}`;
  const orderName={native:'platform metric',momentum:'momentum',editorial:'editorial score',significance:'significance'}[order];
  $('#signals-caption').textContent=`${ranked.length} ${plural(ranked.length,'signal','signals','signals')} over ${WINDOWS[state.period]} · order: ${orderName}`;
  const notes={
    cloud:'Cloud providers own models, agent platforms, price cuts of at least 20% and new AI hardware. Third-party model availability is attached to the model story under Available on.',
    enterprise:'Enterprise products, adoption, private deployment, security and governance. Product and deployment cues must appear in the title or first 1,200 characters. A generic enterprise mention in the body needs two separate occurrences. Adoption stories require an outcome in the title of a lab or publication post; Introducing announcements do not qualify. Relevance: 30 for a headline cue, 24 for adoption reported by a publication, 18 for a vendor customer story or body-only evidence, 12 for further vendor customer stories after a full-weight story within seven days. These stories remain visible.',
    all:'Editorial ranking combines relevance, freshness, evidence, cross-platform coverage and available popularity.',
    models:'Models from Hugging Face and Replicate Hype. Platform sorting puts HF trendingScore first, then likes for models without trendingScore. These groups stay separate. Replicate runs do not count as likes.',
    repos:'GitHub repositories. Platform sorting uses daily stars from Trending. Selecting a longer period does not turn daily stars into weekly or yearly growth.',
    papers:'Research from Hugging Face Papers. Platform sorting uses cumulative upvotes, not growth within the selected period.',
    media:'Publications ranked by editorial score. Feeds have no comparable view or like counts; popularity is never invented.'
  };
  const periodNotes={
    trending:'Trending Now covers the last 12 hours, ordered by momentum: platform popularity plus cross-platform coverage, up to 25. ',
    day:'Day covers the last 24 hours, ordered by editorial score including freshness, up to 100. ',
    week:'Week covers the last seven days, ordered by significance: editorial score without freshness, up to 75. ',
    month:'Month covers the last 30 days, ordered by significance without freshness, up to 75. ',
    year:'Year covers the last 365 days, ordered by significance without freshness, up to 75. '};
  const timing='Periods use the story date: publication, model or paper creation, first appearance in GitHub Trending, or a model update reported by Hype. Fetching an old model again does not make it new. ';
  const shortNotes={trending:'New in the last 12 hours, ranked by momentum.',day:'The leading stories of the day by editorial score.',
    week:'The most significant stories this week, without freshness weighting.',month:'The most significant stories this month, without freshness weighting.',year:'The most significant stories this year, without freshness weighting.'};
  $('#signal-ranking-summary').textContent=shortNotes[state.period];
  $('#signal-ranking-detail').textContent=periodNotes[state.period]+timing+(state.signalCategory==='all'?'':notes[state.signalCategory])+(order==='native'?' Stories without a platform metric follow, ordered by editorial score.':'');
}
const plural=(n,one,few,many)=>n===1?one:many;
const kanbanStacks=()=>innerWidth>1100?3:innerWidth>760?2:1;
// Columns go into the currently shortest stack (by card count), so a long column never leaves a hole under it.
function kanban(rows){
  // Empty columns stay (their header holds the source picker) but are compact and placed after filled ones.
  const parts=Object.keys(GROUPS).map(key=>column(key,rows)),filled=parts.filter(part=>!part.includes('column is-empty')),empty=parts.filter(part=>part.includes('column is-empty'));
  const stacks=Array.from({length:kanbanStacks()},()=>({weight:0,html:''}));
  filled.concat(empty).forEach(part=>{const stack=stacks.reduce((a,b)=>b.weight<a.weight?b:a);stack.html+=part;stack.weight+=1+(part.match(/<article/g)||[]).length});
  return stacks.map(stack=>`<div class="board-stack">${stack.html}</div>`).join('');
}
let resizeTimer,renderedStacks=kanbanStacks();
addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state.view==='kanban'&&kanbanStacks()!==renderedStacks){renderedStacks=kanbanStacks();render()}},150)});
function column(key,rows){const choices=DATA.sources.filter(s=>{const cat=s.category,cats=s.categories||[cat];const native={repos:'repository',models:'model',papers:'paper'};if(native[key])return cats.includes(native[key]);return key==='community'?(cats.includes('community')||s.id==='hn'):key==='labs'?['lab','newsletter','cloud'].includes(cat):cats.includes('news')||cats.includes('aggregator')&&s.id!=='hn'});const selected=state.columns[key];const filtered=rows.filter(p=>p.column===key&&(selected.startsWith('group:')||p.source_id===selected));// Day shows trend lists in the provider's order; other tabs keep the rows ordered by their measure.
  if(['repos','models','papers'].includes(key)&&state.period==='day')filtered.sort((a,b)=>(a.provider_rank??9999)-(b.provider_rank??9999));const options=`<option value="group:${key}">${GROUPS[key]}</option>`+choices.map(s=>`<option value="${e(s.id)}" ${selected===s.id?'selected':''}>${e(s.name)}</option>`).join('');return `<section class="column${filtered.length?'':' is-empty'}" data-column="${key}" aria-label="${GROUPS[key]}"><div class="column-header"><div class="column-name"><span class="column-dot"></span><select data-column-select="${key}" aria-label="Source: ${GROUPS[key]}">${options}</select></div><span class="column-count">${filtered.length}</span></div><div class="column-body">${filtered.slice(0,state.shown[key]).map(card).join('')||'<div class="column-empty compact">No stories in this period. Try another source or period.</div>'}</div>${filtered.length>state.shown[key]?`<button class="more-button" data-more="${key}">Show more · ${filtered.length-state.shown[key]}</button>`:''}</section>`}
function render(){const rows=SignalRanking.rank(items(),'all',periodOrder());document.querySelectorAll('[data-period]').forEach(b=>{b.classList.toggle('active',b.dataset.period===state.period);b.setAttribute('aria-pressed',b.dataset.period===state.period)});document.querySelectorAll('[data-topic]').forEach(b=>{b.classList.toggle('active',b.dataset.topic===state.topic);b.setAttribute('aria-pressed',b.dataset.topic===state.topic)});$('#story-count').textContent=rows.length;renderSignals(rows);$('#feed').className=state.view==='list'?'list-mode':'board';$('#feed').style.setProperty('--stacks',kanbanStacks());$('#feed').innerHTML=state.view==='list'?rows.slice(0,state.shown.list||30).map(card).join('')+(rows.length>(state.shown.list||30)?'<button class="more-button" data-more="list">Show more</button>':''):kanban(rows);$('#kanban-view').classList.toggle('active',state.view==='kanban');$('#list-view').classList.toggle('active',state.view==='list');$('#empty').hidden=rows.length>0;$('#feed').hidden=rows.length===0;$('#rss-link').href=location.protocol==='file:'?'data/feed.xml':'/api/rss?sort='+(isTrending()?'day':state.period);}
function openModal(html){$('#detail-content').innerHTML=html;$('#detail').showModal()}
function breakdown(id,category){const p=(DATA.signal_rankings?.[category]||DATA.items).find(p=>p.id===id);if(!p)return;openModal(`<span class="modal-kicker">Signal ${p.score} / 100 · ${e(p.topic)}</span><h2 class="modal-title">${e(p.title)}</h2><p class="modal-description">${e(p.summary||'No feed description.')}</p>${cloudWhere(p)}<a class="modal-link" href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">Open original source ↗</a>${p.discussion_url?`<a class="modal-link" href="${safe(p.discussion_url)}" target="_blank" rel="noopener noreferrer">Discussion ↗</a>`:''}<div>${p.components.map(c=>`<div class="breakdown-row"><div class="breakdown-label"><span>${e(c.label)}</span><span>${c.available?`${c.points} / ${c.max}`:'no data'}</span></div>${c.explanation?`<div class="metric-explanation">${e(c.explanation)}</div>`:''}<div class="breakdown-bar"><div class="breakdown-fill" style="width:${100*c.points/c.max}%"></div></div></div>`).join('')}</div>${p.metrics?.length?`<p class="coverage-list">${p.metrics.map(m=>`${e(new Intl.NumberFormat('en-US').format(m.value))} ${e(m.label)}`).join(' · ')}</p>`:''}${p.date_kind==='trend'?`<p class="coverage-list">First seen trending: ${date(p.published_at)}${p.original_published_at?' · Published: '+date(p.original_published_at):''}</p>`:''}<p class="coverage-list">Platforms: ${p.sources.map(e).join(' · ')}</p>${p.also?.length?`<div class="coverage-list">Merged coverage of the same story:<ul class="also-list">${p.also.map(a=>`<li><a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer">${e(a.title)}</a> <span class="muted">— ${e(a.source)}</span></li>`).join('')}</ul></div>`:''}${p.enterprise_reasons?.length?`<p class="coverage-list">Enterprise: ${p.enterprise_reasons.map(e).join(' · ')}</p>`:''}<p class="modal-note">${e(p.description_kind)}. The editorial score helps select stories. It does not verify claims or predict popularity. Missing metrics are never invented.</p>`)}
function methodology(){openModal(`<span class="modal-kicker">TOP SIGNALS · ${e(DATA.scoring_version)}</span><h2 class="modal-title">Why does this story rank higher?</h2>
<p class="modal-description">Scores help prioritize stories for an AI channel. Choose models, GitHub, research, publications, cloud or Enterprise. Each category shows the highest-ranked entries without a one-per-source limit. Show more reveals the rest.</p>
<p class="modal-description">Platform sorting uses daily stars for GitHub, HF trendingScore followed by likes when unavailable, and upvotes for papers. Lifetime stars, downloads and different units are never added together. The selected period filters stories; each metric keeps its stated measurement window. Publications use editorial ranking because RSS has no comparable public counters.</p>
<div class="status-row"><span>Periods</span><b>window · order</b></div><p class="modal-description">Trending Now: 12 hours, momentum up to 25 (platform popularity + coverage). Day: 24 hours, editorial score up to 100. Week, Month and Year: 7, 30 and 365 days, significance up to 75 (editorial score without freshness). The headline number is the measure used for sorting; the full editorial score is always in the breakdown. Periods use the story date, not the most recent fetch. Hype model dates represent updates.</p>
<div class="status-row"><span>Relevance</span><b>up to 30</b></div><p class="modal-description">Measured weight releases, release headlines naming a tracked model family, or that family in model trends: 30. Other trending models and measured local inference: 24. Tools: 18. Research: 12. Industry: 6. Enterprise uses its own evidence rules, explained under that category.</p>
<div class="status-row"><span>Freshness</span><b>up to 25</b></div><p class="modal-description">25 × 2<sup>−age / half-life</sup>. News uses publication time (24-hour half-life), papers their publication date (72 hours), models their creation date (168 hours), and GitHub Trending its first archived appearance (24 hours). Hype models use the supplied update date. Returning to a trend list does not reset an old story.</p>
<div class="status-row"><span>Evidence</span><b>up to 15</b></div><p class="modal-description">Measurements: 7; model or repository link: 5; paper, research or benchmark cues: 3.</p>
<div class="status-row"><span>Cross-platform coverage</span><b>up to 15</b></div><p class="modal-description">7.5 points per additional independent platform, capped at 15. Shared links, exact titles and related stories within 72 hours count. Multiple subreddits count as one platform, as do Google AI and DeepMind. Aggregated copies do not create independent coverage or verify claims.</p>
<div class="status-row"><span>Official source</span><b>up to 5</b></div><div class="status-row"><span>Popularity</span><b>up to 10</b></div><p class="modal-description">HN and Reddit: 2.5 × log₁₀(points + 1), capped at 10. Other supported metrics: min(10, 10 × ln(value + 1) / ln(target + 1)). Targets: 1,000 daily stars, 1,000 HF trendingScore or likes, 300 paper upvotes. Units are normalized separately; the strongest available component is used. These are editorial saturation targets, not percentiles. Downloads and Replicate runs are displayed but do not add popularity points. Missing data is never invented.</p>
<p class="modal-note">Descriptions are source excerpts, repository descriptions, model metadata or original paper abstracts, not generated summaries. Daily GitHub growth comes directly from Trending. Historical coverage depends on how long the collector has been running.</p>`)}
function sourcesModal(){openModal(`<span class="modal-kicker">Source status</span><h2 class="modal-title">What is being collected</h2><p class="modal-description">Last checked: ${date(DATA.collected_at||DATA.generated_at)}. Each source has its own schedule. Manual refresh respects cooldowns. A source failure does not stop the others.</p>${DATA.statuses.map(s=>`<div class="status-row"><span>${e(s.name)}${Math.max(s.retry_at||0,s.next_fetch_at||0)>now()?`<small class="source-retry">Next check no earlier than ${date(Math.max(s.retry_at||0,s.next_fetch_at||0))}</small>`:''}</span><span class="${s.status==='ok'?'status-ok':'status-error'}">${s.status==='ok'?s.count+' entries':s.status==='scheduled'?'Awaiting first collection':s.status==='stale'?'Cached data · '+e(s.error||'source unavailable'):e(s.error?.includes('rate limit')?'Rate limited (429)':s.error||'Unavailable')}</span></div>`).join('')}<p class="modal-note">Counts refer to source entries before topic and age filters.</p>`)}
document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b)return;if(b.dataset.period){state.period=b.dataset.period;syncFilterUrl();state.signalLimit=4;store('period',state.period);state.shown={repos:4,models:4,papers:4,community:8,media:8,labs:8};render()}if(b.dataset.topic){state.topic=b.dataset.topic;state.signalLimit=4;render()}if(b.dataset.signalCategory){state.signalCategory=b.dataset.signalCategory;syncFilterUrl();state.signalLimit=4;store('signalCategory',state.signalCategory);render()}if(b.dataset.save){const id=b.dataset.save;state.saved.has(id)?state.saved.delete(id):state.saved.add(id);store('saved',[...state.saved]);render()}if(b.dataset.detail)breakdown(b.dataset.detail,b.dataset.signalKind);if(b.dataset.more){state.shown[b.dataset.more]=(state.shown[b.dataset.more]||(b.dataset.more==='list'?30:8))+15;render()}});
$('#feed').addEventListener('change',event=>{if(event.target.dataset.columnSelect){const key=event.target.dataset.columnSelect;state.columns[key]=event.target.value;state.shown[key]=8;render()}});
$('#search').addEventListener('input',event=>{state.query=event.target.value;state.signalLimit=4;render()});
$('#signal-order').onchange=event=>{state.signalOrder=event.target.value;state.signalLimit=4;store('signalOrder',state.signalOrder);render()};
$('#signals-more').onclick=()=>{state.signalLimit+=4;render()};
$('#kanban-view').onclick=()=>{state.view='kanban';store('view',state.view);render()};$('#list-view').onclick=()=>{state.view='list';store('view',state.view);render()};
$('#reset').onclick=()=>{state.query='';state.topic='all';state.signalCategory='all';syncFilterUrl();state.signalLimit=4;store('signalCategory','all');state.columns={repos:'group:repos',models:'group:models',papers:'group:papers',community:'group:community',media:'group:media',labs:'group:labs'};$('#search').value='';render()};
$('#close-dialog').onclick=()=>$('#detail').close();$('#detail').addEventListener('click',event=>{if(event.target===$('#detail')){const rect=$('#detail').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('#detail').close()}});
$('#methodology').onclick=methodology;$('#source-status').onclick=sourcesModal;
document.documentElement.dataset.theme=read('theme','dark');$('#theme-toggle').onclick=()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;store('theme',theme)};
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)&&!$('#detail').open){event.preventDefault();$('#search').focus()}});
async function managementKey(){
  return new Promise(resolve=>{
    let value=null;
    openModal('<span class="modal-kicker">Feed management</span><h2 class="modal-title">Refresh sources</h2><p class="modal-description">Enter the management key to request collection. It is stored only in this browser tab.</p><form id="management-form"><label class="management-label">Management key<input id="management-key" type="password" autocomplete="off" required></label><button class="refresh-button" type="submit">Continue</button></form>');
    const dialog=$('#detail');
    dialog.addEventListener('close',()=>resolve(value),{once:true});
    $('#management-form').onsubmit=event=>{event.preventDefault();value=$('#management-key').value.trim();if(value){sessionStorage.setItem('signal-management',value);dialog.close()}};
    $('#management-key').focus();
  });
}
$('#refresh').onclick=async()=>{
  if(location.protocol==='file:'){toast('Open the dashboard through the server to refresh sources');return}
  const button=$('#refresh');button.disabled=true;
  try{
    const send=()=>fetch('/api/refresh',{method:'POST',headers:sessionStorage.getItem('signal-management')?{'Authorization':'Bearer '+sessionStorage.getItem('signal-management')}: {}});
    let response=await send();
    if(response.status===401){const key=await managementKey();if(!key){button.disabled=false;return}response=await send()}
    if(!response.ok){if(response.status===401)sessionStorage.removeItem('signal-management');throw Error(response.status===401?'Invalid management key':'Could not start refresh')}
    const request=await response.json();
    if(request.status==='cached'){toast('Sources were checked recently. Another refresh is not needed yet.');button.disabled=false;return}
    toast('Refresh queued. The feed will update when collection finishes.');
    for(let i=0;i<300;i++){
      await new Promise(resolve=>setTimeout(resolve,2000));
      const result=await fetch('/api/status').then(r=>r.json());
      if(!result.refreshing){if(result.error)throw Error(result.error);location.reload();return}
    }
    throw Error('Refresh is still running. Check the page again shortly.');
  }catch(error){toast(error.message);button.disabled=false}
};

const healthy=DATA.statuses.filter(s=>s.status==='ok').length;$('#health-label').textContent=`${healthy} of ${DATA.statuses.length} sources available`;$('#source-status').classList.toggle('healthy',healthy===DATA.statuses.length);$('#updated-at').textContent='Updated '+age(DATA.collected_at||DATA.generated_at);$('#archive-info').textContent=`${DATA.items.length} archived stories · ranking ${DATA.scoring_version}`;render();requestAnimationFrame(()=>requestAnimationFrame(()=>document.documentElement.classList.add('ready')));

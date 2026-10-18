'use strict';
const DATA=JSON.parse(document.getElementById('dashboard-data').textContent);
const $=s=>document.querySelector(s);
const read=(key,fallback)=>{try{return JSON.parse(localStorage.getItem('signal:'+key))??fallback}catch{return fallback}};
const store=(key,value)=>{try{localStorage.setItem('signal:'+key,JSON.stringify(value))}catch{}};
const state={period:new URLSearchParams(location.search).get('sort')||read('period','trending'),topic:'all',signalCategory:new URLSearchParams(location.search).get('signals')||read('signalCategory','all'),signalOrder:read('signalOrder','editorial'),signalLimit:4,query:'',view:read('view','kanban'),saved:new Set(read('saved',[])),columns:{repos:'group:repos',models:'group:models',papers:'group:papers',community:'group:community',media:'group:media',labs:'group:labs'},shown:{repos:4,models:4,papers:4,community:8,media:8,labs:8}};
// Old links and saved settings with sort=now (the removed 48-hour tab) open Trending Now.
const HOURS=Object.fromEntries(Object.entries(SignalRanking.PERIODS).map(([key,value])=>[key,value.hours]));
const WINDOWS={trending:'12 часов',day:'24 часа',week:'7 дней',month:'30 дней',year:'365 дней'};
const isTrending=()=>state.period==='trending';if(!HOURS[state.period])state.period='trending';
// Trending Now measures momentum (0–25); the other periods the editorial score (0–100).
const periodOrder=()=>SignalRanking.PERIODS[state.period].order;
const headline=p=>SignalRanking.headline(p,state.period);
if(!SignalRanking.categories.includes(state.signalCategory))state.signalCategory='all';
if(!['editorial','native'].includes(state.signalOrder))state.signalOrder='editorial';
const SIGNAL_LABELS={all:'Общий рейтинг',models:'Модели',repos:'GitHub',papers:'Научные статьи',media:'Издания',enterprise:'Enterprise',cloud:'Облака'};
const GROUPS={repos:'GitHub Trending',models:'Hugging Face · Models',papers:'Hugging Face · Papers',community:'Сообщество',media:'Медиа',labs:'Лаборатории, облака и авторы'};
const e=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safe=url=>{try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?e(u.href):'#'}catch{return '#'}};
const now=()=>Date.now()/1000;
const age=stamp=>{const h=Math.max(0,(now()-stamp)/3600);return h<1?Math.max(1,Math.floor(h*60))+' мин назад':h<24?Math.floor(h)+' ч назад':Math.floor(h/24)+' д назад'};
const date=stamp=>new Date(stamp*1000).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'});
const initial=name=>name.startsWith('r/')?'r/':name.split(/[\s·]+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),4500)}
function items(input=DATA.items){const q=state.query.toLocaleLowerCase();return input.filter(p=>{return SignalRanking.inPeriod(p,state.period,now())&&(state.topic==='all'||state.topic==='saved'&&state.saved.has(p.id)||['enterprise','cloud'].includes(state.topic)&&SignalRanking.belongs(p,state.topic)||String(p.priority)===state.topic)&&(!q||[p.title,p.summary,p.source_name,p.topic,...p.sources,...(p.also||[]).map(a=>a.source+' '+a.title)].join(' ').toLocaleLowerCase().includes(q))})}
function syncFilterUrl(){if(location.protocol==='file:')return;const url=new URL(location.href);url.searchParams.set('sort',state.period);url.searchParams.set('signals',state.signalCategory);history.replaceState(null,'',url)}
function sourceBadge(p){return `<span class="source-label"><span class="source-icon">${e(initial(p.source_name))}</span><span class="source-name">${e(p.source_name)}</span></span>`}
function saveButton(p){return `<button class="save-button ${state.saved.has(p.id)?'saved':''}" data-save="${p.id}" aria-label="${state.saved.has(p.id)?'Убрать из сохранённого':'Сохранить новость'}" aria-pressed="${state.saved.has(p.id)}">${state.saved.has(p.id)?'★':'☆'}</button>`}
const compact=value=>new Intl.NumberFormat('ru-RU',{notation:'compact',maximumFractionDigits:1}).format(value);
const scoreBar=(value,max=100)=>`<span class="score-bar" aria-hidden="true"><i style="width:${Math.max(0,Math.min(100,100*value/max))}%"></i></span>`;
function stamp(p){return `<time datetime="${new Date(p.published_at*1000).toISOString()}" title="${date(p.published_at)}">${p.date_kind==='trend'?'в трендах · ':''}${age(p.published_at)}</time>`}
function cloudWhere(p){return p.cloud_availability?.length?`<div class="cloud-availability"><span>Где взять:</span> ${p.cloud_availability.slice(0,6).map(a=>`<a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer" title="Анонс: ${date(a.published_at)}">${e(a.label)}${a.stage==='preview'?' (preview)':''} ↗</a>`).join(' · ')}</div>`:''}
// Articles merged into this card (same news from other outlets), linked so nothing is hidden.
function alsoLine(p){return p.also?.length?`<p class="card-also"><span>Также пишут:</span> ${p.also.map(a=>`<a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer" title="${e(a.title)}">${e(a.source)}</a>`).join(', ')}</p>`:''}
function card(p){
  const meta=[`<span class="topic-label">${e(p.topic)}</span>`];
  if(SignalRanking.belongs(p,'enterprise'))meta.push('<span class="enterprise-label">Enterprise</span>');
  if(p.coverage>1)meta.push(`<span>${p.coverage} площадки</span>`);
  (p.metrics||[]).slice(0,2).forEach(m=>meta.push(`<span class="metric">${e(compact(m.value))} <span class="muted">${e(m.label)}</span></span>`));
  if(p.original_published_at)meta.push(`<span title="Исходная дата публикации">от ${new Date(p.original_published_at*1000).toLocaleDateString('ru-RU')}</span>`);
  if(p.raw_score!=null)meta.push(`<span title="Очки на платформе">↑ ${e(p.raw_score)}</span>`);
  return `<article class="news-card" data-id="${p.id}"><div class="card-head">${sourceBadge(p)}${stamp(p)}</div><h3><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.title)}</a></h3>${p.summary?`<p class="card-summary">${e(p.summary)}</p>`:'<p class="card-summary no-summary">В RSS нет описания. Подробности — в первоисточнике.</p>'}${alsoLine(p)}${cloudWhere(p)}<p class="card-meta">${meta.join('')}</p><div class="card-footer"><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.domain.replace(/^www\./,''))} ↗</a><div class="card-actions"><button class="score-link ${headline(p).order}" data-detail="${p.id}" aria-label="${headline(p).name}: ${headline(p).value} из ${headline(p).max}, разбор балла" title="${headline(p).name} из ${headline(p).max}">${headline(p).value}${scoreBar(headline(p).value,headline(p).max)}</button>${saveButton(p)}</div></div></article>`}
function signalCard(p,i){
  const metric=SignalRanking.metric(p,state.signalCategory);
  const h=headline(p);
  // "Link to a model or repository" says nothing on a card that is the model or repository itself.
  const own=['repos','models','papers'].includes(p.column);
  const reasons=(h.order!=='editorial'?[`<li class="date">Ред. балл ${p.score}/100</li>`]:[]).concat(p.original_published_at?[`<li class="date">${p.category==='paper'?'Статья от':'Создано'} ${new Date(p.original_published_at*1000).toLocaleDateString('ru-RU')}</li>`]:[]).concat(p.reasons.filter(x=>!(own&&x==='Ссылка на модель или репозиторий')).map(x=>`<li>${e(x)}</li>`)).slice(0,3);
  return `<article class="signal-card"><header class="signal-head"><span class="signal-rank">${String(i+1).padStart(2,'0')}</span><span class="signal-meta"><span class="signal-source">${e(p.source_name)}</span>${stamp(p)}</span></header><h3><a href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">${e(p.title)}</a></h3><p class="signal-summary">${e(p.summary||'В ленте нет описания. Откройте первоисточник, чтобы узнать подробности.')}</p>${alsoLine(p)}${metric?`<div class="signal-metric">${e(new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(metric.value))} ${e(metric.label)}<small>Снимок ${date(metric.observed_at)}</small></div>`:''}${cloudWhere(p)}<ul class="signal-reasons">${reasons.join('')}</ul><footer class="signal-foot"><button class="signal-score ${h.order}" data-detail="${p.id}" data-signal-kind="${state.signalCategory}" aria-label="${h.name} ${h.value} из ${h.max}, почему в топе"><span class="score-value">${h.value}<small>/${h.max}</small></span>${scoreBar(h.value,h.max)}<span class="score-label">${h.order==='editorial'?'Почему в топе':h.name} ↗</span></button></footer></article>`}
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
  select.querySelector('[value="editorial"]').textContent={momentum:'Импульс',editorial:'Редакционный балл',significance:'Значимость'}[periodOrder()];
  document.querySelectorAll('[data-signal-category]').forEach(button=>{
    const category=button.dataset.signalCategory,count=signalRows(category,rows).filter(p=>SignalRanking.belongs(p,category)).length;
    button.classList.toggle('active',category===state.signalCategory);button.setAttribute('aria-pressed',category===state.signalCategory);
    button.innerHTML=e(SIGNAL_LABELS[category])+` <span class="signal-category-count">${count}</span>`;
    button.classList.toggle('is-empty',count===0&&category!==state.signalCategory);
  });
  $('#signals').innerHTML=ranked.slice(0,state.signalLimit).map(signalCard).join('');
  $('#signals-empty').hidden=ranked.length>0;
  $('#signals-more').hidden=ranked.length<=state.signalLimit;
  $('#signals-more').textContent=`Показать ещё · ${Math.max(0,ranked.length-state.signalLimit)}`;
  const orderName={native:'метрика источника',momentum:'импульс',editorial:'редакционный балл',significance:'значимость'}[order];
  $('#signals-caption').textContent=`${ranked.length} ${plural(ranked.length,'сигнал','сигнала','сигналов')} за ${WINDOWS[state.period]} · порядок: ${orderName}`;
  const notes={
    cloud:'Собственные модели облаков, агентные платформы, снижение цен от 20% и новое AI-железо. Доступность чужих моделей прикрепляется к релизу строкой «Где взять».',
    enterprise:'Корпоративные AI-продукты, внедрения, частное развёртывание, безопасность и управление. Отбор по явным признакам: корпоративные продукты, контроль доступа и частное развёртывание — в заголовке или первых 1200 символах; слово «enterprise» в тексте — только при двух разных упоминаниях; истории внедрения — по заголовкам лабораторий и изданий с результатом («X cuts… with OpenAI»), анонсы «Introducing…» не считаются. Тема в балле: 30 — признак в заголовке; 24 — внедрение, о котором пишет издание; 18 — кейс клиента в блоге вендора или признак только в тексте; 12 — следующие кейсы того же блога, если за 7 дней до них уже был кейс с полным весом. Записи не скрываются, только опускаются ниже.',
    all:'Новости по редакционному баллу: тема, свежесть, конкретика, охват площадок и доступная популярность.',
    models:'Модели Hugging Face. По метрике: сначала trendingScore, затем модели без него по лайкам. Эти две группы не смешиваются.',
    repos:'Репозитории GitHub. По метрике: звёзды за сутки со страницы Trending. Период выше отбирает записи, но не превращает суточную метрику в недельную или годовую.',
    papers:'Научные статьи Hugging Face Papers. По метрике: накопленные голоса, а не прирост за выбранный период.',
    media:'Материалы новостных изданий по редакционному баллу. RSS не содержит сопоставимых просмотров или лайков; популярность не выдумывается.'
  };
  const periodNotes={
    trending:'Trending Now — что появилось за последние 12 часов и сильнее всего обсуждается. Порядок и число на карточке — импульс: популярность на платформе (звёзды за сутки, trendingScore, голоса, очки HN) плюс охват площадок, до 25. ',
    day:'Сутки — главное за 24 часа по редакционному баллу (тема, свежесть, конкретика, охват, первоисточник, популярность), до 100. ',
    week:'Неделя — самое значимое за 7 дней. Значимость — редакционный балл без свежести, до 75: вчерашняя новость не обгоняет важную недельной давности только потому, что новее. ',
    month:'Месяц — самое значимое за 30 дней по значимости (балл без свежести, до 75). ',
    year:'Год — самое значимое за 365 дней по значимости (балл без свежести, до 75). '};
  const timing='Период считается от момента, когда запись стала новостью: публикация, создание модели или статьи, первый день репозитория в GitHub Trending. Старая работа, снова попавшая в тренды, в короткие периоды не попадает. ';
  const shortNotes={trending:'Появилось за 12 часов и обсуждается сильнее всего.',day:'Главное за сутки по редакционному баллу.',
    week:'Самое значимое за неделю, без поправки на свежесть.',month:'Самое значимое за месяц, без поправки на свежесть.',year:'Самое значимое за год, без поправки на свежесть.'};
  $('#signal-ranking-summary').textContent=shortNotes[state.period];
  $('#signal-ranking-detail').textContent=periodNotes[state.period]+timing+(state.signalCategory==='all'?'':notes[state.signalCategory])+(order==='native'?' Записи без метрики идут в конце по редакционному баллу.':'');
}
const plural=(n,one,few,many)=>{const m10=n%10,m100=n%100;return m10===1&&m100!==11?one:m10>=2&&m10<=4&&(m100<12||m100>14)?few:many};
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
function column(key,rows){const choices=DATA.sources.filter(s=>{const cat=s.category;const native={repos:'repository',models:'model',papers:'paper'};if(native[key])return cat===native[key];return key==='community'?(cat==='community'||s.id==='hn'):key==='labs'?['lab','newsletter','cloud'].includes(cat):cat==='news'||cat==='aggregator'&&s.id!=='hn'});const selected=state.columns[key];const filtered=rows.filter(p=>selected.startsWith('group:')?p.column===key:p.source_id===selected);// "Сутки" shows trend lists in the provider's order; other tabs keep the rows ordered by their measure.
  if(['repos','models','papers'].includes(key)&&state.period==='day')filtered.sort((a,b)=>(a.provider_rank??9999)-(b.provider_rank??9999));const options=`<option value="group:${key}">${GROUPS[key]}</option>`+choices.map(s=>`<option value="${e(s.id)}" ${selected===s.id?'selected':''}>${e(s.name)}</option>`).join('');return `<section class="column${filtered.length?'':' is-empty'}" data-column="${key}" aria-label="${GROUPS[key]}"><div class="column-header"><div class="column-name"><span class="column-dot"></span><select data-column-select="${key}" aria-label="Источник: ${GROUPS[key]}">${options}</select></div><span class="column-count">${filtered.length}</span></div><div class="column-body">${filtered.slice(0,state.shown[key]).map(card).join('')||'<div class="column-empty compact">Нет записей за этот период — выберите другой источник или период</div>'}</div>${filtered.length>state.shown[key]?`<button class="more-button" data-more="${key}">Показать ещё · ${filtered.length-state.shown[key]}</button>`:''}</section>`}
function render(){const rows=SignalRanking.rank(items(),'all',periodOrder());document.querySelectorAll('[data-period]').forEach(b=>{b.classList.toggle('active',b.dataset.period===state.period);b.setAttribute('aria-pressed',b.dataset.period===state.period)});document.querySelectorAll('[data-topic]').forEach(b=>{b.classList.toggle('active',b.dataset.topic===state.topic);b.setAttribute('aria-pressed',b.dataset.topic===state.topic)});$('#story-count').textContent=rows.length;renderSignals(rows);$('#feed').className=state.view==='list'?'list-mode':'board';$('#feed').style.setProperty('--stacks',kanbanStacks());$('#feed').innerHTML=state.view==='list'?rows.slice(0,state.shown.list||30).map(card).join('')+(rows.length>(state.shown.list||30)?'<button class="more-button" data-more="list">Показать ещё</button>':''):kanban(rows);$('#kanban-view').classList.toggle('active',state.view==='kanban');$('#list-view').classList.toggle('active',state.view==='list');$('#empty').hidden=rows.length>0;$('#feed').hidden=rows.length===0;$('#rss-link').href=location.protocol==='file:'?'data/feed.xml':'/api/rss?sort='+(isTrending()?'day':state.period);}
function openModal(html){$('#detail-content').innerHTML=html;$('#detail').showModal()}
function breakdown(id,category){const p=(DATA.signal_rankings?.[category]||DATA.items).find(p=>p.id===id);if(!p)return;openModal(`<span class="modal-kicker">Сигнал ${p.score} / 100 · ${e(p.topic)}</span><h2 class="modal-title">${e(p.title)}</h2><p class="modal-description">${e(p.summary||'В RSS нет описания.')}</p>${cloudWhere(p)}<a class="modal-link" href="${safe(p.url)}" target="_blank" rel="noopener noreferrer">Открыть первоисточник ↗</a>${p.discussion_url?`<a class="modal-link" href="${safe(p.discussion_url)}" target="_blank" rel="noopener noreferrer">Обсуждение ↗</a>`:''}<div>${p.components.map(c=>`<div class="breakdown-row"><div class="breakdown-label"><span>${e(c.label)}</span><span>${c.available?`${c.points} / ${c.max}`:'нет данных'}</span></div>${c.explanation?`<div class="metric-explanation">${e(c.explanation)}</div>`:''}<div class="breakdown-bar"><div class="breakdown-fill" style="width:${100*c.points/c.max}%"></div></div></div>`).join('')}</div>${p.metrics?.length?`<p class="coverage-list">${p.metrics.map(m=>`${e(new Intl.NumberFormat('ru-RU').format(m.value))} ${e(m.label)}`).join(' · ')}</p>`:''}${p.date_kind==='trend'?`<p class="coverage-list">Обнаружено в трендах: ${date(p.published_at)}${p.original_published_at?' · Опубликовано: '+date(p.original_published_at):''}</p>`:''}<p class="coverage-list">Площадки: ${p.sources.map(e).join(' · ')}</p>${p.also?.length?`<div class="coverage-list">Объединённые материалы о той же новости:<ul class="also-list">${p.also.map(a=>`<li><a href="${safe(a.url)}" target="_blank" rel="noopener noreferrer">${e(a.title)}</a> <span class="muted">— ${e(a.source)}</span></li>`).join('')}</ul></div>`:''}${p.enterprise_reasons?.length?`<p class="coverage-list">Enterprise: ${p.enterprise_reasons.map(e).join(' · ')}</p>`:''}<p class="modal-note">${e(p.description_kind)}. Редакционный балл помогает выбрать новости, это не оценка их достоверности и не прогноз популярности. Отсутствующие метрики не выдумываются.</p>`)}
function methodology(){openModal(`<span class="modal-kicker">TOP SIGNALS · ${e(DATA.scoring_version)}</span><h2 class="modal-title">Почему эта новость важнее?</h2><p class="modal-description">Баллы отражают приоритет для вашего AI-канала. Учитываем тему, конкретику и свежесть. Категорию рейтинга можно выбрать отдельно: модели, GitHub, научные статьи, издания или Enterprise. В каждой категории показываем лучшие записи без ограничения «одна на источник». Кнопка под рейтингом раскрывает следующие места.</p><p class="modal-description">Редакционный балл — общая формула ниже. «Метрика источника» меняет порядок внутри категории: GitHub — звёзды за сутки; HF Models — сначала trendingScore, затем лайки для моделей без trendingScore; Papers — голоса. Общие звёзды, скачивания и метрики разных платформ не складываются. Период отбирает записи, а окно измерения указано у метрики. Издания ранжируются редакционно: публичных счётчиков в RSS нет.</p><div class="status-row"><span>Вкладки периода</span><b>окно · порядок</b></div><p class="modal-description">Период считается от момента, когда запись стала новостью: публикация статьи или поста, создание модели или статьи HF, первый день репозитория в GitHub Trending. Время последнего чтения трендов не учитывается, поэтому модель от августа, снова попавшая в тренды, не считается новостью дня.</p><p class="modal-description"><b>Trending Now</b> — 12 часов, порядок по импульсу (популярность на платформе + охват площадок, до 25): что обсуждают прямо сейчас. <b>Сутки</b> — 24 часа, редакционный балл до 100 со свежестью. <b>Неделя, Месяц, Год</b> — 7, 30 и 365 дней, порядок по значимости: редакционный балл без свежести, до 75. Внутри длинного периода важная новость не уступает более новой только из-за даты. Число на карточке — показатель, по которому упорядочена вкладка; редакционный балл всегда есть в разборе.</p><div class="status-row"><span>Соответствие теме</span><b>до 30</b></div><p class="modal-description">Релиз весов с замерами, заголовок о релизе известного семейства моделей или такая модель в трендах — 30; другая модель в трендах — 24; локальный запуск с цифрами — 24; инструменты — 18; исследования — 12; индустрия — 6.</p><div class="status-row"><span>Свежесть</span><b>до 25</b></div><p class="modal-description">25 × 2<sup>−возраст / полураспад</sup>. Новости — от даты публикации, полураспад 24 ч. Papers — от даты статьи, 72 ч. Модели — от даты создания, 168 ч. Репозитории GitHub — от первого появления в трендах, 24 ч. Старая статья или модель, снова попавшая в тренды, свежей не считается.</p><div class="status-row"><span>Конкретика</span><b>до 15</b></div><p class="modal-description">Численные показатели — 7; модель или репозиторий — 5; paper, research или benchmark — 3.</p><div class="status-row"><span>Появление на разных площадках</span><b>до 15</b></div><p class="modal-description">7,5 за дополнительную площадку, максимум 15. Учитываем общую ссылку, точный заголовок и тот же сюжет у других площадок за 72 часа: общие номера версий (v4.1, qwen3.8) или большинство ключевых слов заголовка. Несколько сабреддитов считаются одной площадкой, Google AI и DeepMind — тоже. Перепечатка не подтверждает достоверность.</p><div class="status-row"><span>Официальный источник</span><b>до 5</b></div><div class="status-row"><span>Реальная популярность</span><b>до 10</b></div><p class="modal-description">Для HN: 2,5 × log₁₀(очки + 1). Для GitHub: звёзды за сутки; для моделей HF: trendingScore (если его нет — лайки); для Papers: голоса. Эти метрики нормируются отдельно: 10 × ln(значение + 1) / ln(порог + 1), максимум 10. Пороги: 1000 звёзд за сутки, 1000 trendingScore или лайков модели, 300 голосов статьи. У журналов и блогов такой метрики нет, и мы её не придумываем. Это редакционные настройки, а не процентили. Скачивания показываем отдельно и в балл не добавляем. Если метрики нет, баллов за неё нет.</p><p class="modal-note">Собственную скорость роста не показываем: для неё нужны как минимум два наблюдения одной метрики. Звёзды GitHub за сутки берём готовыми со страницы Trending. Описания — выдержки из RSS, статьи, описания репозитория или аннотации Papers. Модели показывают метаданные model card. Это не AI-резюме. Недельный и годовой виды используют накопленные записи; полнота зависит от времени работы сборщика.</p>`)}
function sourcesModal(){openModal(`<span class="modal-kicker">Состояние источников</span><h2 class="modal-title">Что сейчас читается</h2><p class="modal-description">Последняя проверка: ${date(DATA.collected_at||DATA.generated_at)}. Источники проверяются по отдельному расписанию. Паузы действуют и при ручном обновлении. Ошибка одного сайта не останавливает остальные.</p>${DATA.statuses.map(s=>`<div class="status-row"><span>${e(s.name)}${Math.max(s.retry_at||0,s.next_fetch_at||0)>now()?`<small class="source-retry">Следующая проверка не раньше ${date(Math.max(s.retry_at||0,s.next_fetch_at||0))}</small>`:''}</span><span class="${s.status==='ok'?'status-ok':'status-error'}">${s.status==='ok'?s.count+' записей':s.status==='scheduled'?'Ожидает первого сбора':s.status==='stale'?'Сохранённые данные · '+e(s.error||'источник недоступен'):e(s.error?.includes('rate limit')?'Ограничение запросов (429)':s.error||'Недоступен')}</span></div>`).join('')}<p class="modal-note">Количество относится к записям в RSS до тематических и временных фильтров.</p>`)}
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
    openModal('<span class="modal-kicker">Управление лентой</span><h2 class="modal-title">Обновление источников</h2><p class="modal-description">Для запуска сбора нужен ключ управления. Он останется только в этой вкладке браузера.</p><form id="management-form"><label class="management-label">Ключ управления<input id="management-key" type="password" autocomplete="off" required></label><button class="refresh-button" type="submit">Продолжить</button></form>');
    const dialog=$('#detail');
    dialog.addEventListener('close',()=>resolve(value),{once:true});
    $('#management-form').onsubmit=event=>{event.preventDefault();value=$('#management-key').value.trim();if(value){sessionStorage.setItem('signal-management',value);dialog.close()}};
    $('#management-key').focus();
  });
}
$('#refresh').onclick=async()=>{
  if(location.protocol==='file:'){toast('Откройте дашборд через сервер для обновления источников');return}
  const button=$('#refresh');button.disabled=true;
  try{
    const send=()=>fetch('/api/refresh',{method:'POST',headers:sessionStorage.getItem('signal-management')?{'Authorization':'Bearer '+sessionStorage.getItem('signal-management')}: {}});
    let response=await send();
    if(response.status===401){const key=await managementKey();if(!key){button.disabled=false;return}response=await send()}
    if(!response.ok){if(response.status===401)sessionStorage.removeItem('signal-management');throw Error(response.status===401?'Ключ управления не подошёл':'Не удалось начать обновление')}
    const request=await response.json();
    if(request.status==='cached'){toast('Данные уже проверены. Повторное обновление пока не требуется.');button.disabled=false;return}
    toast('Обновление в очереди. Лента появится после завершения сбора.');
    for(let i=0;i<300;i++){
      await new Promise(resolve=>setTimeout(resolve,2000));
      const result=await fetch('/api/status').then(r=>r.json());
      if(!result.refreshing){if(result.error)throw Error(result.error);location.reload();return}
    }
    throw Error('Обновление ещё идёт. Проверьте страницу чуть позже.');
  }catch(error){toast(error.message);button.disabled=false}
};

const healthy=DATA.statuses.filter(s=>s.status==='ok').length;$('#health-label').textContent=`${healthy} из ${DATA.statuses.length} источников доступны`;$('#source-status').classList.toggle('healthy',healthy===DATA.statuses.length);$('#updated-at').textContent='Обновлено '+age(DATA.collected_at||DATA.generated_at);$('#archive-info').textContent=`${DATA.items.length} записей в архиве · ранжирование ${DATA.scoring_version}`;render();requestAnimationFrame(()=>requestAnimationFrame(()=>document.documentElement.classList.add('ready')));

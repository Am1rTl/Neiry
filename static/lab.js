/* NeuroScope research interface. All plotted values come from the local API. */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const preview = window.NEUROSCOPE_PREVIEW || null;
  const exportUrls = [];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null;
  const fmt = (v, d = 1) => num(v) === null ? '—' : Number(v).toLocaleString('ru-RU', {maximumFractionDigits:d, minimumFractionDigits:d});
  const integer = v => fmt(v, 0);
  const percent = v => num(v) === null ? '—' : `${fmt(v, 1)}%`;
  const arr = v => Array.isArray(v) ? v : [];
  const values = v => Array.isArray(v) ? v : v && typeof v === 'object' ? Object.entries(v).map(([name, data]) => ({name, ...(typeof data === 'object' ? data : {value:data})})) : [];
  const textOf = v => typeof v === 'string' ? v : v && typeof v === 'object' ? (v.description || v.detail || v.title || JSON.stringify(v)) : '';
  const safeUrl = url => /^https?:\/\//i.test(String(url || '')) ? esc(url) : '#';
  const icon = (name, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const colors = ['#7ce3cb','#8ca9ff','#e4a2ad','#f0c38a','#b7a7ee'];
  const bandDefs = [{id:'delta',name:'Дельта',range:'1–4 Гц',color:'#8da8cb'},{id:'theta',name:'Тета',range:'4–8 Гц',color:'#a49cde'},{id:'alpha',name:'Альфа',range:'8–13 Гц',color:'#7ce3cb'},{id:'beta',name:'Бета',range:'13–30 Гц',color:'#8ca9ff'},{id:'gamma',name:'30–45 Гц',range:'Верхняя полоса',color:'#d1b489'}];
  const titles = {
    device: ['Подключение устройства', 'Поиск, состояние Bluetooth и калибровка без навязанных экранов.', 'Устройство'],
    live: ['Живой монитор', 'ЭЭГ, ФПГ, движение и показатели SDK в реальном времени.', 'Живой монитор'],
    sessions: ['Записи и воспроизведение', 'Управляйте сессиями, анализом, загрузкой и replay в одном окне.', 'Записи'],
    experiments: ['Эксперименты', 'Выполнение протоколов с временными маркерами без отдельного интерфейса.', 'Эксперименты'],
    segment: ['Исследование фрагмента', 'Выделяйте участки сигнала, сохраняйте аннотации и экспортируйте данные.', 'Исследователь'],
    history: ['История исследований', 'Сопоставляйте только явно отмеченные измерения одного участника.', 'История'],
    overview: ['Лаборатория сигналов', 'Отдельные измерения. Общая картина. Проверяемые выводы.', 'Обзор записей'],
    signals: ['Сигналы и метрики', 'Проверяйте каждый вывод по исходному сигналу и условиям расчёта.', 'Сигналы и метрики'],
    compare: ['Сравнение сессий', 'Различия записей с учётом качества, длительности и условий.', 'Сравнить сессии'],
    knowledge: ['Как это работает', 'Нейрофизиология, возможности измерений и научные источники.', 'Как это работает'],
    protocol: ['Следующая запись', 'Больше контекста и меньше артефактов для более полезного анализа.', 'Протокол записи']
  };
  const state = {sessions:[], current:null, analysis:null, cache:new Map(), knowledge:null, view:'overview', charts:[], channel:0, raw:false, log:true, renderId:0, context:null};

  function duration(v) {
    if (num(v) === null) return '—';
    const n = Math.max(0, Math.round(Number(v))), h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
    return h ? `${h} ч ${m} мин` : m ? `${m} мин ${s.toString().padStart(2,'0')} с` : `${s} с`;
  }
  function dateLabel(v, long = false) {
    const d = new Date(v);
    if (!Number.isFinite(d.getTime())) return String(v || 'Дата не указана');
    return new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:long?'long':'short', ...(long?{year:'numeric'}:{}), hour:'2-digit', minute:'2-digit', timeZone:'Europe/Moscow'}).format(d).replace(' г.', '');
  }
  function statusInfo(v) {
    const key = String(typeof v === 'object' ? v.status : v || '').toLowerCase();
    if (['ok','good','acceptable','usable','pass','available'].includes(key)) return {label:'Пройден фильтр качества', cls:'good'};
    if (['poor','bad','unusable','unreliable','failed','insufficient','insufficient_data','rejected'].includes(key)) return {label:'Ограниченная пригодность', cls:'bad'};
    if (['missing','no_eeg','not_available','unavailable','absent','empty','no_data'].includes(key)) return {label:'Недостаточно данных', cls:''};
    if (['warning','warn','caution','limited','degraded','mixed'].includes(key)) return {label:'Есть ограничения', cls:'warning'};
    return {label:key ? 'Нужна проверка условий' : 'Статус не определён', cls:'warning'};
  }
  function tag(label, cls = '') { return `<span class="tag ${cls}"><i class="tag-dot"></i>${esc(label)}</span>`; }
  function empty(title, description, symbol = 'signal') { return `<div class="empty-state">${icon(symbol)}<h3>${esc(title)}</h3><p>${esc(description)}</p></div>`; }
  function notice(body, cls = '') { return `<div class="notice ${cls}">${icon('info')}<p>${body}</p></div>`; }
  function notify(message, kind = '') { const el = $('#notification'); el.className = `notification ${kind}`; el.textContent = message; el.hidden = !message; }
  function cardHeader(title, subtitle = '', right = '') { return `<div class="card-header"><div><h2>${esc(title)}</h2>${subtitle ? `<p class="card-subtitle">${esc(subtitle)}</p>` : ''}</div>${right ? `<div class="card-header-right">${right}</div>` : ''}</div>`; }
  function metric(label, value, unit, note, symbol, badge = '') { return `<section class="metric-card"><div class="metric-label"><span>${esc(label)}</span>${icon(symbol)}</div><div class="metric-value ${String(value).length>9?'long-value':''}">${esc(value)}${unit?`<small>${esc(unit)}</small>`:''}</div><div class="metric-note">${esc(note)}</div>${badge}</section>`; }
  function cleanUnits(v) { return !v || /unknown|not.?confirmed|unverified/i.test(String(v)) ? 'ед. SDK' : String(v).replace('SDK²/Hz','ед. SDK²/Гц').replace('SDK','ед. SDK'); }

  async function api(path, options = {}) {
    if (preview) {
      if (options.method && options.method !== 'GET') throw new Error('В режиме просмотра сохранённые данные доступны только для чтения. Пересчёт и запись контекста доступны в запущенном приложении.');
      if (path === '/api/lab/sessions') return {sessions:preview.sessions};
      if (path === '/api/lab/knowledge') return preview.knowledge;
      if (path.startsWith('/api/lab/context/')) return preview.analyses[decodeURIComponent(path.split('/').pop())]?.context || {eye_state:'unknown',task:'',notes:''};
      if (path.startsWith('/api/lab/analysis/')) {
        const data = preview.analyses[decodeURIComponent(path.split('/').pop())];
        if (data) return data;
      }
      throw new Error('Эти данные не включены в сохранённый просмотр.');
    }
    const response = await fetch(path, {...options, headers:{'Accept':'application/json', ...(options.body?{'Content-Type':'application/json'}:{}), ...options.headers}});
    if (!response.ok) {
      let detail = '';
      try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : ''; } catch (_) {}
      throw new Error(detail || `Сервер вернул HTTP ${response.status}.`);
    }
    return response.json();
  }
  async function analysisFor(id) {
    if (state.cache.has(id)) return state.cache.get(id);
    const result = await api(`/api/lab/analysis/${encodeURIComponent(id)}`);
    const data = result.analysis || result;
    state.cache.set(id, data); return data;
  }
  async function knowledgeData() {
    if (state.knowledge) return state.knowledge;
    state.knowledge = await api('/api/lab/knowledge'); return state.knowledge;
  }
  function destroyCharts() { state.charts.forEach(c => c.destroy()); state.charts = []; }
  function chart(id, datasets, opts = {}) {
    const canvas = $(`#${id}`);
    if (!canvas || !window.Chart) return;
    const options = {
      responsive:true, maintainAspectRatio:false, animation:false, normalized:true, parsing:false,
      interaction:{mode:'nearest',axis:'x',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#1c2a3d',titleColor:'#ecf2f8',bodyColor:'#c4d1e1',padding:11,displayColors:true,cornerRadius:7,borderColor:'#3c4c64',borderWidth:1,
        callbacks:{title: items => `${fmt(items[0]?.parsed.x, opts.frequency?2:1)} ${opts.frequency?'Гц':'с'}`,label:ctx=>`${ctx.dataset.label}: ${fmt(ctx.parsed.y, opts.digits??2)}${opts.suffix||''}`}}},
      elements:{point:{radius:0,hitRadius:6,hoverRadius:3},line:{borderWidth:1.7,tension:0,fill:false}},
      scales:{x:{type:'linear',min:opts.xmin??undefined,max:opts.xmax??undefined,border:{display:false},grid:{color:'#25324560',drawTicks:false},ticks:{color:'#7f91aa',font:{size:9},maxTicksLimit:8,padding:10},title:{display:true,text:opts.frequency?'Частота, Гц':'Время от начала потока, с',color:'#71839e',font:{size:9}}},
        y:{type:opts.log?'logarithmic':'linear',min:opts.ymin??undefined,max:opts.ymax??undefined,border:{display:false},grid:{color:'#25324580',drawTicks:false},ticks:{color:'#7f91aa',font:{size:9},maxTicksLimit:5,padding:10,callback:v => Math.abs(v)>=10000||Math.abs(v)>0&&Math.abs(v)<.001 ? Number(v).toExponential(0) : fmt(v,Math.abs(v)<1?3:1)},title:{display:!!opts.ytitle,text:opts.ytitle,color:'#71839e',font:{size:9}}}}
    };
    try { state.charts.push(new Chart(canvas,{type:opts.type||'line',data:{datasets},options})); }
    catch (error) { console.error('Chart rendering:',error); canvas.parentElement.innerHTML = empty('График недоступен','Числовые результаты остаются доступны в таблицах и экспорте.'); }
  }
  function points(xs, ys, positive = false) { return arr(xs).map((x, i) => ({x:Number(x),y:num(arr(ys)[i])!==null&&(!positive||Number(ys[i])>0)?Number(ys[i]):null})); }
  function dataset(label, data, color, extra = {}) { return {label,data,borderColor:color,backgroundColor:color,spanGaps:false,...extra}; }
  function channelOptions(channels) { return channels.map((c,i)=>`<option value="${i}" ${i===state.channel?'selected':''}>${esc(c.name)}</option>`).join(''); }
  function legend(channels) { return `<div class="legend">${channels.map((c,i)=>`<span><i style="background:${colors[i%colors.length]}"></i>${esc(c.name)}</span>`).join('')}</div>`; }

  function renderSessions() {
    $('#session-selector').innerHTML = state.sessions.map(s => `<button class="session-option ${s.id===state.current?'selected':''}" data-session="${esc(s.id)}" aria-pressed="${s.id===state.current}" title="${esc(s.id)}"><span class="session-symbol">${icon(s.eeg_available?'wave':'heart')}</span><span class="session-info"><strong>${esc(dateLabel(s.started_at))}</strong><small>${esc(duration(s.wall_duration_s??s.duration_s))} · ${s.eeg_available?'ЭЭГ + пульс + движение':'Без потока ЭЭГ'}</small></span><span class="session-state" aria-hidden="true"></span></button>`).join('');
    $$('#session-selector [data-session]').forEach(b => b.addEventListener('click',()=>selectSession(b.dataset.session)));
    $('#session-selector').hidden = ['knowledge','protocol','device','live','sessions','experiments','segment','history'].includes(state.view);
  }
  function updateExports() {
    ['html','json','csv'].forEach(format => { $(`#export-${format}`).href = `/api/lab/analysis/${encodeURIComponent(state.current || '')}/export?format=${format}`; });
    if (preview && state.analysis) previewExports(state.analysis);
    $('#analysis-version').textContent = state.analysis?.analysis_version ? `Анализ ${state.analysis.analysis_version}` : 'Воспроизводимый анализ';
    $('#analysis-actions').hidden = ['knowledge','protocol','compare','device','live','sessions','experiments','segment','history'].includes(state.view)||!state.current;
  }
  function previewExports(a) {
    exportUrls.splice(0).forEach(url=>URL.revokeObjectURL(url));
    const bind=(format,body,type)=>{const url=URL.createObjectURL(new Blob([body],{type}));exportUrls.push(url);const link=$(`#export-${format}`);link.href=url;link.download=`${a.session?.id||'analysis'}.${format}`;};
    bind('json',JSON.stringify(a,null,2),'application/json;charset=utf-8');
    const rows=[['session','channel','metric','value','units']];
    arr(a.eeg?.channels).forEach(c=>{bandDefs.forEach(b=>{rows.push([a.session?.id,c.name,`${b.id}_relative_power`,c.relative_power?.[b.id]??'','fraction']);});rows.push([a.session?.id,c.name,'alpha_peak_hz',c.alpha_peak_hz??'','Hz']);rows.push([a.session?.id,c.name,'spectral_entropy',c.entropy??'','0..1']);rows.push([a.session?.id,c.name,'usable_windows_pct',c.quality?.usable_pct??'','percent_of_overlapping_windows']);});
    rows.push([a.session?.id,'PPG','pulse_rate_bpm',a.ppg?.pulse_rate_bpm??'','bpm']);
    const csvCell=v=>{let value=String(v??'');if(/^[=+@\t\r]|^-(?!\d)/.test(value))value="'"+value;return `"${value.replace(/"/g,'""')}"`;};
    const csv='\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n');
    bind('csv',csv,'text/csv;charset=utf-8');
    const html=`<!doctype html><html lang="ru"><meta charset="utf-8"><title>NeuroScope · ${esc(a.session?.id)}</title><style>body{max-width:980px;margin:40px auto;padding:0 24px;font:15px/1.8 system-ui,sans-serif;color:#182438}h1,h2{line-height:1.3}h2{margin-top:35px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}small{color:#627089}.tag{font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}.observation-number{color:#62877e}.observation h3{margin-bottom:0}.observation p{margin-top:6px}</style><h1>NeuroScope · анализ записи</h1><p>${esc(dateLabel(a.session?.started_at,true))} · ${esc(a.session?.id)}</p><p>Исследовательский отчёт. Не медицинское заключение. Версия: ${esc(a.analysis_version)}</p><h2>Наблюдения</h2>${observations(a,99)}<h2>Каналы ЭЭГ</h2>${arr(a.eeg?.channels).map(c=>`<h3>${esc(c.name)}</h3>${channelStats(c)}`).join('')||'<p>ЭЭГ отсутствует.</p>'}<h2>Качество</h2><ul>${arr(a.quality?.flags).map(f=>`<li><strong>${esc(f.title)}</strong>: ${esc(f.detail)}</li>`).join('')}</ul><h2>Пульс</h2><p>${fmt(a.ppg?.pulse_rate_bpm,1)} уд/мин. ${esc(a.ppg?.prv?.description||'')}</p><h2>Ограничения</h2><ul>${arr(a.limitations).map(x=>`<li>${esc(textOf(x))}</li>`).join('')}</ul><h2>Метод</h2>${methodSummary(a)}</html>`;
    bind('html',html,'text/html;charset=utf-8');
  }
  function nav(view) {
    if (!titles[view]) view = 'overview';
    state.view = view;
    const [title, subtitle, short] = titles[view];
    $('#page-title').innerHTML = `${esc(title)}<span class="title-dot">.</span>`;
    $('#page-subtitle').textContent = subtitle;
    $('#breadcrumb-current').textContent = short;
    $$('.nav-item[data-view]').forEach(a=>{const active=a.dataset.view===view;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    $('#session-selector').hidden = ['knowledge','protocol','device','live','sessions'].includes(view);
    updateExports(); closeMenu(); render();
  }
  async function selectSession(id) {
    state.current = id; state.context = null; renderSessions();
    try { localStorage.setItem('neuroscope.session',id); } catch (_) {}
    await render();
  }
  async function render() {
    const renderId = ++state.renderId; destroyCharts(); notify('');
    if (['device','live','sessions'].includes(state.view)) {
      window.NeuroStudio?.leave();
      await window.NeuroLive.renderView(state.view);
      return;
    }
    window.NeuroLive?.leave();
    if (['experiments','segment','history'].includes(state.view)) {
      await window.NeuroStudio?.renderView(state.view,{api,notify,preview:Boolean(preview)});
      return;
    }
    window.NeuroStudio?.leave();
    $('#content').innerHTML = `<div class="loading-panel"><span class="loader"></span><h2>Готовим ${state.view==='knowledge'?'научную базу':state.view==='compare'?'сравнение':'анализ'}</h2><p>Загружаем результаты из локального проекта…</p></div>`;
    try {
      if (state.view==='knowledge' || state.view==='protocol') {
        const knowledge = await knowledgeData(); if(renderId!==state.renderId)return;
        state.view==='knowledge' ? renderKnowledge(knowledge) : renderProtocol(knowledge);
        return;
      }
      if (state.view==='compare') {
        const results = await Promise.allSettled(state.sessions.map(s=>analysisFor(s.id))); if(renderId!==state.renderId)return;
        renderComparison(results.map(r=>r.status==='fulfilled'?r.value:null)); return;
      }
      if (!state.current) { $('#content').innerHTML=empty('Записей пока нет','Подключите устройство в живом мониторе и начните запись. Сессии появятся здесь после сохранения.','folder'); return; }
      const data = await analysisFor(state.current); if(renderId!==state.renderId)return;
      state.analysis = data; state.channel = Math.min(state.channel,Math.max(0,arr(data.eeg?.channels).length-1)); updateExports();
      state.view==='signals' ? renderSignals(data) : renderOverview(data);
    } catch (error) {
      if(renderId!==state.renderId)return;
      $('#content').innerHTML=`<div class="error-panel">${icon('info')}<h2>Не удалось открыть результаты</h2><p>${esc(error.message)} Проверьте, что сервер запущен и файлы записи доступны.</p><button class="button primary" id="retry-button">Повторить</button></div>`;
      $('#retry-button')?.addEventListener('click',render);
    }
  }

  function headlineMetrics(a) {
    const eeg=a.eeg||{}, cs=arr(eeg.channels), p=a.ppg||{}, c=cs[0]||{}, q=statusInfo(a.quality?.status);
    const signalDuration = num(c.sample_count)!==null&&num(eeg.sample_rate_hz)!==null ? c.sample_count/eeg.sample_rate_hz : null;
    const peak = num(c.alpha_peak_hz), peak2 = num(cs[1]?.alpha_peak_hz);
    const alphaValue = !eeg.available?'—':peak!==null?fmt(peak,2):'Не выделен';
    const alphaNote = cs[1] ? `${c.name||'Канал 1'}${peak2!==null?` · ${cs[1].name}: ${fmt(peak2,2)} Гц`:` · ${cs[1].name}: пик не выделен`}` : (eeg.available?'Кандидат в диапазоне 8–13 Гц':'В записи отсутствует ЭЭГ');
    return `<div class="metric-grid">${metric('Длительность ЭЭГ',eeg.available?duration(signalDuration):'Нет ЭЭГ','',eeg.available?`${integer(eeg.sample_rate_hz)} Гц · ${cs.length} биполярных канала`:'Доступны другие потоки','clock',tag(eeg.available?'По количеству отсчётов':'Не измерено',eeg.available?'blue':''))}${metric('Окна без флагов',fmt(a.quality?.usable_eeg_pct,1),num(a.quality?.usable_eeg_pct)!==null?'%':'','Перекрывающиеся окна 4 с, шаг 2 с','shield',tag(q.label,q.cls))}${metric('Альфа-пик · кандидат',alphaValue,peak!==null?'Гц':'',alphaNote,'wave',tag('Не показатель интеллекта',''))}${metric('Частота пульса',fmt(p.pulse_rate_bpm,1),num(p.pulse_rate_bpm)!==null?'уд/мин':'',p.available?'Медиана принятых окон PPG':'Оптический сигнал отсутствует','heart',tag(num(p.pulse_rate_bpm)!==null?'Проверяйте качество PPG':'Оценка недоступна',num(p.pulse_rate_bpm)!==null?'blue':''))}</div>`;
  }
  function spectrumCard(a, id='overview-spectrum') {
    const cs=arr(a.eeg?.channels), c=cs[state.channel]||{};
    const selector=cs.length?`<label class="screen-reader" for="band-channel">Канал для полос мощности</label><select id="band-channel" class="select">${channelOptions(cs)}</select>`:'';
    return `<section class="card">${cardHeader('Спектральный профиль','Распределение мощности ЭЭГ по частотам',selector)}${a.eeg?.available?`${legend(cs)}<div class="chart-shell"><canvas id="${id}" aria-label="Спектральная плотность мощности ЭЭГ" role="img"></canvas></div><div class="bands">${bandDefs.map(b=>{const power=num(c.relative_power?.[b.id]);return `<div><div class="band-name"><i style="background:${b.color}"></i>${b.name}</div><div class="band-value">${power===null?'—':`${fmt(power*100,1)}<small style="font-size:10px"> %</small>`}</div><div class="band-range">${b.range}</div><div class="mini-bar"><span style="width:${power===null?0:Math.min(100,Math.max(0,power*100))}%;background:${b.color}"></span></div></div>`;}).join('')}</div><p class="chart-caption">Доли мощности в 1–45 Гц для ${esc(c.name||'выбранного канала')}. Отобранные окна; доли не являются долями «работы мозга». При недостатке окон оценка канала не выводится.</p>`:empty('В этой сессии нет ЭЭГ','Спектр и ритмы нельзя восстановить из пульса, движения или оценок SDK.')}</section>`;
  }
  function drawSpectrum(a,id='overview-spectrum',raw=false) {
    const datasets=[];
    arr(a.eeg?.channels).forEach((c,i)=>{
      if (arr(c.psd?.power).some(v=>num(v)!==null)) datasets.push(dataset(`${c.name} · отбор`,points(c.psd?.freq_hz,c.psd?.power,state.log),colors[i]));
      if (raw) datasets.push(dataset(`${c.name} · исходный`,points(c.psd?.freq_hz,c.psd?.raw_power,state.log),colors[i]+'75',{borderDash:[4,4],borderWidth:1}));
    });
    chart(id,datasets,{frequency:true,xmin:1,xmax:45,log:state.log,ytitle:cleanUnits(a.eeg?.channels?.[0]?.psd?.units||'SDK²/Hz'),digits:5});
  }
  function observations(a,limit=4) {
    const list=arr(a.observations);
    if(!list.length)return empty('Пока нет интерпретаций','Смотрите качество и доступные числовые показатели.');
    return `<div class="observations">${list.slice(0,limit).map((o,i)=>`<article class="observation"><span class="observation-number">${String(i+1).padStart(2,'0')}</span><div><h3>${esc(o.title||'Наблюдение')}</h3><p>${esc(o.detail||o.description)}</p>${o.confidence?`<span class="confidence-label">${esc(confidenceLabel(o.confidence))}</span>`:''}</div></article>`).join('')}</div>${list.length>limit?`<details class="more-observations details-block"><summary>Ещё ${list.length-limit} наблюдений</summary>${observations({...a,observations:list.slice(limit)},99)}</details>`:''}`;
  }
  function confidenceLabel(v) { return ({high:'Уверенность в описании данных: высокая',moderate:'Уверенность в интерпретации: умеренная',medium:'Уверенность в интерпретации: умеренная',low:'Интерпретация ограничена',descriptive:'Описательный факт',conditional:'Вывод зависит от условий записи',not_assessed:'Уверенность не оценивалась'})[v]||`Основание: ${v}`; }
  function montage() {
    return `<svg class="montage" viewBox="0 0 200 205" role="img" aria-label="Условная схема двух биполярных отведений O1–T3 и O2–T4; вид сверху"><defs><linearGradient id="head-fill" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#23413f" stop-opacity=".4"/><stop offset="1" stop-color="#152030"/></linearGradient></defs><path d="M90 24 100 11l10 13" fill="none" stroke="#4b6077" stroke-width="1.4"/><ellipse cx="100" cy="102" rx="69" ry="80" fill="url(#head-fill)" stroke="#4b6077" stroke-width="1.4"/><path d="M32 81C17 76 17 117 31 121M168 81c15-5 15 36 1 40M100 24v157" fill="none" stroke="#35485f" stroke-width="1.3" stroke-dasharray="4 5"/><path d="M57 51C82 29 91 56 84 71S56 86 62 108s32 15 22 44M143 51c-25-22-34 5-27 20s28 15 22 37-32 15-22 44" fill="none" stroke="#3b5264" stroke-width="1.2" opacity=".6"/><path d="M34 101 73 164" stroke="#7ce3cb" stroke-width="2"/><path d="m166 101-39 63" stroke="#8ca9ff" stroke-width="2"/><circle cx="34" cy="101" r="5" fill="#7ce3cb" stroke="#173931" stroke-width="5"/><circle cx="73" cy="164" r="5" fill="#7ce3cb" stroke="#173931" stroke-width="5"/><circle cx="166" cy="101" r="5" fill="#8ca9ff" stroke="#253351" stroke-width="5"/><circle cx="127" cy="164" r="5" fill="#8ca9ff" stroke="#253351" stroke-width="5"/><text x="9" y="97">T3</text><text x="176" y="97">T4</text><text x="57" y="187">O1</text><text x="121" y="187">O2</text><text x="100" y="201" text-anchor="middle" style="font-size:8px;fill:#71839e">ВИД СВЕРХУ · СХЕМА</text></svg>`;
  }
  function qualityCard(a,full=false) {
    const q=statusInfo(a.quality?.status), flags=arr(a.quality?.flags);
    return `<section class="card">${cardHeader('Проверка качества','Качество записи ограничивает силу выводов')}<div class="quality-head"><span class="quality-icon">${icon('shield')}</span><div><h3>${esc(q.label)}</h3><p>Эвристики анализа, без клинического порога</p></div></div><div class="quality-flags">${flags.slice(0,full?100:5).map(f=>`<details class="quality-flag ${esc(f.severity||'warning')}"><summary>${esc(f.title||f.code)}</summary><p>${esc(f.detail||f.description||'Подробности доступны в экспорте анализа.')}</p></details>`).join('')||`<p class="card-subtitle">Флаги не зарегистрированы. Это само по себе не подтверждает отсутствие артефактов.</p>`}</div>${!full&&flags.length>5?`<a href="#signals" data-view="signals" class="button text">Все ${flags.length} замечаний ${icon('arrow')}</a>`:''}</section>`;
  }
  function timelineCard(a,id='overview-timeline') {
    return `<section class="card">${cardHeader('Альфа-диапазон во времени','Относительная мощность 8–13 Гц в последовательных окнах',tag('Динамика','blue'))}${a.eeg?.available?`${legend(arr(a.eeg.channels))}<div class="chart-shell small"><canvas id="${id}" role="img" aria-label="Динамика доли мощности альфа-диапазона"></canvas></div><p class="chart-caption">Пробелы — окна, исключённые фильтром. Изменения могут зависеть от глаз, движений, контакта электродов и задачи.</p>`:empty('Динамика ЭЭГ недоступна','Для этой сессии не сохранены электрические отсчёты.')}</section>`;
  }
  function drawTimeline(a,id='overview-timeline',band='alpha') {
    chart(id,arr(a.eeg?.channels).map((c,i)=>dataset(c.name,arr(c.timeline).map(w=>({x:Number(w.time_s),y:w.usable&&num(w.bands?.[band])!==null?w.bands[band]*100:null})),colors[i])),{ymin:0,ymax:100,ytitle:'Доля мощности, %',suffix:' %'});
  }
  function streamRows(a) {
    const streams = a.session?.streams || {};
    return ['eeg','ppg','mems','resistances'].map(k=>{
      const s=Array.isArray(streams)?streams.find(x=>x.name===k)||{}:streams[k]||{};
      const known={eeg:a.eeg?.available,ppg:a.ppg?.available,mems:a.motion?.available,resistances:a.impedance?.available}[k];
      const count=s.sample_count??s.samples??s.rows??s.row_count??(k==='eeg'?a.eeg?.channels?.[0]?.sample_count:null);
      const names={eeg:'ЭЭГ',ppg:'Оптический пульс',mems:'Движение',resistances:'Сопротивление'};
      const rate=s.sample_rate_hz??s.nominal_rate_hz??{eeg:a.eeg?.sample_rate_hz,ppg:100,mems:250}[k];
      return `<tr><td><span class="stream-symbol">${icon({eeg:'wave',ppg:'heart',mems:'signal',resistances:'shield'}[k])}${names[k]}</span></td><td>${tag(known?'Есть данные':'Нет данных',known?'good':'')}</td><td>${integer(count)}</td><td>${known&&rate?`${integer(rate)} Гц`:'—'}</td><td>${esc(duration(s.nominal_duration_s??s.signal_duration_s??s.duration_s))}</td></tr>`;
    }).join('');
  }
  function renderOverview(a) {
    $('#content').innerHTML=`${a.eeg?.spectral_interpretability==='severely_limited'?notice('<strong>Интерпретация спектра сильно ограничена.</strong> Спектры описывают сохранённый сигнал; достоверный индивидуальный альфа-пик не установлен. Причины указаны в проверке качества.','warning'):''}${headlineMetrics(a)}<p class="chart-caption" style="margin:-10px 0 18px">Доля окон, прошедших фильтр, не равна доле чистого времени и не оценивает здоровье мозга.</p><div class="content-grid">${spectrumCard(a)}<section class="card full">${cardHeader('Связность двух отведений и 3D-схема','Исследовательская согласованность электрических сигналов, не локализация мозга')}${window.NeuroPairedPanel?.markup()||''}</section><section class="card">${cardHeader('Что видно в этой записи','Автоматические наблюдения, связанные с данными',tag('Автоанализ','good'))}${observations(a,3)}</section>${timelineCard(a)}${qualityCard(a)}<section class="card">${cardHeader('Что измеряет эта повязка','Два биполярных отведения вместо карты всего мозга')}<div class="brain-card">${montage()}<div class="brain-copy"><h3>O1–T3 и O2–T4</h3><p>Каждый канал — разность электрических потенциалов двух точек. Это смесь источников, а не отдельный участок мозга.</p><dl class="compact-kv"><dt>Схема</dt><dd>Затылочно-височная</dd><dt>Частота ЭЭГ</dt><dd>${integer(a.eeg?.sample_rate_hz||250)} Гц</dd><dt>Единицы</dt><dd>${esc(cleanUnits(a.eeg?.units))}</dd></dl><a class="button text" href="#knowledge" data-view="knowledge">Возможности и ограничения ${icon('arrow')}</a></div></div></section><section class="card">${cardHeader('Контекст записи','Добавленный вами контекст хранится отдельно от исходных данных')}${contextForm()}</section><section class="card full">${cardHeader('Состав записи','Номинальная длительность и реальные временные метки рассматриваются отдельно')}<div class="table-wrap"><table class="data-table"><thead><tr><th>ПОТОК</th><th>ДОСТУПНОСТЬ</th><th>ОТСЧЁТЫ</th><th>ЧАСТОТА</th><th>ДЛИТЕЛЬНОСТЬ</th></tr></thead><tbody>${streamRows(a)}</tbody></table></div><p class="table-caption">Время в интерфейсе: Москва (UTC+3). Параметры частот взяты из метаданных записи; временные метки проверяются отдельно в автоанализе.</p></section></div>`;
    window.NeuroPairedPanel?.attach($('#content [data-pair-panel]'),a.paired_eeg);
    drawSpectrum(a); drawTimeline(a); bindContent(); bindChannel(); loadContext();
  }
  function bindChannel() { $('#band-channel')?.addEventListener('change',e=>{state.channel=Number(e.target.value);destroyCharts();state.view==='overview'?renderOverview(state.analysis):renderSignals(state.analysis);}); }
  function contextForm() {
    if (preview) return `<div class="text-content"><p>В исходных записях нет синхронных меток состояния глаз и выполняемой задачи.</p><p>В режиме просмотра контекст недоступен для изменения. Добавить свои заметки и новые записи можно в локально запущенном приложении.</p></div>${tag('Сохранённый анализ','blue')}`;
    return `<form id="context-form"><div class="control-row"><label for="eye-state">Глаза</label><select id="eye-state" class="select"><option value="unknown">Неизвестно</option><option value="open">Открыты</option><option value="closed">Закрыты</option><option value="mixed">Чередовались</option></select></div><label class="card-subtitle" for="context-task">Задача или состояние во время записи</label><input class="source-filter" style="margin-top:5px" id="context-task" maxlength="240" placeholder="Например: спокойное сидение, чтение"><label class="card-subtitle" for="participant-code">Псевдоним участника (не ФИО)</label><input class="source-filter" id="participant-code" maxlength="64" placeholder="Например: user01"><label class="card-subtitle" for="protocol-id">ID протокола (если известен)</label><input class="source-filter" id="protocol-id" maxlength="80" placeholder="eyes, rest, task"><label class="card-subtitle" for="context-notes">Заметки</label><textarea id="context-notes" maxlength="3000" rows="2" style="width:100%;resize:vertical;background:#0c1623;border:1px solid var(--line);border-radius:8px;padding:10px 12px;color:var(--text);font:inherit;font-size:11px;margin:5px 0 9px" placeholder="Сон, кофеин, движения, особенности контакта…"></textarea><div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><button class="button subtle" type="submit">Сохранить контекст</button><span id="context-state" class="card-subtitle" role="status">Загрузка…</span></div><p class="chart-caption">Это заметки после записи. Они не заменяют синхронные метки событий и не доказывают причин изменений.</p></form>`;
  }
  async function loadContext() {
    if (preview) return;
    const id=state.current;
    try {
      const result=await api(`/api/lab/context/${encodeURIComponent(id)}`);
      if(state.current!==id||!$('#context-form'))return;
      const c=result.context||result; state.context=c;
      $('#eye-state').value=c.eye_state||'unknown'; $('#participant-code').value=c.participant_code||''; $('#protocol-id').value=c.protocol_id||''; $('#context-task').value=c.task||''; $('#context-notes').value=c.notes||''; $('#context-state').textContent=c.updated_at?'Контекст сохранён':'Контекст не указан';
    } catch (_) { if($('#context-state'))$('#context-state').textContent='Можно добавить контекст'; }
    $('#context-form')?.addEventListener('submit',async e=>{
      e.preventDefault();const contextId=state.current;const button=$('button',e.target);button.disabled=true;$('#context-state').textContent='Сохраняем…';
      try { await api(`/api/lab/context/${encodeURIComponent(contextId)}`,{method:'PATCH',body:JSON.stringify({eye_state:$('#eye-state').value,task:$('#context-task').value,notes:$('#context-notes').value,participant_code:$('#participant-code').value,protocol_id:$('#protocol-id').value})});if(state.current===contextId&&$('#context-state'))$('#context-state').textContent='Сохранено'; }
      catch(error){if(state.current===contextId&&$('#context-state')){$('#context-state').textContent='Ошибка сохранения';notify(error.message,'error');}}
      finally{button.disabled=false;}
    });
  }

  function advancedCard(c) {
    const a=c.advanced||{};
    const content=a.available?`<dl class="signal-stat-row"><div><dt>Hjorth mobility</dt><dd>${fmt(a.hjorth_median?.mobility,3)}</dd></div><div><dt>Hjorth complexity</dt><dd>${fmt(a.hjorth_median?.complexity,3)}</dd></div><div><dt>Наклон log-PSD</dt><dd>${fmt(a.log_spectral_slope,3)}</dd></div><div><dt>R² аппроксимации</dt><dd>${fmt(a.log_spectral_r2,3)}</dd></div></dl><p class="chart-caption">${esc(a.slope_method||'')} ${esc(a.notice||'')}</p>`:
      `<p>${esc(a.reason||'Недостаточно надёжных участков для расширенных метрик.')}</p>`;
    return `<section class="card">${cardHeader('Дополнительные исследовательские метрики','Hjorth и описательный наклон log-PSD, не параметрическое разделение specparam')}${content}</section>`;
  }

  function renderSignals(a) {
    const cs=arr(a.eeg?.channels), c=cs[state.channel]||{}, p=a.ppg||{}, prv=p.prv||{}, motion=a.motion||{}, impedance=a.impedance||{};
    const controls=`<div class="control-row"><label for="band-channel">Канал ЭЭГ <select class="select" id="band-channel">${channelOptions(cs)}</select></label><span class="spacer"></span><label class="toggle"><input type="checkbox" id="raw-toggle" ${state.raw?'checked':''}>Исходный спектр</label><label class="toggle"><input type="checkbox" id="log-toggle" ${state.log?'checked':''}>Логарифмическая шкала</label></div>`;
    $('#content').innerHTML=`${notice('<strong>Единицы ЭЭГ:</strong> значения сохранены в формате SDK. Без подтверждённого масштаба их нельзя подписывать как микровольты. Фильтр отмечает подозрительные окна; он не гарантирует устранение всех артефактов.')} ${a.eeg?.available?controls:''}<div class="content-grid even"><section class="card">${cardHeader('Спектр мощности','Метод Welch; отобранные и, по желанию, исходные окна')}${a.eeg?.available?`${legend(cs)}<div class="chart-shell"><canvas id="signal-spectrum" aria-label="Спектр мощности каналов ЭЭГ" role="img"></canvas></div><p class="chart-caption">Сплошная линия — отобранные окна. Пунктир — исходные окна при включённом переключателе. Отсутствие сплошной линии означает отсутствие пригодной оценки.</p>`:empty('ЭЭГ не записана','Для этой сессии анализ электрической активности недоступен.')}</section><section class="card">${cardHeader(`Метрики канала ${c.name||''}`,'Описательные показатели сигнала')}${a.eeg?.available?channelStats(c):empty('Нет показателей канала','Пульс и движение не заменяют электрическую запись.')}</section><section class="card full">${cardHeader('Фрагмент исходной ЭЭГ',c.excerpt?.selection||'Сохранённые значения без изменения масштаба',tag(c.name||'Нет ЭЭГ','blue'))}${a.eeg?.available&&arr(c.excerpt?.values).length?`<div class="chart-shell small"><canvas id="raw-eeg" aria-label="Фрагмент исходной ЭЭГ" role="img"></canvas></div><p class="chart-caption">Фрагмент служит визуальной проверке формы сигнала. Он не является всей записью; скачайте исходные файлы через управление записями.</p>`:empty('Нет фрагмента ЭЭГ','Исходные отсчёты отсутствуют или недоступны.')}</section><section class="card full">${cardHeader('Динамика ритмов','Доли спектральной мощности в отобранных окнах',`<label for="timeline-band" class="screen-reader">Диапазон частот</label><select id="timeline-band" class="select">${bandDefs.map(b=>`<option value="${b.id}" ${b.id==='alpha'?'selected':''}>${b.name} · ${b.range}</option>`).join('')}</select>`)}${a.eeg?.available?`${legend(cs)}<div class="chart-shell small"><canvas id="signals-timeline" aria-label="Изменение доли мощности выбранного диапазона" role="img"></canvas></div>`:empty('Нет временного ряда ЭЭГ','Ритмы требуют сохранённой ЭЭГ.')}</section><section class="card">${cardHeader('Оптический пульс','PPG: пульсовая волна и качество её регистрации',tag('PPG','blue'))}${p.available?`<div class="chart-shell small"><canvas id="ppg-excerpt" aria-label="Фрагмент оптического пульсового сигнала" role="img"></canvas></div><dl class="signal-stat-row"><div><dt>ПУЛЬС</dt><dd>${fmt(p.pulse_rate_bpm,1)} <small>уд/мин</small></dd></div><div><dt>ПРОШЛО ФИЛЬТР</dt><dd>${percent(p.quality?.usable_pct)}</dd></div><div><dt>ПРИНЯТО ОКОН</dt><dd>${integer(p.quality?.accepted_windows)} <small>/ ${integer(p.quality?.total_windows)}</small></dd></div></dl><details class="details-block"><summary>Пульсовая вариабельность · PRV</summary><dl class="signal-stat-row"><div><dt>RMSSD</dt><dd>${fmt(prv.rmssd_ms,1)} <small>мс</small></dd></div><div><dt>SDNN</dt><dd>${fmt(prv.sdnn_ms,1)} <small>мс</small></dd></div><div><dt>ИНТЕРВАЛЫ</dt><dd>${integer(prv.n_intervals)}</dd></div></dl><p class="chart-caption">Блок PRV: ${esc(duration(prv.duration_s))}; начало ${fmt(prv.block_start_s,1)} с, конец ${fmt(prv.block_end_s,1)} с. Пригодные интервалы: ${esc(duration(prv.valid_interval_duration_s))}.</p><p class="chart-caption">${esc(prv.description||'Метрики по пульсовым интервалам. PPG не эквивалентна ЭКГ; движение и ошибки обнаружения пиков искажают вариабельность.')}</p></details>`:empty('Нет PPG','В этой записи отсутствует оптический сигнал.','heart')}</section><section class="card">${cardHeader('Пульс во времени','Только окна, принятые проверкой качества')}${p.available?`<div class="chart-shell small"><canvas id="ppg-timeline" aria-label="Частота пульса во времени" role="img"></canvas></div><p class="chart-caption">Разрывы — невалидные или недостаточные фрагменты. Изменения пульса неспецифичны: на них влияют движение, дыхание и множество других факторов.</p>`:empty('Нет динамики PPG','Частота пульса недоступна.','heart')}</section><section class="card">${cardHeader('Движение','Датчики MEMS помогают заметить возможные артефакты')}${motion.available?`<div class="chart-shell small"><canvas id="motion-chart" aria-label="Динамика величины вращения по гироскопу" role="img"></canvas></div><p class="chart-caption">${esc(motion.summary?.units_note||'Единицы сохранены из SDK. Величина движения — контекст для качества сигнала, не показатель работы мозга.')}</p><dl class="signal-stat-row"><div><dt>ОКНА С ДВИЖЕНИЕМ</dt><dd>${percent(motion.summary?.movement_windows_pct)}</dd></div><div><dt>ПОРОГ ЭВРИСТИКИ</dt><dd>${fmt(motion.summary?.movement_threshold,2)}</dd></div></dl>`:empty('Нет движения','Данные MEMS отсутствуют.')}</section><section class="card">${cardHeader('Контакт электродов','Распределение сопротивления по каналам')}${impedance.available?`<div class="table-wrap"><table class="data-table"><thead><tr><th>КАНАЛ</th><th>МЕДИАНА</th><th>P95</th><th>&gt; 1 МОм</th></tr></thead><tbody>${arr(impedance.channels).map(ch=>`<tr><td>${esc(ch.name)}</td><td>${fmt(num(ch.median_ohm)===null?null:ch.median_ohm/1000,0)} кОм</td><td>${fmt(num(ch.p95_ohm)===null?null:ch.p95_ohm/1000,0)} кОм</td><td>${percent(ch.above_1m_pct)}</td></tr>`).join('')}</tbody></table></div><div class="chart-shell small"><canvas id="impedance-chart" aria-label="Сопротивление электродов во времени" role="img"></canvas></div><p class="chart-caption">${esc(impedance.threshold_note||'Пороги проекта не являются клинической нормой. Связь сопротивления с качеством зависит от схемы усилителя и электродов.')}</p>`:empty('Нет сопротивлений','Контакт электродов в этой сессии не измерен.')}</section><section class="card full">${cardHeader('Сравнение двух ЭЭГ-каналов · связность','Визуализация 3D-монтажа и проверка парных спектров')}${window.NeuroPairedPanel?.markup()||''}</section>${advancedCard(c)}${qualityCard(a,true)}<section class="card">${cardHeader('Ограничения интерпретации','Что следует учитывать перед любым выводом')}<div class="text-content"><ul>${arr(a.limitations).map(x=>`<li>${esc(textOf(x))}</li>`).join('')||'<li>Смотрите научную базу и протокол записи для требований к интерпретации.</li>'}</ul></div></section><section class="card full">${cardHeader('Оценки SDK','Отдельный источник: показатели производителя не равны доказанным состояниям мозга')}${vendorSummary(a.vendor||{})}</section><section class="card full">${cardHeader('Воспроизводимость и метод','Параметры анализа и происхождение данных')}${methodSummary(a)}</section></div>`;
    window.NeuroPairedPanel?.attach($('#content [data-pair-panel]'),a.paired_eeg);
    drawSpectrum(a,'signal-spectrum',state.raw);drawTimeline(a,'signals-timeline');
    chart('raw-eeg',[dataset(c.name||'ЭЭГ',points(c.excerpt?.time_s,c.excerpt?.values),colors[state.channel])],{ytitle:cleanUnits(a.eeg?.units),digits:4});
    chart('ppg-excerpt',[dataset('PPG',points(p.excerpt?.time_s,p.excerpt?.values),colors[2])],{ytitle:'Единицы SDK',digits:0});
    chart('ppg-timeline',[dataset('Пульс',arr(p.timeline).map(w=>({x:Number(w.time_s),y:w.usable?num(w.pulse_rate_bpm):null})),colors[2])],{ytitle:'уд/мин',digits:1});
    chart('motion-chart',[dataset('Гироскоп · RMS',arr(motion.timeline).map(w=>({x:Number(w.time_s),y:num(w.gyro_rms)})),colors[3])],{ytitle:'Единицы SDK',digits:2});
    chart('impedance-chart',arr(impedance.channels).map((ch,i)=>dataset(ch.name,arr(ch.timeline).map(w=>({x:Number(w.time_s),y:num(w.value_ohm)===null?null:w.value_ohm/1000})),colors[i])),{ytitle:'кОм',digits:0});
    bindContent();bindChannel();
    $('#raw-toggle')?.addEventListener('change',e=>{state.raw=e.target.checked;destroyCharts();renderSignals(a);});
    $('#log-toggle')?.addEventListener('change',e=>{state.log=e.target.checked;destroyCharts();renderSignals(a);});
    $('#timeline-band')?.addEventListener('change',e=>{const existing=state.charts.find(c=>c.canvas.id==='signals-timeline');if(existing){existing.destroy();state.charts=state.charts.filter(c=>c!==existing);}drawTimeline(a,'signals-timeline',e.target.value);});
  }
  function channelStats(c) {
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>ДИАПАЗОН</th><th>ДОЛЯ ПОСЛЕ ОТБОРА</th><th>ИСХОДНАЯ ДОЛЯ</th></tr></thead><tbody>${bandDefs.map(b=>`<tr><td>${b.name}<span class="subcell">${b.range}</span></td><td>${num(c.relative_power?.[b.id])===null?'—':percent(c.relative_power[b.id]*100)}</td><td>${num(c.relative_power_raw?.[b.id])===null?'—':percent(c.relative_power_raw[b.id]*100)}</td></tr>`).join('')}</tbody></table></div><dl class="signal-stat-row"><div><dt>АЛЬФА-ПИК</dt><dd>${fmt(c.alpha_peak_hz,2)} <small>Гц</small></dd></div><div><dt>СПЕКТР. ЭНТРОПИЯ</dt><dd>${fmt(c.entropy,3)}</dd></div><div><dt>ПРИНЯТЫЕ ОКНА</dt><dd>${percent(c.quality?.usable_pct)}</dd></div></dl><p class="chart-caption">${esc(c.alpha_peak?.description||'Альфа-пик — спектральный кандидат. Энтропия описывает распределение мощности, а не сложность мышления.')}</p>`;
  }
  function vendorSummary(v) {
    const streams=values(v.streams);
    if(!streams.length)return empty('Оценки SDK отсутствуют','В записи нет соответствующих строк метрик.');
    return `${notice('«Эмоции», «продуктивность» и другие метки SDK показаны как сохранённые выходы алгоритмов производителя. По этим данным не подтверждаются психологические состояния или медицинские диагнозы.')}<div class="vendor-fields">${streams.map(s=>`<details><summary>${esc(s.name||s.stream)} · ${integer(s.count)} строк · повторных значений ${percent(s.duplicate_pct)}</summary><div class="table-wrap"><table class="data-table"><thead><tr><th>ПОЛЕ SDK</th><th>МЕДИАНА</th><th>P05–P95</th><th>ДИАПАЗОН</th></tr></thead><tbody>${Object.entries(s.fields||{}).map(([key,x])=>`<tr><td class="mono">${esc(key)}</td><td>${fmt(x.median,3)}</td><td>${fmt(x.p05,3)} — ${fmt(x.p95,3)}</td><td>${fmt(x.min,3)} — ${fmt(x.max,3)}</td></tr>`).join('')}</tbody></table></div></details>`).join('')}</div>${v.calibration?`<details class="details-block"><summary>Сохранённая калибровка SDK</summary><pre class="mono break-text" style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(JSON.stringify(v.calibration,null,2))}</pre><p class="chart-caption">Частота SDK и повторная спектральная оценка могут отличаться из-за метода, временного участка и качества. Это не изменение способностей человека.</p></details>`:''}`;
  }
  function methodSummary(a) {
    const methods=a.methods||{};
    return `<div class="text-content"><p><strong>Версия:</strong> ${esc(a.analysis_version||'не указана')} · <strong>Схема:</strong> ${esc(a.schema_version||'не указана')}</p><dl class="compact-kv" style="grid-template-columns:minmax(110px,.3fr) 1fr">${Object.entries(methods).map(([key,value])=>`<dt>${esc(key)}</dt><dd style="text-align:left">${esc(textOf(value)||String(value))}</dd>`).join('')}</dl></div><details class="details-block"><summary>Происхождение и контрольные суммы</summary><pre class="mono break-text" style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(JSON.stringify(a.provenance||{},null,2))}</pre></details>`;
  }

  function renderComparison(list) {
    const analyses=list.filter(Boolean);
    const row=(title,fn,note='')=>`<tr><td>${esc(title)}${note?`<span class="subcell">${esc(note)}</span>`:''}</td>${list.map(a=>`<td>${a?fn(a):'Недоступно'}</td>`).join('')}</tr>`;
    const channelLines=(a, fn)=>arr(a.eeg?.channels).map(c=>`<span class="subcell">${esc(c.name)}: <span style="color:var(--text)">${fn(c)}</span></span>`).join('')||'Нет ЭЭГ';
    $('#content').innerHTML=`${notice('<strong>Сравнение описательное.</strong> Разные даты, продолжительность, контакт электродов и неразмеченные состояния глаз не позволяют приписать различия обучению, восстановлению или изменениям здоровья.','warning')}<section class="card">${cardHeader('Сессии рядом','Сравнивайте наличие и качество данных до сравнения ритмов')}<div class="table-wrap"><table class="data-table comparison-table"><thead><tr><th>ПОКАЗАТЕЛЬ</th>${state.sessions.map(s=>`<th>${esc(dateLabel(s.started_at,true))}<span class="subcell" style="display:block;margin-top:5px;color:var(--soft)">Москва · UTC+3</span></th>`).join('')}</tr></thead><tbody>${row('Наличие ЭЭГ',a=>tag(a.eeg?.available?'Два канала':'Нет ЭЭГ',a.eeg?.available?'good':''))}${row('Номинальная длительность',a=>`<span class="value-emphasis">${esc(duration(a.session?.duration_s))}</span>`,'По метаданным анализа')}${row('Временной охват',a=>esc(duration(a.session?.wall_duration_s)),'По временным меткам')}${row('Окна ЭЭГ без флагов',a=>percent(a.quality?.usable_eeg_pct),'Эвристический отбор')}${row('Альфа-пик · кандидат',a=>channelLines(a,c=>`${fmt(c.alpha_peak_hz,2)} Гц`))}${row('Доля альфа 8–13 Гц',a=>channelLines(a,c=>num(c.relative_power?.alpha)===null?'—':percent(c.relative_power.alpha*100)),'Отобранные окна, от 1–45 Гц')}${row('Спектральная энтропия',a=>channelLines(a,c=>fmt(c.entropy,3)),'Без психологической трактовки')}${row('Частота пульса',a=>`${fmt(a.ppg?.pulse_rate_bpm,1)} уд/мин`,'PPG; при пройденной проверке')}${row('Окна PPG без флагов',a=>percent(a.ppg?.quality?.usable_pct))}${row('Движение',a=>percent(a.motion?.summary?.movement_windows_pct),'Доля окон по эвристике')}${row('Сопротивление · медиана',a=>arr(a.impedance?.channels).map(c=>`<span class="subcell">${esc(c.name)}: ${fmt(num(c.median_ohm)===null?null:c.median_ohm/1000,0)} кОм</span>`).join('')||'Нет измерений')}${row('Статус качества',a=>{const q=statusInfo(a.quality?.status);return tag(q.label,q.cls);})}</tbody></table></div><p class="table-caption">Порог движения определяется отдельно для каждой сессии. Доли отмеченных окон не являются общей шкалой двигательной активности.</p></section><div class="content-grid wide-gap"><section class="card">${cardHeader('Спектры O1–T3','Отобранные окна; амплитуды в исходном масштабе SDK')}<div class="legend">${analyses.filter(a=>a.eeg?.available).map((a,i)=>`<span><i style="background:${colors[i]}"></i>${esc(dateLabel(a.session?.started_at))}</span>`).join('')}</div><div class="chart-shell"><canvas id="comparison-spectrum" aria-label="Сравнение спектров канала O1–T3 между сессиями" role="img"></canvas></div><p class="chart-caption">Мощность может меняться из-за контакта и артефактов. Совпадение аппаратуры не делает условия записи одинаковыми.</p></section><section class="card">${cardHeader('Что улучшит сравнимость','Рекомендации для следующих сессий')}<div class="text-content"><ol><li>Повторяйте короткий протокол в одинаковой позе и в сходное время суток.</li><li>Отдельно размечайте открытые и закрытые глаза; записывайте движения и задачу.</li><li>Сохраняйте качество контакта и фиксируйте сон, кофеин и самочувствие.</li><li>Сравнивайте сопоставимые по длительности чистые участки, а не только средние всей сессии.</li></ol></div><a class="button text" href="#protocol" data-view="protocol">Открыть протокол ${icon('arrow')}</a></section></div>`;
    chart('comparison-spectrum',analyses.filter(a=>a.eeg?.available).map((a,i)=>{const c=a.eeg.channels?.[0]||{};return dataset(dateLabel(a.session?.started_at),points(c.psd?.freq_hz,c.psd?.power,true),colors[i]);}),{frequency:true,xmin:1,xmax:45,log:true,ytitle:'ед. SDK²/Гц',digits:5});bindContent();
  }

  function sourceLinks(ids, sources) {
    return `<div class="source-links">Основания ${arr(ids).map(id=>{const n=sources.findIndex(s=>String(s.id)===String(id));return n<0?'':`<a href="#source-${esc(id)}" data-source-scroll="source-${esc(id)}" title="${esc(sources[n].title)}">${n+1}</a>`;}).join('')}</div>`;
  }
  function metricAvailability(m) {
    const implemented=new Set(['completeness','precision','quality','psd','bandpower','relative_power','paf','spectral_entropy','motion','impedance','pulse','prv_rmssd','prv_sdnn','sdk_indices']);
    if (m.status==='unavailable') return tag('Недоступно по этим данным','');
    if (m.status==='requires_protocol') return tag('Нужна новая запись с протоколом','warning');
    if (!implemented.has(m.id)) return tag('Направление развития · пока не рассчитывается','warning');
    return tag('Рассчитывается при достаточном качестве','blue');
  }
  function renderKnowledge(k) {
    const sections=arr(k.sections),sources=arr(k.sources),metrics=arr(k.metrics);
    $('#content').innerHTML=`<section class="knowledge-hero"><div><div class="section-kicker">НАУЧНАЯ ОСНОВА</div><h2>Мозг сложнее одной метрики.</h2><p>Здесь собраны объяснения того, что измеряет ЭЭГ, какие показатели можно вычислить и какие условия нужны для их интерпретации. Каждое важное утверждение можно проверить по источнику.</p></div><div class="knowledge-count"><strong>${sources.length}</strong><span>источников в научной базе</span></div></section><nav class="knowledge-nav" aria-label="Разделы научной базы">${sections.map(s=>`<a href="#knowledge-${esc(s.id)}" data-section-scroll="knowledge-${esc(s.id)}">${esc(s.title)}</a>`).join('')}<a href="#knowledge-metrics" data-section-scroll="knowledge-metrics">Справочник метрик</a><a href="#knowledge-sources" data-section-scroll="knowledge-sources">Источники</a></nav><div class="content-grid even">${sections.map(s=>`<section class="card knowledge-section" id="knowledge-${esc(s.id)}">${cardHeader(s.title,s.lead||s.summary||'')}<div class="text-content">${arr(s.paragraphs).length?arr(s.paragraphs).map(p=>`<p>${esc(p)}</p>`).join(''):String(s.body||'').split(/\n\s*\n/).filter(Boolean).map(p=>`<p>${esc(p)}</p>`).join('')}${arr(s.bullets||s.items).length?`<ul>${arr(s.bullets||s.items).map(x=>`<li>${esc(textOf(x))}</li>`).join('')}</ul>`:''}</div>${sourceLinks(s.sources||s.source_ids,sources)}</section>`).join('')}<section class="card full knowledge-section" id="knowledge-metrics">${cardHeader('Справочник метрик','Каталог возможностей: часть метрик требует нового протокола или дополнительной валидации')}<div class="metric-library">${metrics.map(m=>`<article class="knowledge-metric"><h3>${esc(m.name||m.title)}</h3>${metricAvailability(m)}<p style="margin-top:9px">${esc(m.meaning||m.description)}</p>${m.evidence?tag(textOf(m.evidence),'blue'):''}<details><summary>Условия и ограничения</summary>${arr(m.requirements).length?`<p style="margin-top:10px"><strong>Что нужно</strong></p><ul>${arr(m.requirements).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${arr(m.limitations).length?`<p><strong>Что мешает интерпретации</strong></p><ul>${arr(m.limitations).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</details>${sourceLinks(m.source_ids||m.sources,sources)}</article>`).join('')||'<p class="card-subtitle">Справочник доступен в полном отчёте проекта.</p>'}</div></section><section class="card full knowledge-section" id="knowledge-sources">${cardHeader('Источники и доказательства','Первичные публикации, документация и методологические рекомендации')}<label class="screen-reader" for="source-search">Найти источник</label><input class="source-filter" id="source-search" type="search" placeholder="Поиск по названию, авторам или теме…"><ol class="sources-list">${sources.map((s,i)=>`<li class="source-item" id="source-${esc(s.id)}" data-search="${esc(`${s.title} ${s.authors} ${s.claim} ${s.year}`.toLowerCase())}"><span class="source-index">${String(i+1).padStart(2,'0')}</span><div><a href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ${icon('link','small')}</a><small>${esc(Array.isArray(s.authors)?s.authors.join(', '):s.authors||'')} ${s.year?`· ${esc(s.year)}`:''}${s.doi?` · DOI: ${esc(s.doi)}`:''}${s.accessed_at?` · Проверено ${esc(s.accessed_at)}`:''}</small>${s.claim?`<p>${esc(s.claim)}</p>`:''}</div></li>`).join('')}</ol><p id="sources-empty" class="card-subtitle" hidden>Источников по запросу не найдено.</p></section></div>`;
    bindContent();
    $$('[data-section-scroll],[data-source-scroll]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();const target=document.getElementById(a.dataset.sectionScroll||a.dataset.sourceScroll);target?.scrollIntoView({behavior:'smooth',block:'start'});}));
    $('#source-search')?.addEventListener('input',e=>{const term=e.target.value.trim().toLowerCase();let visible=0;$$('.source-item').forEach(s=>{s.hidden=!s.dataset.search.includes(term);if(!s.hidden)visible++;});$('#sources-empty').hidden=visible>0;});
  }
  function renderProtocol(k) {
    const protocols=arr(k.protocol||k.protocols);
    $('#content').innerHTML=`<section class="knowledge-hero"><div><div class="section-kicker">ПЛАН НА СЛЕДУЮЩУЮ СЕССИЮ</div><h2>Хороший анализ начинается<br>с хорошей записи.</h2><p>Зафиксируйте условия, разметьте события и оставьте достаточно неподвижных фрагментов. Это повышает полезность показателей и делает сравнение между днями осмысленнее.</p></div><div class="knowledge-count">${icon('checklist')}<span>Контекст + качество + повторение</span></div></section><div class="protocol-grid"><div class="protocol-list">${protocols.map(p=>`<section class="card protocol-card"><h2>${esc(p.title)}</h2><div class="text-content"><ol>${arr(p.steps).map(s=>`<li>${esc(textOf(s))}</li>`).join('')}</ol>${arr(p.notes).map(n=>`<p>${esc(n)}</p>`).join('')}</div></section>`).join('')||'<section class="card"><h2>Подготовка</h2><p class="text-content">Сядьте удобно, проверьте контакт электродов, фиксируйте состояния глаз и события. Подробный протокол приведён в отчёте.</p></section>'}</div><aside class="card sticky-card">${cardHeader('Перед началом','Чек-лист для текущего сеанса')}<div id="protocol-checklist">${['Проверен контакт обоих каналов','Позу и положение повязки удобно сохранять','Указаны задача и состояния глаз','Записаны время, сон, кофеин и контекст','Подготовлены метки переходов и движений','Подтверждено, что сохраняется поток ЭЭГ'].map((text,i)=>`<label class="check-row"><input type="checkbox" data-check="${i}"><span>${esc(text)}</span></label>`).join('')}</div><p id="checklist-progress" class="chart-caption">Выполнено 0 из 6</p><a class="button primary" href="#live" data-view="live" style="width:100%;margin-top:10px">Открыть живой монитор ${icon('arrow')}</a><p class="chart-caption">Чек-лист не запускает запись. Управление устройством и записью находится в живом мониторе.</p></aside></div>${arr(k.limitations).length?`<section class="card section-spaced">${cardHeader('Пределы этого протокола','Даже качественная запись не отвечает на любой вопрос')}<div class="text-content"><ul>${arr(k.limitations).map(x=>`<li>${esc(textOf(x))}</li>`).join('')}</ul></div></section>`:''}`;
    $$('#protocol-checklist input').forEach(input=>input.addEventListener('change',()=>{$('#checklist-progress').textContent=`Выполнено ${$$('#protocol-checklist input:checked').length} из 6`;}));bindContent();
  }
  function bindContent() { $$('#content [data-view]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();location.hash=a.dataset.view;})); }
  function closeMenu() { $('#sidebar').classList.remove('open');$('#menu-button').setAttribute('aria-expanded','false'); }

  async function start() {
    if (preview) {
      $('.sidebar-foot').innerHTML='<span class="local-dot"></span><div>Без подключения к сети<small>Результаты встроены в этот файл</small></div>';
      $('#refresh-button').hidden=true;
      $('.export-dropdown a[href="/api/lab/report"]').href='docs/NeuroScope_Research_Report.pdf';
      $('.brand').href='#overview';
    }
    window.NeuroLive?.init({api, notify, preview:!!preview, selectSession, reloadSessions: async ()=>{const r=await api('/api/lab/sessions');state.sessions=arr(r.sessions||r);renderSessions();}});
    $$('.nav-item[data-view], .top-help[data-view]').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();if(location.hash.slice(1)===a.dataset.view)nav(a.dataset.view);else location.hash=a.dataset.view;}));
    window.addEventListener('hashchange',()=>{if(titles[location.hash.slice(1)])nav(location.hash.slice(1));});
    $('#menu-button').addEventListener('click',()=>{const open=$('#sidebar').classList.toggle('open');$('#menu-button').setAttribute('aria-expanded',String(open));});
    $('#sidebar-scrim').addEventListener('click',closeMenu);
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeMenu();$('#export-menu').open=false;}});
    document.addEventListener('click',e=>{if(!$('#export-menu').contains(e.target))$('#export-menu').open=false;});
    $('#refresh-button').addEventListener('click',async()=>{
      if(!state.current)return;const analysisId=state.current;const button=$('#refresh-button');button.disabled=true;button.innerHTML=`${icon('refresh')}<span>Анализируем…</span>`;notify('Пересчитываем показатели из исходных файлов. Это может занять некоторое время.');
      try {const result=await api(`/api/lab/analysis/${encodeURIComponent(analysisId)}/refresh`,{method:'POST'});state.cache.delete(analysisId);if(result.eeg||result.analysis)state.cache.set(analysisId,result.analysis||result);if(state.current===analysisId){await render();notify('Анализ пересчитан из исходных данных.','success');}else notify('Пересчёт предыдущей сессии завершён.','success');}
      catch(error){notify(error.message,'error');}finally{button.disabled=false;button.innerHTML=`${icon('refresh')}<span>Пересчитать</span>`;}
    });
    try {
      const result=await api('/api/lab/sessions');state.sessions=arr(result.sessions||result).filter(s=>s&&s.id);
      let saved=null;try{saved=localStorage.getItem('neuroscope.session');}catch(_){}
      const query=new URLSearchParams(location.search).get('session');
      state.current=state.sessions.find(s=>s.id===(query||saved))?.id||state.sessions[0]?.id||null;
      state.view=titles[location.hash.slice(1)]?location.hash.slice(1):'overview';renderSessions();nav(state.view);
    } catch(error) {
      $('#content').innerHTML=`<div class="error-panel"><h2>Список записей недоступен</h2><p>${esc(error.message)}</p><button class="button primary" id="initial-retry">Повторить загрузку</button></div>`;
      $('#initial-retry')?.addEventListener('click',()=>location.reload());
    }
  }
  start();
})();

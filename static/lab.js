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
    device: ['Подключение повязки', 'Найдите устройство и проверьте контакт электродов перед записью.'],
    live: ['Живой монитор', 'Следите за потоками ЭЭГ, пульса и движения и отмечайте события во время записи.'],
    sessions: ['Архив записей', 'Откройте запись для анализа, воспроизведите её или скачайте исходные данные.'],
    experiments: ['Эксперименты', 'Выберите протокол для повязки или откройте журнал прошедших экспериментов.'],
    'timed-protocol': ['Протоколы с таймером', 'Настройте длительность этапов покоя, задачи и восстановления.'],
    segment: ['Анализ фрагмента', 'Выберите участок записи для детального просмотра, аннотации и экспорта.'],
    history: ['История исследований', 'Просмотрите условия записи и сопоставимые сессии одного участника.'],
    overview: ['Обзор записи', 'Качество сигнала и основные показатели выбранной сессии.'],
    signals: ['Сигналы и метрики', 'Исходные сигналы, спектры и результаты расчётов по каналам.'],
    compare: ['Сравнение записей', 'Сопоставьте спектры и показатели с учётом качества, длительности и условий записи.'],
    knowledge: ['Как это работает', 'Какие сигналы измеряет повязка и как интерпретировать их по научным источникам.'],
    protocol: ['Протокол записи', 'Подготовьте повязку, зафиксируйте условия и наметьте этапы сессии.']
  };
  const state = {sessions:[], current:null, analysis:null, analysisPending:new Map(), knowledge:null, view:'overview', charts:[], channel:0, raw:false, log:true, renderId:0, context:null};

  function duration(v) {
    if (num(v) === null) return '—';
    const n = Math.max(0, Math.round(Number(v))), h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
    return h ? `${h} ч ${m} мин` : m ? `${m} мин ${s.toString().padStart(2,'0')} с` : `${s} с`;
  }
  const dateOptions = {day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow'};
  const shortDate = new Intl.DateTimeFormat('ru-RU', {...dateOptions, month: 'short'});
  const longDate = new Intl.DateTimeFormat('ru-RU', {...dateOptions, month: 'long', year: 'numeric'});

  function dateLabel(v, long = false) {
    if (!v) return 'Дата не указана';
    const d = new Date(v);
    if (!Number.isFinite(d.getTime())) return String(v || 'Дата не указана');
    return (long ? longDate : shortDate).format(d).replace(' г.', '');
  }
  function statusInfo(v) {
    const key = String(v?.status ?? v ?? '').toLowerCase();
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
  function analysisFor(id) {
    if (state.analysisPending.has(id)) return state.analysisPending.get(id);
    const pending = api(`/api/lab/analysis/${encodeURIComponent(id)}`)
      .then(result => result.analysis || result)
      .finally(() => state.analysisPending.delete(id));
    state.analysisPending.set(id, pending);
    return pending;
  }
  async function reloadSessions(renderId = state.renderId) {
    const result = await api('/api/lab/sessions');
    if (renderId !== state.renderId) return;
    state.sessions = arr(result.sessions || result).filter(session => session?.id);
    if (!state.sessions.some(session => session.id === state.current)) {
      state.current = (state.sessions.find(session => !session.active) || state.sessions[0])?.id || null;
    }
    renderSessions();
  }
  async function knowledgeData() {
    if (state.knowledge) return state.knowledge;
    state.knowledge = await api('/api/lab/knowledge'); return state.knowledge;
  }
  function destroyCharts() {
    window.NeuroBrainMap.unmount($('#content [data-pair-map]'));
    state.charts.forEach(c => c.destroy());
    state.charts = [];
  }
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
    const panel = $('#session-selector');
    const hadFocus = panel.contains(document.activeElement);
    panel.hidden = !state.sessions.length || !['overview', 'signals'].includes(state.view);
    const options = state.sessions.map(session => {
      const date = dateLabel(session.started_at, true);
      const length = duration(session.wall_duration_s ?? session.duration_s);
      const label = `${date} · ${length} · ${session.active ? 'Идёт запись' : session.id}`;
      return `<option value="${esc(session.id)}">${esc(label)}</option>`;
    });
    panel.innerHTML = `<label class="record-picker" for="analysis-session">
      <span>${icon('folder')} Запись для анализа <small>МСК</small></span>
      <select id="analysis-session" class="select">${options.join('')}</select>
    </label>`;
    const select = $('#analysis-session');
    select.value = state.current || '';
    select.addEventListener('change', () => selectSession(select.value));
    if (hadFocus && !panel.hidden) select.focus({preventScroll:true});
  }
  function updateExports() {
    ['html','json','csv'].forEach(format => { $(`#export-${format}`).href = `/api/lab/analysis/${encodeURIComponent(state.current || '')}/export?format=${format}`; });
    if (preview && state.analysis) previewExports(state.analysis);
    $('#analysis-version').textContent = state.analysis?.analysis_version ? `Анализ ${state.analysis.analysis_version}` : 'Воспроизводимый анализ';
    $('#analysis-actions').hidden = ['knowledge','protocol','compare','device','live','sessions','experiments','timed-protocol','segment','history'].includes(state.view)||!state.current||!state.analysis;
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
  function nav(view, refreshList = true) {
    if (!titles[view]) view = 'overview';
    state.view = view;
    const [title, subtitle] = titles[view];
    document.title = `${title} · NeuroScope`;
    $('#page-title').textContent = title;
    $('#page-subtitle').textContent = subtitle;
    const section = view === 'timed-protocol' ? 'experiments' : view;
    for (const link of $$('.nav-item[data-view]')) {
      const active = link.dataset.view === section;
      link.classList.toggle('active', active);
      if (active) {
        link.setAttribute('aria-current', view === section ? 'page' : 'location');
        $('#breadcrumb-current').textContent = link.closest('nav').querySelector('h2').textContent;
      } else {
        link.removeAttribute('aria-current');
      }
    }
    $('#session-selector').hidden = !state.sessions.length || !['overview', 'signals'].includes(view);
    updateExports();
    setMenuOpen(false);
    if (refreshList) {
      $('#page-title').focus({preventScroll: true});
      window.scrollTo({top: 0, behavior: 'instant'});
    }
    render({refreshList});
  }
  async function selectSession(id) {
    state.current = id; state.context = null; renderSessions();
    try { localStorage.setItem('neuroscope.session',id); } catch (_) {}
    if (['overview','signals','compare'].includes(state.view)) await render();
  }
  async function loadComparison(renderId) {
    const sessions = state.sessions.slice();
    const total = sessions.filter(session => !session.active).length;
    if (!total) {
      $('#content').innerHTML = empty('Нет завершённых записей', 'Завершённые сессии появятся здесь после сохранения.', 'folder');
      return;
    }
    $('#content').innerHTML = `<div class="loading-panel"><span class="loader" aria-hidden="true"></span>
      <h2>Готовим сравнение</h2><p id="compare-progress" role="status"></p></div>`;
    const progress = $('#compare-progress');
    const results = [];
    let completed = 0;
    for (const session of sessions) {
      if (renderId !== state.renderId) return;
      if (session.active) {
        results.push(null);
        continue;
      }
      progress.textContent = `Запись ${completed + 1} из ${total} · ${dateLabel(session.started_at)}`;
      try {
        results.push(await analysisFor(session.id));
      } catch (_) {
        results.push(null);
      }
      completed++;
    }
    if (renderId === state.renderId) renderComparison(results);
  }
  async function render({refreshList = false} = {}) {
    const renderId = ++state.renderId; destroyCharts(); notify('');
    state.analysis = null;
    updateExports();
    if (state.view === 'experiments') {
      window.NeuroLive?.leave();
      window.NeuroStudio?.leave();
      window.NeuroExperimentLab?.open({api,notify,preview:Boolean(preview)});
      return;
    }
    window.NeuroExperimentLab?.leave();
    if (['device','live','sessions'].includes(state.view)) {
      window.NeuroStudio?.leave();
      await window.NeuroLive.renderView(state.view);
      return;
    }
    window.NeuroLive?.leave();
    if (['timed-protocol','segment','history'].includes(state.view)) {
      await window.NeuroStudio?.renderView(state.view==='timed-protocol'?'experiments':state.view,{api,notify,preview:Boolean(preview)});
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
      if (refreshList) {
        await reloadSessions(renderId);
        if (renderId !== state.renderId) return;
      }
      if (state.view==='compare') {
        await loadComparison(renderId);
        return;
      }
      if (!state.current) {
        $('#content').innerHTML = `<div class="archive-start">
          <section class="card archive-welcome">
            <span class="start-icon">${icon('folder')}</span>
            <h2>Записей пока нет</h2>
            <p>Подключите повязку и проверьте контакт электродов. После остановки записи здесь появятся сигналы, доля пригодных окон и результаты анализа.</p>
            <a class="button primary" href="#device">Подключить повязку ${icon('arrow')}</a>
          </section>
          <section class="card archive-prepare">
            <h2>Подготовка к записи</h2>
            <a href="#experiments">${icon('checklist')}<span><strong>Выбрать эксперимент</strong><small>Условия и порядок выполнения</small></span>${icon('arrow')}</a>
            <a href="#protocol">${icon('book')}<span><strong>Открыть протокол</strong><small>Посадка повязки и метки событий</small></span>${icon('arrow')}</a>
          </section>
        </div>`;
        return;
      }
      const session = state.sessions.find(item => item.id === state.current);
      if (session?.active) {
        $('#content').innerHTML = empty('Сессия ещё записывается','После остановки записи NeuroScope автоматически откроет результаты при следующем просмотре.','clock')
          + '<a class="button subtle section-spaced" href="#live">Открыть живой монитор</a>';
        return;
      }
      $('#content').innerHTML = `<div class="loading-panel" role="status"><span class="loader" aria-hidden="true"></span>
        <h2>${session?.analysis_ready?'Открываем результаты':'Анализируем запись'}</h2>
        <p>${session?.analysis_ready?'Проверяем исходные файлы записи.':'Считаем отсчёты, проверяем качество ЭЭГ и ФПГ.'}</p></div>`;
      const data = await analysisFor(state.current); if(renderId!==state.renderId)return;
      if (session && !session.analysis_ready) {
        session.analysis_ready = true;
        session.duration_s = data.session?.duration_s;
        session.wall_duration_s = data.session?.wall_duration_s;
        renderSessions();
      }
      state.analysis = data; state.channel = Math.min(state.channel,Math.max(0,arr(data.eeg?.channels).length-1)); updateExports();
      state.view==='signals' ? renderSignals(data) : renderOverview(data);
    } catch (error) {
      if(renderId!==state.renderId)return;
      $('#content').innerHTML=`<div class="error-panel">${icon('info')}<h2>Не удалось открыть результаты</h2><p>${esc(error.message)} Проверьте, что сервер запущен и файлы записи доступны.</p><button class="button primary" id="retry-button">Повторить</button></div>`;
      $('#retry-button')?.addEventListener('click',()=>render({refreshList:true}));
    }
  }

  function headlineMetrics(a) {
    const eeg = a.eeg || {};
    const channels = arr(eeg.channels);
    const sampleCount = num(channels[0]?.sample_count);
    const rate = num(eeg.sample_rate_hz);
    const signalDuration = sampleCount !== null && rate > 0 ? duration(sampleCount / rate) : '—';
    const firstPeak = channels.find(channel => num(channel.alpha_peak_hz) !== null);
    const peaks = channels.filter(channel => num(channel.alpha_peak_hz) !== null);
    const coverage = num(a.quality?.usable_eeg_pct);
    const pulse = num(a.ppg?.pulse_rate_bpm);
    return `<div class="metric-grid">
      ${metric('Длительность ЭЭГ', signalDuration, '', eeg.available ? `По отсчётам · ${fmt(rate,0)} Гц` : 'ЭЭГ отсутствует', 'clock')}
      ${metric('Окна без флагов', fmt(coverage), coverage === null ? '' : '%', 'Среднее по каналам · окна по 4 с', 'shield')}
      ${metric('Кандидат в альфа-пик', firstPeak ? fmt(firstPeak.alpha_peak_hz,2) : '—', firstPeak ? 'Гц' : '',
        firstPeak ? [firstPeak.name, ...peaks.slice(1).map(c => `${c.name} · ${fmt(c.alpha_peak_hz,2)} Гц`)].join(' / ') : 'Нет выделенного пика в 8–13 Гц', 'wave')}
      ${metric('Частота пульса', fmt(pulse), pulse === null ? '' : 'уд/мин',
        pulse !== null ? 'Медиана по принятым окнам ФПГ' : 'Недостаточно данных ФПГ для оценки', 'heart')}
    </div>`;
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
      const recorded=num(count)===null?known:num(count)>0;
      const names={eeg:'ЭЭГ',ppg:'Оптический пульс',mems:'Движение',resistances:'Сопротивление'};
      const rate=s.sample_rate_hz??s.nominal_rate_hz??a.session?.sample_rates?.[k];
      return `<tr><td><span class="stream-symbol">${icon({eeg:'wave',ppg:'heart',mems:'signal',resistances:'shield'}[k])}${names[k]}</span></td><td>${tag(recorded?'Записано':'Нет данных',recorded?'blue':'')}</td><td>${integer(count)}</td><td>${recorded&&rate?`${integer(rate)} Гц`:'—'}</td><td>${esc(duration(s.nominal_duration_s??s.signal_duration_s??s.duration_s))}</td></tr>`;
    }).join('');
  }
  function quickReview(a) {
    const review = a.quick_review;
    if (!review) return '';
    const labels = {
      unavailable:['Оценка недоступна','warning'],
      too_short:['Короткий фрагмент','warning'], rejected:['Окна отклонены','bad'],
      partial:['Частично пригоден','warning']
    };
    const readyLabels = {eeg:'Спектр рассчитан',ppg:'Пульс оценён',mems:'Движение рассчитано',resistances:'Измерения разобраны'};
    const limitedLabels = {eeg:'Спектр сильно ограничен',ppg:'Оценка по одному окну'};
    const streamNames = {eeg:'ЭЭГ',ppg:'ФПГ',mems:'Движение',resistances:'Сопротивление'};
    const reasonNames = {
      flatline:'постоянный сигнал', nonfinite:'некорректные значения', movement:'сильное движение',
      sample_index_discontinuity:'разрыв индекса', repeated_rail_0p8:'повторяющиеся крайние значения',
      line_component_dominant:'компонента 50 Гц', amplitude_outlier:'выброс амплитуды',
      derivative_outlier:'резкие скачки', too_few_pulses:'мало пульсовых пиков',
      implausible_intervals:'нетипичные интервалы между пиками',
      irregular_or_noisy_intervals:'нестабильные интервалы между пиками',
      low_periodicity:'слабая периодичность', large_transient:'выброс сигнала'
    };
    const recordedStreams = [];
    const rows = Object.entries(streamNames).map(([key,name])=>{
      const stream = review.streams?.[key];
      if (!stream) return '';
      const recorded = stream.recorded ?? (num(stream.sample_count) > 0);
      if (recorded) recordedStreams.push(name);
      const [label, tone] = stream.status==='ready' ? [readyLabels[key],'good']
        : stream.status==='limited' ? [limitedLabels[key]||'Ограниченная оценка','warning']
        : labels[stream.status] || labels.unavailable;
      const samples = stream.sample_count ? `${integer(stream.sample_count)} ${key==='resistances'?'измерений':'отсчётов'}` : '';
      let detail = '';
      if (key==='eeg' || key==='ppg') {
        if (stream.total_windows) {
          detail = `Скрининг: ${integer(stream.accepted_windows)} из ${integer(stream.total_windows)} окон${key==='eeg'&&stream.channel_count>1?' по всем каналам':''}`;
          if (key==='eeg' && stream.status==='partial') detail += ` · спектр доступен для ${integer(stream.spectral_channels)} из ${integer(stream.channel_count)} каналов`;
          if (key==='eeg' && arr(stream.coarse_channels).length) detail += ` · мало амплитудных уровней: ${stream.coarse_channels.join(', ')}`;
        } else if (stream.status==='too_short') {
          detail = `Для одного окна требуется ${integer(stream.window_seconds)} с`;
        }
      }
      if (key==='resistances' && stream.channels_with_values) detail = `Доступны значения по ${integer(stream.channels_with_values)} из ${integer(stream.channel_count)} каналов`;
      const pulse = num(stream.pulse_rate_bpm);
      if (pulse !== null) detail += ` · оценка пульса ${fmt(pulse,1)} уд/мин`;
      const reasons = arr(stream.top_reasons).map(code=>reasonNames[code]).filter(Boolean);
      if (reasons.length && ['rejected','partial','limited'].includes(stream.status)) {
        detail += ` · чаще всего: ${reasons.join(', ')}`;
      }
      return `<article class="review-stream"><div class="review-stream-title"><strong>${name}</strong>${tag(recorded?'Записано':'Нет данных',recorded?'blue':'')}</div>
        ${samples? `<div class="review-samples">${samples}${stream.sample_rate_hz? ` · ${integer(stream.sample_rate_hz)} Гц` : ''}</div>` : ''}
        ${recorded? `<p>${tag(label,tone)}</p>` : ''}
        ${detail? `<p>${esc(detail)}</p>` : ''}</article>`;
    }).join('');
    const sdk = review.sdk || {};
    const timing = sdk.eeg_timing || {};
    const artifacts = arr(sdk.eeg_artifact_channels);
    const indexBreaks = Object.entries(review.streams || {}).filter(([,stream])=>stream.index_discontinuities>0);
    const evidence = [];
    if (timing.valid_packets || timing.invalid_packets) {
      evidence.push(`<li>Метки SDK · ${integer(timing.valid_packets)} валидных пакетов${timing.invalid_packets?`, ${integer(timing.invalid_packets)} не разобраны`:''}.
        ${timing.compared_steps ? `Сравнено ${integer(timing.compared_steps)} соседних границ. Самый длинный положительный шаг ${num(timing.longest_step_ms)===null?'—':`${integer(timing.longest_step_ms)} мс`}.` : 'Соседних границ для сравнения нет.'}
        ${timing.compared_steps && timing.long_step_threshold_ms? `Свыше ${fmt(timing.long_step_threshold_ms,1)} мс: ${integer(timing.long_steps)}.` : ''}
        ${timing.nonincreasing_steps? `Нулевых и обратных шагов: ${integer(timing.nonincreasing_steps)}.`:''}
        ${timing.noncontiguous_indices? `Несмежных пар по индексу: ${integer(timing.noncontiguous_indices)}.`:''}
        Эти шаги описывают время SDK. Для подтверждения потерь нужен счётчик устройства.</li>`);
    }
    for (const channel of artifacts) {
      const name = a.session?.streams?.eeg?.columns?.[channel.index] || `Канал ${channel.index+1}`;
      evidence.push(`<li>${esc(name)} · ненулевой флаг SDK в ${integer(channel.nonzero_flags)} из ${integer(channel.observations)} обновлений.</li>`);
    }
    for (const [key,stream] of indexBreaks) {
      evidence.push(`<li>${streamNames[key]} · ${integer(stream.index_discontinuities)} непоследовательных переходов индекса сохранённых строк.</li>`);
    }
    const streams = recordedStreams.length ? recordedStreams.join(' · ') : 'Нет записанных потоков';
    return `<details class="card quick-review review-disclosure">
      <summary><span class="review-icon">${icon('checklist')}</span><span><strong>Состав и качество записи</strong>
        <small>${esc(streams)}</small></span><span class="review-chevron">${icon('chevron')}</span></summary>
      <div class="review-grid">${rows}</div>
      ${evidence.length ? `<details class="details-block"><summary>Метки времени и артефакты</summary><ul>${evidence.join('')}</ul></details>` : ''}
    </details>`;
  }
  function montageCard(a) {
    const names = arr(a.eeg?.channels).map(c=>c.name).filter(Boolean);
    if (!names.length) return '';
    const normalized = names.map(name=>String(name).replace(/[–—]/g,'-').toUpperCase()).sort();
    const pairedHeadband = normalized.join(',')==='O1-T3,O2-T4';
    const rates = `<dl class="compact-kv"><dt>Частота ЭЭГ</dt><dd>${fmt(a.eeg?.sample_rate_hz,1)} Гц</dd><dt>Единицы</dt><dd>${esc(cleanUnits(a.eeg?.units))}</dd></dl>`;
    return `<section class="card">${cardHeader('Монтаж ЭЭГ',names.join(' · '))}
      <div class="${pairedHeadband?'brain-card':''}">${pairedHeadband?montage():''}
      <div class="brain-copy">${pairedHeadband?'<p>Каждое отведение измеряет разность потенциалов двух точек. Сигнал объединяет вклад нескольких источников.</p>':''}
      ${rates}</div></div></section>`;
  }
  function pairedCard() {
    return `<section class="card full">${cardHeader('Спектр ЭЭГ на 3D-схеме','Доли мощности обоих отведений по частотным полосам')}${window.NeuroPairedPanel.markup()}</section>`;
  }
  function renderOverview(a) {
    $('#content').innerHTML=`${quickReview(a)}${a.eeg?.spectral_interpretability==='severely_limited'?notice('<strong>Интерпретация спектра сильно ограничена.</strong> Спектры описывают сохранённый сигнал; достоверный индивидуальный альфа-пик не установлен. Причины указаны в проверке качества.','warning'):''}${headlineMetrics(a)}<p class="chart-caption" style="margin:-10px 0 18px">Доля окон, прошедших фильтр, не равна доле чистого времени и не оценивает здоровье мозга.</p><div class="content-grid">${spectrumCard(a)}<section class="card">${cardHeader('Что видно в этой записи','Автоматические наблюдения, связанные с данными',tag('Автоанализ','good'))}${observations(a,3)}</section>${timelineCard(a)}${qualityCard(a)}${montageCard(a)}${pairedCard()}<section class="card">${cardHeader('Контекст записи','Добавленный вами контекст хранится отдельно от исходных данных')}${contextForm()}</section><section class="card full">${cardHeader('Состав записи','Номинальная длительность и реальные временные метки рассматриваются отдельно')}<div class="table-wrap"><table class="data-table"><thead><tr><th>ПОТОК</th><th>ДОСТУПНОСТЬ</th><th>ОТСЧЁТЫ</th><th>ЧАСТОТА</th><th>ДЛИТЕЛЬНОСТЬ</th></tr></thead><tbody>${streamRows(a)}</tbody></table></div><p class="table-caption">Время в интерфейсе: Москва (UTC+3). Параметры частот взяты из метаданных записи; временные метки проверяются отдельно в автоанализе.</p></section></div>`;
    window.NeuroPairedPanel.attach($('#content [data-pair-panel]'),a.paired_eeg);
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
    $('#content').innerHTML=`${quickReview(a)}${notice('<strong>Единицы ЭЭГ:</strong> значения сохранены в формате SDK. Без подтверждённого масштаба их нельзя подписывать как микровольты. Фильтр отмечает подозрительные окна; он не гарантирует устранение всех артефактов.')} ${a.eeg?.available?controls:''}<div class="content-grid even"><section class="card">${cardHeader('Спектр мощности','Метод Welch; отобранные и, по желанию, исходные окна')}${a.eeg?.available?`${legend(cs)}<div class="chart-shell"><canvas id="signal-spectrum" aria-label="Спектр мощности каналов ЭЭГ" role="img"></canvas></div><p class="chart-caption">Сплошная линия — отобранные окна. Пунктир — исходные окна при включённом переключателе. Отсутствие сплошной линии означает отсутствие пригодной оценки.</p>`:empty('ЭЭГ не записана','Для этой сессии анализ электрической активности недоступен.')}</section><section class="card">${cardHeader(`Метрики канала ${c.name||''}`,'Описательные показатели сигнала')}${a.eeg?.available?channelStats(c):empty('Нет показателей канала','Пульс и движение не заменяют электрическую запись.')}</section><section class="card full">${cardHeader('Фрагмент исходной ЭЭГ',c.excerpt?.selection||'Сохранённые значения без изменения масштаба',tag(c.name||'Нет ЭЭГ','blue'))}${a.eeg?.available&&arr(c.excerpt?.values).length?`<div class="chart-shell small"><canvas id="raw-eeg" aria-label="Фрагмент исходной ЭЭГ" role="img"></canvas></div><p class="chart-caption">Фрагмент служит визуальной проверке формы сигнала. Он не является всей записью; скачайте исходные файлы через управление записями.</p>`:empty('Нет фрагмента ЭЭГ','Исходные отсчёты отсутствуют или недоступны.')}</section><section class="card full">${cardHeader('Динамика ритмов','Доли спектральной мощности в отобранных окнах',`<label for="timeline-band" class="screen-reader">Диапазон частот</label><select id="timeline-band" class="select">${bandDefs.map(b=>`<option value="${b.id}" ${b.id==='alpha'?'selected':''}>${b.name} · ${b.range}</option>`).join('')}</select>`)}${a.eeg?.available?`${legend(cs)}<div class="chart-shell small"><canvas id="signals-timeline" aria-label="Изменение доли мощности выбранного диапазона" role="img"></canvas></div>`:empty('Нет временного ряда ЭЭГ','Ритмы требуют сохранённой ЭЭГ.')}</section><section class="card">${cardHeader('Оптический пульс','PPG: пульсовая волна и качество её регистрации',tag('PPG','blue'))}${p.available?`<div class="chart-shell small"><canvas id="ppg-excerpt" aria-label="Фрагмент оптического пульсового сигнала" role="img"></canvas></div><dl class="signal-stat-row"><div><dt>ПУЛЬС</dt><dd>${fmt(p.pulse_rate_bpm,1)} <small>уд/мин</small></dd></div><div><dt>ПРОШЛО ФИЛЬТР</dt><dd>${percent(p.quality?.usable_pct)}</dd></div><div><dt>ПРИНЯТО ОКОН</dt><dd>${integer(p.quality?.accepted_windows)} <small>/ ${integer(p.quality?.total_windows)}</small></dd></div></dl><details class="details-block"><summary>Пульсовая вариабельность · PRV</summary><dl class="signal-stat-row"><div><dt>RMSSD</dt><dd>${fmt(prv.rmssd_ms,1)} <small>мс</small></dd></div><div><dt>SDNN</dt><dd>${fmt(prv.sdnn_ms,1)} <small>мс</small></dd></div><div><dt>ИНТЕРВАЛЫ</dt><dd>${integer(prv.n_intervals)}</dd></div></dl><p class="chart-caption">Блок PRV: ${esc(duration(prv.duration_s))}; начало ${fmt(prv.block_start_s,1)} с, конец ${fmt(prv.block_end_s,1)} с. Пригодные интервалы: ${esc(duration(prv.valid_interval_duration_s))}.</p><p class="chart-caption">${esc(prv.description||'Метрики по пульсовым интервалам. PPG не эквивалентна ЭКГ; движение и ошибки обнаружения пиков искажают вариабельность.')}</p></details>`:empty('Нет PPG','В этой записи отсутствует оптический сигнал.','heart')}</section><section class="card">${cardHeader('Пульс во времени','Только окна, принятые проверкой качества')}${p.available?`<div class="chart-shell small"><canvas id="ppg-timeline" aria-label="Частота пульса во времени" role="img"></canvas></div><p class="chart-caption">Разрывы — невалидные или недостаточные фрагменты. Изменения пульса неспецифичны: на них влияют движение, дыхание и множество других факторов.</p>`:empty('Нет динамики PPG','Частота пульса недоступна.','heart')}</section><section class="card">${cardHeader('Движение','Датчики MEMS помогают заметить возможные артефакты')}${motion.available?`<div class="chart-shell small"><canvas id="motion-chart" aria-label="Динамика величины вращения по гироскопу" role="img"></canvas></div><p class="chart-caption">${esc(motion.summary?.units_note||'Единицы сохранены из SDK. Величина движения — контекст для качества сигнала, не показатель работы мозга.')}</p><dl class="signal-stat-row"><div><dt>ОКНА С ДВИЖЕНИЕМ</dt><dd>${percent(motion.summary?.movement_windows_pct)}</dd></div><div><dt>ПОРОГ ЭВРИСТИКИ</dt><dd>${fmt(motion.summary?.movement_threshold,2)}</dd></div></dl>`:empty('Нет движения','Данные MEMS отсутствуют.')}</section><section class="card">${cardHeader('Контакт электродов','Распределение сопротивления по каналам')}${impedance.available?`<div class="table-wrap"><table class="data-table"><thead><tr><th>КАНАЛ</th><th>МЕДИАНА</th><th>P95</th><th>&gt; 1 МОм</th></tr></thead><tbody>${arr(impedance.channels).map(ch=>`<tr><td>${esc(ch.name)}</td><td>${fmt(num(ch.median_ohm)===null?null:ch.median_ohm/1000,0)} кОм</td><td>${fmt(num(ch.p95_ohm)===null?null:ch.p95_ohm/1000,0)} кОм</td><td>${percent(ch.above_1m_pct)}</td></tr>`).join('')}</tbody></table></div><div class="chart-shell small"><canvas id="impedance-chart" aria-label="Сопротивление электродов во времени" role="img"></canvas></div><p class="chart-caption">${esc(impedance.threshold_note||'Пороги проекта не являются клинической нормой. Связь сопротивления с качеством зависит от схемы усилителя и электродов.')}</p>`:empty('Нет сопротивлений','Контакт электродов в этой сессии не измерен.')}</section>${pairedCard()}${advancedCard(c)}${qualityCard(a,true)}<section class="card">${cardHeader('Ограничения интерпретации','Что следует учитывать перед любым выводом')}<div class="text-content"><ul>${arr(a.limitations).map(x=>`<li>${esc(textOf(x))}</li>`).join('')||'<li>Смотрите научную базу и протокол записи для требований к интерпретации.</li>'}</ul></div></section><section class="card full">${cardHeader('Оценки SDK','Отдельный источник: показатели производителя не равны доказанным состояниям мозга')}${vendorSummary(a.vendor||{})}</section><section class="card full">${cardHeader('Воспроизводимость и метод','Параметры анализа и происхождение данных')}${methodSummary(a)}</section></div>`;
    window.NeuroPairedPanel.attach($('#content [data-pair-panel]'),a.paired_eeg);
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
    const spectra = list.filter(Boolean).map(a=>({
      session: a.session,
      channels: arr(a.eeg?.channels).filter(c=>arr(c.psd?.freq_hz).length && arr(c.psd?.power).length),
    }));
    const channelKey = name => String(name||'').replace(/[–—]/g,'-').toUpperCase();
    const available = new Map();
    for (const spectrum of spectra) {
      for (const name of new Set(spectrum.channels.map(c=>channelKey(c.name)).filter(Boolean))) {
        available.set(name,(available.get(name)||0)+1);
      }
    }
    const compareKey = [...available].sort((a,b)=>b[1]-a[1]).find(([,count])=>count>=2)?.[0];
    const series = compareKey ? spectra.flatMap(({session,channels})=>{
      const selected = channels.find(channel=>channelKey(channel.name)===compareKey);
      return selected ? [{session,channel:selected}] : [];
    }) : [];
    const row=(title,fn,note='')=>`<tr><td>${esc(title)}${note?`<span class="subcell">${esc(note)}</span>`:''}</td>${list.map(a=>`<td>${a?fn(a):'Недоступно'}</td>`).join('')}</tr>`;
    const channelLines=(a, fn)=>arr(a.eeg?.channels).map(c=>`<span class="subcell">${esc(c.name)}: <span style="color:var(--text)">${fn(c)}</span></span>`).join('')||'Нет ЭЭГ';
    $('#content').innerHTML=`${notice('<strong>Сравнение описательное.</strong> Разные даты, продолжительность, контакт электродов и неразмеченные состояния глаз не позволяют приписать различия обучению, восстановлению или изменениям здоровья.','warning')}<section class="card">${cardHeader('Сессии рядом','Сравнивайте наличие и качество данных до сравнения ритмов')}<div class="table-wrap"><table class="data-table comparison-table"><thead><tr><th>ПОКАЗАТЕЛЬ</th>${state.sessions.map(s=>`<th>${esc(dateLabel(s.started_at,true))}<span class="subcell" style="display:block;margin-top:5px;color:var(--soft)">Москва · UTC+3</span></th>`).join('')}</tr></thead><tbody>${row('Наличие ЭЭГ',a=>tag(a.eeg?.available?`Каналов: ${arr(a.eeg.channels).length}`:'Нет ЭЭГ',a.eeg?.available?'good':''))}${row('Номинальная длительность',a=>`<span class="value-emphasis">${esc(duration(a.session?.duration_s))}</span>`,'По метаданным анализа')}${row('Временной охват',a=>esc(duration(a.session?.wall_duration_s)),'По временным меткам')}${row('Окна ЭЭГ без флагов',a=>percent(a.quality?.usable_eeg_pct),'Эвристический отбор')}${row('Альфа-пик · кандидат',a=>channelLines(a,c=>`${fmt(c.alpha_peak_hz,2)} Гц`))}${row('Доля альфа 8–13 Гц',a=>channelLines(a,c=>num(c.relative_power?.alpha)===null?'—':percent(c.relative_power.alpha*100)),'Отобранные окна, от 1–45 Гц')}${row('Спектральная энтропия',a=>channelLines(a,c=>fmt(c.entropy,3)),'Без психологической трактовки')}${row('Частота пульса',a=>`${fmt(a.ppg?.pulse_rate_bpm,1)} уд/мин`,'PPG; при пройденной проверке')}${row('Окна PPG без флагов',a=>percent(a.ppg?.quality?.usable_pct))}${row('Движение',a=>percent(a.motion?.summary?.movement_windows_pct),'Доля окон по эвристике')}${row('Сопротивление · медиана',a=>arr(a.impedance?.channels).map(c=>`<span class="subcell">${esc(c.name)}: ${fmt(num(c.median_ohm)===null?null:c.median_ohm/1000,0)} кОм</span>`).join('')||'Нет измерений')}${row('Статус качества',a=>{const q=statusInfo(a.quality?.status);return tag(q.label,q.cls);})}</tbody></table></div><p class="table-caption">Порог движения определяется отдельно для каждой сессии. Доли отмеченных окон не являются общей шкалой двигательной активности.</p></section><div class="content-grid wide-gap"><section class="card">${cardHeader('Сравнение спектров',series.length?`Общий канал ${series[0].channel.name} · отобранные окна`:'График появляется при наличии спектров общего канала')}${series.length?`<div class="legend">${series.map(({session},i)=>`<span><i style="background:${colors[i%colors.length]}"></i>${esc(dateLabel(session?.started_at))}</span>`).join('')}</div><div class="chart-shell"><canvas id="comparison-spectrum" aria-label="Сравнение спектров ${esc(series[0].channel.name)}" role="img"></canvas></div>`:empty('Нет общего спектра','Для сравнения нужны записи с одним и тем же каналом и пригодными окнами.') }<p class="chart-caption">Мощность может меняться из-за контакта и артефактов. Совпадение аппаратуры не делает условия записи одинаковыми.</p></section><section class="card">${cardHeader('Что улучшит сравнимость','Рекомендации для следующих сессий')}<div class="text-content"><ol><li>Повторяйте короткий протокол в одинаковой позе и в сходное время суток.</li><li>Отдельно размечайте открытые и закрытые глаза; записывайте движения и задачу.</li><li>Сохраняйте качество контакта и фиксируйте сон, кофеин и самочувствие.</li><li>Сравнивайте сопоставимые по длительности чистые участки, а не только средние всей сессии.</li></ol></div><a class="button text" href="#protocol" data-view="protocol">Открыть протокол ${icon('arrow')}</a></section></div>`;
    if(series.length)chart('comparison-spectrum',series.map(({session,channel},i)=>
      dataset(dateLabel(session?.started_at),points(channel.psd.freq_hz,channel.psd.power,true),colors[i%colors.length])),
      {frequency:true,log:true,ytitle:'ед. SDK²/Гц',digits:5});
    bindContent();
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
  const mobileMenu = window.matchMedia('(max-width: 800px)');

  function setMenuOpen(open, restoreFocus = false) {
    const sidebar = $('#sidebar');
    const wasOpen = sidebar.classList.contains('open');
    open = open && mobileMenu.matches;
    sidebar.classList.toggle('open', open);
    document.body.classList.toggle('menu-open', open);
    $('#menu-button').setAttribute('aria-expanded', String(open));
    sidebar.inert = mobileMenu.matches && !open;
    for (const area of $$('.main-shell, .checkpoint-widget, .skip-link')) area.inert = open;
    if (open) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
      $('#menu-close').focus();
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
      if (wasOpen && restoreFocus) $('#menu-button').focus();
    }
  }

  function bindNavigation() {
    setMenuOpen(false);
    $('#menu-button').addEventListener('click', () => setMenuOpen(true));
    $('#menu-close').addEventListener('click', () => setMenuOpen(false, true));
    $('#sidebar-scrim').addEventListener('click', () => setMenuOpen(false, true));
    mobileMenu.addEventListener('change', () => {
      const focusWasInside = $('#sidebar').contains(document.activeElement);
      setMenuOpen(false);
      if (mobileMenu.matches && focusWasInside) $('#menu-button').focus();
    });
    $('.skip-link').addEventListener('click', event => {
      event.preventDefault();
      $('#content').focus();
    });
    for (const link of $$('#sidebar [data-view], .top-help[data-view]')) {
      link.addEventListener('click', event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        if (location.hash.slice(1) === link.dataset.view) {
          event.preventDefault();
          nav(link.dataset.view);
        }
      });
    }
    window.addEventListener('hashchange', () => {
      const view = location.hash.slice(1);
      if (Object.hasOwn(titles, view)) nav(view);
      else if (!view) nav('overview');
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        setMenuOpen(false, true);
        const menu = $('#export-menu');
        if (menu.open && menu.contains(document.activeElement)) menu.querySelector('summary').focus();
        menu.open = false;
      } else if (event.key === 'Tab' && $('#sidebar').classList.contains('open')) {
        const controls = $$('#sidebar a[href], #sidebar button').filter(el => el.getClientRects().length);
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
  }

  async function start() {
    if (preview) {
      $('.sidebar-foot span').textContent = 'Автономный отчёт';
      $('#refresh-button').hidden=true;
      $('.export-dropdown a[href="/api/lab/report"]').href='docs/NeuroScope_Research_Report.pdf';
    }
    window.NeuroCheckpoints?.init({api, preview:!!preview});
    window.NeuroLive?.init({api, notify, preview:!!preview, selectSession, reloadSessions, dateLabel, duration});
    bindNavigation();
    document.addEventListener('click',e=>{if(!$('#export-menu').contains(e.target))$('#export-menu').open=false;});
    $('#refresh-button').addEventListener('click',async()=>{
      if(!state.current)return;const analysisId=state.current;const button=$('#refresh-button');button.disabled=true;button.innerHTML=`${icon('refresh')}<span>Анализируем…</span>`;notify('Пересчитываем показатели из исходных файлов. Это может занять некоторое время.');
      try {await api(`/api/lab/analysis/${encodeURIComponent(analysisId)}/refresh`,{method:'POST'});if(state.current===analysisId){await render();notify('Анализ пересчитан из исходных данных.','success');}else notify('Пересчёт предыдущей сессии завершён.','success');}
      catch(error){notify(error.message,'error');}finally{button.disabled=false;button.innerHTML=`${icon('refresh')}<span>Пересчитать</span>`;}
    });
    try {
      const result=await api('/api/lab/sessions');state.sessions=arr(result.sessions||result).filter(s=>s&&s.id);
      let saved=null;try{saved=localStorage.getItem('neuroscope.session');}catch(_){}
      const query=new URLSearchParams(location.search).get('session');
      state.current=(state.sessions.find(s=>s.id===(query||saved))||state.sessions.find(s=>!s.active)||state.sessions[0])?.id||null;
      state.view=Object.hasOwn(titles,location.hash.slice(1))?location.hash.slice(1):'overview';renderSessions();nav(state.view,false);
    } catch(error) {
      $('#content').innerHTML=`<div class="error-panel"><h2>Список записей недоступен</h2><p>${esc(error.message)}</p><button class="button primary" id="initial-retry">Повторить загрузку</button></div>`;
      $('#initial-retry')?.addEventListener('click',()=>location.reload());
    }
  }
  start();
})();

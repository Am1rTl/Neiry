/* Experiment setup, archive and comparisons. The task runtime lives in experiment_runner.js. */
(() => {
  'use strict';
  const prefix = '/api/experiment-lab';
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const fmt = (value, digits=1) => finite(value) ? value.toLocaleString('ru-RU', {maximumFractionDigits: digits}) : '—';
  const date = (value, short=false) => {
    const parsed = new Date(value);
    return value && Number.isFinite(+parsed) ? new Intl.DateTimeFormat('ru-RU', {
      day:'numeric', month:'short', ...(short ? {} : {hour:'2-digit',minute:'2-digit'}), timeZone:'Europe/Moscow',
    }).format(parsed) : '—';
  };
  const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const catalog = {
    alpha: {
      name:'Проверка альфа-ритма', short:'Альфа-ритм', icon:'wave', time:'2–12 мин', channel:'Затылочная ЭЭГ',
      copy:'Следуйте подсказкам для открытых и закрытых глаз. По результатам сравните мощность альфа-ритма в парных блоках.',
      stages:{baseline:'Первый замер',repeat:'Повторный замер'},
      steps:[['Первый замер','В каждой паре один блок с открытыми и один с закрытыми глазами.'],['Повторный замер','Повтори протокол в другой день и сравни результаты по каналам.']],
      outcome:'Отношение мощности альфа-ритма в закрытом и открытом состоянии для каждого канала.',
      advice:'Сядь удобно, надень повязку и включи звуковые подсказки. Держи голову неподвижно при смене состояний.',
    },
    visual: {
      name:'Зрительное обучение', short:'Зрительное обучение', icon:'eye', time:'От 2 мин за сессию', channel:'Точность и ЭЭГ',
      copy:'Определяй наклон полос. Сравни изменение точности и порога различения для двух ориентаций.',
      stages:{baseline:'Исходный тест',training:'Тренировка',post:'Итоговый тест',followup:'Отложенный тест'},
      steps:[['Исходный тест','Три замера в разные дни с двумя ориентациями и без подсказок.'],['Тренировка','10–14 дней. Одна ориентация и подсказка о правильности ответа.'],['Итоговый тест','Две ориентации без подсказок, с теми же настройками, что в исходном тесте.'],['Отложенный тест','Повтори итоговый тест через 7–14 дней без тренировок.']],
      outcome:'Точность, медианное время правильного ответа, порог 75% и затылочная альфа во время задачи.',
      advice:'Сохраняй расстояние до экрана и освещение. Для каждого замера используй одну и ту же тренировочную ориентацию и число проб.',
    },
    feedback: {
      name:'Нейрообратная связь', short:'Нейрообратная связь', icon:'signal', time:'2–5 мин за блок', channel:'Саморегуляция',
      copy:'Попробуй менять затылочный альфа-ритм с обратной связью и проверь, сохраняется ли эффект при её отключении.',
      stages:{baseline:'Исходный блок',live:'Сигнал в реальном времени',control:'Контрольная запись',transfer:'Тест без обратной связи',followup:'Отложенный тест'},
      steps:[['Исходный блок','Запиши активность при открытых глазах.'],['Тренировка и контроль','Сравни блоки с живой обратной связью и заранее записанной траекторией.'],['Тест без обратной связи','Проверь, изменяется ли активность без подсказок.'],['Отложенный тест','Повтори блок без подсказок спустя 7–14 дней.']],
      outcome:'Медианная мощность целевой полосы, доля пригодных измерений и фактическая траектория обратной связи.',
      advice:'Держи глаза открытыми. Для сравнения используй один монтаж, похожее время суток и одинаковую длительность блоков.',
    },
  };
  const statusNames = {completed:'Завершена',aborted:'Прервана',running:'Выполняется'};
  let state = null;
  const post = body => ({method:'POST',body:JSON.stringify(body)});
  const path = id => `/runs/${encodeURIComponent(id)}`;
  const current = s => s === state && s.mount.isConnected;
  const request = (s, endpoint, options) => s.api(prefix + endpoint, options);
  const modeName = mode => mode === 'live' ? 'С повязкой' : 'Демо';
  const stageName = run => catalog[run.kind]?.stages[run.stage] ?? run.stage;
  const badge = (label, style='') => `<span class="ex-badge ${style}"><i></i>${esc(label)}</span>`;
  const metric = (label, value, note='') => `<div class="ex-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong>${note ? `<small>${esc(note)}</small>` : ''}</div>`;
  const errorText = err => err?.message || 'Не удалось получить данные.';
  const formatValue = (v, unit='') => finite(v) ? `${fmt(v)}${unit}` : '—';

  function setupCard(kind, s) {
    const item = catalog[kind];
    return `<button type="button" class="ex-protocol ex-${kind}" data-ex-select="${kind}" aria-pressed="${kind===s.kind}">
      <span class="ex-card-top"><span class="ex-glyph">${icon(item.icon)}</span><span class="ex-select-mark" aria-hidden="true"></span></span>
      <span class="ex-protocol-title">${esc(item.name)}</span><span class="ex-protocol-copy">${esc(item.copy)}</span>
      <span class="ex-protocol-foot">${icon('clock')}${esc(item.time)}<span>${esc(item.channel)}</span></span></button>`;
  }

  function render(s) {
    if (!current(s)) return;
    s.chart?.destroy(); s.chart=null;
    s.mount.innerHTML = `<div class="ex-shell"><nav class="ex-tabs" aria-label="Лаборатория экспериментов">
      ${[['setup','Протоколы','checklist'],['history','Журнал сессий','folder'],['compare','Сравнение','compare']].map(([key,label,glyph])=>
        `<button type="button" class="ex-tab" data-ex-tab="${key}" ${s.tab===key ? 'aria-current="page"' : ''}>${icon(glyph)}${label}</button>`).join('')}
      <span class="ex-device" data-ex-device role="status"></span></nav><div data-ex-page></div></div>`;
    renderTab(s);
    connection(s);
  }
  function renderTab(s) {
    if (s.tab === 'setup') renderSetup(s);
    if (s.tab === 'history') renderHistory(s);
    if (s.tab === 'compare') renderCompare(s);
  }
  function renderSetup(s) {
    const item = catalog[s.kind];
    const form = s.forms[s.kind];
    const references = s.runs.filter(row => row.kind==='feedback' && row.mode==='live' && row.status==='completed' && row.replay_available && Number(row.settings?.duration_s)===Number(form.duration_s));
    const context = s.context;
    $('[data-ex-page]',s.mount).innerHTML = `<div class="ex-protocols" aria-label="Выбор эксперимента">${Object.keys(catalog).map(kind=>setupCard(kind,s)).join('')}</div>
      <div class="ex-workspace"><section class="ex-panel ex-config ex-${s.kind}" aria-labelledby="ex-config-title">
        <header class="ex-panel-head"><span class="ex-glyph">${icon(item.icon)}</span><div><h2 id="ex-config-title">${esc(item.name)}</h2><p>Настройка сессии</p></div></header>
        <form data-ex-form><div class="ex-form-grid">
          <label><span>Источник</span><select name="mode"><option value="live" ${form.mode==='live'?'selected':''}>Neiry Headband</option><option value="demo" ${form.mode==='demo'?'selected':''}>Демо без повязки</option></select></label>
          <label><span>Этап исследования</span><select name="stage">${Object.entries(item.stages).map(([key,label])=>`<option value="${key}" ${key===form.stage?'selected':''}>${esc(label)}</option>`).join('')}</select></label>
          ${s.kind==='alpha' ? `<label><span>Повторы пар блоков</span>${select('repeats',form.repeats,[[2,'2 пары'],[3,'3 пары'],[4,'4 пары']])}</label><label><span>Длительность блока</span>${form.mode==='demo'?'<input value="4 секунды в демо" disabled>':select('block_s',form.block_s,[[30,'30 секунд'],[60,'60 секунд'],[90,'90 секунд']])}</label>
            <label class="ex-wide"><span>Порядок внутри пар</span>${select('alpha_order',form.alpha_order,[['counterbalanced','Балансировать, какое состояние будет первым'],['fixed','Сначала открытые глаза в каждой паре']])}</label>` : ''}
          ${s.kind==='visual' ? `<label><span>Тренировочная ориентация</span>${select('trained_orientation',form.trained_orientation,[[45,'45°'],[135,'135°']])}</label><label><span>Число проб</span>${select('trial_count',form.trial_count,[[80,'80 проб'],[120,'120 проб'],[160,'160 проб'],...(form.stage==='training'?[[360,'360 проб'],[480,'480 проб']]:[])])}</label>` : ''}
          ${s.kind==='feedback' ? `<label><span>Длительность блока</span>${select('duration_s',form.duration_s,[[120,'2 минуты'],[180,'3 минуты'],[300,'5 минут']])}</label>` : ''}
          ${s.kind==='feedback' && form.stage==='control' && form.mode==='live' ? `<label><span>Источник контрольного сигнала</span>${select('replay_run_id',form.replay_run_id,[['','Выбери сессию'],...references.map(row=>[row.id,`${date(row.started_at)} · ${row.context?.participant_code || 'Без кода'}`])])}</label>` : ''}
        </div>
        <fieldset class="ex-context"><legend>Контекст сессии</legend><div class="ex-form-grid">
          <label><span>Код участника</span><input name="participant_code" value="${esc(context.participant_code)}" maxlength="64" placeholder="Например, P01" ${form.mode==='live'?'required':''} autocomplete="off"></label>
          <label><span>Сон, ч</span><input type="number" name="sleep_hours" min="0" max="24" step="0.5" value="${esc(context.sleep_hours)}" placeholder="Например, 7,5"></label>
          <label><span>Качество сна</span>${select('sleep_quality',context.sleep_quality,[['','Не указано'],['poor','Плохое'],['average','Среднее'],['good','Хорошее']])}</label>
          <label><span>Кофеин перед сессией</span><input name="caffeine" maxlength="120" value="${esc(context.caffeine)}" placeholder="Например, кофе 2 часа назад"></label>
          <label class="ex-wide"><span>Заметки</span><textarea name="notes" maxlength="1500" rows="2" placeholder="Время последнего приёма пищи, самочувствие, изменения посадки повязки…">${esc(context.notes)}</textarea></label>
        </div></fieldset>
        <div class="ex-launch"><div>
          ${s.kind==='alpha' ? `<label class="ex-check"><input type="checkbox" name="sound" ${s.sound?'checked':''}> Звуковые переходы</label><button type="button" class="button text" data-ex-test-sound>Проверить сигналы</button><p class="ex-copy">Два тона — открыть глаза, один — закрыть, три — конец. Без звука ведущий вслух подаёт команды с экрана.</p>` : ''}
          <label class="ex-check"><input type="checkbox" name="confirmed" ${s.confirmed?'checked':''} required> Я готов к началу сессии</label>
          <p data-ex-hint aria-live="polite"></p></div><button type="submit" class="button primary ex-start" data-ex-start>${icon('arrow')}<span>${form.mode==='demo'?'Запустить демо':'Начать сессию'}</span></button>
        </div></form></section>
      <aside class="ex-plan"><section class="ex-panel ex-${s.kind}"><h2>План эксперимента</h2><ol class="ex-timeline">${item.steps.map(([title,copy])=>`<li><strong>${esc(title)}</strong><span>${esc(copy)}</span></li>`).join('')}</ol><div class="ex-plan-outcome"><strong>Что измеряем</strong><p>${esc(item.outcome)}</p></div></section>
        <div class="ex-tip">${icon('info')}<p>${esc(item.advice)}</p></div>
      </aside></div><div class="ex-extra"><a href="#timed-protocol">Протоколы с таймером · покой, задача и восстановление</a></div>`;
    connection(s);
  }
  function select(name, value, options) {
    return `<select name="${name}">${options.map(([key,label])=>`<option value="${esc(key)}" ${String(key)===String(value ?? '')?'selected':''}>${esc(label)}</option>`).join('')}</select>`;
  }

  function connection(s) {
    if (!current(s)) return;
    const d = s.device;
    const active = d?.active_run_id || d?.active_run?.id;
    const indicator = $('[data-ex-device]',s.mount);
    if (indicator) indicator.innerHTML = `${badge(!d ? 'Проверка Neiry…' : d.ready ? 'ЭЭГ готова' : 'Neiry не готова',d?.ready ? 'good' : '')}${d && !d.ready ? '<a href="#device">Подключить</a>' : ''}`;
    const submit = $('[data-ex-start]',s.mount);
    const hint = $('[data-ex-hint]',s.mount);
    if (!submit || !hint) return;
    const form = s.forms[s.kind];
    const needsOccipital = ['alpha','feedback'].includes(s.kind) && Array.isArray(d?.occipital_channels) && !d.occipital_channels.length;
    const needTrace = s.kind==='feedback' && form.stage==='control' && form.mode==='live' && !form.replay_run_id;
    submit.disabled = Boolean(s.starting || active || (form.mode==='live' && (!d?.ready || needsOccipital)) || needTrace);
    if (active && !s.runner) {
      if (hint.dataset.active === active) return;
      hint.dataset.active=active;
      hint.innerHTML = '<span>На сервере есть активная сессия.</span> <button type="button" class="ex-text-button" data-ex-abort-active>Завершить её</button>';
    } else {
      delete hint.dataset.active;
      hint.textContent = form.mode==='demo' ? 'Короткий проход интерфейса. Результат сохраняется с пометкой «Демо».' :
        !d?.ready ? 'Подключи Neiry и дождись подтверждения контакта электродов.' :
        needsOccipital ? 'В текущем потоке нет распознанных затылочных отведений.' :
        needTrace ? 'Для контроля выбери завершённую сессию с живой обратной связью той же длительности.' :
        d.recording ? 'Сессия добавит события к текущей записи ЭЭГ.' : 'Запись ЭЭГ начнётся вместе с сессией.';
    }
  }
  async function refreshStatus(s) {
    if (s.statusPending) return;
    s.statusPending=true;
    try { s.device=await request(s,'/status',{signal:AbortSignal.timeout(4000)});connection(s); }
    catch (err) {
      s.device=null;connection(s);
      if (current(s)) $('[data-ex-device]',s.mount)?.replaceChildren(document.createTextNode(errorText(err)));
    } finally { s.statusPending=false; }
  }
  async function refreshRuns(s, repaint=false) {
    try {
      const archive = await request(s,'/runs');
      if (!current(s)) return;
      s.runs = Array.isArray(archive.runs) ? archive.runs : [];
      s.archiveError = '';
      if (repaint && !s.runner && !s.starting && !s.resultId) render(s);
    } catch (err) {
      s.archiveError = errorText(err);
      if (current(s) && s.tab !== 'setup' && !s.resultId) render(s);
    }
  }

  async function clockSync(s) {
    const readings = [];
    for (let i=0; i<4; i++) {
      const before = performance.timeOrigin + performance.now();
      try {
        const result = await request(s,'/clock',{cache:'no-store',signal:AbortSignal.timeout(2000)});
        const after = performance.timeOrigin + performance.now();
        if (finite(result.server_ms)) readings.push({offset:result.server_ms - (after+before)/2, rtt:after-before});
      } catch (_) { /* Unsynchronized demos can still use the browser clock. */ }
    }
    readings.sort((a,b)=>a.rtt-b.rtt);
    return readings[0] || null;
  }
  async function start(s) {
    if (s.starting) return;
    s.starting = true;
    closeTestSound(s);
    connection(s);
    let run = null;
    let cue = false;
    const kind = s.kind;
    const form = {...s.forms[kind]};
    const sound = s.sound;
    const context = {...s.context,sleep_hours:s.context.sleep_hours===''?null:Number(s.context.sleep_hours)};
    try {
      if (!window.NeuroExperimentRunner) throw Error('Модуль экспериментов не загрузился. Обнови страницу.');
      if (kind==='alpha' && sound) cue = window.NeuroExperimentRunner.primeSound();
      const clock = await clockSync(s);
      if (s!==state) return;
      if (kind==='alpha' && form.mode==='live' && sound && cue?.state!=='running') {
        throw Error('Звуковые подсказки недоступны. Разрешите звук в браузере или отключите его и попросите ведущего читать команды вслух.');
      }
      if (form.mode==='live' && !clock) throw Error('Часы браузера и сервера не удалось сопоставить. Повтори запуск.');
      const settings = {...(kind==='alpha' ? {block_s:form.mode==='demo'?4:Number(form.block_s),repeats:Number(form.repeats),alpha_order:form.alpha_order,sound} :
        kind==='visual' ? {trial_count:form.mode==='demo'?12:Number(form.trial_count),trained_orientation:Number(form.trained_orientation),stimulus_ms:250} :
          {duration_s:form.mode==='demo'?15:Number(form.duration_s),replay_run_id:form.replay_run_id || null}),
        clock_offset_ms:clock?.offset ?? null, clock_round_trip_ms:clock?.rtt ?? null,
        time_zone:'Europe/Moscow', timing:clock ? 'browser_monotonic_software_clock' : 'browser_unsynchronized'};
      run = await request(s,'/runs',post({kind,mode:form.mode,stage:form.stage,context,settings}));
      if (s!==state) { await request(s,`${path(run.id)}/finish`,post({status:'aborted'})); return; }
      s.confirmed=false;
      s.mount.innerHTML='<div class="ex-shell" data-ex-runtime></div>';
      const runtime = $('[data-ex-runtime]',s.mount);
      s.runner = new window.NeuroExperimentRunner({root:runtime,request:s.api,run,clockOffset:clock?.offset||0,sound:cue,
        onFinish:doc=>finished(s,doc),onError:err=>{if(current(s))s.notify(errorText(err),'error');}});
      cue = null;
      s.runner.start();
    } catch (err) {
      if (run && !s.runner) await request(s,`${path(run.id)}/finish`,post({status:'aborted'})).catch(()=>{});
      if (current(s)) { render(s); s.notify(errorText(err),'error'); await refreshStatus(s); }
    } finally {
      cue?.close?.().catch(()=>{});
      s.starting=false;
      connection(s);
    }
  }
  function finished(s, doc) {
    s.runner=null;
    if (s!==state) return;
    if (current(s) && doc.local_unsaved_events) s.notify(`Часть событий не дошла до сервера (${doc.local_unsaved_events}).`, 'error');
    s.runs=[doc,...s.runs.filter(row=>row.id!==doc.id)];
    s.tab='history';
    s.historyMode=doc.mode;
    s.resultId=doc.id;
    if (!current(s)) return;
    render(s);
    refreshRuns(s);
    refreshStatus(s);
  }

  function renderHistory(s) {
    const filtered = s.runs.filter(run=>run.mode===s.historyMode);
    const page = $('[data-ex-page]',s.mount);
    page.innerHTML = `<section class="ex-panel"><div class="ex-archive-head"><div><h2>История экспериментов</h2><p>Результаты, маркеры и исходные записи.</p></div>
      <label class="ex-inline-field"><span>Источник</span>${select('history_mode',s.historyMode,[['live','С повязкой'],['demo','Демо']])}</label></div>
      ${s.archiveError ? `<div class="ex-empty">${esc(s.archiveError)} <button class="ex-text-button" type="button" data-ex-refresh>Повторить</button></div>` : ''}
      ${filtered.length ? `<div class="ex-record-list">${filtered.map(run=>`<button type="button" class="ex-record ex-${esc(run.kind)}" data-ex-result="${esc(run.id)}" aria-pressed="${run.id===s.resultId}"><span class="ex-glyph">${icon(catalog[run.kind]?.icon||'folder')}</span><span class="ex-record-main"><strong>${esc(catalog[run.kind]?.name||run.kind)}</strong><small>${esc(stageName(run))} · ${esc(run.context?.participant_code||'Без кода')}</small></span><span class="ex-record-date">${date(run.started_at)}</span>${badge(statusNames[run.status]||run.status,run.status==='completed'?'good':'')}${icon('arrow')}</button>`).join('')}</div>` : `<div class="ex-empty">${icon('folder')}<strong>${s.historyMode==='demo'?'Демонстрационных сессий пока нет':'Сессий с повязкой пока нет'}</strong><span>Здесь появятся результаты после прохождения протокола.</span><button type="button" class="button subtle" data-ex-tab="setup">К протоколам</button></div>`}</section><div data-ex-detail></div>`;
    if (s.resultId && filtered.some(run=>run.id===s.resultId)) openResult(s,s.resultId);
  }

  function table(head, rows) {
    return `<div class="ex-table-wrap"><table class="ex-table"><thead><tr>${head.map(x=>`<th scope="col">${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((x,i)=>i===0?`<th scope="row">${esc(x)}</th>`:`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  const conditionName = key => key==='trained'?'Тренировочная ориентация':'Контрольная ориентация';
  const phaseName = key => ({eyes_open:'Открытые глаза',eyes_closed:'Закрытые глаза',visual_task:'Зрительное задание',baseline:'Исходный блок',calibration:'Калибровка',live:'Сигнал в реальном времени',control:'Контрольная запись',transfer:'Тест без связи',followup:'Отложенный тест'}[key]||key);
  const alphaOrderLabel = settings => settings?.alpha_order==='counterbalanced' ? 'Сбалансированный порядок' : 'Фиксированный порядок';
  const bandLabel = band => Array.isArray(band) && band.length===2 ? `${fmt(band[0])}–${fmt(band[1])} Гц` : '—';
  function resultMetrics(a, run) {
    if (run.kind==='alpha') {
      const summary=a.alpha||{};
      const channels=summary.channels||[];
      const phases=(summary.phases||[]).flatMap((p,i)=>(p.channels||[]).map(c=>[`${i+1} · ${phaseName(p.label)}`,c.name,fmt(c.alpha_power,3),formatValue(c.relative_alpha_pct,'%'),`${c.usable_windows ?? 0} / ${c.total_windows ?? 0}`]));
      const starts = run.settings?.alpha_pair_first;
      const schedule = Array.isArray(starts) ? ` · первое состояние каждой пары: ${starts.map(phaseName).join(' / ')}` : '';
      return `<p class="ex-copy">План · ${esc(alphaOrderLabel(run.settings))}${esc(schedule)}</p>${channels.length ? `<div class="ex-metrics">${channels.map(c=>metric(c.name,formatValue(c.closed_open_ratio,'×'),`Закрытые / открытые · пар: ${c.paired_blocks ?? 0}`)).join('')}</div>` : `<p class="ex-copy">${run.mode==='demo'?'Переходы и таймер сохранены. Для расчёта альфа-мощности нужна запись ЭЭГ.':'Недостаточно пригодных пар блоков для сравнения.'}</p>`}
        ${phases.length?table(['Блок','Канал','Мощность 8–13 Гц','Доля альфа','Окна без флагов'],phases):''}`;
    }
    if (run.kind==='visual') {
      const rows=Object.entries(a.visual?.conditions||{}).filter(([,c])=>c.trials>0);
      const levels=rows.flatMap(([key,c])=>(c.levels||[]).map(x=>[conditionName(key),formatValue(x.angle_deg,'°'),`${x.correct_trials} / ${x.valid_trials}`,formatValue(x.accuracy_pct,'%')]));
      const eeg=(a.visual?.eeg?.phases||[]).flatMap(p=>(p.channels||[]).map(c=>[c.name,fmt(c.alpha_power,3),formatValue(c.relative_alpha_pct,'%'),`${c.usable_windows ?? 0} / ${c.total_windows ?? 0}`]));
      return `${rows.length?table(['Ориентация','Пробы в анализе','Точность','Медиана времени','Порог 75%'],rows.map(([key,c])=>[conditionName(key),`${c.valid_trials} / ${c.trials}`,formatValue(c.accuracy_pct,'%'),formatValue(c.median_correct_rt_ms,' мс'),formatValue(c.threshold_75_deg,'°')])):'<p class="ex-copy">Нет пригодных проб.</p>'}
        <p class="ex-copy">Порог отображается, если измеренная зависимость точности пересекает 75%. Время реакции рассчитано по правильным ответам.</p>
        ${levels.length?`<details class="ex-detail"><summary>Результаты по углам</summary>${table(['Ориентация','Отклонение','Верно / пригодны','Точность'],levels)}</details>`:''}
        ${eeg.length?`<details class="ex-detail"><summary>ЭЭГ во время всей задачи</summary>${table(['Канал','Альфа-мощность','Доля альфа','Окна без флагов'],eeg)}</details>`:''}`;
    }
    const f=a.feedback||{};
    return `<div class="ex-metrics">${metric(run.mode==='demo'?'Синтетический сигнал':'Медианная альфа-мощность',fmt(f.median_alpha,3),'Целевая полоса')}${metric('Целевая полоса',bandLabel(f.band))}${metric('Пригодные измерения',`${f.valid_samples ?? 0} / ${f.total_samples ?? 0}`)}${metric('Отображённые значения',String(f.displayed_samples ?? 0))}${run.mode==='live'?metric('Медиана в полосе 8–13 Гц',fmt(f.median_alpha_fixed,3),'Для сравнения между блоками'):''}</div>
      <p class="ex-copy">В сравнении используется мощность в фиксированной полосе 8–13 Гц. Индивидуальная полоса применяется в тренировке и показана выше.</p>`;
  }
  function quality(a, run) {
    if (run.mode==='demo') return '<div class="ex-tip">'+icon('info')+'<p>В демо показаны работа интерфейса и ответы пользователя. Для анализа ЭЭГ используй сессию с повязкой.</p></div>';
    const q=a.quality||{};
    return `<h3>Качество записи</h3><div class="ex-metrics ex-metrics-small">${metric('Отсчёты в анализе',fmt(q.samples,0),q.channels?.join(', ')||'')}${metric('Разрывы по времени',fmt(q.timestamp_gaps,0),'По программным меткам')}${metric('Пропуски индексов',fmt(q.missing_recorded_rows ?? q.sample_index_gaps,0))}${metric('Ошибки записи',fmt(q.write_errors,0))}${metric('Окна с движением',fmt(q.movement_windows,0),'По данным гироскопа')}</div>
      ${q.warnings?.length?`<ul class="ex-warnings">${q.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul>`:''}
      <p class="ex-copy">Метки экрана и ЭЭГ сопоставлены по программным часам. Аппаратная синхронизация и счётчик пакетов SDK для этого протокола недоступны.</p>`;
  }
  async function openResult(s,id,known) {
    s.chart?.destroy();s.chart=null;s.result=null;
    s.resultId=id;
    for (const b of s.mount.querySelectorAll('[data-ex-result]')) b.setAttribute('aria-pressed',String(b.dataset.exResult===id));
    const detail=$('[data-ex-detail]',s.mount);
    if (!detail) return;
    const key=++s.readKey;
    detail.innerHTML='<section class="ex-panel ex-result" role="status">Загружаем результат…</section>';
    try {
      const [run,a]=await Promise.all([known ? Promise.resolve(known) : request(s,path(id)), analysisFor(s,id)]);
      if (!current(s)||key!==s.readKey||!detail.isConnected) return;
      s.result={run,analysis:a};
      const contactText = finite(run.settings?.clock_round_trip_ms) ? `Отклик сервера при сопоставлении часов ${fmt(run.settings.clock_round_trip_ms)} мс.` : '';
      detail.innerHTML=`<section class="ex-panel ex-result"><div class="ex-archive-head"><div><h2>Результат сессии</h2><p>${esc(catalog[run.kind].name)} · ${esc(stageName(run))} · ${date(run.started_at)} · ${modeName(run.mode)}</p></div><div class="ex-result-actions"><button type="button" class="button subtle" data-ex-export>${icon('download')} JSON</button>${run.session_id?`<a class="button subtle" href="/api/records/${encodeURIComponent(run.session_id)}/download">${icon('folder')} Сырые данные</a>`:''}</div></div>
        ${run.status==='aborted'?'<p class="ex-copy">Сохранена часть сессии. Она доступна в журнале, а в сравнении используются завершённые сессии.</p>':''}
        ${resultMetrics(a,run)}${run.kind==='feedback' && (run.events||[]).some(e=>e.type==='feedback') ? '<div class="ex-chart"><canvas aria-label="Измеренная альфа-мощность и состояние сферы" role="img"></canvas></div>' : ''}${quality(a,run)}${resultContext(run)}<p class="ex-copy">${esc(contactText)}</p>${run.mode==='live'?'<p class="ex-copy">Показатели описывают выполнение задачи и наблюдаемую ЭЭГ. Измерения отдельных синапсов в этом протоколе недоступны.</p>':''}
      </section>`;
      feedbackChart(s,run,$('canvas',detail));
    } catch (err) {
      if (current(s)&&key===s.readKey&&detail.isConnected) detail.innerHTML=`<section class="ex-panel ex-result"><p>${esc(errorText(err))}</p><button class="button subtle" type="button" data-ex-result="${esc(id)}">Загрузить снова</button></section>`;
    }
  }
  function resultContext(run) {
    const c=run.context||{};
    const sleep={poor:'Плохое',average:'Среднее',good:'Хорошее'}[c.sleep_quality];
    const rows=[['Код участника',c.participant_code],['Сон',finite(c.sleep_hours)?`${fmt(c.sleep_hours)} ч`:null],['Качество сна',sleep],['Кофеин',c.caffeine],['Заметки',c.notes]].filter(([,v])=>v);
    return rows.length ? `<details class="ex-detail"><summary>Контекст сессии</summary><dl class="ex-context-summary">${rows.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></details>` : '';
  }
  function feedbackChart(s,run,canvas) {
    if (!canvas || !window.Chart) return;
    const events=run.events.filter(e=>e.type==='feedback'&&e.label!=='calibration'&&finite(e.at_ms));
    if (!events.length) return;
    const start=events[0].at_ms;
    const point=(time,value)=>({x:(time-start)/1000,y:finite(value)?value:null});
    const alpha=events.map(e=>point(e.at_ms,e.alpha_valid?e.alpha_fixed:null));
    const displayed=events.map(e=>point(e.at_ms,e.displayed&&finite(e.value)?e.value*100:null));
    const series=[];
    if (alpha.some(p=>p.y!==null)) series.push({label:run.mode==='demo'?'Синтетическая альфа':'Альфа 8–13 Гц',data:alpha,borderColor:'#89e3cc',yAxisID:'power'});
    if (displayed.some(p=>p.y!==null)) series.push({label:'Показанное значение',data:displayed,borderColor:'#d0b7f4',yAxisID:'shown'});
    if (!series.length) {canvas.closest('.ex-chart').remove();return;}
    s.chart=new Chart(canvas,{type:'line',data:{datasets:series},options:{responsive:true,maintainAspectRatio:false,animation:false,parsing:false,
      elements:{point:{radius:0,hitRadius:5},line:{borderWidth:2}},plugins:{legend:{labels:{color:'#c6d1e3'}}},scales:{
        x:{type:'linear',title:{display:true,text:'Время, с',color:'#adbcd0'},ticks:{color:'#adbcd0'},grid:{color:'#243347'}},
        power:{display:series.some(row=>row.yAxisID==='power'),position:'left',ticks:{color:'#89e3cc'},grid:{color:'#243347'},beginAtZero:true},
        shown:{display:series.some(row=>row.yAxisID==='shown'),position:'right',ticks:{color:'#d0b7f4',callback:v=>`${v}%`},grid:{display:false},min:0,max:100},
      }}});
  }
  async function analysisFor(s,id) {
    if (!s.analyses.has(id)) {
      const pending=request(s,`${path(id)}/analysis`).then(data=>data.analysis || data).catch(err=>{s.analyses.delete(id);throw err;});
      s.analyses.set(id,pending);
    }
    return s.analyses.get(id);
  }

  function comparableRuns(s) {
    return s.runs.filter(run=>run.status==='completed'&&run.mode===s.compareMode&&run.kind===s.compareKind&&run.context?.participant_code);
  }
  function settingsGroup(run) {
    const v=run.settings||{};
    const p=run.kind==='alpha'?[[v.block_s,' с на блок'],[v.repeats,' пары']]:run.kind==='visual'?[[v.trained_orientation,'°'],[v.trial_count,' проб']]:[[v.duration_s,' с на блок']];
    const leads=(run.channels||run.channel_names||[]).join(', ');
    return [...p.filter(([value])=>finite(value)).map(([value,unit])=>`${value}${unit}`),run.kind==='alpha'?alphaOrderLabel(v):'',leads].filter(Boolean).join(' · ')||'Настройки по умолчанию';
  }
  function renderCompare(s) {
    const runs=comparableRuns(s);
    const participants=[...new Set(runs.map(r=>r.context.participant_code))].sort((a,b)=>a.localeCompare(b,'ru'));
    if (!participants.includes(s.compareParticipant)) s.compareParticipant=participants[0]||'';
    const groups=[...new Set(runs.filter(r=>r.context.participant_code===s.compareParticipant).map(settingsGroup))].sort();
    if (!groups.includes(s.compareGroup)) s.compareGroup=groups[0]||'';
    $('[data-ex-page]',s.mount).innerHTML=`<section class="ex-panel"><div class="ex-archive-head"><div><h2>Динамика по сессиям</h2><p>Сравнение по коду участника, источнику и настройкам протокола.</p></div></div>
      <div class="ex-form-grid ex-compare-grid"><label><span>Эксперимент</span>${select('compare_kind',s.compareKind,Object.entries(catalog).map(([key,x])=>[key,x.short]))}</label><label><span>Источник</span>${select('compare_mode',s.compareMode,[['live','С повязкой'],['demo','Демо']])}</label><label><span>Участник</span>${select('compare_participant',s.compareParticipant,participants.length?participants.map(x=>[x,x]):[['','Нет сессий с кодом']])}</label><label><span>Настройки</span>${select('compare_group',s.compareGroup,groups.length?groups.map(x=>[x,x]):[['','—']])}</label></div>
      <div data-ex-compare-output></div></section>`;
    const matched=runs.filter(r=>r.context.participant_code===s.compareParticipant&&settingsGroup(r)===s.compareGroup).sort((a,b)=>new Date(a.started_at)-new Date(b.started_at));
    loadComparison(s,matched);
  }
  function ratioSeries(a){return Object.fromEntries((a.alpha?.channels||[]).map(c=>[c.name,c.closed_open_ratio]));}
  async function loadComparison(s, matched) {
    const output=$('[data-ex-compare-output]',s.mount);
    const chartKind=s.compareKind;
    if (matched.length<2) {
      output.innerHTML=`<div class="ex-empty">${icon('compare')}<strong>Нужны две завершённые сессии</strong><span>Укажи одинаковый код участника и повтори протокол с теми же настройками.</span></div>`;
      return;
    }
    output.textContent='Рассчитываем показатели сессий…';
    const items=[];
    for (let i=0;i<matched.length;i+=3) {
      const batch=await Promise.allSettled(matched.slice(i,i+3).map(row=>analysisFor(s,row.id)));
      if (!current(s)||!output.isConnected) return;
      for (let j=0;j<batch.length;j++) items.push({run:matched[i+j],a:batch[j].value||{}});
    }
    const trained='Тренировочная ориентация', control='Контрольная ориентация';
    const labels=items.map(({run})=>date(run.started_at));
    let series={}, tableHead=[], tableRows=[], yTitle='';
    if (chartKind==='visual') {
      const test=({run})=>run.stage!=='training';
      series={[trained]:items.map(x=>test(x)?x.a.visual?.conditions?.trained?.accuracy_pct:null),[control]:items.map(x=>test(x)?x.a.visual?.conditions?.control?.accuracy_pct:null)};
      yTitle='Точность, %';
      tableHead=['Дата','Этап','Точность · трен.','Точность · контр.','Порог · трен.','Порог · контр.'];
      tableRows=items.map(({run,a},i)=>[labels[i],stageName(run),formatValue(a.visual?.conditions?.trained?.accuracy_pct,'%'),formatValue(a.visual?.conditions?.control?.accuracy_pct,'%'),formatValue(a.visual?.conditions?.trained?.threshold_75_deg,'°'),formatValue(a.visual?.conditions?.control?.threshold_75_deg,'°')]);
    } else if (chartKind==='alpha') {
      const channels=[...new Set(items.flatMap(x=>Object.keys(ratioSeries(x.a))))];
      series=Object.fromEntries(channels.map(ch=>[ch,items.map(x=>ratioSeries(x.a)[ch])]));
      yTitle='Закрытые / открытые';
      tableHead=['Дата','Этап',...channels];
      tableRows=items.map(({run,a},i)=>[labels[i],stageName(run),...channels.map(ch=>formatValue(ratioSeries(a)[ch],'×'))]);
    } else {
      series={'Альфа-мощность 8–13 Гц':items.map(x=>x.a.feedback?.median_alpha_fixed)};
      yTitle='Альфа-мощность 8–13 Гц, ед. SDK²';
      tableHead=['Дата','Этап','Целевая полоса','Медиана 8–13 Гц','Пригодные измерения'];
      tableRows=items.map(({run,a},i)=>[labels[i],stageName(run),bandLabel(a.feedback?.band),fmt(a.feedback?.median_alpha_fixed,3),`${a.feedback?.fixed_valid_samples ?? 0} / ${a.feedback?.total_samples ?? 0}`]);
    }
    const plotted=Object.values(series).some(row=>row.filter(finite).length>1);
    output.innerHTML=`${plotted?'<div class="ex-chart"><canvas aria-label="Динамика показателей по датам" role="img"></canvas></div>':'<p class="ex-copy">Пока нет двух пригодных точек для одной серии графика.</p>'}${table(tableHead,tableRows)}
      <p class="ex-copy">${chartKind==='visual'?'На графике показаны тесты без подсказок. Тренировки видны в таблице. ':''}${s.compareMode==='demo'?'Сравнение демонстрационных сессий. ':''}Одна точка соответствует одной сессии. Оценивай изменение вместе с качеством записи, датой и условиями проведения.</p>`;
    if (plotted && window.Chart) {
      const colors=['#83e4ca','#8dabff','#d1b1f9','#f3bf7f'];
      s.chart=new Chart($('canvas',output),{type:'line',data:{labels,datasets:Object.entries(series).map(([label,data],i)=>({label,data,borderColor:colors[i%colors.length],backgroundColor:colors[i%colors.length],pointRadius:4,borderWidth:2,tension:0}))},
        options:{responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{labels:{color:'#c6d1e3',usePointStyle:true}}},scales:{x:{ticks:{color:'#adbcd0'},grid:{display:false}},y:{title:{display:true,text:yTitle,color:'#adbcd0'},ticks:{color:'#adbcd0'},grid:{color:'#243347'},...(chartKind==='visual'?{min:0,max:100}:{beginAtZero:true})}}}});
    }
  }

  function exportResult(s) {
    if (!s.result) return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(s.result,null,2)],{type:'application/json'}));
    const link=document.createElement('a');
    link.href=url;link.download=`experiment-${s.result.run.id}.json`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),3000);
  }
  function closeTestSound(s) {
    const audio = s.testAudio;
    s.testAudio = null;
    if (audio && audio.state !== 'closed') void audio.close().catch(()=>{});
  }
  async function testCues(s, button) {
    if (s.testAudio) return;
    const Runner = window.NeuroExperimentRunner;
    const audio = Runner.primeSound();
    if (!audio) { s.notify('Звуковой выход браузера недоступен.', 'error'); return; }
    button.disabled = true;
    s.testAudio = audio;
    let played = false;
    try {
      await audio.resume();
      if (s.testAudio !== audio || !current(s)) return;
      if (audio.state !== 'running') throw Error('Браузер не разрешил звук.');
      Runner.playCue(audio, 'open');
      Runner.playCue(audio, 'closed', 1);
      Runner.playCue(audio, 'complete', 2);
      played = true;
      await new Promise(resolve=>setTimeout(resolve, 2700));
    } catch (error) {
      if (s.testAudio === audio && current(s)) s.notify(errorText(error), 'error');
    } finally {
      if (s.testAudio === audio) closeTestSound(s);
      else played = false;
      button.disabled = false;
    }
    if (played && current(s) && s.tab==='setup' && !s.starting && !s.runner) {
      s.notify('Вы должны услышать два тона, затем один и три. Если какой-то сигнал не слышен, проверьте громкость.');
    }
  }

  function bind(s) {
    const root=s.mount;
    root.addEventListener('input',event=>{
      const {name,value,checked}=event.target;
      if (name in s.context) s.context[name]=value;
      else if (name==='confirmed') s.confirmed=checked;
      else if (name==='sound') s.sound=checked;
    });
    root.addEventListener('change',event=>{
      const {name,value}=event.target;
      const form=s.forms[s.kind];
      if (name in form) {
        form[name]=value;
        if (name==='stage' && s.kind==='visual' && value!=='training' && Number(form.trial_count)>160) form.trial_count=80;
        if (name==='duration_s') form.replay_run_id='';
        if (['stage','mode','duration_s'].includes(name)) {render(s); $(`select[name="${name}"]`,s.mount)?.focus();}
        else connection(s);
      } else if (name==='history_mode') {s.historyMode=value;s.resultId=null;render(s);}
      else if (name?.startsWith('compare_')) {
        const property={compare_kind:'compareKind',compare_mode:'compareMode',compare_participant:'compareParticipant',compare_group:'compareGroup'}[name];
        if(property){s[property]=value;render(s);}
      }
    });
    root.addEventListener('submit',event=>{
      if (!event.target.matches('[data-ex-form]')) return;
      event.preventDefault();start(s);
    });
    root.addEventListener('click',event=>{
      const b=event.target.closest('button');
      if (!b || b.disabled || s.starting) return;
      if ('exTestSound' in b.dataset) {
        void testCues(s, b);
      } else if (b.dataset.exSelect) {
        s.kind=b.dataset.exSelect;s.confirmed=false;render(s);
        $(`[data-ex-select="${s.kind}"]`,s.mount)?.focus();
      } else if (b.dataset.exTab) {
        s.tab=b.dataset.exTab;s.resultId=null;s.readKey++;render(s);
        $(`[data-ex-tab="${s.tab}"]`,s.mount)?.focus();
        if(s.tab==='history'||s.tab==='compare')refreshRuns(s,true);
      } else if (b.dataset.exResult) openResult(s,b.dataset.exResult);
      else if ('exExport' in b.dataset) exportResult(s);
      else if ('exRefresh' in b.dataset) refreshRuns(s,true);
      else if ('exAbortActive' in b.dataset) {
        const active=s.device?.active_run_id||s.device?.active_run?.id;
        if (!active) return;
        b.disabled=true;
        request(s,`${path(active)}/finish`,post({status:'aborted'})).then(doc=>finished(s,doc)).catch(err=>{if(current(s))s.notify(errorText(err),'error');}).finally(()=>{b.disabled=false;});
      }
    });
  }
  function open(options) {
    const content=$('#content');
    if(state){
      const s=state;
      content.replaceChildren(s.mount);
      s.runner?.setAway('section',false);
      if(!s.runner&&!s.starting){render(s);refreshStatus(s);refreshRuns(s,true);}
      s.timer ??= setInterval(()=>{if(!s.runner&&!s.starting)refreshStatus(s);},2500);
      return;
    }
    if(options.preview){content.innerHTML='<section class="ex-panel"><h2>Лаборатория экспериментов</h2><p>Для запуска протоколов открой NeuroScope на локальном сервере.</p></section>';return;}
    content.innerHTML='<div id="experiment-lab"></div>';
    const s={...options,mount:$('#experiment-lab'),tab:'setup',kind:'alpha',historyMode:'live',compareMode:'live',compareKind:'alpha',
      compareParticipant:'',compareGroup:'',runs:[],analyses:new Map(),readKey:0,sound:true,confirmed:false,
      context:{participant_code:'',sleep_hours:'',sleep_quality:'',caffeine:'',notes:''},
      forms:{alpha:{mode:'live',stage:'baseline',block_s:60,repeats:4,alpha_order:'counterbalanced'},visual:{mode:'live',stage:'baseline',trained_orientation:45,trial_count:80},feedback:{mode:'live',stage:'baseline',duration_s:120,replay_run_id:''}}};
    state=s;bind(s);render(s);refreshStatus(s);refreshRuns(s,true);
    s.timer=setInterval(()=>{if(!s.runner&&!s.starting)refreshStatus(s);},2500);
  }
  function leave() {
    if (!state) return;
    const s=state;
    closeTestSound(s);
    clearInterval(s.timer);s.timer=null;
    s.chart?.destroy();s.chart=null;
    if(s.runner||s.starting){
      s.runner?.setAway('section',true);
      s.mount.remove();
    }else state=null;
  }
  window.NeuroExperimentLab={open,leave};
})();

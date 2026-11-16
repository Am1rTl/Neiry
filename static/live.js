/* Unified NeuroScope hardware, real-time monitor and session manager.
 * Connection status, guided calibration, and signal views use the local device API.
 */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const n = v => v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
  const fmt = (v, digits=1) => n(v) == null ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:digits});
  const blank = '—';
  let ctx = {}, view = null, preview = false, pollTimer = null, statusRequest = null, ws = null, wsRetry = null;
  let status = null, replay = {active:false}, chartClock = 0, statusFingerprint = '', sessionRows = [], liveRange = 15, metricRange = 300, allowRaw = false;
  let calibrationAudio = null, useCalibrationSound = true;
  let calibrationAudioRun = false, calibrationCue = '', nextPulseAt = 0;
  let plotObserver = null, chartFrame = null, lastStreamStatusAt = -Infinity, lastIndicatorsAt = -Infinity;
  let diagnosticTimer = null, diagnosticRequest = null, diagnosticSource = '';
  const diagnosticInterval = {quality:4000, spectrogram:4000, paired:12000};
  const lastDiagnosticAt = {};
  const visibleCharts = new Set();
  const visibleDiagnostics = new Set();
  const renderedMarkup = new WeakMap();
  const playingNotes = new Set();
  const chimes = {
    closed:[587.33,440], open:[440,587.33], progress:[493.88],
    complete:[523.25,659.25,783.99], failed:[392,329.63]
  };
  const samples = {}, lastRawTime = {}, charts = new Map();
  const channelColors = ['#7ce3cb','#8ca9ff','#f0c38a','#e4a2ad','#c1a9f0','#85c9ee'];
  const axisFormat = new Intl.NumberFormat('ru-RU',{maximumSignificantDigits:5});
  let chartSource = '', chartEnd = 0, resistanceSince = -Infinity;
  const chartDefs = [
    {id:'live-eeg',keys:[],series:[],raw:true},
    {id:'live-ppg',keys:['ppg'],series:[['ФПГ','#e4a2ad']],raw:true},
    {id:'live-motion',keys:['motion'],series:[['Модуль ускорения','#f0c38a']],raw:true},
    {id:'live-acc',keys:['accX','accY','accZ'],series:[['X','#7ce3cb'],['Y','#8ca9ff'],['Z','#f0c38a']],raw:true},
    {id:'live-gyro',keys:['gyroX','gyroY','gyroZ'],series:[['X','#7ce3cb'],['Y','#8ca9ff'],['Z','#e4a2ad']],raw:true},
    {id:'live-resistance',keys:[],series:[],unit:'кОм'},
    {id:'live-hr-chart',keys:['hr'],series:[['Пульс','#e4a2ad']],unit:'уд/мин'},
    {id:'live-si-chart',keys:['stressIndex'],series:[['Индекс стресса SDK','#f0c38a']]},
    {id:'live-ki-chart',keys:['kaplanIndex'],series:[['Индекс Каплана SDK','#8ca9ff']]},
    {id:'live-emotions',keys:['attention','relaxation','cognitiveLoad','cognitiveControl','selfControl'],series:[['Внимание','#7ce3cb'],['Расслабление','#8ca9ff'],['Когнитивная нагрузка','#f0c38a'],['Контроль','#e4a2ad'],['Самоконтроль','#c1a9f0']]},
    {id:'live-phy',keys:['phyRelax','phyConcentration','phyFatigue','phyStress','phyInvolvement'],series:[['Релаксация','#7ce3cb'],['Концентрация','#8ca9ff'],['Утомление','#f0c38a'],['Стресс','#e4a2ad'],['Вовлечённость','#c1a9f0']]},
    {id:'live-prod',keys:['productivityScore','concentrationScore','relaxationScore','fatigueScore'],series:[['Продуктивность','#7ce3cb'],['Концентрация','#8ca9ff'],['Релаксация','#c1a9f0'],['Утомление','#f0c38a']]},
    {id:'live-nfb',keys:['alpha','beta','theta','delta','smr'],series:[['Альфа','#7ce3cb'],['Бета','#8ca9ff'],['Тета','#f0c38a'],['Дельта','#e4a2ad'],['SMR','#c1a9f0']]}
  ];
  for(const spec of chartDefs) for(const key of spec.keys) samples[key]=[];
  const statuses = {
    idle:['Не подключено','neutral'], searching:['Поиск устройства','pending'],
    found:['Устройство найдено','pending'], connecting:['Подключаем…','pending'],
    connected:['Подключено','good'], disconnected:['Связь потеряна','bad'],
    error:['Ошибка подключения','bad'], sdk_missing:['SDK не найден','bad']
  };
  function label(state) { return statuses[state] || ['Ожидание','neutral']; }
  function notice(message, kind='') { ctx.notify?.(message,kind); }
  function setText(el,value) {
    if(!el || el.textContent===value)return;
    renderedMarkup.delete(el);
    el.textContent=value;
  }
  function setMarkup(el,html) {
    if(!el || renderedMarkup.get(el)===html)return;
    renderedMarkup.set(el,html);
    el.innerHTML=html;
  }
  function pButton(name, action, cls='subtle', disabled=false) {
    return `<button type="button" class="button ${cls}" data-action="${esc(action)}" ${disabled?'disabled':''}>${esc(name)}</button>`;
  }
  function connected() { return Boolean(status?.connected); }
  function safeInfo(s) {
    if (!s?.sdk_available || s.status === 'sdk_missing') return 'SDK не найден. Все архивные записи и научный анализ доступны без устройства.';
    if (s.status === 'searching') return 'Поиск Bluetooth выполняется. Включите повязку и держите её рядом с компьютером.';
    if (s.status === 'connecting') return 'Проверяем реальное соединение. Если подключение не удастся, появится ошибка и кнопка повторного поиска.';
    return s?.message || 'Нажмите «Найти устройство», чтобы обнаружить повязку.';
  }
  function stopCalibrationNotes() {
    for (const note of playingNotes) note.stop();
    playingNotes.clear();
  }
  async function unlockCalibrationAudio() {
    try {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!calibrationAudio) calibrationAudio = new Audio();
      await calibrationAudio.resume();
      return calibrationAudio.state === 'running';
    } catch (_) {
      return false;
    }
  }
  function calibrationChime(kind, delay=0) {
    if (!useCalibrationSound || calibrationAudio?.state !== 'running') return;
    const audio = calibrationAudio;
    const peak = kind==='progress' ? 0.035 : 0.075;
    chimes[kind].forEach((frequency, i)=>{
      const at = audio.currentTime + 0.02 + delay + i * 0.18;
      const tone = audio.createOscillator();
      const volume = audio.createGain();
      tone.frequency.value = frequency;
      volume.gain.setValueAtTime(0, at);
      volume.gain.linearRampToValueAtTime(peak, at+0.05);
      volume.gain.exponentialRampToValueAtTime(0.0001, at+0.43);
      tone.connect(volume).connect(audio.destination);
      tone.onended = ()=>{playingNotes.delete(tone);tone.disconnect();volume.disconnect();};
      playingNotes.add(tone);
      tone.start(at);
      tone.stop(at+0.44);
    });
  }
  function syncCalibrationAudio(s) {
    if (!calibrationAudioRun) return;
    const run = s?.calibration_run;
    if (!run || s.calibration_phase !== 'running') {
      stopCalibrationNotes();
      calibrationChime(s?.calibration_phase==='complete'?'complete':'failed');
      calibrationAudioRun = false;
      calibrationCue = '';
      return;
    }
    const now = performance.now();
    const cue = `${run.mode}:${run.stage}`;
    if (cue !== calibrationCue) {
      stopCalibrationNotes();
      calibrationCue = cue;
      nextPulseAt = now + 4500;
      calibrationChime(run.eye_state);
    } else if (!run.preparing && now >= nextPulseAt) {
      nextPulseAt = now + 4500;
      calibrationChime('progress');
    }
  }
  function openCalibrationSetup() {
    const dialog = $('#calibration-setup');
    if (!dialog) return;
    dialog.querySelector('#calibration-mode-full').disabled = !status?.calibration_modes?.includes('full');
    dialog.querySelector('#calibration-mode-quick').checked = true;
    dialog.querySelector('#calibration-sound').checked = true;
    dialog.querySelector('#calibration-auto-record').checked = true;
    dialog.querySelector('#calibration-confirmed-eyes').checked = false;
    dialog.querySelector('#calibration-dialog-error').textContent = '';
    dialog.showModal();
  }
  function setupCalibrationDialog() {
    const dialog = document.createElement('dialog');
    dialog.id = 'calibration-setup';
    dialog.className = 'cal-dialog';
    dialog.setAttribute('aria-labelledby', 'calibration-dialog-title');
    dialog.innerHTML = `
      <form>
        <h2 id="calibration-dialog-title">Калибровка Capsule</h2>
        <p>Сядьте удобно, держите голову неподвижно. В открытых этапах смотрите на крест.</p>
        <fieldset>
          <legend>Режим</legend>
          <label class="cal-choice"><input type="radio" name="mode" id="calibration-mode-quick" value="quick" checked>
            <span><strong>Быстрая · около 30 с</strong><small>Глаза закрыты до сигнала завершения.</small></span></label>
          <label class="cal-choice"><input type="radio" name="mode" id="calibration-mode-full" value="full">
            <span><strong>Полная · около 1,5 мин</strong><small>Закрыть → открыть → закрыть → открыть. Четыре этапа по ≈20 с.</small></span></label>
        </fieldset>
        <label class="cal-choice"><input type="checkbox" id="calibration-sound" checked><span><strong>Звуковые подсказки</strong>
          <small>Два нисходящих тона — закрыть глаза, два восходящих — открыть. Тихий одиночный тон звучит во время этапа.</small></span></label>
        <button type="button" class="button text" id="calibration-preview">Прослушать подсказки</button>
        <label class="cal-choice"><input type="checkbox" id="calibration-auto-record" checked><span><strong>Начать запись после калибровки</strong>
          <small>NeuroScope сохранит сигналы сразу после результата Capsule.</small></span></label>
        <label class="cal-choice"><input type="checkbox" id="calibration-confirmed-eyes" required><span>
          <strong>Я готов закрыть глаза по сигналу и оставаться неподвижным</strong></span></label>
        <p id="calibration-dialog-error" role="status"></p>
        <div class="cal-actions"><button type="submit" class="button primary">Подтвердить и начать</button>
          <button type="button" class="button subtle" id="calibration-cancel">Отмена</button></div>
      </form>`;
    document.body.append(dialog);
    const form = dialog.querySelector('form');
    const checkbox = dialog.querySelector('#calibration-sound');
    const confirmation = dialog.querySelector('#calibration-confirmed-eyes');
    const error = dialog.querySelector('#calibration-dialog-error');
    const start = form.querySelector('[type="submit"]');
    const cancel = dialog.querySelector('#calibration-cancel');
    const previewButton = dialog.querySelector('#calibration-preview');
    dialog.addEventListener('close', ()=>{
      if (!calibrationAudioRun) stopCalibrationNotes();
    });
    dialog.addEventListener('cancel', e=>{if(start.disabled)e.preventDefault();});
    cancel.addEventListener('click', ()=>dialog.close());
    previewButton.addEventListener('click', async ()=>{
      useCalibrationSound = true;
      if (!await unlockCalibrationAudio()) {
        error.textContent = 'Браузер не смог включить звук.';
        return;
      }
      if (!dialog.open) return;
      error.textContent = '';
      stopCalibrationNotes();
      calibrationChime('closed');
      calibrationChime('open', 0.75);
      calibrationChime('progress', 1.55);
    });
    form.addEventListener('submit', async e=>{
      e.preventDefault();
      if (!confirmation.checked) return;
      const mode = new FormData(form).get('mode');
      const auto_record = dialog.querySelector('#calibration-auto-record').checked;
      start.disabled = cancel.disabled = previewButton.disabled = true;
      error.textContent = '';
      stopCalibrationNotes();
      useCalibrationSound = checkbox.checked;
      try {
        if (useCalibrationSound && !await unlockCalibrationAudio()) {
          notice('Звук недоступен. Следите за инструкцией на экране.','error');
        }
        await api('/api/device/calibration/start',{method:'POST',body:JSON.stringify({mode,auto_record,confirmed_closed_eyes:true})});
        calibrationAudioRun = true;
        calibrationCue = '';
        dialog.close();
        await poll();
        scheduleStatusPoll();
      } catch (err) {
        error.textContent = err.message || 'Не удалось запустить калибровку.';
      } finally {
        start.disabled = cancel.disabled = previewButton.disabled = false;
      }
    });
  }
  function calibrationPanel(s) {
    const run = s.calibration_run;
    if (run) {
      const eyes = run.eye_state==='closed'?'Закройте глаза':'Откройте глаза';
      return `<div class="cal-state" role="status"><small>Этап ${run.stage} из ${run.stage_count}</small><strong class="cal-eyes">${eyes}</strong></div>
        <div class="cal-fixation" ${run.eye_state==='closed'?'hidden':''} aria-hidden="true">+</div>
        <progress id="calibration-stage-progress" max="${run.expected_stage_s}" value="0" aria-label="Примерный прогресс этапа"></progress>
        <small class="cal-timer" id="calibration-stage-time"></small>`;
    }
    const titles = {ready:'Калибровка доступна',failed:'Калибровка не удалась',skipped:'Калибровка пропущена',
      unavailable:'Калибратор недоступен',complete:'Калибровка NFB завершена'};
    let detail = s.calibration_available ? 'Выберите режим и следуйте подсказкам.' : s.contact?.ready
      ? 'Калибратор Capsule недоступен.' : 'Дождитесь стабильного контакта электродов.';
    if (s.calibration_phase==='failed') detail = s.message || detail;
    if (s.calibration_reconnect) detail += ' Переподключите повязку для повтора.';
    if (s.calibration_phase==='complete' && s.calibration_result) {
      const result = s.calibration_result;
      detail = `Индивидуальная частота SDK · ${fmt(result.individualFrequency,2)} Гц. Глаза можно открыть.`;
      if (s.recording_error) detail += ` ${s.recording_error}`;
      else if (s.recording) detail += s.recording_origin==='calibration'?' Запись началась автоматически.':' Запись продолжается.';
    }
    return `<div class="cal-state" role="status"><strong>${titles[s.calibration_phase]||'Нет калибровки'}</strong><small>${esc(detail)}</small></div>
      <div class="cal-actions">${pButton(s.calibrated?'Повторить калибровку':'Настроить калибровку','calibration','subtle',!s.calibration_available)}
        ${s.calibration_phase==='ready'?pButton('Пропустить','skip-cal','subtle'):''}</div>`;
  }
  function updateCalibrationProgress(s) {
    const run = s.calibration_run;
    const bar = $('#calibration-stage-progress');
    const time = $('#calibration-stage-time');
    if (!run || !bar || !time) return;
    bar.value = Math.min(run.expected_stage_s, run.elapsed_s);
    time.textContent = run.preparing ? `Начало через ${Math.ceil(run.prepare_remaining_s)} с`
      : run.elapsed_s < run.expected_stage_s ? `${Math.floor(run.elapsed_s)} / ≈${run.expected_stage_s} с`
      : 'Ожидаем подтверждение этапа от Capsule…';
  }
  function contactPanel(contact, compact=false) {
    const c = contact || {state:'waiting',channels:[],message:'Ожидаем реальные показания сопротивления SDK.'};
    const stateLabel={disconnected:'Без подключения',waiting:'Ожидаем измерения',stale:'Данные устарели',
      incomplete:'Не все электроды',poor:'Контакт плохой',settling:'Контакт стабилизируется',ready:'Контакт готов'}[c.state]||'Контакт не определён';
    const channelRows=(c.channels||[]).map((r,i)=>{
      const value=n(r.kohm),max=n(c.ready_below_kohm)||1000;
      const fill=value===null?0:Math.max(0,Math.min(100,value/max*100));
      return `<div class="contact-electrode ${r.good?'good':'bad'}">
         <div class="contact-electrode-head"><strong>${esc(r.channel||`Электрод ${i+1}`)}</strong><span>${value===null?'Нет измерения':`${fmt(value,1)} кОм${c.state==='stale'?' · устарело':''}`}</span></div>
         <div class="contact-meter"><span style="width:${fill}%"></span></div>
         <small>${value===null?'Нет достоверного значения':r.good?(r.preferred?'Хороший контакт по порогу приложения':'Контакт допустим, можно улучшить'): `Сопротивление выше порога ${fmt(max,0)} кОм`}</small></div>`;
    }).join('');
    const progress=c.state==='settling'?`<div class="contact-progress"><progress max="${n(c.required_seconds)||6}" value="${n(c.stable_seconds)||0}"></progress><span>Стабильный контакт: ${fmt(c.stable_seconds,1)} / ${fmt(c.required_seconds,0)} с · ${fmt(c.sample_count,0)} измерений</span></div>`:'';
    return `<div class="contact-panel ${c.state==='ready'?'ready':c.state==='poor'?'poor':''}">
      <div class="contact-overview"><span class="contact-flag ${esc(c.state)}">${esc(stateLabel)}</span><strong>${esc(c.message||'Ожидаем данные')}</strong></div>
      ${channelRows?`<div class="contact-electrodes">${channelRows}</div>`:'<p class="chart-caption">Нет актуальных измерений сопротивления. Значения не моделируются.</p>'}
      ${progress}<p class="chart-caption">Только настоящие отсчёты сопротивления Capsule SDK · порог ${fmt(c.ready_below_kohm,0)} кОм по каждому каналу; желательнее ниже ${fmt(c.preferred_below_kohm,0)} кОм. Это проверка контакта, а не подтверждение нахождения на голове или чистоты ЭЭГ.</p>
      ${compact?'':'<p class="chart-caption">Поправьте посадку, раздвиньте волосы под электродами. Не давите слишком сильно. При снятии или потере данных отсчёт стабильности начинается заново.</p>'}
    </div>`;
  }
  function contactSignature(contact){
    if(!contact)return '';
    const {state,message,ready_below_kohm,preferred_below_kohm,required_seconds,stable_seconds,sample_count,channels}=contact;
    return JSON.stringify([state,message,ready_below_kohm,preferred_below_kohm,
      state==='settling'?[required_seconds,stable_seconds,sample_count]:null,
      (channels||[]).map(r=>[r.channel,n(r.kohm)===null?null:Math.round(r.kohm*10),r.good,r.preferred])]);
  }
  function statusBox(s) {
    const [title, tone] = label(s?.status);
    return `<div class="device-hero ${tone}">
      <div class="device-emblem"><span class="connection-ring"></span><svg class="icon"><use href="#i-wave"/></svg></div>
      <div class="device-hero-copy"><div class="section-kicker">ПОДКЛЮЧЕНИЕ / CAPSULE</div>
        <h2>${esc(title)}</h2><p>${esc(safeInfo(s))}</p>
        ${s?.connected?`<div class="device-facts"><span>${esc(s.device || 'Neiry Headband')}</span><span>Серийный № ${esc(s.serial||blank)}</span><span>Батарея ${n(s.battery)!==null?`${fmt(s.battery,0)}%`:blank}</span>${s.firmware_version?`<span>Прошивка ${esc(s.firmware_version)}</span>`:''}${s.sdk_version?`<span>Capsule ${esc(s.sdk_version)}</span>`:''}</div>`:''}
      </div><div class="device-hero-actions">${s?.connected?pButton('Отключить','disconnect','subtle'):
        pButton(s?.scanning?'Идёт поиск…':'Найти устройство','scan','primary',Boolean(s?.scanning||s?.status==='connecting'||!s?.sdk_ready))}</div>
    </div>`;
  }
  function devicePanel(s) {
    if (!s) s = {status:'idle',sdk_available:false,discovered:[]};
    const devices = Array.isArray(s.discovered) ? s.discovered : [];
    const found = s.connected ? `<div class="info-stack"><strong>Текущее подключение</strong><p>Потоки доступны в разделе «Живой монитор». Отключение завершит текущую запись и освободит устройство.</p><a class="button primary" href="#live" data-go="live">Перейти к сигналам</a></div>`:
      devices.length ? devices.map(d=>`<div class="found-device"><span class="found-icon"><svg class="icon"><use href="#i-wave"/></svg></span><span class="found-copy"><strong>${esc(d.name)}</strong><small>${esc(d.serial)}</small></span>${pButton('Подключить','connect:'+d.serial,'primary',s.status==='connecting')}</div>`).join('') :
      `<div class="no-device">${s.scanning?'<span class="loader"></span> Поиск выполняется…':'Пока нет найденных устройств.'}<small>Поиск выполняется только после нажатия кнопки. Его можно повторить.</small></div>`;
    const cal = s.connected ? `${contactPanel(s.contact)}<div class="cal-box">${calibrationPanel(s)}</div>`:
      `<div class="no-device small">Подключите повязку для калибровки.</div>`;
    return `${statusBox(s)}<div class="device-two-col">
      <section class="card">${heading('Обнаруженные устройства','Сначала поиск, затем явный выбор и проверка соединения')}${found}
      <div class="connection-options"><label class="toggle-row"><input type="checkbox" id="auto-reconnect" ${s.auto_reconnect?'checked':''} ${!s.sdk_ready?'disabled':''}><span><strong>Автоматическое переподключение</strong><small>По умолчанию выключено; после потери связи повторить поиск и попытаться подключить прежнее устройство</small></span></label></div></section>
      <section class="card">${heading('Контакт электродов и калибровка','Сначала измерения сопротивления, затем стабильный контакт и калибровка')}${cal}
      <div class="steps-list"><div><b>01</b><span>Включите повязку и Bluetooth на компьютере.</span></div><div><b>02</b><span>Нажмите «Найти устройство», дождитесь окончания поиска.</span></div><div><b>03</b><span>Выберите найденный Neiry Headband. Приложение подтвердит связь через SDK.</span></div><div><b>04</b><span>Наденьте повязку, дождитесь снижения реального сопротивления каждого канала и стабильности показаний.</span></div><div><b>05</b><span>После проверки контакта запустите калибровку. При пропуске персональные метрики SDK не отображаются.</span></div></div></section>
    </div>${!s.sdk_ready?`<section class="card device-warning">${heading('SDK не готов','Это не значит, что записи недоступны')}<p>Живое подключение требует поставку Capsule SDK и нативную библиотеку для вашей системы. Укажите <code>CAPSULE_SDK_DIR</code> перед запуском и проверьте, что Bluetooth доступен приложению.</p><p class="device-error">${esc(s.sdk_error || 'При отсутствии SDK аппаратный поиск недоступен.')}</p><a href="#sessions" data-go="sessions" class="button subtle">Открыть сохранённые записи</a></section>`:''}`;
  }
  function heading(title,subtitle='') {return `<div class="card-header"><div><h2>${esc(title)}</h2>${subtitle?`<p class="card-subtitle">${esc(subtitle)}</p>`:''}</div></div>`;}
  function livePlot(id, title, subtitle='', wide=false, details='') {
    return `<section class="card${wide?' wide':''}">${heading(title,subtitle)}
      <div class="live-canvas"><canvas id="${id}" aria-label="${esc(title)}" role="img"></canvas>
        <p class="live-chart-empty">Ожидаем отсчёты</p></div>${details}</section>`;
  }
  function livePanel() {
    return `<div class="live-banner" id="live-banner"><span class="activity-dot"></span><div><strong id="live-mode">Ожидание сигнала</strong><p id="live-message">Подключите устройство или запустите воспроизведение записи.</p></div><div class="live-banner-actions"><a class="button subtle" href="#device" data-go="device">Устройство</a><button class="button subtle" id="replay-pause" data-action="pause-replay" hidden>Пауза</button><button class="button subtle" id="replay-forward" data-action="seek-forward" hidden>+30 с</button><button class="button subtle" id="replay-stop" data-action="stop-replay" hidden>Остановить воспроизведение</button></div></div>
      <div id="live-contact-card" class="contact-live-wrap">${contactPanel(status?.contact,true)}</div>
      <div class="live-metrics"><section class="metric-card"><div class="metric-label">Пульс · SDK</div><div class="metric-value" id="live-hr">—</div><div class="metric-note" id="live-hr-note">уд/мин · Capsule SDK</div></section>
      <section class="metric-card"><div class="metric-label">Внимание · SDK</div><div class="metric-value" id="live-attention">—</div><div class="metric-note">Индекс производителя, не % концентрации</div></section>
      <section class="metric-card"><div class="metric-label">Расслабление · SDK</div><div class="metric-value" id="live-relax">—</div><div class="metric-note">Индекс производителя</div></section></div>
      <section class="card live-stream-card">${heading('Контакт и поток','Отсчёты датчиков и доступность расчётов Capsule')}<div id="live-sensors" class="sensor-summary"><p>Потоков пока нет.</p></div><div id="live-resistances" class="resistance-values"></div></section>
      <div class="studio-controls"><label style="max-width:200px">Сырые сигналы · окно<select id="live-time-range">${[5,15,30,60].map(s=>`<option value="${s}" ${s===liveRange?'selected':''}>${s} с</option>`).join('')}</select></label><label style="max-width:210px">Индексы SDK · окно<select id="metric-time-range">${[60,300,900].map(s=>`<option value="${s}" ${s===metricRange?'selected':''}>${s/60} мин</option>`).join('')}</select></label><label class="toggle-row raw-toggle"><input type="checkbox" id="show-unverified-raw" ${allowRaw?'checked':''}><span><strong>Диагностический сырой поток</strong><small>Показывать ЭЭГ и ФПГ до проверки контакта</small></span></label></div>
      <section class="live-graph-group" aria-labelledby="live-primary-label">
        <h2 id="live-primary-label">ЭЭГ и пульс</h2><div class="live-graphs">
          ${livePlot('live-eeg','ЭЭГ','Метки каналов и частота дискретизации из SDK',true,'<p class="chart-caption"><span id="live-channels">Каналы: —</span> <span id="live-eeg-sr"></span></p>')}
          ${livePlot('live-ppg','ФПГ · пульсовая волна','Оптический сигнал',false,'<p class="chart-caption" id="live-ppg-sr"></p>')}
          ${livePlot('live-hr-chart','Пульс во времени · SDK','Фрагменты, прошедшие проверку флагов Capsule')}
          <section class="card wide" data-live-diagnostic="spectrogram">${heading('Спектрограмма ЭЭГ','Мощность нормируется отдельно для каждого канала')}<div class="live-spectrograms" id="live-spectrograms"></div><p class="chart-caption" id="live-spectrogram-note">Для построения нужны не менее трёх секунд ЭЭГ.</p></section>
          <section class="card wide" data-live-diagnostic="quality">${heading('Диагностика ЭЭГ','Постоянный сигнал, округление и помехи 50/60 Гц')}<div id="live-quality-state" class="studio-grid"><p class="chart-caption">Ожидаем достаточное окно ЭЭГ.</p></div></section>
          <section class="card wide" data-live-diagnostic="paired">${heading('Спектр ЭЭГ на 3D-схеме','Доли мощности обоих отведений по частотным полосам')}${window.NeuroPairedPanel.markup()}</section>
        </div>
      </section>
      <section class="live-graph-group" aria-labelledby="live-motion-label">
        <h2 id="live-motion-label">Движение и электроды</h2><div class="live-graphs">
          ${livePlot('live-motion','Модуль ускорения','Модуль вектора XYZ',true,'<p class="chart-caption" id="live-mems-sr"></p>')}
          ${livePlot('live-acc','Акселерометр · XYZ')}
          ${livePlot('live-gyro','Гироскоп · XYZ')}
          ${livePlot('live-resistance','Сопротивление электродов','Отдельная линия для каждого электрода',true)}
        </div>
      </section>
      <section class="live-graph-group" aria-labelledby="live-sdk-label">
        <h2 id="live-sdk-label">Индексы Capsule SDK</h2><div class="live-graphs">
          ${livePlot('live-nfb','Нейрообратная связь','Пять частотных полос α, β, θ, δ и SMR',true)}
          ${livePlot('live-emotions','Оценки внимания и состояний','Индексы производителя',true)}
          ${livePlot('live-phy','Физиологические оценки','Capsule обычно обновляет их раз в минуту')}
          ${livePlot('live-prod','Продуктивность')}
          ${livePlot('live-si-chart','Кардиоиндекс стресса','Расчёт SDK по ФПГ')}
          ${livePlot('live-ki-chart','Индекс Каплана','Расчёт SDK по ФПГ')}
        </div>
      </section>
      <section class="card live-controls">${heading('Управление сессией','Запись и метки требуют реального подключения; воспроизведение доступно без устройства')}
        <div class="controls-grid"><div><h3>Запись</h3><p id="live-recording-state">Нет активной записи</p><div class="control-actions">${pButton('Начать запись','start-record','primary')}${pButton('Остановить запись','stop-record')}</div></div>
        <div><h3>Чекпоинты</h3><p>Момент сохраняется сразу. Название можно добавить позже.</p><div class="control-actions">
          <button type="button" class="button subtle" data-checkpoint-create aria-keyshortcuts="Alt+M" aria-controls="checkpoint-panel" disabled>Поставить чекпоинт <kbd aria-hidden="true">Alt+M</kbd></button>
          <button type="button" class="button subtle" data-checkpoint-list aria-controls="checkpoint-panel" disabled>Список</button>
        </div></div></div>
      </section>`;
  }
  function sessionsPanel() {
    return `<section class="card">${heading('Сохранённые сессии','Все действия прямо в NeuroScope: анализ, воспроизведение, экспорт и удаление')}
      <div class="sessions-toolbar"><button class="button subtle" data-action="reload-sessions">Обновить список</button><span id="sessions-summary" class="card-subtitle">Загружаем записи…</span></div><div id="sessions-body" class="sessions-list"><div class="loading-panel"><span class="loader"></span><p>Читаем записи…</p></div></div></section>`;
  }
  function previewPanel(which) { return `<section class="card"><div class="offline-icon"><svg class="icon"><use href="#i-wave"/></svg></div><h2>${which==='live'?'Живой монитор':which==='device'?'Подключение устройства':'Управление сессиями'} доступно в запущенном проекте</h2><p class="card-subtitle" style="margin-top:12px">Это автономный HTML-файл со встроенными результатами ранее записанных измерений. В нём нет сервера, WebSocket, поиска Bluetooth или управления записью. Для этих функций запустите NeuroScope через start.sh / start.bat.</p><a class="button primary" style="margin-top:22px" href="#overview" data-go="overview">Вернуться к анализу</a></section>`; }
  function setTopStatus(s) {
    if (s && 'recording' in s) window.NeuroCheckpoints?.setRecording(s.recording);
    const badge=$('#device-top-status');if (!badge)return;
    const [title,tone]=label(s?.status);
    setMarkup(badge,`<span class="status-pin ${tone}"></span>${esc(title)}${s?.connected?` · ${esc(s.device||'Neiry')}${s.contact?.ready?' · контакт готов':' · контакт не подтверждён'}`:''}`);
    badge.title=safeInfo(s);
  }
  async function api(path,opts={}) { return ctx.api(path,opts); }
  async function act(action, extra) {
    const map={scan:'/api/device/scan',disconnect:'/api/device/disconnect','skip-cal':'/api/device/calibration/skip',
      'start-record':'/api/recording/start','stop-record':'/api/recording/stop','stop-replay':'/api/replay/stop','pause-replay':'/api/replay/pause','resume-replay':'/api/replay/resume'};
    try {
      if (action==='calibration') {openCalibrationSetup();return;}
      if (action.startsWith('connect:')) {
        await api('/api/device/connect',{method:'POST',body:JSON.stringify({serial:action.slice(8)})}); notice('Подключение начато. Дождитесь подтверждения SDK.');
      } else if(action==='seek-forward') {await api('/api/replay/seek',{method:'POST',body:JSON.stringify({offset_s:(replay?.progress_s||0)+30})});clearSamples();requestChartFrame();notice('Переходим на 30 секунд вперёд.');} else if(action==='reload-sessions') {await loadSessions();return;}
      else if(action.startsWith('replay:')) {
        const id=action.slice(7); await api(`/api/replay/${encodeURIComponent(id)}/start?speed=1`,{method:'POST'});
        notice('Воспроизведение запущено. В мониторе будут показаны архивные данные.','success'); location.hash='live';
      } else if(action.startsWith('delete:')) {
        const id=action.slice(7);
        if(!window.confirm(`Удалить запись ${id} без возможности восстановления?`))return;
        await api(`/api/records/${encodeURIComponent(id)}`,{method:'DELETE'});
        await loadSessions(); await ctx.reloadSessions?.(); notice('Запись удалена.','success');return;
      } else if(map[action]) {
        const result=await api(map[action],{method:'POST'});
        if (action==='start-record'||action==='stop-record') window.NeuroCheckpoints?.setRecording(result.recording);
        notice(({scan:'Поиск запущен.',disconnect:'Отключение запрошено.','skip-cal':'Калибровка пропущена.','start-record':'Запись начата.','stop-record':'Запись остановлена.','stop-replay':'Воспроизведение остановлено.'})[action]||'Готово.','success');
      }
      await poll();
    } catch (error) { notice(error.message||'Не удалось выполнить действие.','error'); }
  }
  function bind(root=$('#content')) {
    if (!root || root.dataset.neuroLiveBound) return;
    root.dataset.neuroLiveBound = '1';
    root.addEventListener('click', e=>{
      const btn=e.target.closest('[data-action]');
      if(btn){e.preventDefault();if(!btn.disabled)act(btn.dataset.action);return;}
      const go=e.target.closest('[data-go]');
      if(go){e.preventDefault();location.hash=go.dataset.go;}
    });
    root.addEventListener('change', async e=>{
      if(e.target.id==='live-time-range'){
        const previous=liveRange;
        liveRange=Number(e.target.value)||15;
        if(liveRange>previous){closeWS();openWS();}
        else requestChartFrame();
        return;
      }
      if(e.target.id==='metric-time-range'){metricRange=Number(e.target.value)||300;requestChartFrame();return;}
      if(e.target.id==='show-unverified-raw'){
        allowRaw=Boolean(e.target.checked);
        if(!allowRaw){
          for(const key of [...plotConfig('live-eeg').keys,'ppg'])clearSeries(key);
          requestChartFrame();
        }
        refreshDiagnosticSource();
        return;
      }
      if(e.target.id!=='auto-reconnect')return;
      const enabled=e.target.checked;
      try{await api('/api/device/reconnect-settings',{method:'POST',body:JSON.stringify({enabled})});await poll();}
      catch(err){notice(err.message,'error');e.target.checked=!enabled;}
    });
  }
  function plotConfig(id) {return chartDefs.find(cfg=>cfg.id===id);}
  function dataset([label,color]) {
    return {label,data:[],borderColor:color,backgroundColor:color,borderWidth:1.55,
            pointRadius:0,pointHoverRadius:3,tension:0,spanGaps:false};
  }
  function chartRange(cfg) {return cfg.raw?liveRange:metricRange;}
  function timeScale(cfg) {return cfg.raw?1:60;}
  function timeLabel(t,cfg) {return axisFormat.format(t/timeScale(cfg));}
  function makeChart(cfg) {
    const el = $(`#${cfg.id}`);
    if (!el || !window.Chart) return null;
    const config = {
      type: 'line',
      data: {datasets: cfg.series.map(dataset)},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        parsing: false, locale:'ru-RU',
        interaction: {mode: 'nearest', axis: 'x', intersect: false},
        plugins: {
          legend: {display: false, position: 'bottom',
                   labels: {color: '#b1c0d4', boxWidth: 12, font: {size: 11}}},
          tooltip: {backgroundColor: '#172335', titleColor: '#ecf2f8', bodyColor: '#cbd6e6', callbacks: {
            title: items=>items.length?`${timeLabel(items[0].parsed.x-items[0].chart.scales.x.max,cfg)} ${cfg.raw?'с':'мин'}`:'',
            label: item=>`${item.dataset.label}: ${axisFormat.format(item.parsed.y)} ${cfg.unit||'ед. SDK'}`
          }}
        },
        scales: {
          x: {type: 'linear',display:false,grid: {color: '#25324566'},
              ticks: {color: '#9baac0',maxTicksLimit: 6, callback(value){return timeLabel(value-this.max,cfg);}},
              title: {display: true, text: `Время относительно края окна, ${cfg.raw?'с':'мин'}`, color: '#9baac0'}},
          y: {display:false,grace:'8%',grid: {color: '#25324566'},
              ticks: {color: '#9baac0',maxTicksLimit: 6,callback:value=>axisFormat.format(value)},
              title:{display:true,text:cfg.unit||'Единицы SDK',color:'#9baac0'}}
        }
      }
    };
    const instance = new Chart(el, config);
    charts.set(cfg.id,instance);
    return instance;
  }
  function clearSeries(key){
    if(samples[key])samples[key].length=0;
    delete lastRawTime[key];
  }
  function clearSamples(){
    Object.keys(samples).forEach(clearSeries);
    chartClock=0;
    chartEnd=0;
    resistanceSince=-Infinity;
  }
  function clearGraphs(){
    window.NeuroBrainMap.unmount($('#content [data-live-diagnostic="paired"] [data-pair-map]'));
    plotObserver?.disconnect();
    plotObserver=null;
    visibleCharts.clear();
    visibleDiagnostics.clear();
    cancelChartFrame();
    stopDiagnostics();
    diagnosticSource='';
    charts.forEach(c=>c.destroy());
    charts.clear();
    chartSource='';
    clearSamples();
  }
  function syncChannels(id,prefix,labels){
    const cfg=plotConfig(id);
    if(labels.length===cfg.series.length && labels.every((label,i)=>label===cfg.series[i][0]))return false;
    for(const key of cfg.keys){delete samples[key];delete lastRawTime[key];}
    cfg.keys=labels.map((_,i)=>`${prefix}${i+1}`);
    cfg.series=labels.map((label,i)=>[label,channelColors[i%channelColors.length]]);
    cfg.keys.forEach(key=>samples[key]=[]);
    const chart=charts.get(id);
    if(chart)chart.data.datasets=cfg.series.map(dataset);
    return true;
  }
  function lowerBound(series,t){
    let lo=0,hi=series.length;
    while(lo<hi){const mid=(lo+hi)>>1;if(series[mid].x<t)lo=mid+1;else hi=mid;}
    return lo;
  }
  function visibleWindow(series,begin,end,width){
    const start=lowerBound(series,begin);
    const columns=Math.max(1,Math.floor(width/2));
    if(series.length-start<=2*columns)return series.slice(start);
    // Match the drawing resolution while retaining peaks and explicit gaps.
    const points=[];
    const scale=(columns-1)/Math.max(end-begin,1);
    let column=-1, low=null, high=null;
    function flush(){
      if(low===null)return;
      points.push(low.x<=high.x?low:high);
      if(low!==high)points.push(low.x<=high.x?high:low);
      low=high=null;
    }
    for(let i=start;i<series.length;i++){
      const point=series[i];
      if(point.y===null){
        flush();
        if(points.at(-1)?.y!==null)points.push(point);
        column=-1;
        continue;
      }
      const current=Math.floor((point.x-begin)*scale);
      if(current!==column){flush();column=current;}
      if(low===null||point.y<low.y)low=point;
      if(high===null||point.y>high.y)high=point;
    }
    flush();
    return points;
  }
  function addSeries(key,packet,values){
    if(!packet||!Array.isArray(values)||!values.length)return;
    const sr=n(packet.sr),t0=n(packet.t0);
    const step=sr>0?1/sr:null;
    const stride=Math.max(1,Math.ceil((sr||0)/70),Math.ceil(values.length/2500));
    const dst=samples[key];
    let previous=Math.max(lastRawTime[key]??-Infinity,dst.at(-1)?.x??-Infinity), lo=null, hi=null;
    const flush=()=>{
      if(!lo)return;
      if(lo===hi)dst.push(lo);
      else dst.push(...(lo.x<hi.x?[lo,hi]:[hi,lo]));
      lo=hi=null;
    };
    let missed=packet.missed_display_samples>0;
    for(let i=0;i<values.length;i++){
      const x=n(packet.times?.[i])??(step!==null&&t0!==null?t0+i*step:null);
      if(x===null || x<=previous)continue;
      const y=n(values[i]);
      if(y===null || missed || x-previous>Math.max(.05,4*(step||0))){
        flush();
        if(dst.length && dst.at(-1).y!==null)dst.push({x:y===null?x:(x+previous)/2,y:null});
      }
      missed=false;
      previous=x;
      if(y===null)continue;
      const point={x,y};
      if(!lo || y<lo.y)lo=point;
      if(!hi || y>hi.y)hi=point;
      if((i+1)%stride===0)flush();
    }
    flush();
    if(Number.isFinite(previous))lastRawTime[key]=previous;
    dst.splice(0,Math.max(lowerBound(dst,previous-65),dst.length-9500));
  }
  function addPoint(key,time,value,max=10000){
    const t=n(time),v=n(value);
    const dst=samples[key],last=dst.at(-1);
    if(t===null || last && (t<=last.x || last.y===null&&v===null) || !last&&v===null)return;
    dst.push({x:t,y:v});
    dst.splice(0,Math.max(lowerBound(dst,t-920),dst.length-max));
  }
  function appendMetrics(tick,qualified,allowArchived){
    const cardioFields=[['hr','heartRate'],['stressIndex','stressIndex'],['kaplanIndex','kaplanIndex']];
    if(tick.cardio&&qualified?.cardio){
      const c=tick.cardio;
      for(const [key,prop] of cardioFields)addPoint(key,c.t,c[prop]);
    }else{
      const t=allowArchived?tick.sensor_status?.cardio?.t:tick.t;
      for(const [key] of cardioFields)addPoint(key,t,null);
    }
    if(tick.emotions&&(qualified?.emotions||allowArchived))
      for(const key of ['attention','relaxation','cognitiveLoad','cognitiveControl','selfControl'])addPoint(key,tick.emotions.t,tick.emotions[key]);
    if(tick.phy&&(qualified?.phy||allowArchived))
      for(const [key,prop] of [['phyRelax','relaxation'],['phyConcentration','concentration'],['phyFatigue','fatigue'],['phyStress','stress'],['phyInvolvement','involvement']])addPoint(key,tick.phy.t,tick.phy[prop]);
    if(tick.prod&&(qualified?.prod||allowArchived))
      for(const key of ['productivityScore','concentrationScore','relaxationScore','fatigueScore'])addPoint(key,tick.prod.t,tick.prod[key]);
    if(Array.isArray(tick.nfb)&&(qualified?.nfb||allowArchived))
      for(const item of tick.nfb)for(const key of ['alpha','beta','theta','delta','smr'])addPoint(key,item?.t,item?.[key]);
    if(!allowArchived){
      for(const [source,ids] of [['emotions',['live-emotions']],['phy',['live-phy']],
                                ['prod',['live-prod']],['nfb',['live-nfb']]]){
        if(!qualified[source])for(const id of ids)for(const key of plotConfig(id).keys)addPoint(key,tick.t,null);
      }
    }
    if(tick.connected){
      const times=tick.resistances_times||[],history=tick.resistances_hist||[];
      const names=tick.res_channel_names||[];
      const count=Math.max(names.length,history.at(-1)?.length||0);
      const labels=Array.from({length:count},(_,i)=>names[i]||`Электрод ${i+1}`);
      const changingLayout=plotConfig('live-resistance').keys.some(key=>samples[key].length);
      if(syncChannels('live-resistance','res',labels) && changingLayout)resistanceSince=n(times.at(-1))??-Infinity;
      const length=Math.min(times.length,history.length);
      for(let i=0;i<length;i++){
        const row=history[history.length-length+i];
        const time=times[times.length-length+i];
        if(time<resistanceSince)continue;
        plotConfig('live-resistance').keys.forEach((key,k)=>{
          const value=n(row?.[k]);
          addPoint(key,time,value>0?value/1000:null);
        });
      }
    }
  }
  function renderCharts(){
    for(const id of visibleCharts){
      const cfg=plotConfig(id);
      const chart=charts.get(id)||makeChart(cfg);
      if(!chart)continue;
      const begin=chartEnd-chartRange(cfg);
      let found=false;
      cfg.keys.forEach((key,i)=>{
        const items=visibleWindow(samples[key],begin,chartEnd,chart.width);
        const series=chart.data.datasets[i];
        series.data=items;
        series.pointRadius=items.length<=3?2.5:0;
        found=found||items.some(p=>p.y!==null);
      });
      const empty=chart.canvas.parentElement.querySelector('.live-chart-empty');
      if(empty)empty.hidden=found;
      if(!found&&!chart.options.scales.x.display)continue;
      chart.options.scales.x.min=begin;
      chart.options.scales.x.max=chartEnd;
      chart.options.scales.x.display=chart.options.scales.y.display=found;
      chart.options.plugins.legend.display=found&&cfg.keys.length>1;
      chart.update('none');
    }
  }
  function requestChartFrame(){
    if(chartFrame!==null||!visibleCharts.size||document.hidden)return;
    chartFrame=requestAnimationFrame(()=>{
      chartFrame=null;
      if(view==='live'&&!document.hidden)renderCharts();
    });
  }
  function cancelChartFrame(){
    if(chartFrame!==null)cancelAnimationFrame(chartFrame);
    chartFrame=null;
  }
  function observePlots(){
    const diagnostics=$$('[data-live-diagnostic]');
    const plotNodes=chartDefs.map(cfg=>document.getElementById(cfg.id)).filter(Boolean);
    if(typeof IntersectionObserver!=='function'){
      plotNodes.forEach(node=>visibleCharts.add(node.id));
      diagnostics.forEach(node=>visibleDiagnostics.add(node.dataset.liveDiagnostic));
      requestChartFrame();
      scheduleDiagnostics();
      return;
    }
    const observer=new IntersectionObserver(entries=>{
      if(plotObserver!==observer)return;
      for(const {target,isIntersecting} of entries){
        const name=target.dataset.liveDiagnostic;
        const set=name?visibleDiagnostics:visibleCharts;
        if(isIntersecting)set.add(name||target.id);
        else set.delete(name||target.id);
      }
      if(!visibleDiagnostics.size)stopDiagnostics();
      else scheduleDiagnostics();
      requestChartFrame();
    });
    plotObserver=observer;
    [...plotNodes,...diagnostics].forEach(node=>observer.observe(node));
  }
  function drawHeatmap(id,channel){
    const canvas=$(`#${id}`);if(!canvas)return;
    const context=canvas.getContext('2d');if(!context)return;
    const width=canvas.width,height=canvas.height;
    context.fillStyle='#0a1423';context.fillRect(0,0,width,height);
    if(!channel?.available || !channel?.log10_psd?.length)return;
    const matrix=channel.log10_psd,rows=matrix.length,cols=matrix[0]?.length||0;
    if(!cols)return;
    const values=matrix.flat().filter(Number.isFinite).sort((a,b)=>a-b);
    if(!values.length)return;
    const lo=values[Math.floor(values.length*.1)],hi=values[Math.floor(values.length*.95)];
    for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){
      const v=matrix[r][c],z=Number.isFinite(v)?Math.min(1,Math.max(0,(v-lo)/Math.max(.001,hi-lo))):0;
      context.fillStyle=`hsl(${Math.round(223-z*172)},${Math.round(48+z*32)}%,${Math.round(12+z*48)}%)`;
      context.fillRect(Math.floor(c*width/cols),Math.floor((rows-r-1)*height/rows),
         Math.ceil(width/cols)+1,Math.ceil(height/rows)+1);
    }
  }
  function renderSpectrograms(tiles){
    const target=$('#live-spectrograms');
    if(!target)return;
    const channels=tiles?.channels||[];
    const signature=JSON.stringify(channels.map(c=>[c.name,c.available]));
    if(target.dataset.signature!==signature){
      target.innerHTML=channels.map((c,i)=>`<figure class="live-spectrum"><figcaption>${esc(c.name)}</figcaption>
        ${c.available?`<div class="live-spectrum-plot"><div class="live-spectrum-frequency" id="live-spectrogram-y-${i}"></div>
        <canvas class="studio-spectrogram" id="live-spectrogram-${i}" width="660" height="180" role="img" aria-label="Спектрограмма, ${esc(c.name)}"></canvas>
        <div class="live-spectrum-time" id="live-spectrogram-x-${i}"></div></div>`:'<p class="chart-caption">Спектрограмма канала пока недоступна.</p>'}</figure>`).join('');
      target.dataset.signature=signature;
    }
    channels.forEach((c,i)=>{
      if(!c.available)return;
      const f=c.freq_hz||[],t=c.time_s||[];
      setMarkup($(`#live-spectrogram-y-${i}`),f.length?`<span>${fmt(f.at(-1))} Гц</span><span>${fmt(f[0])} Гц</span>`:'');
      setMarkup($(`#live-spectrogram-x-${i}`),t.length?`<span>${fmt(t[0])} с</span><span>${fmt(t.at(-1))} с</span>`:'');
      drawHeatmap(`live-spectrogram-${i}`,c);
    });
  }
  function diagnosticsAllowed(){
    return replay.active||status?.validity?.eeg||allowRaw&&connected();
  }
  function diagnosticAvailable(name){
    return name==='paired' ? connected()&&!replay.active&&Boolean(status?.validity?.eeg) : diagnosticsAllowed();
  }
  function pairedPanel(){
    return $('#content [data-live-diagnostic="paired"] [data-pair-panel]');
  }
  function activeDiagnostics(){
    return [...visibleDiagnostics].filter(diagnosticAvailable);
  }
  function stopDiagnostics(){
    clearTimeout(diagnosticTimer);
    diagnosticTimer=null;
    diagnosticRequest?.abort();
    diagnosticRequest=null;
  }
  function refreshDiagnosticSource(){
    if(view!=='live')return;
    const allowed=diagnosticsAllowed();
    const pairAllowed=diagnosticAvailable('paired');
    const source=`${chartSource}:${Boolean(allowed)}:${pairAllowed}`;
    if(source===diagnosticSource)return;
    diagnosticSource=source;
    stopDiagnostics();
    for(const name of Object.keys(lastDiagnosticAt))delete lastDiagnosticAt[name];
    setText($('#live-quality-state'),allowed?'Ожидаем достаточное окно ЭЭГ.':'ЭЭГ пока не подтверждена контактом. Для проверки аппаратного шума доступен диагностический показ сырого сигнала.');
    setText($('#live-spectrogram-note'),allowed?'Для построения нужны не менее трёх секунд ЭЭГ.':'Спектрограмма скрыта: контакт электродов не подтверждён.');
    renderSpectrograms(null);
    const pairReason=replay.active
      ? 'Парный анализ сохранённой записи доступен в разделе «Сигналы и метрики».'
      : pairAllowed ? 'Ожидаем достаточное окно двух каналов ЭЭГ.'
      : 'Для парного анализа нужны стабильный контакт электродов и живая ЭЭГ.';
    window.NeuroPairedPanel.attach(pairedPanel(),{available:false,reason:pairReason});
    scheduleDiagnostics();
  }
  function scheduleDiagnostics(){
    clearTimeout(diagnosticTimer);
    diagnosticTimer=null;
    if(view!=='live'||document.hidden||diagnosticRequest)return;
    const active=activeDiagnostics();
    if(!active.length)return;
    const due=Math.min(...active.map(name=>(lastDiagnosticAt[name]??-Infinity)+diagnosticInterval[name]));
    diagnosticTimer=setTimeout(updateSignalQuality,Math.max(0,due-performance.now()));
  }
  async function fetchLiveDiagnostics(request, options, quality, spectrogram){
    try{
      const result=await api(`/api/live/diagnostics?quality=${quality}&spectrogram=${spectrogram}`,options);
      if(request!==diagnosticRequest)return;
      if(result.quality&&visibleDiagnostics.has('quality')){
        const q=result.quality;
        setMarkup($('#live-quality-state'),q.available?q.channels.map(c=>`<div class="studio-state"><strong>${esc(c.name)} · ${c.status==='screened'?'Без обнаруженных флагов':esc(c.status)}</strong><span>${(c.reasons||[]).map(esc).join('; ')||'Проверки не выявили заданных инженерных предупреждений; это не оценка здоровья.'}</span><small>Различных уровней: ${fmt(c.unique_levels,0)} · Помеха: ${c.line_power_fraction==null?'—':fmt(c.line_power_fraction*100)}%</small></div>`).join(''):`<p class="chart-caption">${esc(q.reason||'Нет данных')}</p>`);
      }
      if(result.spectrogram&&visibleDiagnostics.has('spectrogram')){
        const tiles=result.spectrogram;
        renderSpectrograms(tiles);
        setText($('#live-spectrogram-note'),tiles.available?(replay.active?'Относительная мощность архивной ЭЭГ.':status?.validity?.eeg?'Время отсчитывается от начала показанного окна.':'Диагностический сигнал без подтверждённого контакта.'):'Для построения нужны не менее трёх секунд ЭЭГ.');
      }
    }catch(e){
      if(request!==diagnosticRequest)return;
      if(quality&&visibleDiagnostics.has('quality'))setText($('#live-quality-state'),'Диагностика недоступна: '+e.message);
      if(spectrogram&&visibleDiagnostics.has('spectrogram'))setText($('#live-spectrogram-note'),'Спектрограмма недоступна: '+e.message);
    }
  }
  async function fetchPairedDiagnostics(request, options){
    const current=()=>request===diagnosticRequest&&visibleDiagnostics.has('paired')&&diagnosticAvailable('paired');
    try{
      const pair=await api('/api/live/paired',options);
      if(current())window.NeuroPairedPanel.attach(pairedPanel(),pair);
    }catch(e){
      if(current())window.NeuroPairedPanel.attach(pairedPanel(),{available:false,reason:'Парный анализ недоступен: '+e.message});
    }
  }
  async function updateSignalQuality(){
    diagnosticTimer=null;
    if(view!=='live'||document.hidden||diagnosticRequest)return;
    const now=performance.now();
    const due=activeDiagnostics().filter(name=>now-(lastDiagnosticAt[name]??-Infinity)>=diagnosticInterval[name]);
    if(!due.length){scheduleDiagnostics();return;}
    due.forEach(name=>{lastDiagnosticAt[name]=now;});
    const quality=due.includes('quality'), spectrogram=due.includes('spectrogram');
    const request=new AbortController();
    diagnosticRequest=request;
    const options={signal:AbortSignal.any([request.signal,AbortSignal.timeout(5000)])};
    try{
      await Promise.all([
        quality||spectrogram ? fetchLiveDiagnostics(request,options,quality,spectrogram) : null,
        due.includes('paired') ? fetchPairedDiagnostics(request,options) : null,
      ]);
    }finally{
      if(request===diagnosticRequest){
        diagnosticRequest=null;
        scheduleDiagnostics();
      }
    }
  }
  function updateLiveTick(tick, firstFrame) {
    if(view!=='live'||document.hidden)return;
    const now=performance.now();
    const eeg=tick.eeg_stream,ppg=tick.ppg_stream,mems=tick.mems_stream;
    const valid=tick.validity||{};
    if(tick.replay){
      replay=tick.replay;
      lastStreamStatusAt=now;
      const {connected,device,message,recording,recording_origin,recording_error,contact,validity,sensor_status}=tick;
      status={...status,connected,device,message,recording,recording_origin,recording_error,contact,validity,sensor_status,status:tick.status};
    }
    const archived=valid.source_mode==='replay'||(!valid.source_mode&&replay?.active);
    const source=archived?`replay:${tick.stream_epoch??''}`:tick.connected?`live:${tick.serial||''}:${tick.stream_epoch??''}`:'idle';
    const sourceChanged=source!==chartSource;
    if(sourceChanged){clearSamples();chartSource=source;}
    else if(firstFrame){
      // The server sends a fresh window. Preserve slow metrics but break the
      // line between readings separated by a disconnected display.
      for(const cfg of chartDefs)for(const key of cfg.keys){
        if(cfg.raw)clearSeries(key);
        else{
          const last=samples[key].at(-1);
          if(last&&last.y!==null)addPoint(key,last.x+1e-6,null);
        }
      }
    }
    const names=tick.eeg_channels||[];
    const packetCount=eeg?.samples?1:Object.keys(eeg||{}).filter(key=>/^ch[1-9]\d*$/.test(key)).length;
    const currentCount=plotConfig('live-eeg').keys.length;
    const count=Math.max(names.length,packetCount)||(!eeg&&!sourceChanged&&source!=='idle'?currentCount:0);
    const eegLabels=Array.from({length:count},(_,i)=>names[i]||(!sourceChanged&&plotConfig('live-eeg').series[i]?.[0])||`Канал ${i+1}`);
    syncChannels('live-eeg','eeg',eegLabels);
    const eegVisible=archived||valid.eeg||allowRaw && tick.connected;
    const ppgVisible=archived||valid.ppg||allowRaw && tick.connected;
    plotConfig('live-eeg').keys.forEach((key,i)=>{
      if(eegVisible)addSeries(key,eeg,eeg?.[`ch${i+1}`]??(i===0?eeg?.samples:null));
      else addPoint(key,tick.t,null);
    });
    if(ppgVisible)addSeries('ppg',ppg,ppg?.samples);
    else addPoint('ppg',tick.t,null);
    if(mems&&Array.isArray(mems.acc)) {
      const magnitude=v=>Array.isArray(v)&&v.every(x=>n(x)!==null)?Math.hypot(...v.map(Number)):null;
      addSeries('motion',mems,mems.acc.map(magnitude));
      for(let i=0;i<3;i++)addSeries(['accX','accY','accZ'][i],mems,mems.acc.map(v=>Array.isArray(v)?v[i]:null));
      if(Array.isArray(mems.gyro))for(let i=0;i<3;i++)addSeries(['gyroX','gyroY','gyroZ'][i],mems,mems.gyro.map(v=>Array.isArray(v)?v[i]:null));
    }
    appendMetrics(tick,valid,archived);
    refreshDiagnosticSource();
    if(sourceChanged||now-lastIndicatorsAt>=500){
      lastIndicatorsAt=now;
      updateLiveIndicators(tick,archived,eegLabels);
    }
    const newest=Math.max(0,...Object.values(samples).map(series=>series.at(-1)?.x??0));
    chartEnd=Math.max(chartEnd,newest,archived?0:n(tick.t)||0);
    if(now-chartClock<400&&!sourceChanged&&!firstFrame)return;
    chartClock=now;
    requestChartFrame();
  }
  function updateLiveIndicators(tick,archived,eegLabels) {
    const liveContact=$('#live-contact-card');
    const signature=archived?'replay':contactSignature(tick.contact);
    if(liveContact&&liveContact.dataset.contactSignature!==signature){
      liveContact.dataset.contactSignature=signature;
      setMarkup(liveContact,archived?'<div class="contact-panel"><strong>Архивное воспроизведение</strong><p class="chart-caption">Данные из сохранённого файла. Текущий контакт электродов не оценивается.</p></div>':contactPanel(tick.contact,true));
    }
    const valid=tick.validity||{};
    const field=(id,v)=>setText($(`#${id}`),fmt(v,1));
    field('live-hr',valid.cardio?tick.cardio?.heartRate:null);
    setText($('#live-hr-note'), archived
      ? (valid.cardio?'уд/мин · сохранённое значение SDK':tick.sensor_status?.cardio?.reason||'Ожидаем кардиопоказатели из записи.')
      : 'уд/мин · Capsule SDK');
    field('live-attention',(valid.emotions||archived)?tick.emotions?.attention:null);
    field('live-relax',(valid.emotions||archived)?tick.emotions?.relaxation:null);
    setText($('#live-channels'),eegLabels.length?`Каналы (${eegLabels.length}): ${eegLabels.join(' / ')}`:'Каналы: —');
    for(const name of ['eeg','ppg','mems']){
      const rate=tick[`${name}_sr`];
      setText($(`#live-${name}-sr`),rate>0?`${name==='eeg'?'· ':''}${fmt(rate,1)} Гц`:'');
    }
    if(tick.replay){
      setTopStatus(status);
      updateLiveStatus();
    }else{
      updateRecordingStatus(tick);
      updateStreamSummary(tick,archived);
    }
    setMarkup($('#live-resistances'),(tick.contact?.channels||[]).map(r=>`<span>${esc(r.channel)}: <strong>${n(r.kohm)!==null?`${fmt(r.kohm,1)} кОм`:'нет измерения'}</strong></span>`).join('')||'<span>Нет актуального измерения электродов</span>');
  }
  function updateRecordingStatus(s) {
    setText($('#live-recording-state'),s?.recording ? `Идёт запись${s.recording_origin==='calibration'?' · автоматически после калибровки':''} · ${s.recording}`
      : s?.recording_error || 'Нет активной записи');
  }
  function updateStreamSummary(tick, archived) {
    const panel = $('#live-sensors');
    if (!panel) return;
    const names = {eeg:'ЭЭГ',skin:'Контакт электродов',ppg:'Сырая ФПГ',cardio:'Пульс и кардиоиндексы',
      mems:'Движение',nfb:'Нейрообратная связь',emotions:'SDK эмоции',phy:'SDK физиология',prod:'SDK продуктивность'};
    const rows = archived ? [] : Object.entries(names).map(([key,name])=>[name,tick.sensor_status?.[key]]);
    const html = !tick.connected || archived ? `<p>${archived?'Данные из архива. Статусы SDK относятся к живому подключению.':'Нет активных потоков Capsule.'}</p>`
      : rows.map(([name,v])=>{
        const state = v?.state || 'none';
        const rate = state==='ok' && n(v?.sr)>0 ? ` · ${fmt(v.sr,0)} Гц` : '';
        const progress = v?.baseline?.state==='running' && n(v.baseline.progress)!==null
          ? ` ${fmt(v.baseline.progress*100,0)}%.` : '';
        const caption = state==='ok' ? 'Доступен' : state==='warn' ? 'Ограничен' : 'Нет данных';
        const reason = state==='ok' ? '' : (v?.reason || 'Ожидаем данные Capsule.')+progress;
        return `<div class="sensor-item"><span class="sensor-pill ${esc(state)}">
          <span class="sensor-light" aria-hidden="true"></span>${esc(name)}${rate}<span class="screen-reader">. ${caption}</span>
          </span>${reason?`<small>${esc(reason)}</small>`:''}</div>`;
      }).join('');
    setMarkup(panel,html);
  }
  function updateLiveStatus() {
    if(view!=='live'||!$('#live-mode'))return;
    const activeReplay=replay?.active;
    const isConnected=connected();
    setText($('#live-mode'),activeReplay?'Воспроизведение сохранённой записи':isConnected?(status.contact?.ready?'Контакт электродов подтверждён':'Подключено, контакта пока нет'):'Нет активного источника сигнала');
    setText($('#live-message'),activeReplay?`Запись ${replay.session_id} · скорость ×${fmt(replay.speed,1)} · это не текущие данные человека.`:
      isConnected?(status.contact?.message || 'Подключено. Ожидаем показатели сопротивления электродов.'):
      'Подключите устройство на вкладке «Устройство» или воспроизведите архивную сессию на вкладке «Записи».');
    const stop=$('#replay-stop');if(stop)stop.hidden=!activeReplay;
    const pause=$('#replay-pause');if(pause){pause.hidden=!activeReplay;pause.dataset.action=replay?.paused?'resume-replay':'pause-replay';setText(pause,replay?.paused?'Продолжить':'Пауза');}
    const forward=$('#replay-forward');if(forward)forward.hidden=!activeReplay;
    const start=$('[data-action="start-record"]');if(start)start.disabled=!isConnected||!status?.validity?.eeg||Boolean(status?.recording);
    const stopRecord=$('[data-action="stop-record"]');if(stopRecord)stopRecord.disabled=!isConnected||!Boolean(status?.recording);
    updateRecordingStatus(status);
    if(status?.sensor_status)updateStreamSummary(status,activeReplay);
  }
  function openWS() {
    if(preview||view!=='live'||document.hidden||ws)return;
    const scheme=location.protocol==='https:'?'wss':'ws';
    const socket=new WebSocket(`${scheme}://${location.host}/ws?profile=live&history=${liveRange}`);
    let firstFrame=true;
    ws=socket;
    socket.onmessage=e=>{
      if(ws!==socket||document.hidden)return;
      try{
        const tick=JSON.parse(e.data);
        if(tick.type==='tick'){
          updateLiveTick(tick,firstFrame);
          firstFrame=false;
        }
      }catch(err){console.warn('NeuroScope WS data:',err);}
    };
    socket.onclose=()=>{
      if(ws!==socket)return;
      ws=null;
      lastStreamStatusAt=-Infinity;
      if(view==='live'&&!document.hidden){
        wsRetry=setTimeout(openWS,1700);
        scheduleStatusPoll(0);
      }
    };
    socket.onerror=()=>socket.close();
  }
  function closeWS(){
    clearTimeout(wsRetry);
    wsRetry=null;
    lastStreamStatusAt=-Infinity;
    if(ws){const old=ws;ws=null;old.close();}
  }
  function healthyLiveStatus(){
    return view==='live'&&performance.now()-lastStreamStatusAt<1500;
  }
  async function poll(force=true) {
    if(preview||document.hidden&&!calibrationAudioRun||!force&&(statusRequest||healthyLiveStatus()&&!calibrationAudioRun))return;
    statusRequest?.abort();
    const request=new AbortController();
    statusRequest=request;
    const options={signal:AbortSignal.any([request.signal,AbortSignal.timeout(1600)])};
    try {
      const [s,replayStatus]=await Promise.all([api('/api/device/status',options),
        view==='live'&&!healthyLiveStatus()?api('/api/replay/status',options).catch(()=>null):null]);
      if(request!==statusRequest)return;
      status=s;setTopStatus(s);syncCalibrationAudio(s);
      if(view==='device'&&$('#content')){
        const fingerprint=JSON.stringify({status:s.status,message:s.message,connected:s.connected,discovered:s.discovered,calibration_phase:s.calibration_phase,calibration_available:s.calibration_available,calibration_stage:s.calibration_run?.stage,calibration_result:s.calibration_result,contact:s.calibration_run?null:contactSignature(s.contact),scanning:s.scanning,auto_reconnect:s.auto_reconnect,battery:s.battery,firmware_version:s.firmware_version,sdk_version:s.sdk_version,recording:s.recording,recording_error:s.recording_error});
        if(fingerprint!==statusFingerprint){statusFingerprint=fingerprint;$('#content').innerHTML=devicePanel(s);bind();}
        updateCalibrationProgress(s);
      }
      if(view==='live'){
        if(replayStatus&&!healthyLiveStatus())replay=replayStatus;
        updateLiveStatus();
        refreshDiagnosticSource();
      }
    }catch(e){
      if(request!==statusRequest)return;
      syncCalibrationAudio(null);
      statusFingerprint='';
      setTopStatus({status:'error',message:'Не удаётся связаться с локальным сервером'});
      if(view==='device')$('#content').innerHTML=`<section class="card"><h2>Нет связи с сервером</h2><p>${esc(e.message)}</p>${pButton('Повторить','scan')}</section>`;
    }finally{
      if(request===statusRequest)statusRequest=null;
    }
  }
  function scheduleStatusPoll(delay){
    clearTimeout(pollTimer);
    pollTimer=null;
    if(preview||document.hidden&&!calibrationAudioRun)return;
    pollTimer=setTimeout(async()=>{
      pollTimer=null;
      await poll(false);
      if(pollTimer===null)scheduleStatusPoll();
    },delay??(calibrationAudioRun||status?.calibration_phase==='running'?250:1900));
  }
  function visibilityChanged(){
    if(document.hidden){
      closeWS();
      cancelChartFrame();
      stopDiagnostics();
      if(!calibrationAudioRun){
        statusRequest?.abort();
        statusRequest=null;
      }
    }else if(view==='live'){
      openWS();
      scheduleDiagnostics();
    }
    scheduleStatusPoll(0);
  }
  async function loadSessions() {
    if(preview||view!=='sessions')return;
    try {
      const out=await api('/api/records');sessionRows=Array.isArray(out.sessions)?out.sessions:[];
      $('#sessions-summary').textContent=`Всего: ${sessionRows.length}`;
      const box=$('#sessions-body');if(!box)return;
      if(!sessionRows.length){box.innerHTML='<div class="no-device">Пока нет сохранённых сессий. Подключите устройство и начните запись.</div>';return;}
      box.innerHTML=sessionRows.map(s=>{
        const id=s.session_id,meta=s.meta||{},counts=s.summary?.counts||{};
        return `<article class="session-list-row"><div class="session-list-icon"><svg class="icon"><use href="#i-folder"/></svg></div><div class="session-list-data"><strong>${esc(id)}</strong><small>${esc(meta.started_at||meta.started_at_iso||'Сохранённая сессия')} ${s.active?' · Идёт запись':''}</small><div class="session-list-info">${Object.keys(counts).slice(0,4).map(k=>`${esc(k)}: ${fmt(counts[k],0)}`).join(' · ')}</div></div>
        <div class="session-list-actions"><a class="button subtle" href="#signals" data-session-analysis="${esc(id)}">Анализ</a><button type="button" class="button subtle" data-checkpoint-list="${esc(id)}" aria-controls="checkpoint-panel">Чекпоинты</button>${pButton('Воспроизвести','replay:'+id,'subtle',s.active||connected())}<a class="button subtle" href="/api/records/${encodeURIComponent(id)}/download" download>ZIP</a>${pButton('Удалить','delete:'+id,'subtle',s.active)}</div></article>`;
      }).join('');
      $$('[data-session-analysis]',box).forEach(a=>a.addEventListener('click',e=>{e.preventDefault();ctx.selectSession?.(a.dataset.sessionAnalysis);location.hash='signals';}));
    }catch(err){if($('#sessions-body'))$('#sessions-body').innerHTML=`<div class="no-device">Ошибка списка записей: ${esc(err.message)}</div>`;}
  }
  function init(options) {
    ctx=options;preview=Boolean(options.preview);
    if(preview){setTopStatus({status:'idle'});return;}
    setupCalibrationDialog();
    document.addEventListener('visibilitychange',visibilityChanged);
    // A hardware view loads its status on entry; periodic polling covers other views.
    scheduleStatusPoll();
  }
  async function renderView(nextView) {
    closeWS();clearGraphs();view=nextView;
    const box=$('#content');if(!box)return;
    if(preview){box.innerHTML=previewPanel(view);bind();return;}
    if(view==='device'){
      statusFingerprint='';box.innerHTML=devicePanel(status);bind();await poll();
    }else if(view==='live'){
      box.innerHTML=livePanel();bind();
      window.NeuroCheckpoints?.syncControls();
      observePlots();
      openWS(); await poll();
    }else if(view==='sessions'){
      box.innerHTML=sessionsPanel();bind();await loadSessions();
    }
  }
  function leave(){closeWS();clearGraphs();view=null;}
  window.NeuroLive={init,renderView,leave,poll};
})();

/* Unified NeuroScope hardware, real-time monitor and session manager.
 * One route, one design system; no hidden calibration modal or simulated connection.
 */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const n = v => v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
  const fmt = (v, digits=1) => n(v) == null ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:digits});
  const blank = '—';
  let ctx = {}, view = null, preview = false, pollTimer = null, ws = null, wsRetry = null, wsEpoch = 0;
  let status = null, replay = {active:false}, charts = [], chartClock = 0, statusFingerprint = '', sessionRows = [], liveRange = 15, metricRange = 300, allowRaw = false;
  const samples = {};
  const chartDefs = [
    {id:'live-eeg',keys:['eeg1','eeg2'],series:[['O1–T3','#7ce3cb'],['O2–T4','#8ca9ff']],raw:true},
    {id:'live-ppg',keys:['ppg'],series:[['ФПГ','#e4a2ad']],raw:true},
    {id:'live-motion',keys:['motion'],series:[['Модуль ускорения','#f0c38a']],raw:true},
    {id:'live-acc',keys:['accX','accY','accZ'],series:[['X','#7ce3cb'],['Y','#8ca9ff'],['Z','#f0c38a']],raw:true},
    {id:'live-gyro',keys:['gyroX','gyroY','gyroZ'],series:[['X','#7ce3cb'],['Y','#8ca9ff'],['Z','#e4a2ad']],raw:true},
    {id:'live-resistance',keys:['res1','res2'],series:[['Электрод 1, кОм','#7ce3cb'],['Электрод 2, кОм','#8ca9ff']],raw:false,contact:true},
    {id:'live-hr-chart',keys:['hr'],series:[['Пульс, уд/мин','#e4a2ad']]},
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
      ${progress}<p class="chart-caption">Только настоящие отсчёты сопротивления Capsule SDK · порог ${fmt(c.ready_below_kohm,0)} кОм по каждому каналу; желательнее ниже ${fmt(c.preferred_below_kohm,0)} кОм. Возраст данных: ${c.updated_age_s==null?'—':`${fmt(c.updated_age_s,1)} с`}. Это проверка контакта, а не подтверждение нахождения на голове или чистоты ЭЭГ.</p>
      ${compact?'':'<p class="chart-caption">Поправьте посадку, раздвиньте волосы под электродами. Не давите слишком сильно. При снятии или потере данных отсчёт стабильности начинается заново.</p>'}
    </div>`;
  }
  function statusBox(s) {
    const [title, tone] = label(s?.status);
    return `<div class="device-hero ${tone}">
      <div class="device-emblem"><span class="connection-ring"></span><svg class="icon"><use href="#i-wave"/></svg></div>
      <div class="device-hero-copy"><div class="section-kicker">ПОДКЛЮЧЕНИЕ / CAPSULE</div>
        <h2>${esc(title)}</h2><p>${esc(safeInfo(s))}</p>
        ${s?.connected?`<div class="device-facts"><span>${esc(s.device || 'Neiry Headband')}</span><span>Серийный № ${esc(s.serial||blank)}</span><span>Батарея ${n(s.battery)!==null?`${fmt(s.battery,0)}%`:blank}</span></div>`:''}
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
    const cal = s.connected ? `${contactPanel(s.contact)}<div class="cal-box"><div class="cal-state"><strong>${s.calibrated?'Калибровка выполнена':{
      ready:'Калибровка доступна',running:'Калибровка выполняется',failed:'Калибровка не удалась',
      skipped:'Калибровка пропущена',unavailable:'Калибратор недоступен',complete:'Калибровка выполнена'}[s.calibration_phase]||'Нет калибровки'}</strong><small>${s.contact?.ready?'Контакт стабилен — калибровку можно запустить.':'Калибровка заблокирована до стабильных показаний всех каналов.'} Калибровка нужна для персонализированных показателей SDK.</small></div>
      <div class="cal-actions">${pButton(s.calibration_phase==='complete'?'Повторить калибровку':'Начать калибровку','calibration','subtle',!s.calibration_available||s.calibration_phase==='running')}${s.calibration_phase==='ready'?pButton('Пропустить','skip-cal','subtle'):''}</div></div>`:
      `<div class="no-device small">Калибровка недоступна: сначала подключите реальное устройство. Она никогда не запускается сама.</div>`;
    return `${statusBox(s)}<div class="device-two-col">
      <section class="card">${heading('Обнаруженные устройства','Сначала поиск, затем явный выбор и проверка соединения')}${found}
      <div class="connection-options"><label class="toggle-row"><input type="checkbox" id="auto-reconnect" ${s.auto_reconnect?'checked':''} ${!s.sdk_ready?'disabled':''}><span><strong>Автоматическое переподключение</strong><small>По умолчанию выключено; после потери связи повторить поиск и попытаться подключить прежнее устройство</small></span></label></div></section>
      <section class="card">${heading('Контакт электродов и калибровка','Сначала измерения сопротивления, затем стабильный контакт и калибровка')}${cal}
      <div class="steps-list"><div><b>01</b><span>Включите повязку и Bluetooth на компьютере.</span></div><div><b>02</b><span>Нажмите «Найти устройство», дождитесь окончания поиска.</span></div><div><b>03</b><span>Выберите найденный Neiry Headband. Приложение подтвердит связь через SDK.</span></div><div><b>04</b><span>Наденьте повязку, дождитесь снижения реального сопротивления каждого канала и стабильности показаний.</span></div><div><b>05</b><span>После проверки контакта запустите калибровку. При пропуске персональные метрики SDK не отображаются.</span></div></div></section>
    </div>${!s.sdk_ready?`<section class="card device-warning">${heading('SDK не готов','Это не значит, что записи недоступны')}<p>Живое подключение требует поставку Capsule SDK и нативную библиотеку для вашей системы. Укажите <code>CAPSULE_SDK_DIR</code> перед запуском и проверьте, что Bluetooth доступен приложению.</p><p class="device-error">${esc(s.sdk_error || 'При отсутствии SDK аппаратный поиск недоступен.')}</p><a href="#sessions" data-go="sessions" class="button subtle">Открыть сохранённые записи</a></section>`:''}`;
  }
  function heading(title,subtitle='') {return `<div class="card-header"><div><h2>${esc(title)}</h2>${subtitle?`<p class="card-subtitle">${esc(subtitle)}</p>`:''}</div></div>`;}
  function livePanel() {
    return `<div class="live-banner" id="live-banner"><span class="activity-dot"></span><div><strong id="live-mode">Ожидание сигнала</strong><p id="live-message">Подключите устройство или запустите воспроизведение записи.</p></div><div class="live-banner-actions"><a class="button subtle" href="#device" data-go="device">Устройство</a><button class="button subtle" id="replay-pause" data-action="pause-replay" hidden>Пауза</button><button class="button subtle" id="replay-forward" data-action="seek-forward" hidden>+30 с</button><button class="button subtle" id="replay-stop" data-action="stop-replay" hidden>Остановить воспроизведение</button></div></div>
      <div id="live-contact-card" class="contact-live-wrap">${contactPanel(status?.contact,true)}</div>
      <div class="live-metrics"><section class="metric-card"><div class="metric-label">Пульс · SDK</div><div class="metric-value" id="live-hr">—</div><div class="metric-note">уд/мин · если доступно</div></section>
      <section class="metric-card"><div class="metric-label">Внимание · SDK</div><div class="metric-value" id="live-attention">—</div><div class="metric-note">Индекс производителя, не % концентрации</div></section>
      <section class="metric-card"><div class="metric-label">Расслабление · SDK</div><div class="metric-value" id="live-relax">—</div><div class="metric-note">Индекс производителя</div></section>
      <section class="metric-card"><div class="metric-label">Возраст данных</div><div class="metric-value" id="live-age">—</div><div class="metric-note">с · время получения сервером</div></section></div>
      <div class="studio-controls"><label style="max-width:200px">Сырые сигналы · окно<select id="live-time-range"><option value="5">5 секунд</option><option value="15" selected>15 секунд</option><option value="30">30 секунд</option><option value="60">60 секунд</option></select></label><label style="max-width:210px">Индексы SDK · окно<select id="metric-time-range"><option value="60">1 минута</option><option value="300" selected>5 минут</option><option value="900">15 минут</option></select></label><label class="toggle-row raw-toggle"><input type="checkbox" id="show-unverified-raw"><span><strong>Показывать сырой поток без контакта</strong><small>Только для диагностики шума, не для выводов о работе мозга</small></span></label></div>
      <p class="chart-caption">До подтверждения контакта мозговые показатели и графики ЭЭГ/ФПГ скрыты. В режиме диагностики показываются реальные необработанные отсчёты SDK, даже если повязка лежит на столе.</p>
      <div class="live-graphs">
        <section class="card wide">${heading('ЭЭГ · живой сигнал','Два биполярных отведения, значения в единицах SDK')}<div class="live-canvas"><canvas id="live-eeg" aria-label="График живой ЭЭГ"></canvas></div><p class="chart-caption"><span id="live-channels">Каналы: —</span> · <span id="live-eeg-sr">—</span> Гц. График сигнала не является оценкой мыслей или функций отдельных областей мозга.</p></section>
        <section class="card">${heading('ФПГ · пульсовая волна','Оптический сигнал, не ЭКГ')}<div class="live-canvas"><canvas id="live-ppg" aria-label="Живой график ФПГ"></canvas></div></section>
        <section class="card">${heading('Движение · акселерометр','Модуль ускорения, единицы SDK')}<div class="live-canvas"><canvas id="live-motion" aria-label="График движения"></canvas></div></section>
        <section class="card">${heading('Нейрообратная связь · SDK','Пять частотных полос: α, β, θ, δ и SMR. Только после калибровки.')}<div class="live-canvas"><canvas id="live-nfb" aria-label="График пяти NFB-полос"></canvas></div><p class="chart-caption">Это выходы алгоритма производителя. Физический масштаб и психологическая трактовка не подтверждены.</p></section>
        <section class="card">${heading('Акселерометр · XYZ','Реальные значения трёх осей в единицах SDK')}<div class="live-canvas"><canvas id="live-acc" aria-label="Три оси акселерометра"></canvas></div></section>
        <section class="card">${heading('Гироскоп · XYZ','Реальные значения трёх осей в единицах SDK')}<div class="live-canvas"><canvas id="live-gyro" aria-label="Три оси гироскопа"></canvas></div></section>
        <section class="card wide">${heading('Сопротивление электродов','График измерений SDK в кОм, без фиктивных значений')}<div class="live-canvas"><canvas id="live-resistance" aria-label="Сопротивление контактов во времени"></canvas></div></section>
        <section class="card">${heading('Пульс во времени','Только при подтверждённом контакте ФПГ без флагов артефактов')}<div class="live-canvas"><canvas id="live-hr-chart" aria-label="Подтверждённый пульс SDK"></canvas></div></section>
        <section class="card">${heading('Индекс стресса SDK','Производный кардиопоказатель, не психологический стресс')}<div class="live-canvas"><canvas id="live-si-chart" aria-label="Индекс стресса SDK"></canvas></div></section>
        <section class="card">${heading('Индекс Каплана · SDK','Только если SDK считает показатели доступными')}<div class="live-canvas"><canvas id="live-ki-chart" aria-label="Индекс Каплана SDK"></canvas></div></section>
        <section class="card wide">${heading('Оценки внимания и состояний SDK','Пять отдельных выходов алгоритма производителя, не проценты мышления')}<div class="live-canvas"><canvas id="live-emotions" aria-label="Пять метрик SDK: внимание и состояния"></canvas></div></section>
        <section class="card">${heading('Физиологические оценки SDK','Расслабление, концентрация, утомление, стресс, вовлечённость')}<div class="live-canvas"><canvas id="live-phy" aria-label="Физиологические оценки SDK"></canvas></div></section>
        <section class="card">${heading('Продуктивность и связанные оценки SDK','Нет данных или не пройдена калибровка — пустой график')}<div class="live-canvas"><canvas id="live-prod" aria-label="Продуктивность SDK"></canvas></div></section>
        <section class="card">${heading('Контакт и поток','Проверка доступности сигналов')}<div id="live-sensors" class="sensor-summary"><p>Потоков пока нет.</p></div><div id="live-resistances" class="resistance-values"></div></section>
        <section class="card wide">${heading('Диагностика живой ЭЭГ','Проверка плоского сигнала, округления, помех 50/60 Гц; это не сертификат чистоты')}<div id="live-quality-state" class="studio-grid"><p class="chart-caption">Нет достаточного потока ЭЭГ для проверки.</p></div></section>
        <section class="card wide">${heading('Спектрограмма 2–45 Гц','Динамика спектральной мощности. Яркость нормирована отдельно для каждого канала.')}<div class="studio-grid"><div><h3>O1–T3</h3><canvas class="studio-spectrogram" id="live-spectrogram-0" width="660" height="180" aria-label="Спектрограмма первого канала"></canvas></div><div><h3>O2–T4</h3><canvas class="studio-spectrogram" id="live-spectrogram-1" width="660" height="180" aria-label="Спектрограмма второго канала"></canvas></div></div><p class="chart-caption" id="live-spectrogram-note">Для построения нужны не менее трёх секунд потока. Шкала исходных единиц SDK не подтверждена.</p></section>
      </div>
      <section class="card live-controls">${heading('Управление сессией','Запись и метки требуют реального подключения; воспроизведение доступно без устройства')}
        <div class="controls-grid"><div><h3>Запись</h3><p id="live-recording-state">Нет активной записи</p><div class="control-actions">${pButton('Начать запись','start-record','primary')}${pButton('Остановить запись','stop-record')}</div></div>
        <div><h3>Метка события</h3><p>Добавьте маркер к текущей записи.</p><div class="control-actions"><label for="live-marker" class="screen-reader">Название метки</label><input id="live-marker" class="live-input" maxlength="120" placeholder="Например: глаза закрыты">${pButton('Добавить метку','marker')}</div></div></div>
      </section>`;
  }
  function sessionsPanel() {
    return `<section class="card">${heading('Сохранённые сессии','Все действия прямо в NeuroScope: анализ, воспроизведение, экспорт и удаление')}
      <div class="sessions-toolbar"><button class="button subtle" data-action="reload-sessions">Обновить список</button><span id="sessions-summary" class="card-subtitle">Загружаем записи…</span></div><div id="sessions-body" class="sessions-list"><div class="loading-panel"><span class="loader"></span><p>Читаем записи…</p></div></div></section>`;
  }
  function previewPanel(which) { return `<section class="card"><div class="offline-icon"><svg class="icon"><use href="#i-wave"/></svg></div><h2>${which==='live'?'Живой монитор':which==='device'?'Подключение устройства':'Управление сессиями'} доступно в запущенном проекте</h2><p class="card-subtitle" style="margin-top:12px">Это автономный HTML-файл со встроенными результатами ранее записанных измерений. В нём нет сервера, WebSocket, поиска Bluetooth или управления записью. Для этих функций запустите NeuroScope через start.sh / start.bat.</p><a class="button primary" style="margin-top:22px" href="#overview" data-go="overview">Вернуться к анализу</a></section>`; }
  function setTopStatus(s) {
    const badge=$('#device-top-status');if (!badge)return;
    const [title,tone]=label(s?.status);
    badge.innerHTML=`<span class="status-pin ${tone}"></span>${esc(title)}${s?.connected?` · ${esc(s.device||'Neiry')}${s.contact?.ready?' · контакт готов':' · контакт не подтверждён'}`:''}`;
    badge.title=safeInfo(s);
  }
  async function api(path,opts={}) { return ctx.api(path,opts); }
  async function act(action, extra) {
    const map={scan:'/api/device/scan',disconnect:'/api/device/disconnect',calibration:'/api/device/calibration/start','skip-cal':'/api/device/calibration/skip',
      'start-record':'/api/recording/start','stop-record':'/api/recording/stop','stop-replay':'/api/replay/stop','pause-replay':'/api/replay/pause','resume-replay':'/api/replay/resume'};
    try {
      if (action==='marker') {
        const input=$('#live-marker');const value=input?.value.trim();if(!value){notice('Введите название метки.','error');input?.focus();return;}
        await api('/api/markers',{method:'POST',body:JSON.stringify({label:value})}); input.value='';notice('Метка добавлена к текущей записи.','success');
      } else if (action.startsWith('connect:')) {
        await api('/api/device/connect',{method:'POST',body:JSON.stringify({serial:action.slice(8)})}); notice('Подключение начато. Дождитесь подтверждения SDK.');
      } else if(action==='seek-forward') {await api('/api/replay/seek',{method:'POST',body:JSON.stringify({offset_s:(replay?.progress_s||0)+30})});notice('Переходим на 30 секунд вперёд.');} else if(action==='reload-sessions') {await loadSessions();return;}
      else if(action.startsWith('replay:')) {
        const id=action.slice(7); await api(`/api/replay/${encodeURIComponent(id)}/start?speed=1`,{method:'POST'});
        notice('Воспроизведение запущено. В мониторе будут показаны архивные данные.','success'); location.hash='live';
      } else if(action.startsWith('delete:')) {
        const id=action.slice(7);
        if(!window.confirm(`Удалить запись ${id} без возможности восстановления?`))return;
        await api(`/api/records/${encodeURIComponent(id)}`,{method:'DELETE'});
        await loadSessions(); await ctx.reloadSessions?.(); notice('Запись удалена.','success');return;
      } else if(map[action]) {
        await api(map[action],{method:'POST'});notice(({scan:'Поиск запущен.',disconnect:'Отключение запрошено.',calibration:'Калибровка запрошена.','skip-cal':'Калибровка пропущена.','start-record':'Запись начата.','stop-record':'Запись остановлена.','stop-replay':'Воспроизведение остановлено.'})[action]||'Готово.','success');
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
      if(e.target.id==='live-time-range'){liveRange=Number(e.target.value)||15;return;}
      if(e.target.id==='metric-time-range'){metricRange=Number(e.target.value)||300;return;}
      if(e.target.id==='show-unverified-raw'){
        allowRaw=Boolean(e.target.checked);
        if(!allowRaw){for(const key of ['eeg1','eeg2','ppg']) samples[key].length=0;}
        return;
      }
      if(e.target.id!=='auto-reconnect')return;
      const enabled=e.target.checked;
      try{await api('/api/device/reconnect-settings',{method:'POST',body:JSON.stringify({enabled})});await poll();}
      catch(err){notice(err.message,'error');e.target.checked=!enabled;}
    });
  }
  function makeChart(id, datasets) {
    const el = $(`#${id}`);
    if (!el || !window.Chart) return null;
    const config = {
      type: 'line',
      data: {datasets: datasets.map(([label, color]) => ({
        label, data: [], borderColor: color, backgroundColor: color,
        borderWidth: 1.65, pointRadius: 0, tension: 0, spanGaps: false
      }))},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        normalized: true, parsing: false,
        interaction: {mode: 'nearest', axis: 'x', intersect: false},
        plugins: {
          legend: {display: datasets.length > 1, position: 'bottom',
                   labels: {color: '#9baac0', boxWidth: 12, font: {size: 10}}},
          tooltip: {backgroundColor: '#172335', titleColor: '#ecf2f8', bodyColor: '#cbd6e6'}
        },
        scales: {
          x: {type: 'linear', grid: {color: '#25324566'},
              ticks: {color: '#71839e', maxTicksLimit: 7},
              title: {display: true, text: 'Время, с', color: '#71839e'}},
          y: {grid: {color: '#25324566'}, ticks: {color: '#71839e', maxTicksLimit: 6}}
        }
      }
    };
    const instance = new Chart(el, config);
    charts.push(instance);
    return instance;
  }
  function clearGraphs() {charts.forEach(c=>c.destroy());charts=[];Object.values(samples).forEach(a=>a.length=0);chartClock=0;}
  function addSeries(key,packet,values,max=8500) {
    if(!packet||!Array.isArray(values)||!values.length)return;
    const sr=n(packet.sr)||1,t0=n(packet.t0)||0;
    // Peak-preserving decimation, unlike stride-only subsampling.
    const stride=Math.max(1,Math.ceil(sr/70),Math.ceil(values.length/500));
    const dst=samples[key];
    for(let i=0;i<values.length;i+=stride){
      const bucket=[];
      for(let j=i;j<Math.min(values.length,i+stride);j++){
        const y=n(values[j]);if(y!==null)bucket.push({x:t0+j/sr,y});
      }
      if(!bucket.length)continue;
      let lo=bucket[0],hi=bucket[0];
      for(const point of bucket){if(point.y<lo.y)lo=point;if(point.y>hi.y)hi=point;}
      if(lo.x<hi.x){dst.push(lo);if(hi!==lo)dst.push(hi);}else{dst.push(hi);if(hi!==lo)dst.push(lo);}
    }
    const newest=dst[dst.length-1]?.x||0;
    while(dst.length && dst[0].x<newest-65)dst.shift();
    if(dst.length>max)dst.splice(0,dst.length-max);
  }
  function addPoint(key,time,value,max=2500){
    const t=n(time),v=n(value);if(t===null||v===null)return;
    const dst=samples[key] || (samples[key]=[]);
    if(dst.length && t<=dst[dst.length-1].x)return;
    dst.push({x:t,y:v});
    if(dst.length>max)dst.splice(0,dst.length-max);
  }
  function appendMetrics(tick,qualified){
    const allowArchived=replay?.active||tick.validity?.source_mode==='replay';
    if(tick.cardio&&(qualified?.cardio||allowArchived)){
      const c=tick.cardio;
      for(const [key,prop] of [['hr','heartRate'],['stressIndex','stressIndex'],['kaplanIndex','kaplanIndex']])addPoint(key,c.t,c[prop]);
    }
    if(tick.emotions&&(qualified?.emotions||allowArchived))
      for(const key of ['attention','relaxation','cognitiveLoad','cognitiveControl','selfControl'])addPoint(key,tick.emotions.t,tick.emotions[key]);
    if(tick.phy&&(qualified?.phy||allowArchived))
      for(const [key,prop] of [['phyRelax','relaxation'],['phyConcentration','concentration'],['phyFatigue','fatigue'],['phyStress','stress'],['phyInvolvement','involvement']])addPoint(key,tick.phy.t,tick.phy[prop]);
    if(tick.prod&&(qualified?.prod||allowArchived))
      for(const key of ['productivityScore','concentrationScore','relaxationScore','fatigueScore'])addPoint(key,tick.prod.t,tick.prod[key]);
    if(Array.isArray(tick.nfb)&&(qualified?.nfb||allowArchived))
      for(const item of tick.nfb)for(const key of ['alpha','beta','theta','delta','smr'])addPoint(key,item?.t,item?.[key]);
    const times=tick.resistances_times||[],hist=tick.resistances_hist||[];
    if(tick.connected && Array.isArray(hist)){
      const offset=Math.max(0,hist.length-times.length);
      for(let i=0;i<times.length;i++){
        const row=hist[i+offset];if(!Array.isArray(row))continue;
        for(let k=0;k<2;k++){const v=n(row[k]);if(v!==null&&v>0)addPoint(`res${k+1}`,times[i],v/1000);}
      }
    }
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
  async function updateSignalQuality(){
    if(preview||view!=='live')return;
    // Raw FFT panels are diagnostics, never unlabelled "brain activity" off-head.
    if(!replay?.active && !status?.validity?.eeg && !allowRaw){
      const box=$('#live-quality-state');if(box)box.textContent='ЭЭГ пока не подтверждена контактом. Включите диагностический показ сырого сигнала, если хотите изучить аппаратный шум.';
      const note=$('#live-spectrogram-note');if(note)note.textContent='Спектрограмма скрыта: контакт электродов не подтверждён.';
      for(let i=0;i<2;i++)drawHeatmap(`live-spectrogram-${i}`,null);
      return;
    }
    try{
      const [quality,tiles]=await Promise.all([api('/api/live/quality'),api('/api/live/spectrogram?seconds=20')]);
      if(view!=='live')return;
      const box=$('#live-quality-state');if(box){
        box.innerHTML=quality.available?quality.channels.map(c=>`<div class="studio-state"><strong>${esc(c.name)} · ${c.status==='screened'?'Без обнаруженных флагов':esc(c.status)}</strong><span>${(c.reasons||[]).map(esc).join('; ')||'Проверки не выявили заданных инженерных предупреждений; это не оценка здоровья.'}</span><small>Различных уровней: ${fmt(c.unique_levels,0)} · Помеха: ${c.line_power_fraction==null?'—':fmt(c.line_power_fraction*100)}%</small></div>`).join(''):`<p class="chart-caption">${esc(quality.reason||'Нет данных')}</p>`;
      }
      (tiles.channels||[]).forEach((c,i)=>drawHeatmap(`live-spectrogram-${i}`,c));
      const note=$('#live-spectrogram-note');if(note)note.textContent=tiles.available?(status?.validity?.eeg?'Относительная мощность из текущего контакта; не медицинская диагностика.':'Только аппаратный сырой сигнал без проверенного контакта — это может быть шум.'):'Для построения нужны не менее трёх секунд ЭЭГ.';
    }catch(e){const el=$('#live-quality-state');if(el)el.textContent='Диагностика недоступна: '+e.message;}
  }
  function updateLiveTick(tick) {
    if(view!=='live')return;
    const eeg=tick.eeg_stream,ppg=tick.ppg_stream,mems=tick.mems_stream;
    const archived=Boolean(replay?.active||tick.validity?.source_mode==='replay');
    const valid=tick.validity||{};
    const eegVisible=archived||valid.eeg||allowRaw && tick.connected;
    const ppgVisible=archived||valid.ppg||allowRaw && tick.connected;
    if(eegVisible&&eeg){addSeries('eeg1',eeg,eeg.ch1);addSeries('eeg2',eeg,eeg.ch2);}
    else{samples.eeg1.length=0;samples.eeg2.length=0;}
    if(ppgVisible&&ppg)addSeries('ppg',ppg,ppg.samples);
    else samples.ppg.length=0;
    if(mems&&Array.isArray(mems.acc)) {
      const magnitude=v=>Array.isArray(v)&&v.every(x=>n(x)!==null)?Math.hypot(...v.map(Number)):null;
      addSeries('motion',mems,mems.acc.map(magnitude));
      for(let i=0;i<3;i++)addSeries(['accX','accY','accZ'][i],mems,mems.acc.map(v=>Array.isArray(v)?v[i]:null));
      if(Array.isArray(mems.gyro))for(let i=0;i<3;i++)addSeries(['gyroX','gyroY','gyroZ'][i],mems,mems.gyro.map(v=>Array.isArray(v)?v[i]:null));
    }
    if(!valid.cardio&&!archived)for(const key of ['hr','stressIndex','kaplanIndex'])samples[key].length=0;
    if(!valid.emotions&&!archived)for(const key of ['attention','relaxation','cognitiveLoad','cognitiveControl','selfControl'])samples[key].length=0;
    if(!valid.phy&&!archived)for(const key of ['phyRelax','phyConcentration','phyFatigue','phyStress','phyInvolvement'])samples[key].length=0;
    if(!valid.prod&&!archived)for(const key of ['productivityScore','concentrationScore','relaxationScore','fatigueScore'])samples[key].length=0;
    if(!valid.nfb&&!archived)for(const key of ['alpha','beta','theta','delta','smr'])samples[key].length=0;
    appendMetrics(tick,valid);
    const liveContact=$('#live-contact-card');
    if(liveContact)liveContact.innerHTML=archived?'<div class="contact-panel"><strong>Архивное воспроизведение</strong><p class="chart-caption">Данные из сохранённого файла. Текущий контакт электродов не оценивается.</p></div>':contactPanel(tick.contact,true);
    const field=(id,v)=>{const el=$(`#${id}`);if(el)el.textContent=n(v)===null?'—':fmt(v,1);};
    field('live-hr',(valid.cardio||archived)?tick.cardio?.heartRate:null);
    field('live-attention',(valid.emotions||archived)?tick.emotions?.attention:null);
    field('live-relax',(valid.emotions||archived)?tick.emotions?.relaxation:null);
    field('live-age',tick.data_age_s);
    if($('#live-channels'))$('#live-channels').textContent=`Каналы: ${(tick.eeg_channels||[]).join(' / ')||'—'}`;
    if($('#live-eeg-sr'))$('#live-eeg-sr').textContent=fmt(tick.eeg_sr,0);
    if($('#live-recording-state'))$('#live-recording-state').textContent=tick.recording?`Идёт запись: ${tick.recording}`:'Нет активной записи';
    const names={eeg:'ЭЭГ',skin:'Контакт',ppg:'ФПГ',cardio:'Кардио',mems:'Движение',nfb:'NFB',emotions:'SDK эмоции',phy:'SDK физиология',prod:'SDK продуктивность'};
    if($('#live-sensors'))$('#live-sensors').innerHTML=Object.entries(names).map(([key,name])=>{
      const v=tick.sensor_status?.[key]||{state:'none'};
      return `<span class="sensor-pill ${esc(v.state||'none')}"><span class="sensor-light"></span>${esc(name)}</span>`;
    }).join('');
    if($('#live-resistances'))$('#live-resistances').innerHTML=(tick.contact?.channels||[]).map(r=>`<span>${esc(r.channel)}: <strong>${n(r.kohm)!==null?`${fmt(r.kohm,1)} кОм`:'нет измерения'}</strong></span>`).join('')||'<span>Нет актуального измерения электродов</span>';
    if(Date.now()-chartClock<550)return;
    chartClock=Date.now();
    for(let idx=0;idx<chartDefs.length;idx++){
      const cfg=chartDefs[idx],ch=charts[idx];if(!ch)continue;
      cfg.keys.forEach((key,i)=>ch.data.datasets[i].data=samples[key]);
      const current=Math.max(0,...cfg.keys.map(key=>samples[key]?.at(-1)?.x??0));
      const range=cfg.raw?liveRange:cfg.contact?Math.min(metricRange,300):metricRange;
      const max=Math.max(range,current);ch.options.scales.x.min=Math.max(0,max-range);ch.options.scales.x.max=max;
      ch.update('none');
    }
  }
  function updateLiveStatus() {
    if(view!=='live'||!$('#live-mode'))return;
    const activeReplay=replay?.active;
    const isConnected=connected();
    $('#live-mode').textContent=activeReplay?'Воспроизведение сохранённой записи':isConnected?(status.contact?.ready?'Контакт электродов подтверждён':'Подключено, контакта пока нет'):'Нет активного источника сигнала';
    $('#live-message').textContent=activeReplay?`Запись ${replay.session_id} · скорость ×${fmt(replay.speed,1)} · это не текущие данные человека.`:
      isConnected?(status.contact?.message || 'Подключено. Ожидаем показатели сопротивления электродов.'):

      'Подключите устройство на вкладке «Устройство» или воспроизведите архивную сессию на вкладке «Записи».';
    const stop=$('#replay-stop');if(stop)stop.hidden=!activeReplay;
    const pause=$('#replay-pause');if(pause){pause.hidden=!activeReplay;pause.dataset.action=replay?.paused?'resume-replay':'pause-replay';pause.textContent=replay?.paused?'Продолжить':'Пауза';}
    const forward=$('#replay-forward');if(forward)forward.hidden=!activeReplay;
    const start=$('[data-action="start-record"]');if(start)start.disabled=!isConnected||!status?.validity?.eeg||Boolean(status?.recording);
    const marker=$('[data-action="marker"]');if(marker)marker.disabled=!isConnected||!Boolean(status?.recording);
    const stopRecord=$('[data-action="stop-record"]');if(stopRecord)stopRecord.disabled=!isConnected||!Boolean(status?.recording);
    if($('#live-recording-state'))$('#live-recording-state').textContent=status?.recording?`Идёт запись: ${status.recording}`:'Нет активной записи';
  }
  function openWS() {
    if(preview||view!=='live'||ws)return;
    const epoch=++wsEpoch;
    const scheme=location.protocol==='https:'?'wss':'ws';
    ws=new WebSocket(`${scheme}://${location.host}/ws`);
    ws.onmessage=e=>{try{const tick=JSON.parse(e.data);if(tick.type==='tick')updateLiveTick(tick);}catch(err){console.warn('NeuroScope WS data:',err);}};
    ws.onclose=()=>{ws=null;if(view==='live'&&epoch===wsEpoch){wsRetry=setTimeout(openWS,1700);}};
    ws.onerror=()=>{try{ws?.close();}catch(_){}};
  }
  function closeWS(){wsEpoch++;clearTimeout(wsRetry);wsRetry=null;if(ws){const old=ws;ws=null;old.onclose=null;old.close();}}
  async function poll() {
    if(preview)return;
    try {
      const s=await api('/api/device/status');status=s;setTopStatus(s);
      if(view==='device'&&$('#content')){
        const fingerprint=JSON.stringify({status:s.status,message:s.message,connected:s.connected,discovered:s.discovered,calibration_phase:s.calibration_phase,calibration_available:s.calibration_available,contact:s.contact,scanning:s.scanning,auto_reconnect:s.auto_reconnect,battery:s.battery});
        if(fingerprint!==statusFingerprint){statusFingerprint=fingerprint;$('#content').innerHTML=devicePanel(s);bind();}
      }
      if(view==='live'){
        try{replay=await api('/api/replay/status');}catch(_){replay={active:false};}
        updateLiveStatus();
        await updateSignalQuality();
      }
    }catch(e){setTopStatus({status:'error',message:'Не удаётся связаться с локальным сервером'});if(view==='device'&&$('#content'))$('#content').innerHTML=`<section class="card"><h2>Нет связи с сервером</h2><p>${esc(e.message)}</p>${pButton('Повторить','scan')}</section>`;}
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
        <div class="session-list-actions"><a class="button subtle" href="#signals" data-session-analysis="${esc(id)}">Анализ</a>${pButton('Воспроизвести','replay:'+id,'subtle',s.active||connected())}<a class="button subtle" href="/api/records/${encodeURIComponent(id)}/download" download>ZIP</a>${pButton('Удалить','delete:'+id,'subtle',s.active)}</div></article>`;
      }).join('');
      $$('[data-session-analysis]',box).forEach(a=>a.addEventListener('click',e=>{e.preventDefault();ctx.selectSession?.(a.dataset.sessionAnalysis);location.hash='signals';}));
    }catch(err){if($('#sessions-body'))$('#sessions-body').innerHTML=`<div class="no-device">Ошибка списка записей: ${esc(err.message)}</div>`;}
  }
  function init(options) {
    ctx=options;preview=Boolean(options.preview);
    if(preview){setTopStatus({status:'idle'});return;}
    if(pollTimer)clearInterval(pollTimer);
    pollTimer=setInterval(poll,1900);
    poll();
  }
  async function renderView(nextView) {
    closeWS();clearGraphs();view=nextView;
    const box=$('#content');if(!box)return;
    if(preview){box.innerHTML=previewPanel(view);bind();return;}
    if(view==='device'){
      statusFingerprint='';box.innerHTML=devicePanel(status);bind();await poll();
    }else if(view==='live'){
      box.innerHTML=livePanel();bind();
      for(const cfg of chartDefs)makeChart(cfg.id,cfg.series);
      openWS(); await poll();
    }else if(view==='sessions'){
      box.innerHTML=sessionsPanel();bind();await loadSessions();
    }
  }
  function leave(){closeWS();clearGraphs();view=null;}
  window.NeuroLive={init,renderView,leave,poll};
})();

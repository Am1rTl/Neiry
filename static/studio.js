/* NeuroScope: experiments, personal history and detailed session fragments.
 * Local-first; all user-supplied strings are escaped before HTML insertion.
 */
(() => {
  'use strict';
  const $ = (s,r=document)=>r.querySelector(s);
  const esc = s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=(v,d=1)=>Number.isFinite(Number(v))&&v!==null?Number(v).toLocaleString('ru-RU',{maximumFractionDigits:d}):'—';
  const btn=(label,act,style='subtle',disabled=false)=>`<button class="button ${style}" data-studio="${esc(act)}" ${disabled?'disabled':''}>${esc(label)}</button>`;
  let ctx={},view=null,poller=null,chart=null,sessions=[];
  let viewEvents, doorRun, presentedPhase, doorAway, experimentBusy=false;
  let bounds=null, segmentRequest, displayed=null;
  let visibilityRequest=Promise.resolve();
  const doorProtocol='door_imagery';
  const doorStopReasons={
    signal_lost:'Свежая ЭЭГ или контакт электродов пропали.',
    stimulus_hidden:'Вкладка с экспериментом стала недоступна.',
    stimulus_missing:'Браузер не подтвердил показ этапа вовремя.',
    pause_timeout:'Истекло время ожидания возвращения к заданию.',
    recording_stopped:'Запись завершилась до конца эксперимента.',
    recording_rotated:'Во время эксперимента начался новый файл записи.',
  };
  const content=()=>$('#content');
  function message(s,error=false){ctx.notify?.(s,error?'error':'success');}
  async function api(url,options){return ctx.api(url,options);}
  function leaveDoor(){
    if(!doorRun)return;
    const run=doorRun;
    doorRun=null;
    api(`/api/experiments/stop?run_id=${encodeURIComponent(run)}`,{method:'POST',keepalive:true}).catch(()=>{});
  }
  window.addEventListener('pagehide',leaveDoor);
  function setDoorAway(away){
    const run=doorRun;
    if(!run||doorAway===away)return visibilityRequest;
    doorAway=away;
    presentedPhase=null;
    visibilityRequest=visibilityRequest.then(()=>api('/api/experiments/door/visibility',{
      method:'POST',keepalive:true,body:JSON.stringify({run_id:run,paused:away}),
    })).catch(()=>{
      if(doorRun===run&&doorAway===away)doorAway=null;
    });
    return visibilityRequest;
  }
  function shutdown(){
    if(view==='experiments')void setDoorAway(true);
    viewEvents?.abort();
    segmentRequest?.abort();
    bounds=null; displayed=null;
    clearTimeout(poller);
    poller=null;
    if(chart){chart.destroy();chart=null;}
    view=null;
  }
  function card(title,body,subtitle=''){return `<section class="card"><div class="card-header"><div><h2>${esc(title)}</h2>${subtitle?`<p class="card-subtitle">${esc(subtitle)}</p>`:''}</div></div>${body}</section>`;}
  async function showExperiments(){
    const signal=viewEvents.signal;
    content().innerHTML=card('Эксперименты с временными метками',
      '<div id="studio-exp-state">Проверяем устройство и запись…</div><div class="studio-controls">'+
      btn('Начать запись','record','primary',true)+btn('Завершить запись','record-stop','subtle',true)+
      btn('Остановить протокол','abort','subtle',true)+'</div>');
    try {
      const payload=await api('/api/experiments/templates',{signal});
      const list=Object.entries(payload.templates).map(([key,value])=>card(value.title,
        `<p>${esc(value.description)}</p><ol class="studio-list">${value.phases.map(p=>`<li>${esc(p.label)} · ${fmt(p.duration_s,0)} с</li>`).join('')}</ol>${btn('Запустить протокол',`start:${key}`,'primary',true)}`));
      content().insertAdjacentHTML('beforeend',doorCard(payload.door_trials)+`<div class="studio-grid">${list.join('')}</div>`);
      bindActions();
      document.addEventListener('visibilitychange',()=>{void setDoorAway(document.hidden);},{signal});
      await setDoorAway(document.hidden);
      if(!signal.aborted)await pollExperiment(signal);
    } catch(e){if(!signal.aborted)message(e.message,true);}
  }
  function doorCard(trials){
    return card('Виртуальная дверь',`
      <p class="chart-caption">Держите глаза открытыми и смотрите в центр. После исчезновения подсказки выполните указанную задачу и продолжайте до открытия двери.</p>
      <ul class="studio-list">
        <li><strong>Представьте движение</strong> — мысленно почувствуйте, как правая рука поворачивает ручку двери. Оставьте руку неподвижной.</li>
        <li><strong>Покой</strong> — спокойно смотрите на крест, без воображаемого движения.</li>
      </ul>
      <div class="door-stage" id="door-stage">
        <div class="door-frame" aria-hidden="true"><div class="door-leaf"></div></div>
        <span class="door-cue" id="door-cue">+</span>
      </div>
      <p id="door-status" class="chart-caption" role="status">Начните запись, затем запустите эксперимент.</p>
      <div class="studio-controls">
        ${btn('Запустить эксперимент',`start:${doorProtocol}`,'primary',true)}
        <a id="door-download" class="button subtle" hidden>Скачать ZIP</a>
      </div>
      <p class="chart-caption">При уходе со вкладки эксперимент ставится на паузу. При возвращении текущая проба начнётся заново.</p>`,
      `${trials} испытаний · поровну покоя и воображаемого движения · около 4 минут`);
  }
  function renderDoor(s,d,signal){
    const isDoor=s.protocol_id===doorProtocol;
    const phase=isDoor&&s.state==='running'&&s.phase;
    const local=phase&&s.run_id===doorRun;
    const own=local&&!document.hidden&&!s.paused&&doorAway!==true;
    const presentation=`${s.presentation_revision ?? 0}:${s.phase_index}`;
    const stage=$('#door-stage'), status=$('#door-status'), download=$('#door-download');
    stage.dataset.open=String(Boolean(own&&phase.label==='feedback'));
    const cue=own&&phase.label==='cue'?(phase.condition==='imagery'?'Представьте движение':'Покой'):'+';
    const cueElement=$('#door-cue');
    if(cueElement.textContent!==cue)cueElement.textContent=cue;
    let label=d.recording?'Запись активна. Можно запустить эксперимент.':'Начните запись, затем запустите эксперимент.';
    if(isDoor){
      if(phase&&s.paused)label='Протокол на паузе. При возвращении текущая проба начнётся заново.';
      else if(phase)label=own?`Испытание ${phase.trial} из ${s.total_trials}`:'Эксперимент запущен в другой вкладке или завершается.';
      else if(s.state==='completed')label=`Готово · ${s.total_trials} испытаний · ${s.session_id}`;
      else if(s.state==='aborted')label=`Остановлено. ${doorStopReasons[s.reason]||'Сохранены уже записанные данные.'}`;
    }
    if(status.textContent!==label)status.textContent=label;
    download.hidden=!isDoor||s.state==='running'||!s.session_id||d.recording===s.session_id;
    if(!download.hidden){
      download.href=`/api/records/${encodeURIComponent(s.session_id)}/download`;
    }
    if(!phase&&s.run_id===doorRun)doorRun=null;
    if(local&&doorAway===null)void setDoorAway(document.hidden);
    if(!own||presentedPhase===presentation)return;
    presentedPhase=presentation;
    requestAnimationFrame(()=>requestAnimationFrame(frameTime=>{
      if(signal.aborted||document.hidden||doorRun!==s.run_id||presentedPhase!==presentation)return;
      api('/api/experiments/door/presented',{method:'POST',signal,body:JSON.stringify({
        run_id:s.run_id,phase_index:s.phase_index,presentation_revision:s.presentation_revision ?? 0,
        client_unix_ms:performance.timeOrigin+frameTime,
      })}).catch(()=>{if(presentedPhase===presentation&&doorRun===s.run_id)presentedPhase=null;});
    }));
  }
  async function pollExperiment(signal){
    let delay=950;
    const requestSignal=AbortSignal.any([signal,AbortSignal.timeout(1500)]);
    try {
      const [s,d]=await Promise.all([api('/api/experiments/status',{signal:requestSignal}),
        api('/api/device/status',{signal:requestSignal})]);
      if(signal.aborted)return;
      const area=$('#studio-exp-state');
      const active=s.state==='running';
      const phase=s.phase;
      const doorActive=active&&s.protocol_id===doorProtocol;
      if(doorActive||experimentBusy)delay=180;
      area.innerHTML=`<div class="studio-state"><strong>${active?'Идёт протокол':s.state==='completed'?'Протокол завершён':s.state==='aborted'?'Протокол остановлен':'Нет активного протокола'}</strong>
       <span>${active&&!doorActive?`Этап ${s.phase_index+1}/${s.total_phases}: ${esc(phase?.label)} · осталось ${fmt(s.remaining_s,0)} с`:
                 `Устройство ${d.connected?'подключено':'не подключено'} · Запись ${d.recording?'активна':'не запущена'}`}</span>
       ${!active&&s.session_id&&s.run_id?`<a class="button primary" href="#segment?session=${encodeURIComponent(s.session_id)}&amp;run=${encodeURIComponent(s.run_id)}">ЭЭГ эксперимента</a>`:''}
       ${active&&!doorActive?`<progress value="${Math.max(0,(phase.duration_s-s.remaining_s))}" max="${phase.duration_s}" style="width:100%"></progress>`:''}</div>`;
      const eegReady=d.connected&&d.validity?.eeg&&!d.stream_stale;
      $$('[data-studio^="start:"]').forEach(b=>{
        const door=b.dataset.studio===`start:${doorProtocol}`;
        b.disabled=experimentBusy||active||!d.connected||!d.recording||
          d.calibration_phase==='running'||(door&&!eegReady);
      });
      $('[data-studio="record"]').disabled=experimentBusy||!!d.recording||!eegReady;
      $('[data-studio="record-stop"]').disabled=experimentBusy||!d.recording;
      $('[data-studio="abort"]').disabled=experimentBusy||!active;
      renderDoor(s,d,signal);
    }catch(e){
      if(!signal.aborted){
        $('#studio-exp-state').textContent=e.message;
        delay=doorRun?180:950;
      }
    }finally{
      if(!signal.aborted)poller=setTimeout(()=>pollExperiment(signal),delay);
    }
  }
  const $$=(s,r=document)=>[...r.querySelectorAll(s)];
  function historyRow(row) {
    const status = ctx.archiveStatus(row);
    const ready = row.analysis_state === 'ready';
    const quality = ready && row.quality_status ? ctx.statusInfo(row.quality_status).label : '—';
    const link = `?session=${encodeURIComponent(row.id)}#overview`;
    const name = status.action ? `<a href="${link}">${esc(row.id)}</a>` : esc(row.id);
    const units = (value, unit) => ready && value != null ? `${fmt(value)} ${unit}` : '—';
    return `<tr><td>${name}</td><td>${esc(row.participant_code || 'Не указан')}</td><td>${esc(row.protocol_id || '—')}</td>
      <td>${esc(row.eye_state || '—')}</td><td>${esc(status.label)}</td><td>${esc(quality)}</td>
      <td>${units(row.usable_eeg_pct, '%')}</td><td>${units(row.pulse_rate_bpm, 'уд/мин')}</td></tr>`;
  }

  async function verifyHistory(rows, button, signal) {
    button.disabled = true;
    const progress = $('#history-progress');
    const errors = [];
    let completed = 0;
    for (const row of rows) {
      if (signal.aborted) return;
      progress.textContent = `Проверяем ${completed + errors.length + 1} из ${rows.length} · ${row.id}`;
      try {
        await api(`/api/lab/analysis/${encodeURIComponent(row.id)}/verify`, {method:'POST', signal});
        completed++;
      } catch (error) {
        if (signal.aborted) return;
        errors.push(`${row.id} — ${error.message}`);
      }
    }
    if (!signal.aborted) await showHistory({completed, errors});
  }

  async function showHistory(lastCheck) {
    const signal = viewEvents.signal;
    content().innerHTML=card('История исследований','<div class="loading-panel">Читаем локальный архив…</div>');
    try {
      const history = await api('/api/lab/history', {signal});
      if (signal.aborted) return;
      const rows = history.sessions || [];
      const closed = rows.filter(row => !row.active && row.analysis_state !== 'active');
      const waiting = closed.filter(row => row.analysis_state !== 'ready');
      const targets = waiting.length ? waiting : closed;
      const caption = waiting.length ? 'Проверить ожидающие' : 'Сверить файлы ещё раз';
      const action = targets.length ? `<button type="button" id="verify-history" class="button subtle">${caption} (${targets.length})</button>` : '';
      const result = lastCheck ? `Проверено: ${lastCheck.completed}. Ошибок: ${lastCheck.errors.length}.` : '';
      const errors = lastCheck?.errors.length ? `<ul>${lastCheck.errors.map(error=>`<li>${esc(error)}</li>`).join('')}</ul>` : '';
      const body = `<p class="chart-caption">${esc(history.notice||'')}</p>
        <div class="sessions-toolbar">${action}<span id="history-progress" role="status">${result}</span></div>${errors}
        <div class="table-wrap"><table class="data-table"><thead><tr><th>Сессия</th><th>Участник</th><th>Протокол</th><th>Глаза</th><th>Состояние отчёта</th><th>Качество сигнала</th><th>Окна без флагов</th><th>Пульс</th></tr></thead>
        <tbody>${rows.map(historyRow).join('')}</tbody></table></div>`;
      content().innerHTML=card('История исследований',body,'Показатели доступны после проверки отчёта по исходным файлам.')+
        card('Сопоставимые группы',`<div class="studio-grid">${(history.comparable_groups||[]).map(g=>`<div class="studio-state"><strong>${esc(g.participant_code)} · ${esc(g.protocol_id||'без протокола')}</strong><span>${g.count} записей · глаза: ${esc(g.eye_state)}</span><p>${g.sessions.map(esc).join(', ')}</p></div>`).join('')||'<p>Для сравнения нужны две проверенные записи с одинаковым кодом участника, протоколом, условием глаз, задачей и монтажом. Условия каждой записи можно заполнить в обзоре.</p>'}</div>`);
      const button = $('#verify-history');
      button?.addEventListener('click', () => verifyHistory(targets, button, signal), {once:true, signal});
    } catch (error) {
      if (!signal.aborted) content().innerHTML=card('История недоступна',`<p>${esc(error.message)}</p>`);
    }
  }
  function resetRange(){
    const session=sessions.find(row=>row.id===$('#studio-session')?.value);
    setRange(0,bounds?bounds.end_s-bounds.start_s:Math.min(session?.eeg_duration_s||10,86400));
  }
  async function showSegment(){
    const signal=viewEvents.signal;
    const params=new URLSearchParams(location.hash.split('?')[1]);
    const session=params.get('session'), run=params.get('run');
    try {
      if(run){
        bounds=await api(`/api/lab/segment/${encodeURIComponent(session)}/experiment/${encodeURIComponent(run)}`,{signal});
        sessions=[{id:session}];
      }else{
        const r=await api('/api/lab/sessions',{signal});
        sessions=(r.sessions||[]).filter(s=>s.id);
      }
    }catch(e){
      if(!signal.aborted)content().innerHTML=card('ЭЭГ недоступна',`<p>${esc(e.message)}</p>`);
      return;
    }
    if(signal.aborted)return;
    content().innerHTML=card(run?'ЭЭГ эксперимента':'Исследователь фрагментов',
      (bounds?`<p>${esc(bounds.name)} · ${fmt(bounds.end_s-bounds.start_s,3)} с · начало в записи: ${fmt(bounds.start_s,3)} с${bounds.approximate?' · границы приблизительные, по времени маркеров':''}</p>`:'')+
      `<div class="studio-controls"><label>Сессия<select id="studio-session" ${run?'disabled':''}>${sessions.map(s=>`<option value="${esc(s.id)}">${esc(s.id)}</option>`).join('')}</select></label>`+
      `<label>От, с<input id="studio-start" type="number" min="0" step="any" value="0"></label>`+
      `<label>До, с<input id="studio-end" type="number" min="0" step="any" value="${bounds?bounds.end_s-bounds.start_s:10}"></label>${btn('Показать','segment','primary')}${btn('Сбросить масштаб','reset')}</div>`+
      '<p class="chart-caption">Потяните по графику, чтобы приблизить участок. Для оценки мощности ритмов выберите от 2 до 120 секунд.</p><div id="studio-segment-result" aria-live="polite"></div>')+
      card('Рецензирование участка',`<div class="studio-controls"><label>Тип<select id="studio-annotation-kind"><option value="note">Заметка</option><option value="artifact">Артефакт</option><option value="event">Событие</option></select></label><label>Описание<input id="studio-annotation-label" maxlength="120" placeholder="Что видно в сигнале"></label>${btn('Сохранить аннотацию','annotation')}</div><div id="studio-annotations"></div><p class="chart-caption">Аннотации сохраняются отдельно от исходных файлов. Пометка артефакта не пересчитывает автоматически все метрики.</p>`)+
      (run?'':card('Экспорт в EEG-BIDS',`<p>Доступен только после проверки физического масштаба ЭЭГ в SDK. Неизвестные единицы нельзя объявлять микровольтами автоматически.</p><div class="studio-controls"><label>Участник<input id="studio-subject" value="01" pattern="[A-Za-z0-9]+"></label><label>Подтверждённые единицы<select id="studio-units"><option value="">Не подтверждены</option><option>uV</option><option>mV</option><option>V</option></select></label><label class="studio-confirm"><input type="checkbox" id="studio-units-confirm">Масштаб проверен по документации SDK</label>${btn('Экспортировать BIDS','bids')}</div><p class="chart-caption">Требуется отдельная установка requirements-research.txt. Аппаратная синхронизация и расположение электродов не восстанавливаются.</p>`));
    bindActions();
    if(sessions.length){
      resetRange();
      await Promise.all([fetchSegment(),fetchAnnotations()]);
    }else{
      $('#studio-segment-result').textContent='Пока нет записей ЭЭГ.';
    }
  }
  async function fetchSegment(){
    if(view!=='segment')return;
    const id=$('#studio-session')?.value;
    if(!id)return;
    const start=$('#studio-start').valueAsNumber, end=$('#studio-end').valueAsNumber;
    const duration=end-start, offset=bounds?.start_s||0;
    if(!Number.isFinite(duration)||start<0||duration<=0||(bounds&&end>bounds.end_s-offset+1e-9)){
      message('Укажите начало и конец внутри записи. Конец должен быть позже начала.',true);return;
    }
    segmentRequest?.abort();
    segmentRequest=new AbortController();
    const signal=AbortSignal.any([viewEvents.signal,segmentRequest.signal]);
    chart?.destroy();chart=null;displayed=null;
    const area=$('#studio-segment-result');area.innerHTML='<div class="loading-panel">Читаем выбранный участок…</div>';
    try{
      const d=await api(`/api/lab/segment/${encodeURIComponent(id)}?start_s=${start+offset}&duration_s=${duration}`,{signal});
      if(signal.aborted)return;
      if(!d.available){area.innerHTML=`<p>${esc(d.reason||'Нет данных')}</p>`;return;}
      displayed={start_s:d.start_s,duration_s:d.duration_s};
      const from=Math.max(0,d.start_s-offset), to=bounds?Math.min(bounds.end_s-offset,from+d.duration_s):from+d.duration_s;
      setRange(from,to);
      const table=d.bands.map(b=>`<tr><td>${esc(b.channel)}</td>${Object.values(b.bands||{}).map(v=>`<td>${fmt(v,1)}%</td>`).join('')||'<td colspan="5">Недоступно</td>'}</tr>`).join('');
      area.innerHTML=`<div class="live-canvas studio-plot"><canvas id="studio-segment-chart" aria-label="Фрагмент ЭЭГ"></canvas></div><p class="chart-caption">${esc(d.sample_count)} отсчётов · ${fmt(d.sample_rate_hz)} Гц · ${esc(d.notice)}</p><div class="table-wrap"><table class="data-table"><thead><tr><th>Канал</th><th>Дельта</th><th>Тета</th><th>Альфа</th><th>Бета</th><th>30–45</th></tr></thead><tbody>${table}</tbody></table></div>`;
      if(window.Chart){chart=new Chart($('#studio-segment-chart'),{type:'line',data:{datasets:d.channels.map((name,i)=>({label:name,data:d.points.map(p=>({x:p.t-offset,y:p.values[i]})),borderColor:['#7ce3cb','#8ca9ff','#e4a2ad'][i%3],pointRadius:0,borderWidth:1.5,spanGaps:false}))},options:{animation:false,responsive:true,maintainAspectRatio:false,parsing:false,normalized:true,scales:{x:{type:'linear',min:from,max:to,ticks:{color:'#90a3bd'},title:{display:true,text:bounds?'От начала эксперимента, с':'От начала ЭЭГ, с',color:'#90a3bd'}},y:{ticks:{color:'#90a3bd'}}},plugins:{legend:{labels:{color:'#b6c6d6'}}}}});bindSelection(chart);}
    }catch(e){if(signal.aborted)return;area.innerHTML=`<p class="device-error">${esc(e.message)}</p>`;}
  }
  function setRange(start,end){
    $('#studio-start').value=start;
    $('#studio-end').value=end;
  }
  function bindSelection(plot){
    const canvas=plot.canvas, overlay=document.createElement('div');
    overlay.className='studio-selection';
    canvas.parentElement.append(overlay);
    const pixel=e=>Math.max(plot.chartArea.left,Math.min(plot.chartArea.right,
      (e.clientX-canvas.getBoundingClientRect().left)*plot.width/canvas.getBoundingClientRect().width));
    let anchor=null, pointer=null;
    canvas.onpointerdown=e=>{
      if(e.button!==0||pointer!==null)return;
      const y=e.clientY-canvas.getBoundingClientRect().top;
      if(y<plot.chartArea.top||y>plot.chartArea.bottom)return;
      anchor=pixel(e);pointer=e.pointerId;
      canvas.setPointerCapture(pointer);
    };
    canvas.onpointermove=e=>{
      if(e.pointerId!==pointer)return;
      const x=pixel(e);
      Object.assign(overlay.style,{display:'block',left:Math.min(anchor,x)+'px',width:Math.abs(anchor-x)+'px',
        top:plot.chartArea.top+'px',height:plot.chartArea.height+'px'});
    };
    canvas.onlostpointercapture=()=>{pointer=null;overlay.style.display='none';};
    canvas.onpointerup=e=>{
      if(e.pointerId!==pointer)return;
      const x=pixel(e);
      if(Math.abs(x-anchor)<4)return;
      const scale=plot.scales.x;
      setRange(scale.getValueForPixel(Math.min(anchor,x)),scale.getValueForPixel(Math.max(anchor,x)));
      void fetchSegment();
    };
  }
  async function fetchAnnotations(){
    const id=$('#studio-session')?.value;if(!id)return;
    const signal=viewEvents.signal;
    try{const d=await api(`/api/lab/annotations/${encodeURIComponent(id)}`,{signal});
      if(signal.aborted||$('#studio-session')?.value!==id)return;
      const el=$('#studio-annotations');
      const items=(d.items||[]).filter(a=>!bounds||(a.start_s>=bounds.start_s&&a.start_s<bounds.end_s));
      if(el)el.innerHTML=items.map(a=>`<div class="studio-state"><strong>${esc(a.category)} · ${fmt(a.start_s-(bounds?.start_s||0))} с</strong><span>${esc(a.label)}</span></div>`).join('')||'<p class="chart-caption">Нет аннотаций</p>';}catch(e){if(!signal.aborted)message(e.message,true);}
  }
  async function exportBids(){
    const id=$('#studio-session').value;
    const payload={subject:$('#studio-subject').value,units:$('#studio-units').value,confirmed:$('#studio-units-confirm').checked,task:'rest'};
    if(!payload.confirmed||!payload.units){message('Сначала подтвердите физический масштаб ЭЭГ по документации SDK.',true);return;}
    const r=await fetch(`/api/lab/bids/${encodeURIComponent(id)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!r.ok){let reason='Ошибка экспорта';try{reason=(await r.json()).detail||reason;}catch(_){}throw Error(reason);}
    const object=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=object;a.download=`${id}_bids.zip`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(object),120000);
  }
  function bindActions(){
    const root=content();if(!root)return;
    const signal=viewEvents.signal;
    root.addEventListener('click',async e=>{const button=e.target.closest('[data-studio]');if(!button||button.disabled)return;e.preventDefault();const action=button.dataset.studio;
      const experiment=view==='experiments';
      if(experiment&&experimentBusy)return;
      if(experiment)experimentBusy=true;
      button.disabled=true;
      try{
        if(action==='record'){await api('/api/recording/start',{method:'POST'});message('Запись начата. Выберите протокол.');}
        else if(action==='record-stop'){await api('/api/recording/stop',{method:'POST'});message('Запись сохранена.');}
        else if(action==='abort'){await api('/api/experiments/stop',{method:'POST'});message('Протокол остановлен.');}
        else if(action.startsWith('start:')){
          const protocol=action.split(':')[1];
          if(protocol===doorProtocol)$('#door-stage').scrollIntoView({block:'center',behavior:'instant'});
          const result=await api('/api/experiments/start',{method:'POST',body:JSON.stringify({protocol_id:protocol})});
          if(protocol===doorProtocol){
            doorRun=result.run_id;
            doorAway=null;
            presentedPhase=null;
            if(view!=='experiments'||document.hidden)await setDoorAway(true);
            else if(!signal.aborted)renderDoor(result,{recording:result.session_id},signal);
          }else if(!signal.aborted)message('Протокол запущен. Маркеры будут сохранены с записью.');
        }
        else if(action==='reset'){resetRange();await fetchSegment();}
        else if(action==='segment'){await fetchSegment();}
        else if(action==='annotation'){
          if(!displayed)throw Error('Сначала откройте участок ЭЭГ');
          const label=$('#studio-annotation-label').value.trim();if(!label)throw Error('Введите описание');
          await api(`/api/lab/annotations/${encodeURIComponent($('#studio-session').value)}`,{method:'POST',body:JSON.stringify({label,category:$('#studio-annotation-kind').value,...displayed})});
          $('#studio-annotation-label').value='';await fetchAnnotations();message('Аннотация сохранена.');
        } else if(action==='bids'){await exportBids();}
      }catch(err){if(!signal.aborted)message(err.message||'Ошибка',true);}finally{
        if(experiment)experimentBusy=false;
        if(!experiment)button.disabled=false;
      }
    },{signal});
    const select=$('#studio-session');if(select)select.addEventListener('change',()=>{resetRange();fetchSegment();fetchAnnotations();});
  }
  async function renderView(next,options){shutdown();view=next;ctx=options;viewEvents=new AbortController();
    if(options.preview){content().innerHTML=card('Доступно в локальном приложении','Эксперименты, BIDS-экспорт и выделение произвольных участков требуют запущенного сервера. Автономный HTML хранит только рассчитанные ранее результаты.');return;}
    if(view==='experiments')await showExperiments();
    else if(view==='history')await showHistory();
    else if(view==='segment')await showSegment();
  }
  window.NeuroStudio={renderView,leave:shutdown};
})();

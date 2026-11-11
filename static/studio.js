/* NeuroScope: experiments, personal history and detailed session fragments.
 * Local-first; all user-supplied strings are escaped before HTML insertion.
 */
(() => {
  'use strict';
  const $ = (s,r=document)=>r.querySelector(s);
  const esc = s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt=(v,d=1)=>Number.isFinite(Number(v))&&v!==null?Number(v).toLocaleString('ru-RU',{maximumFractionDigits:d}):'—';
  const btn=(label,act,style='subtle',disabled=false)=>`<button class="button ${style}" data-studio="${esc(act)}" ${disabled?'disabled':''}>${esc(label)}</button>`;
  let ctx={},view=null,poller=null,chart=null,sessions=[],templates={},history=null;
  const content=()=>$('#content');
  function message(s,error=false){ctx.notify?.(s,error?'error':'success');}
  async function api(url,options){return ctx.api(url,options);}
  function shutdown(){clearInterval(poller);poller=null;if(chart){chart.destroy();chart=null;}view=null;}
  function card(title,body,subtitle=''){return `<section class="card"><div class="card-header"><div><h2>${esc(title)}</h2>${subtitle?`<p class="card-subtitle">${esc(subtitle)}</p>`:''}</div></div>${body}</section>`;}
  async function showExperiments(){
    content().innerHTML=card('Эксперименты с временными метками',
      '<p>Запись запускается отдельно, затем можно выбрать протокол. Переходы между этапами отмечает сервер по своим часам — это не аппаратные триггеры.</p><div id="studio-exp-state"></div><div class="studio-controls">'+btn('Начать запись','record','primary')+btn('Остановить протокол','abort')+'</div>',
      'Открытые/закрытые глаза · покой · задача/восстановление');
    try {
      const payload=await api('/api/experiments/templates');templates=payload.templates||{};
      const list=Object.entries(templates).map(([key,value])=>card(value.title,
        `<p>${esc(value.description)}</p><ol class="studio-list">${value.phases.map(p=>`<li>${esc(p.label)} · ${fmt(p.duration_s,0)} с</li>`).join('')}</ol>${btn('Запустить протокол',`start:${key}`,'primary')}`));
      content().insertAdjacentHTML('beforeend',`<div class="studio-grid">${list.join('')}</div>`);
      bindActions();await pollExperiment();poller=setInterval(pollExperiment,950);
    } catch(e){message(e.message,true);}
  }
  async function pollExperiment(){
    if(view!=='experiments')return;
    try {
      const [s,d]=await Promise.all([api('/api/experiments/status'),api('/api/device/status')]);
      const area=$('#studio-exp-state');if(!area)return;
      const active=s.state==='running';
      const phase=s.phase;
      area.innerHTML=`<div class="studio-state"><strong>${active?'Идёт протокол':s.state==='completed'?'Протокол завершён':s.state==='aborted'?'Протокол остановлен':'Нет активного протокола'}</strong>
       <span>${active?`Этап ${s.phase_index+1}/${s.total_phases}: ${esc(phase?.label)} · осталось ${fmt(s.remaining_s,0)} с`:
                 `Устройство: ${d.connected?'подключено':'не подключено'} · Запись: ${d.recording?'активна':'не запущена'}`}</span>
       ${active?`<progress value="${Math.max(0,(phase.duration_s-s.remaining_s))}" max="${phase.duration_s}" style="width:100%"></progress>`:''}</div>`;
      $$('[data-studio^="start:"]').forEach(b=>b.disabled=active||!d.connected||!d.recording);
      const rec=$('[data-studio="record"]');if(rec)rec.disabled=!!d.recording||!d.connected||d.stream_stale;
      const abort=$('[data-studio="abort"]');if(abort)abort.disabled=!active;
    }catch(e){const area=$('#studio-exp-state');if(area)area.textContent=e.message;}
  }
  const $$=(s,r=document)=>[...r.querySelectorAll(s)];
  async function showHistory(){
    content().innerHTML=card('История и персональные сравнения',
      '<p>Для сопоставления нескольких измерений укажите один и тот же псевдоним участника и одинаковые условия в контексте каждой записи. При отсутствии участника записи не объединяются.</p><div class="loading-panel">Читаем локальный архив…</div>');
    try {
      history=await api('/api/lab/history');const rows=history.sessions||[];
      const body=`<p class="chart-caption">${esc(history.notice||'')}</p>`+
        `<div class="table-wrap"><table class="data-table"><thead><tr><th>Сессия</th><th>Участник</th><th>Протокол</th><th>Глаза</th><th>Качество</th><th>Окна без флагов</th><th>Пульс</th></tr></thead><tbody>${rows.map(s=>`<tr><td>${esc(s.id)}</td><td>${esc(s.participant_code||'Не указан')}</td><td>${esc(s.protocol_id||'—')}</td><td>${esc(s.eye_state||'—')}</td><td>${esc(s.quality_status||'—')}</td><td>${fmt(s.usable_eeg_pct)}%</td><td>${fmt(s.pulse_rate_bpm)} уд/мин</td></tr>`).join('')}</tbody></table></div>`;
      content().innerHTML=card('История исследований',body,'Показатели — описательные, качество и условия нужно оценивать отдельно')+
        card('Сопоставимые группы',`<div class="studio-grid">${(history.comparable_groups||[]).map(g=>`<div class="studio-state"><strong>${esc(g.participant_code)} · ${esc(g.protocol_id||'без протокола')}</strong><span>${g.count} записей · глаза: ${esc(g.eye_state)}</span><p>${g.sessions.map(esc).join(', ')}</p></div>`).join('')||'<p>Пока нет как минимум двух записей с одинаковым кодом участника, протоколом, условием глаз, задачей и монтажом. Для новых данных заполните контекст в «Обзоре».</p>'}</div>`);
    }catch(e){content().innerHTML=card('История недоступна',`<p>${esc(e.message)}</p>`);}
  }
  async function showSegment(){
    const r=await api('/api/lab/sessions');sessions=(r.sessions||[]).filter(s=>s.id);
    content().innerHTML=card('Исследователь фрагментов',
      `<div class="studio-controls"><label>Сессия<select id="studio-session">${sessions.map(s=>`<option value="${esc(s.id)}">${esc(s.id)}</option>`).join('')}</select></label>`+
      `<label>Начало, с<input id="studio-start" type="number" min="0" max="72000" step="1" value="0"></label>`+
      `<label>Длина, с<input id="studio-duration" type="number" min="2" max="120" step="1" value="10"></label>${btn('Исследовать','segment','primary')}</div>`+
      '<p class="chart-caption">Отсчёты сохраняют исходный порядок. Для обзорных графиков применено прореживание с сохранением минимумов/максимумов. Спектральные оценки выбранного интервала не прошли полный контроль артефактов.</p><div id="studio-segment-result"></div>')+
      card('Рецензирование участка',`<div class="studio-controls"><label>Тип<select id="studio-annotation-kind"><option value="note">Заметка</option><option value="artifact">Артефакт</option><option value="event">Событие</option></select></label><label>Описание<input id="studio-annotation-label" maxlength="120" placeholder="Что видно в сигнале"></label>${btn('Сохранить аннотацию','annotation')}</div><div id="studio-annotations"></div><p class="chart-caption">Аннотации сохраняются отдельно от исходных файлов. Пометка артефакта не пересчитывает автоматически все метрики.</p>`)+
      card('Экспорт в EEG-BIDS',`<p>Доступен только после проверки физического масштаба ЭЭГ в SDK. Неизвестные единицы нельзя объявлять микровольтами автоматически.</p><div class="studio-controls"><label>Участник<input id="studio-subject" value="01" pattern="[A-Za-z0-9]+"></label><label>Подтверждённые единицы<select id="studio-units"><option value="">Не подтверждены</option><option>uV</option><option>mV</option><option>V</option></select></label><label class="studio-confirm"><input type="checkbox" id="studio-units-confirm">Масштаб проверен по документации SDK</label>${btn('Экспортировать BIDS','bids')}</div><p class="chart-caption">Требуется отдельная установка requirements-research.txt. Аппаратная синхронизация и расположение электродов не восстанавливаются.</p>`);
    bindActions();if(sessions.length)await Promise.all([fetchSegment(),fetchAnnotations()]);
  }
  async function fetchSegment(){
    if(view!=='segment')return;
    const id=$('#studio-session')?.value, start=Number($('#studio-start').value), duration=Number($('#studio-duration').value);
    if(!id)return;
    const area=$('#studio-segment-result');area.innerHTML='<div class="loading-panel">Читаем выбранный участок…</div>';
    try{
      const d=await api(`/api/lab/segment/${encodeURIComponent(id)}?start_s=${start}&duration_s=${duration}`);
      if(!d.available){area.innerHTML=`<p>${esc(d.reason||'Нет данных')}</p>`;return;}
      const table=d.bands.map(b=>`<tr><td>${esc(b.channel)}</td>${Object.values(b.bands||{}).map(v=>`<td>${fmt(v,1)}%</td>`).join('')||'<td colspan="5">Недоступно</td>'}</tr>`).join('');
      area.innerHTML=`<div class="live-canvas studio-plot"><canvas id="studio-segment-chart" aria-label="Фрагмент ЭЭГ"></canvas></div><p class="chart-caption">${esc(d.sample_count)} отсчётов · ${fmt(d.sample_rate_hz)} Гц · ${esc(d.notice)}</p><div class="table-wrap"><table class="data-table"><thead><tr><th>Канал</th><th>Дельта</th><th>Тета</th><th>Альфа</th><th>Бета</th><th>30–45</th></tr></thead><tbody>${table}</tbody></table></div>`;
      if(chart){chart.destroy();chart=null;}
      if(window.Chart){chart=new Chart($('#studio-segment-chart'),{type:'line',data:{datasets:d.channels.map((name,i)=>({label:name,data:d.points.map(p=>({x:p.t,y:p.values[i]})),borderColor:['#7ce3cb','#8ca9ff','#e4a2ad'][i%3],pointRadius:0,borderWidth:1.5,spanGaps:false}))},options:{animation:false,responsive:true,maintainAspectRatio:false,parsing:false,normalized:true,scales:{x:{type:'linear',ticks:{color:'#90a3bd'},title:{display:true,text:'Время от начала ЭЭГ, с',color:'#90a3bd'}},y:{ticks:{color:'#90a3bd'}}},plugins:{legend:{labels:{color:'#b6c6d6'}}}}});}
    }catch(e){area.innerHTML=`<p class="device-error">${esc(e.message)}</p>`;}
  }
  async function fetchAnnotations(){
    const id=$('#studio-session')?.value;if(!id)return;
    try{const d=await api(`/api/lab/annotations/${encodeURIComponent(id)}`);const el=$('#studio-annotations');if(el)el.innerHTML=(d.items||[]).map(a=>`<div class="studio-state"><strong>${esc(a.category)} · ${fmt(a.start_s)} с</strong><span>${esc(a.label)}</span></div>`).join('')||'<p class="chart-caption">Нет аннотаций</p>';}catch(e){message(e.message,true);}
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
    root.addEventListener('click',async e=>{const button=e.target.closest('[data-studio]');if(!button||button.disabled)return;e.preventDefault();const action=button.dataset.studio;
      button.disabled=true;
      try{
        if(action==='record'){await api('/api/recording/start',{method:'POST'});message('Запись начата. Выберите протокол.');}
        else if(action==='abort'){await api('/api/experiments/stop',{method:'POST'});message('Протокол остановлен.');}
        else if(action.startsWith('start:')){await api('/api/experiments/start',{method:'POST',body:JSON.stringify({protocol_id:action.split(':')[1]})});message('Протокол запущен. Маркеры будут сохранены с записью.');}
        else if(action==='segment'){await fetchSegment();}
        else if(action==='annotation'){
          const label=$('#studio-annotation-label').value.trim();if(!label)throw Error('Введите описание');
          await api(`/api/lab/annotations/${encodeURIComponent($('#studio-session').value)}`,{method:'POST',body:JSON.stringify({label,category:$('#studio-annotation-kind').value,start_s:Number($('#studio-start').value),duration_s:Number($('#studio-duration').value)})});
          $('#studio-annotation-label').value='';await fetchAnnotations();message('Аннотация сохранена.');
        } else if(action==='bids'){await exportBids();}
      }catch(err){message(err.message||'Ошибка',true);}finally{button.disabled=false; if(view==='experiments')await pollExperiment();}
    });
    const select=$('#studio-session');if(select)select.addEventListener('change',()=>{fetchSegment();fetchAnnotations();});
  }
  async function renderView(next,options){shutdown();view=next;ctx=options;
    if(options.preview){content().innerHTML=card('Доступно в локальном приложении','Эксперименты, BIDS-экспорт и выделение произвольных участков требуют запущенного сервера. Автономный HTML хранит только рассчитанные ранее результаты.');return;}
    if(view==='experiments')await showExperiments();
    else if(view==='history')await showHistory();
    else if(view==='segment')await showSegment();
  }
  window.NeuroStudio={renderView,leave:shutdown};
})();

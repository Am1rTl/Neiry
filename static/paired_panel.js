/* Shared live + archive scientific UI for two bipolar EEG leads. */
(function(root){
  'use strict';
  const names={delta:'δ · 1–4 Гц',theta:'θ · 4–8 Гц',alpha:'α · 8–13 Гц',
               beta:'β · 13–30 Гц',low_gamma:'низкая γ · 30–45 Гц'};
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function pct(x){return typeof x==='number'&&Number.isFinite(x)?(x*100).toFixed(1)+'%':'—';}
  function num(x){return typeof x==='number'&&Number.isFinite(x)?x.toFixed(3):'—';}
  function markup(){
    return `<div class="pair-workbench" data-pair-panel>
      <div class="pair-controls"><label>Частотная полоса <select class="select" data-pair-band>${Object.entries(names).map(([k,v])=>`<option value="${k}" ${k==='alpha'?'selected':''}>${v}</option>`).join('')}</select></label>
      <span class="pair-legend"><i class="pair-dot left"></i> O1–T3 <i class="pair-dot right"></i> O2–T4</span></div>
      <div class="pair-body"><div class="pair-map-widget"><canvas class="pair-canvas" data-pair-map role="img" aria-label="Интерактивная условная трёхмерная схема монтажа, электроды O1, T3, O2, T4. Управление мышью или стрелками."></canvas><p data-map-caption class="chart-caption">Схема расположения электродов; не карта активности мозга.</p></div>
      <div class="pair-results" aria-live="polite"><div data-pair-status class="pair-status">Проверяем наличие двух синхронных каналов…</div>
        <div class="pair-stat-grid"><div><small>O1–T3 · доля мощности</small><strong data-pair-left>—</strong></div><div><small>O2–T4 · доля мощности</small><strong data-pair-right>—</strong></div>
        <div><small>Когерентность MSC</small><strong data-pair-coh>—</strong></div><div><small>Модуль ImCoh</small><strong data-pair-imcoh>—</strong></div>
        <div><small>wPLI</small><strong data-pair-wpli>—</strong></div><div><small>Асимметрия ln(О2/О1)</small><strong data-pair-asym>—</strong></div></div>
        <div class="pair-quality" data-pair-quality></div></div></div>
      <p class="pair-disclaimer">Два биполярных канала, не два независимо измеренных полушария. <b>MSC, ImCoh и wPLI</b> описывают согласованность электрических сигналов; их нельзя читать как нейронную связь, причинное влияние или доказательство «доминантности». Подсветка схемы относится только к отведениям, не к участкам коры.</p>
    </div>`;
  }
  function render(panel,data){
    if(!panel)return;
    if(data!==undefined)panel._pairedData=data;
    const v=panel._pairedData||{},band=panel.querySelector('[data-pair-band]')?.value||'alpha';
    const row=v.available?v.bands?.[band]:null;
    const put=(selector,value)=>{const target=panel.querySelector(selector);if(target)target.textContent=value;};
    put('[data-pair-status]',v.available?'Исследовательская оценка двух отведений · не диагноз':
      v.reason||'Парные метрики недоступны: ожидаем качественный сигнал двух каналов.');
    put('[data-pair-left]',row?pct(row.left_relative_power):'—');
    put('[data-pair-right]',row?pct(row.right_relative_power):'—');
    put('[data-pair-coh]',row?num(row.coherence_msc):'—');
    put('[data-pair-imcoh]',row?num(row.imaginary_coherency_magnitude):'—');
    put('[data-pair-wpli]',row?num(row.wpli):'—');
    put('[data-pair-asym]',row?num(row.relative_log_right_left):'—');
    const quality=v.quality||{};
    put('[data-pair-quality]',quality.total_epochs?`Окон без перекрытия: ${quality.accepted_epochs??0} / ${quality.total_epochs} · длительность каждого: ${quality.epoch_seconds||'—'} с. Отбор по сигналу; не независимая медицинская оценка.`:'Недостаточно парных окон для проверки качества.');
    const canvas=panel.querySelector('[data-pair-map]');
    root.NeuroBrainMap?.mount(canvas,v,band);
  }
  function attach(panel,data){
    if(!panel)return;
    if(!panel._pairedBound){
      panel._pairedBound=true;
      panel.querySelector('[data-pair-band]')?.addEventListener('change',()=>render(panel));
    }
    render(panel,data);
  }
  root.NeuroPairedPanel={markup,attach,render};
})(typeof window!=='undefined'?window:globalThis);

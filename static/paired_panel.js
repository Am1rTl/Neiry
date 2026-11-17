/* Shared live and archive view of the two recorded bipolar EEG leads. */
(function (root) {
  'use strict';

  const bands = [
    {id: 'delta', name: 'δ', title: 'Дельта', range: '1–4 Гц', color: '#75b8fa'},
    {id: 'theta', name: 'θ', title: 'Тета', range: '4–8 Гц', color: '#bc9cfa'},
    {id: 'alpha', name: 'α', title: 'Альфа', range: '8–13 Гц', color: '#65dcc2'},
    {id: 'beta', name: 'β', title: 'Бета', range: '13–30 Гц', color: '#efba77'},
    {id: 'low_gamma', name: 'γ', title: 'Гамма', range: '30–45 Гц', color: '#f397af'},
  ];
  const leadNames = ['O1–T3', 'O2–T4'];
  const powerKeys = ['left_relative_power', 'right_relative_power'];

  function number(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function percent(value) {
    return value === null ? '—' : (value * 100).toFixed(1) + '%';
  }

  // Compatibility with a running server from before spectral_power was added.
  // The existing PSD endpoint supplies real raw data; it never supplies coupling.
  function rawPowerFromSpectrum(spec) {
    const frequencies = spec?.freqs, psds = spec?.psd_per_channel;
    const names = spec?.channels?.map(name => String(name).toUpperCase().replace(/–/g, '-').replace(/ /g, ''));
    if (!Array.isArray(frequencies) || frequencies.length < 3 || !Array.isArray(psds)
        || names?.length !== 2 || !names.includes('O1-T3') || !names.includes('O2-T4')) return null;
    if (frequencies.some((f,i) => number(f) === null || (i > 0 && f <= frequencies[i-1]))
        || frequencies.at(-1) < 45) return null;
    const step = frequencies[1] - frequencies[0], seconds = 1 / step;
    if (!Number.isFinite(seconds) || seconds < 12 - 1e-6
        || frequencies.some((f,i) => Math.abs(f - frequencies[0] - i * step) > step * .001)) return null;
    const integrate = (psd, low, high) => {
      let total = 0;
      for (let i=1;i<frequencies.length;i++) {
        if (frequencies[i-1] >= low && frequencies[i] < high) {
          total += (psd[i-1]+psd[i]) * .5 * (frequencies[i]-frequencies[i-1]);
        }
      }
      return total;
    };
    const result = {available:false,status:'raw',bands:{},channels:[],quality:{window_seconds:seconds,method:'rectangular periodogram'}};
    for (const [index,name] of ['O1-T3','O2-T4'].entries()) {
      const psd=psds[names.indexOf(name)];
      const valid=Array.isArray(psd) && psd.length === frequencies.length && psd.every(v=>number(v)!==null&&v>=0);
      const total=valid ? integrate(psd,1,45) : 0;
      const available=total>1e-18;
      result.channels.push({name:leadNames[index],available,window_seconds:seconds});
      result.available ||= available;
      for (const band of bands) {
        const bounds={delta:[1,4],theta:[4,8],alpha:[8,13],beta:[13,30],low_gamma:[30,45]}[band.id];
        (result.bands[band.id] ||= {})[powerKeys[index]]=available?integrate(psd,...bounds)/total:null;
      }
    }
    return result;
  }

  function powerCell(lead) {
    return `<span class="pair-power"><span class="pair-track"><span class="pair-fill" aria-hidden="true"></span></span>
      <strong data-power="${lead}">—</strong></span>`;
  }

  function markup() {
    return `<div class="pair-workbench" data-pair-panel>
      <div class="pair-controls">
        <label>Частотные полосы <select class="select" data-pair-band>
          <option value="all">Все полосы</option>
          ${bands.map(band => `<option value="${band.id}">${band.name} ${band.title} · ${band.range}</option>`).join('')}
        </select></label>
        <label>Мощность <select class="select" data-pair-source>
          <option value="auto">Доступный спектр</option>
          <option value="screened">После скрининга</option>
          <option value="raw">Исходный сигнал</option>
        </select></label>
        <span class="pair-legend"><span><i class="pair-dot left"></i> ${leadNames[0]}</span><span><i class="pair-dot right"></i> ${leadNames[1]}</span></span>
      </div>
      <div class="pair-body">
        <div class="pair-map-widget">
          <div class="pair-map-heading"><span>СХЕМА МОНТАЖА</span><small>4 электрода · 2 отведения</small></div>
          <div class="pair-map-views" role="group" aria-label="Ракурс схемы">
            <button type="button" data-map-view="oblique" aria-pressed="true">3D</button>
            <button type="button" data-map-view="rear" aria-pressed="false">Сзади</button>
            <button type="button" data-map-view="top" aria-pressed="false">Сверху</button>
            <button type="button" data-map-view="side" aria-pressed="false">Сбоку</button>
            <button type="button" class="pair-map-reset" data-map-reset title="Сбросить ракурс (Home)" aria-label="Сбросить ракурс">↺</button>
          </div>
          <canvas class="pair-canvas" data-pair-map role="img" aria-label="Трёхмерная схема отведений O1–T3 и O2–T4.">Схема электродов O1–T3 и O2–T4. Значения мощности приведены в таблице полос.</canvas>
          <div class="pair-map-key" data-map-key aria-hidden="true"></div>
          <div class="pair-map-footer">
            <p data-map-caption class="chart-caption">Цветные столбики у отведений показывают долю мощности по шкале 0–100%.</p>
            <p class="chart-caption">Перетащите для вращения · стрелки на клавиатуре · Home для сброса</p>
          </div>
        </div>
        <div class="pair-results">
          <div data-pair-status class="pair-status" role="status">Проверяем наличие двух синхронных каналов…</div>
          <div class="pair-spectrum" aria-label="Относительная мощность по полосам">
            <div class="pair-spectrum-heading"><span>Полоса</span><span>${leadNames[0]}</span><span>${leadNames[1]}</span></div>
            ${bands.map(band => `
              <button type="button" class="pair-band-row" data-band-row="${band.id}" aria-pressed="false" style="--pair-band-color:${band.color}">
                <span class="pair-band-name"><b>${band.name}</b><span>${band.title}<small>${band.range}</small></span></span>
                ${powerCell('left')}${powerCell('right')}
              </button>`).join('')}
          </div>
          <div class="pair-details" data-pair-details hidden>
            <div><small>MSC</small><strong data-pair-coh>—</strong></div>
            <div><small>|ImCoh|</small><strong data-pair-imcoh>—</strong></div>
            <div><small>wPLI</small><strong data-pair-wpli>—</strong></div>
            <div><small>Асимметрия ln(O2/O1)</small><strong data-pair-asym>—</strong></div>
          </div>
          <p class="pair-detail-hint" data-pair-connectivity hidden></p>
          <p class="pair-detail-hint" data-pair-hint>Выберите полосу, чтобы увидеть показатели согласованности и асимметрии.</p>
          <div class="pair-quality" data-pair-quality></div>
        </div>
      </div>
      <p class="pair-disclaimer">Схема показывает измеренные биполярные отведения и положения электродов. MSC, |ImCoh| и wPLI описывают согласованность записанных сигналов.</p>
    </div>`;
  }

  function put(panel, selector, text) {
    const element = panel.querySelector(selector);
    if (element.textContent !== text) element.textContent = text;
  }

  function render(panel, data) {
    if (!panel) return;
    if (data !== undefined) panel._pairedData = data;
    const measurement = panel._pairedData || {};
    const selected = panel.querySelector('[data-pair-band]').value;
    const full = selected === 'all';
    const source = panel.querySelector('[data-pair-source]').value;
    const screened = measurement.spectral_power, raw = measurement.raw_spectral_power;
    const spectrum = source === 'raw' ? raw : source === 'screened' ? screened
      : screened?.available ? screened : !screened && measurement.available ? null : raw?.available ? raw : screened;
    const values = spectrum?.available ? spectrum.bands : !spectrum && measurement.available ? measurement.bands : null;
    const powerAvailable = Boolean(values);
    panel.dataset.powerState = powerAvailable ? (spectrum?.status || 'screened') : 'unavailable';
    const series = bands.map(band => ({
      ...band, powers: powerKeys.map(key => number(values?.[band.id]?.[key])),
    }));

    put(panel, '[data-pair-status]', powerAvailable
      ? (spectrum?.status === 'raw' ? 'Исходный спектр · без отбора по качеству, может содержать помехи' : spectrum?.status === 'limited' ? 'Мощность по отобранным окнам · ограниченная оценка' : 'Мощность по отобранным окнам')
      : spectrum?.reason || measurement.reason || 'Ожидаем пригодные окна ЭЭГ.');
    for (const band of series) {
      const row = panel.querySelector(`[data-band-row="${band.id}"]`);
      row.setAttribute('aria-pressed', String(band.id === selected));
      ['left', 'right'].forEach((lead, index) => {
        const label = row.querySelector(`[data-power="${lead}"]`);
        const value = band.powers[index];
        label.textContent = percent(value);
        label.parentElement.style.setProperty('--pair-power', Math.max(0, Math.min(value ?? 0, 1)) * 100 + '%');
      });
    }
    panel.querySelector('[data-pair-details]').hidden = full;
    panel.querySelector('[data-pair-hint]').hidden = !full;
    const detail = measurement.available ? measurement.bands?.[selected] : null;
    for (const [key, selector] of [
      ['coherence_msc', 'coh'], ['imaginary_coherency_magnitude', 'imcoh'],
      ['wpli', 'wpli'], ['relative_log_right_left', 'asym'],
    ]) {
      put(panel, `[data-pair-${selector}]`, number(detail?.[key])?.toFixed(3) ?? '—');
    }
    const quality = measurement.quality || {};
    const reasons = {line_noise: 'помеха около 50 Гц', quality_screen: 'разрывы или фильтр качества',
      flatline: 'постоянный сигнал', quantized: 'мало уровней сигнала', nonfinite: 'нечисловые отсчёты',
      suspected_rail: 'ограничение амплитуды', low_power: 'слишком слабый сигнал'};
    const channelQuality = spectrum?.channels?.map(channel => {
      const rejected = Object.entries(channel.rejected_by || {}).map(([key, count]) => `${reasons[key] || key}: ${count}`).join(', ');
      if (channel.window_seconds) return `${channel.name}: исходный фрагмент ${channel.window_seconds.toFixed(0)} с · без отбора по артефактам`;
      return `${channel.name}: ${channel.accepted_epochs} / ${channel.total_epochs} окон${rejected ? ` · ${spectrum.status === 'raw' ? 'флаги фильтра' : 'исключено'} (${rejected})` : ''}`;
    }).join('\n');
    const pairWarnings = spectrum?.quality?.window_seconds && quality.rejected_by
      ? Object.entries(quality.rejected_by).map(([key,count])=>`${reasons[key] || key}: ${count} парных окон`).join(', ') : '';
    put(panel, '[data-pair-quality]', (channelQuality ? channelQuality + (pairWarnings ? '\nФильтр: ' + pairWarnings : '') : '') || (quality.total_epochs
      ? `Окон без перекрытия · ${quality.accepted_epochs ?? 0} / ${quality.total_epochs} · по ${quality.epoch_seconds || '—'} с`
      : 'Ожидаем достаточное окно записи.'));
    put(panel, '[data-pair-connectivity]', measurement.available ? ''
      : `Связность не рассчитана. ${measurement.reason || 'Нужны ≥8 пригодных парных окон по 4 с.'}`);
    panel.querySelector('[data-pair-connectivity]').hidden = full || measurement.available;

    const shown = full ? series : series.filter(band => band.id === selected);
    const valid = shown.some(band => band.powers.some(value => value !== null));
    put(panel, '[data-map-caption]', valid
      ? 'Цветные столбики у отведений показывают долю мощности по шкале 0–100%.'
      : 'Схема показывает размещение электродов. Доли мощности пока недоступны.');
    const canvas = panel.querySelector('[data-pair-map]');
    const bandSummary = shown.map(band => `${band.name} ${band.range}: ${band.powers.map(percent).join(' и ')}`).join('. ');
    canvas.setAttribute('aria-label', `3D-схема отведений ${leadNames.join(' и ')}. ${valid ? bandSummary : 'Нет пригодной оценки мощности.'} Вращение стрелками, сброс клавишей Home.`);
    const key = panel.querySelector('[data-map-key]');
    key.innerHTML = shown.map(band => `<span><i class="pair-dot" style="background:${band.color}"></i>${band.name} ${band.range}</span>`).join('');
    root.NeuroBrainMap?.mount(canvas, shown);
  }

  function attach(panel, data) {
    if (!panel) return;
    if (!panel._pairedBound) {
      panel._pairedBound = true;
      const select = panel.querySelector('[data-pair-band]');
      const canvas = panel.querySelector('[data-pair-map]');
      for (const button of panel.querySelectorAll('[data-map-view]')) {
        button.addEventListener('click', () => root.NeuroBrainMap?.setView(canvas, button.dataset.mapView));
      }
      panel.querySelector('[data-map-reset]').addEventListener('click', () => root.NeuroBrainMap?.setView(canvas, 'oblique'));
      select.addEventListener('change', () => render(panel));
      panel.querySelector('[data-pair-source]').addEventListener('change', () => render(panel));
      for (const row of panel.querySelectorAll('[data-band-row]')) {
        row.addEventListener('click', () => {
          const band = row.dataset.bandRow;
          select.value = select.value === band ? 'all' : band;
          render(panel);
        });
      }
    }
    render(panel, data);
  }

  root.NeuroPairedPanel = {markup, attach, render, rawPowerFromSpectrum};
})(typeof window !== 'undefined' ? window : globalThis);

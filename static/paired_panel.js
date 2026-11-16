/* Shared live and archive view of the two recorded bipolar EEG leads. */
(function (root) {
  'use strict';

  const bands = [
    {id: 'delta', name: 'δ', range: '1–4 Гц', color: '#75b8fa'},
    {id: 'theta', name: 'θ', range: '4–8 Гц', color: '#bc9cfa'},
    {id: 'alpha', name: 'α', range: '8–13 Гц', color: '#65dcc2'},
    {id: 'beta', name: 'β', range: '13–30 Гц', color: '#efba77'},
    {id: 'low_gamma', name: 'γ', range: '30–45 Гц', color: '#f397af'},
  ];
  const leadNames = ['O1–T3', 'O2–T4'];
  const powerKeys = ['left_relative_power', 'right_relative_power'];

  function number(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function percent(value) {
    return value === null ? '—' : (value * 100).toFixed(1) + '%';
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
          ${bands.map(band => `<option value="${band.id}">${band.name} · ${band.range}</option>`).join('')}
        </select></label>
        <span class="pair-legend"><span><i class="pair-dot left"></i> ${leadNames[0]}</span><span><i class="pair-dot right"></i> ${leadNames[1]}</span></span>
      </div>
      <div class="pair-body">
        <div class="pair-map-widget">
          <canvas class="pair-canvas" data-pair-map role="img" aria-label="Трёхмерная схема отведений O1–T3 и O2–T4."></canvas>
          <div class="pair-map-footer">
            <p data-map-caption class="chart-caption">Длина цветного участка показывает долю мощности по шкале от 0 до 100%.</p>
            <p class="chart-caption">Вращение · перетащите схему или используйте стрелки. Home возвращает вид.</p>
          </div>
        </div>
        <div class="pair-results">
          <div data-pair-status class="pair-status" role="status">Проверяем наличие двух синхронных каналов…</div>
          <div class="pair-spectrum" aria-label="Относительная мощность по полосам">
            <div class="pair-spectrum-heading"><span>Полоса</span><span>${leadNames[0]}</span><span>${leadNames[1]}</span></div>
            ${bands.map(band => `
              <button type="button" class="pair-band-row" data-band-row="${band.id}" aria-pressed="false" style="--pair-band-color:${band.color}">
                <span class="pair-band-name"><i class="pair-dot"></i><b>${band.name}</b><small>${band.range}</small></span>
                ${powerCell('left')}${powerCell('right')}
              </button>`).join('')}
          </div>
          <div class="pair-details" data-pair-details hidden>
            <div><small>MSC</small><strong data-pair-coh>—</strong></div>
            <div><small>|ImCoh|</small><strong data-pair-imcoh>—</strong></div>
            <div><small>wPLI</small><strong data-pair-wpli>—</strong></div>
            <div><small>Асимметрия ln(O2/O1)</small><strong data-pair-asym>—</strong></div>
          </div>
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
    const values = measurement.available ? measurement.bands : null;
    const series = bands.map(band => ({
      ...band, powers: powerKeys.map(key => number(values?.[band.id]?.[key])),
    }));

    put(panel, '[data-pair-status]', measurement.available ? 'Исследовательская оценка двух отведений'
      : measurement.reason || 'Парные метрики недоступны: ожидаем качественный сигнал двух каналов.');
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
    const detail = values?.[selected];
    for (const [key, selector] of [
      ['coherence_msc', 'coh'], ['imaginary_coherency_magnitude', 'imcoh'],
      ['wpli', 'wpli'], ['relative_log_right_left', 'asym'],
    ]) {
      put(panel, `[data-pair-${selector}]`, number(detail?.[key])?.toFixed(3) ?? '—');
    }
    const quality = measurement.quality || {};
    put(panel, '[data-pair-quality]', quality.total_epochs
      ? `Окон без перекрытия · ${quality.accepted_epochs ?? 0} / ${quality.total_epochs} · по ${quality.epoch_seconds || '—'} с`
      : 'Недостаточно парных окон для проверки качества.');

    const shown = full ? series : series.filter(band => band.id === selected);
    const valid = shown.some(band => band.powers.some(value => value !== null));
    put(panel, '[data-map-caption]', valid
      ? 'Длина цветного участка показывает долю мощности по шкале от 0 до 100%.'
      : 'Схема показывает размещение электродов. Доли мощности пока недоступны.');
    const canvas = panel.querySelector('[data-pair-map]');
    const bandSummary = shown.map(band => `${band.name} ${band.range}: ${band.powers.map(percent).join(' и ')}`).join('. ');
    canvas.setAttribute('aria-label', `3D-схема отведений ${leadNames.join(' и ')}. ${valid ? bandSummary : 'Нет пригодной оценки мощности.'} Вращение стрелками, сброс клавишей Home.`);
    root.NeuroBrainMap?.mount(canvas, shown);
  }

  function attach(panel, data) {
    if (!panel) return;
    if (!panel._pairedBound) {
      panel._pairedBound = true;
      const select = panel.querySelector('[data-pair-band]');
      select.addEventListener('change', () => render(panel));
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

  root.NeuroPairedPanel = {markup, attach, render};
})(typeof window !== 'undefined' ? window : globalThis);

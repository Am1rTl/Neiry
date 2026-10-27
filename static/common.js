/* NEIRY Capsule — общий JS всех страниц.
 * WS-подключение с экспоненциальным бэкоффом, оверлей калибровки +
 * кольцо таймера, i18n RU/EN, рендер sensor-status, stale-индикатор,
 * дефолты Chart.js. Протокол бэкенда не меняется: читаем поля тика как есть.
 * Глобальный неймспейс: Capsule. */
(function (global) {
  'use strict';

  var Capsule = global.Capsule || {};
  global.Capsule = Capsule;

  /* --- CDN-фолбэк для вендорных скриптов ---
   * Вызывается из onerror во время парсинга: document.write синхронно
   * подгружает CDN-копию, порядок скриптов сохраняется. */
  Capsule.vendorFallback = function (cdnUrl) {
    document.write('<script src="' + cdnUrl + '"><\/script>');
  };

  /* --- Дефолты Chart.js + неоновый градиент заливки --- */
  Capsule.GRID_COLOR = 'rgba(26, 37, 80, 0.55)';
  Capsule.TICK_COLOR = '#3a4a7a';

  Capsule.setupCharts = function () {
    if (typeof Chart === 'undefined') return false;
    Chart.defaults.color = '#6f7bb0';
    Chart.defaults.borderColor = 'rgba(26, 37, 80, 0.6)';
    Chart.defaults.font.family = "'JetBrains Mono', 'Fira Code', 'Courier New', monospace";
    Chart.defaults.font.size = 10;
    return true;
  };

  Capsule.neonGradient = function (_ctx, area, hex) {
    if (!area || typeof area.top !== 'number' || typeof area.bottom !== 'number') return hex + '40';
    try {
      var g = _ctx.createLinearGradient(0, area.top, 0, area.bottom);
      g.addColorStop(0, hex + 'cc');
      g.addColorStop(1, hex + '00');
      return g;
    } catch (e) { return hex + '40'; }
  };

  /* --- WebSocket с экспоненциальным бэкоффом ---
   * connectWS(path, { onTick, onStatus, onOpen })
   * Возвращает handle { send(obj), close() }. */
  Capsule.connectWS = function (path, handlers) {
    handlers = handlers || {};
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var url = proto + '://' + location.host + (path || '/ws');
    var ws = null;
    var attempt = 0;
    var closed = false;
    var BASE_MS = 1000;
    var MAX_MS = 30000;

    function delay() {
      // 1s, 2s, 4s, … до 30s + небольшой джиттер против грозы переподключений
      var d = Math.min(MAX_MS, BASE_MS * Math.pow(2, attempt));
      return d + Math.floor(Math.random() * 500);
    }

    function connect() {
      if (closed) return;
      try {
        ws = new WebSocket(url);
      } catch (e) {
        schedule();
        return;
      }
      if (handlers.onStatus) handlers.onStatus('connecting');
      if (handlers.onOpen) { var _o = handlers.onOpen; ws.addEventListener('open', function () { attempt = 0; _o(); }); }
      else { ws.addEventListener('open', function () { attempt = 0; }); }
      ws.addEventListener('message', function (ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        if (d && d.status && handlers.onStatus) handlers.onStatus(d.status);
        if (handlers.onTick) handlers.onTick(d);
      });
      ws.addEventListener('close', function () {
        if (handlers.onStatus) handlers.onStatus('error');
        schedule();
      });
      ws.addEventListener('error', function () { try { ws.close(); } catch (e) {} });
    }

    function schedule() {
      if (closed) return;
      var ms = delay();
      attempt += 1;
      setTimeout(connect, ms);
    }

    connect();
    return {
      send: function (obj) {
        if (ws && ws.readyState === 1) {
          try { ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
        }
        return false;
      },
      close: function () { closed = true; try { ws.close(); } catch (e) {} },
    };
  };

  /* --- Оверлей калибровки + кольцо таймера ---
   * Ожидает стандартную разметку: #setup-overlay > .setup-card[data-state],
   * #timer-ring / #timer-text, #setup-searching-msg, #cal-fail-msg, #cal-result-line. */
  Capsule.CAL_DURATION = 30;
  Capsule.RING_CIRC = 2 * Math.PI * 78;

  Capsule.overlay = function () {
    var overlay = document.getElementById('setup-overlay');
    var calStartedAt = 0;
    var calInterval = null;

    function showState(name) {
      if (!overlay) return;
      overlay.className = 'show setup-state-' + name;
      var cards = overlay.querySelectorAll('[data-state]');
      for (var i = 0; i < cards.length; i++) {
        cards[i].style.display = (cards[i].dataset.state === name) ? '' : 'none';
      }
    }
    function hide() { if (overlay) overlay.classList.remove('show'); }

    function tickTimer() {
      var remaining = Math.max(0, Capsule.CAL_DURATION - ((Date.now() / 1000) - calStartedAt));
      var ring = document.getElementById('timer-ring');
      var text = document.getElementById('timer-text');
      if (!ring || !text) return;
      ring.setAttribute('stroke-dashoffset', (1 - (remaining / Capsule.CAL_DURATION)) * Capsule.RING_CIRC);
      text.textContent = Math.ceil(remaining);
      if (remaining < 5) ring.classList.add('warn'); else ring.classList.remove('warn');
      if (remaining <= 0 && calInterval) { clearInterval(calInterval); calInterval = null; }
    }
    function startTimer(f) {
      calStartedAt = f || (Date.now() / 1000);
      if (calInterval) clearInterval(calInterval);
      tickTimer();
      calInterval = setInterval(tickTimer, 100);
    }
    function stopTimer() { if (calInterval) { clearInterval(calInterval); calInterval = null; } }

    // Единое решение «какое состояние показать» по тику.
    // Возвращает имя состояния; hideOnComplete=true (index) — прятать оверлей
    // при complete/skipped, иначе (charts) — показать карточку complete.
    function stateForTick(d) {
      var phase = d.calibration_phase || 'idle';
      var connected = (d.status === 'connected' || d.status === 'streaming' || d.device);
      if (phase === 'complete' || phase === 'skipped') return 'complete';
      if (phase === 'running') return 'running';
      if (connected && (phase === 'ready' || phase === 'idle')) return 'ready';
      if (phase === 'failed') return 'failed';
      return 'searching';
    }

    return {
      showState: showState, hide: hide,
      startTimer: startTimer, stopTimer: stopTimer,
      stateForTick: stateForTick,
    };
  };

  /* --- Рендер sensor-status в .sensor[data-sensor] ---
   * t — функция перевода из i18n (ключи sens*). */
  Capsule.renderSensorStatus = function (s, t) {
    if (!s) return;
    function setEl(key, name, value) {
      var el = document.querySelector('.sensor[data-sensor="' + key + '"]');
      if (!el) return;
      el.className = 'sensor state-' + ((s[key] && s[key].state) || 'none');
      var n = el.querySelector('.name');
      var v = el.querySelector('.value');
      if (n) n.textContent = name;
      if (v) v.textContent = value;
    }
    var eegVal = (s.eeg && s.eeg.state === 'ok')
      ? (s.eeg.sr || '?') + ' Hz · ' + ((s.eeg.channels || []).join(' · '))
      : t('sensNoData');
    setEl('eeg', t('sensEeg'), eegVal);

    var skinVal = '—';
    if (s.skin) {
      if (s.skin.state === 'ok') skinVal = t('sensGood')((s.skin.max_r / 1000).toFixed(1));
      else if (s.skin.state === 'warn') skinVal = s.skin.n_good + '/' + s.skin.n_ch + ' ok';
      else if (s.skin.state === 'error') skinVal = t('sensContact');
    }
    setEl('skin', t('sensSkin'), skinVal);
    setEl('ppg', t('sensPpg'), (s.ppg && s.ppg.state === 'ok') ? (s.ppg.sr || '?') + ' Hz' : t('sensNoData'));
    setEl('cardio', t('sensHeart'), (s.cardio && s.cardio.state === 'ok') ? s.cardio.hr.toFixed(0) + ' bpm' : '—');
    setEl('mems', t('sensMotion'), (s.mems && s.mems.state === 'ok') ? (s.mems.sr || '?') + ' Hz' : t('sensNoData'));
    setEl('nfb', t('sensNfb'), (s.nfb && s.nfb.state === 'ok')
      ? t('sensPts')(s.nfb.n)
      : (s.nfb && s.nfb.state === 'warn' ? t('sensAfterCal') : '—'));
  };

  /* --- Индикатор свежести данных ---
   * updateStale(tServer): возраст = now - t_server.
   * <3с — LIVE, <10с — STALE, дальше — DEAD. Тикает раз в секунду. */
  Capsule.stale = function (elId, t) {
    var el = document.getElementById(elId || 'chip-stale');
    var label = el ? el.querySelector('.stale-val') : null;
    var lastServerT = 0;
    function render() {
      if (!el || !label) return;
      if (!lastServerT) { label.textContent = '—'; el.className = 'chip'; return; }
      var age = Date.now() / 1000 - lastServerT;
      if (age < 3) {
        el.className = 'chip';
        label.textContent = t('staleLive');
      } else if (age < 10) {
        el.className = 'chip stale';
        label.textContent = t('staleStale')(age.toFixed(0));
      } else {
        el.className = 'chip dead';
        label.textContent = t('staleDead')(age.toFixed(0));
      }
    }
    setInterval(render, 1000);
    return {
      update: function (tServer) {
        if (typeof tServer === 'number' && tServer > 0) lastServerT = tServer;
        render();
      },
    };
  };

  /* --- i18n: общий словарь RU/EN + переключатель ---
   * createLang(pageDict): pageDict.{en,ru} дополняют/перекрывают базу.
   * Статика — через [data-i18n] / [data-i18n-html], динамика — через t(). */
  Capsule.LANG_KEY = 'capsule_lang';

  Capsule.BASE_STR = {
    en: {
      status_idle: 'idle', status_connecting: 'connecting', status_connected: 'connected',
      status_disconnected: 'disconnected', status_searching: 'searching', status_error: 'error',
      status_cached: 'cached', status_streaming: 'streaming', status_lost: 'lost',
      navDashboard: '◈ DASHBOARD', navCharts: '📈 FULL CHARTS →', navMore: ' MORE ', navSessions: '🗂 SESSIONS',
      staleLive: 'LIVE', staleStale: function (s) { return 'STALE ' + s + 's'; }, staleDead: function (s) { return 'NO DATA ' + s + 's'; },
      lastKnown: 'last known data',
      sensNoData: 'no data', sensGood: function (v) { return 'good · ' + v + ' kΩ'; },
      sensContact: 'no contact', sensPts: function (n) { return n + ' pts'; }, sensAfterCal: 'after cal',
      sensEeg: 'EEG', sensSkin: 'SKIN', sensPpg: 'PPG', sensHeart: 'HEART', sensMotion: 'MOTION', sensNfb: 'NFB',
      ovSearching: 'Searching for the headband…', ovPairing: 'put it in pairing mode',
      ovCalTitle: '⚙ Calibration',
      ovCalTextHtml: 'For best NFB results, do a quick <b style="color:var(--neon-cyan)">30-second eyes-closed</b> calibration.<br> Sit still, relax, and close your eyes once you press <b>Start</b>.',
      ovStart: '▶ START CALIBRATION', ovSkip: 'SKIP', ovHint: 'You can recalibrate later from the main view',
      ovCloseEyes: '👁 Close your eyes', ovStill: "Sit still · relax · don't blink too hard", ovMeasuring: 'measuring your individual α-rhythm…',
      ovCalibrated: '✓ CALIBRATED', ovResultHtml: 'individual α-frequency: <b style="color:var(--neon-cyan)">— Hz</b>', ovEntering: 'Entering dashboard…',
      ovFailed: '✗ CALIBRATION FAILED', ovFailedMsg: 'Could not measure individual α-rhythm.', ovRetry: '↻ RETRY',
    },
    ru: {
      status_idle: 'ожидание', status_connecting: 'подключение', status_connected: 'подключено',
      status_disconnected: 'отключено', status_searching: 'поиск', status_error: 'ошибка',
      status_cached: 'кэш', status_streaming: 'поток', status_lost: 'потеряно',
      navDashboard: '◈ ДАШБОРД', navCharts: '📈 ГРАФИКИ →', navMore: ' ЕЩЁ ', navSessions: '🗂 СЕССИИ',
      staleLive: 'LIVE', staleStale: function (s) { return 'STALE ' + s + ' c'; }, staleDead: function (s) { return 'НЕТ ДАННЫХ ' + s + ' c'; },
      lastKnown: 'последние известные данные',
      sensNoData: 'нет данных', sensGood: function (v) { return 'норма · ' + v + ' кОм'; },
      sensContact: 'нет контакта', sensPts: function (n) { return 'точек: ' + n; }, sensAfterCal: 'после калиб.',
      sensEeg: 'ЭЭГ', sensSkin: 'КОЖА', sensPpg: 'PPG', sensHeart: 'ПУЛЬС', sensMotion: 'ДВИЖЕНИЕ', sensNfb: 'NFB',
      ovSearching: 'Поиск повязки…', ovPairing: 'включите режим сопряжения',
      ovCalTitle: '⚙ Калибровка',
      ovCalTextHtml: 'Для точного NFB пройдите быструю <b style="color:var(--neon-cyan)">30-секундную калибровку с закрытыми глазами</b>.<br> Сидите спокойно, расслабьтесь и закройте глаза после нажатия <b>Старт</b>.',
      ovStart: '▶ НАЧАТЬ КАЛИБРОВКУ', ovSkip: 'ПРОПУСТИТЬ', ovHint: 'Позже можно перекалиброваться с главного экрана',
      ovCloseEyes: '👁 Закройте глаза', ovStill: 'Сидите спокойно · расслабьтесь · не моргайте сильно', ovMeasuring: 'измерение индивидуального α-ритма…',
      ovCalibrated: '✓ КАЛИБРОВКА ГОТОВА', ovResultHtml: 'индивидуальная α-частота: <b style="color:var(--neon-cyan)">— Гц</b>', ovEntering: 'Открываю дашборд…',
      ovFailed: '✗ КАЛИБРОВКА НЕ УДАЛАСЬ', ovFailedMsg: 'Не удалось измерить α-ритм.', ovRetry: '↻ ПОВТОРИТЬ',
    },
  };

  Capsule.createLang = function (pageDict) {
    pageDict = pageDict || {};
    var STR = { en: {}, ru: {} };
    ['en', 'ru'].forEach(function (lang) {
      var k;
      for (k in Capsule.BASE_STR[lang]) STR[lang][k] = Capsule.BASE_STR[lang][k];
      var page = pageDict[lang] || {};
      for (k in page) STR[lang][k] = page[k];
    });
    var current = 'ru';
    try {
      var saved = null;
      if (typeof localStorage !== 'undefined') saved = localStorage.getItem(Capsule.LANG_KEY);
      if (saved === 'en' || saved === 'ru') current = saved;
    } catch (e) {}

    function t(key) { return STR[current][key] !== undefined ? STR[current][key] : STR.en[key]; }
    function apply() {
      document.documentElement.lang = current;
      var els = document.querySelectorAll('[data-i18n]');
      for (var i = 0; i < els.length; i++) {
        var v = t(els[i].dataset.i18n);
        if (typeof v === 'string') els[i].textContent = v;
      }
      var htmls = document.querySelectorAll('[data-i18n-html]');
      for (var j = 0; j < htmls.length; j++) {
        var h = t(htmls[j].dataset.i18nHtml);
        if (typeof h === 'string') htmls[j].innerHTML = h;
      }
      var btn = document.getElementById('lang-toggle');
      if (btn) btn.textContent = current === 'ru' ? 'RU' : 'EN';
    }
    // onSwitch — перерисовать динамические строки (последний тик и т.п.)
    function bindToggle(onSwitch) {
      var btn = document.getElementById('lang-toggle');
      if (!btn) return;
      btn.addEventListener('click', function () {
        current = (current === 'ru') ? 'en' : 'ru';
        try { localStorage.setItem(Capsule.LANG_KEY, current); } catch (e) {}
        apply();
        if (onSwitch) onSwitch();
      });
    }
    return {
      t: t, apply: apply, bindToggle: bindToggle,
      get: function () { return current; },
    };
  };
})(window);

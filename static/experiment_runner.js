/* Runs one protocol in the foreground. The server owns the recording and event log. */
(() => {
  'use strict';

  const TITLES = {
    alpha: 'Альфа-реактивность',
    visual: 'Зрительное обучение',
    feedback: 'Нейрообратная связь',
  };
  const ABORTED = Symbol('aborted');
  const REFERENCE_MS = 360;
  const INTERVAL_MS = 260;
  const STIMULUS_MS = 250;
  const RESPONSE_MS = 1800;
  const MIN_RT_MS = 100;
  const BASE = '/api/experiment-lab/runs';
  const stamp = () => performance.now();

  function shuffle(items) {
    for (let index = items.length - 1; index > 0; index--) {
      const other = Math.floor(Math.random() * (index + 1));
      [items[index], items[other]] = [items[other], items[index]];
    }
    return items;
  }

  function trialPlan(count, training, trainedOrientation) {
    const levels = count <= 16 ? [1, 4, 12] : [1, 2, 4, 8, 12];
    const conditions = training ? ['trained'] : ['trained', 'control'];
    const plan = [];
    while (plan.length < count) {
      const cycle = shuffle(levels.flatMap(angle => [-1, 1].flatMap(sign =>
        conditions.map(condition => ({
          condition,
          angle_deg: angle,
          signed_offset_deg: angle * sign,
          base_orientation_deg: condition === 'trained' ? trainedOrientation : (trainedOrientation + 90) % 180,
        }))
      )));
      plan.push(...cycle.slice(0, count - plan.length));
    }
    return plan;
  }

  function grating(context, angle, size) {
    const image = context.createImageData(size, size);
    const rad = angle * Math.PI / 180;
    const frequency = 2 * Math.PI * 4.5 / size;
    const c = Math.cos(rad) * frequency;
    const s = Math.sin(rad) * frequency;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const dx = (x + .5) - size / 2;
        const dy = (y + .5) - size / 2;
        const distance = Math.hypot(dx, dy) / (size / 2);
        const envelope = Math.exp(-4 * distance ** 2) * Math.max(0, Math.min(1, (1 - distance) * 8));
        const shade = Math.round(128 + 57 * envelope * Math.cos(dx * c + dy * s));
        const offset = (y * size + x) * 4;
        image.data[offset] = image.data[offset + 1] = image.data[offset + 2] = shade;
        image.data[offset + 3] = 255;
      }
    }
    return image;
  }

  class ExperimentRunner {
    constructor({root, request, run, onFinish, onError, clockOffset = 0, sound = true}) {
      this.root = root;
      this.request = request;
      this.run = run;
      this.onFinish = onFinish;
      this.onError = onError;
      this.clockOffset = Number.isFinite(clockOffset) ? clockOffset : 0;
      this.sound = sound;
      this.path = `${BASE}/${encodeURIComponent(run.id)}`;
      this.controller = new AbortController();
      this.pending = [];
      this.sequence = 0;
      this.displayed = null;
      this.lastStatus = '';
    }

    number(name, fallback, min, max) {
      const value = this.run.settings?.[name];
      return Number.isFinite(value) && value >= min && value <= max ? value : fallback;
    }

    eventTime(now = stamp()) {
      return performance.timeOrigin + now + this.clockOffset;
    }

    event(type, fields = {}, now = stamp()) {
      this.pending.push({event_id: `${this.run.id}-${++this.sequence}`, type, at_ms: this.eventTime(now), ...fields});
    }

    start() {
      if (this.started || this.controller.signal.aborted) return;
      this.started = true;
      this.mount();
      const signal = this.controller.signal;
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) void this.stop('tab_hidden');
      }, {signal});
      window.addEventListener('pagehide', () => { void this.stop('page_closed'); }, {signal});
      this.flushTimer = setInterval(() => {
        this.flush().catch(() => this.checkRun());
      }, 1200);
      this.heartbeatTimer = setInterval(() => this.event('heartbeat'), 15000);

      this.execute().then(() => {
        if (!signal.aborted) void this.finish('completed');
      }).catch(error => {
        if (error === ABORTED || signal.aborted) return;
        this.onError?.(error);
        void this.stop('runtime_error');
      });
    }

    mount() {
      this.root.innerHTML = `
        <section class="exp-runner card" aria-labelledby="exp-run-heading">
          <header class="exp-run-header">
            <div>
              <div class="exp-run-mode" data-demo="${this.run.mode === 'demo'}"><i></i>${this.run.mode === 'demo' ? 'Демонстрация' : 'Запись ЭЭГ'}</div>
              <h2 id="exp-run-heading" tabindex="-1">${TITLES[this.run.kind]}</h2>
            </div>
            <button type="button" class="button subtle exp-run-stop">Остановить</button>
          </header>
          <div class="exp-run-track">
            <progress aria-label="Прогресс эксперимента" max="1" value="0"></progress>
            <span class="exp-run-count"></span>
          </div>
          <div class="exp-run-content"></div>
          <footer class="exp-run-status" aria-live="polite"></footer>
        </section>`;
      const get = selector => this.root.querySelector(selector);
      this.heading = get('h2');
      this.stopButton = get('.exp-run-stop');
      this.progress = get('progress');
      this.count = get('.exp-run-count');
      this.body = get('.exp-run-content');
      this.statusNode = get('.exp-run-status');
      this.stopButton.addEventListener('click', () => void this.stop());
      this.heading.focus({preventScroll: true});
    }

    status(message, format = '') {
      if (this.lastStatus === message && this.statusNode?.dataset.format === format) return;
      this.lastStatus = message;
      if (this.statusNode) {
        this.statusNode.textContent = message;
        this.statusNode.dataset.format = format;
      }
    }

    post(suffix, body) {
      return this.request(this.path + suffix, {
        method: 'POST', keepalive: true, body: JSON.stringify(body),
      });
    }

    flush() {
      if (this.flushing) return this.flushing;
      if (!this.pending.length) return Promise.resolve();
      const batch = this.pending.slice(0, 40);
      this.flushing = this.post('/events', {events: batch})
        .then(() => { this.pending.splice(0, batch.length); })
        .finally(() => { this.flushing = null; });
      return this.flushing;
    }

    async drain() {
      while (this.pending.length) await this.flush();
    }

    async checkRun() {
      if (this.checkingRun || this.controller.signal.aborted) return;
      this.status('События ожидают сохранения', 'warn');
      this.checkingRun = true;
      try {
        const run = await this.request(this.path);
        if (run.status !== 'running' && !this.controller.signal.aborted) {
          this.release();
          this.finished = run;
          this.onFinish?.({...run, local_unsaved_events: this.pending.filter(event => event.type !== 'heartbeat').length});
        }
      } catch (_) {
        // Keep the pending events while the server is temporarily unreachable.
      } finally {
        this.checkingRun = false;
      }
    }

    openPhase(label, index, duration_s, details = {}) {
      this.phase = {label, index};
      this.event('phase_start', {...this.phase, ...(duration_s ? {duration_s} : {}), ...details});
    }

    closePhase(reason = '') {
      if (!this.phase) return;
      this.event('phase_end', {...this.phase, ...(reason ? {reason} : {})});
      this.phase = null;
    }

    wait(duration) {
      if (this.controller.signal.aborted) return Promise.reject(ABORTED);
      const signal = this.controller.signal;
      return new Promise((resolve, reject) => {
        const stop = () => { clearTimeout(timer); reject(ABORTED); };
        const timer = setTimeout(() => {
          signal.removeEventListener('abort', stop);
          resolve();
        }, duration);
        signal.addEventListener('abort', stop, {once: true});
      });
    }

    paint(draw) {
      if (this.controller.signal.aborted) return Promise.reject(ABORTED);
      const signal = this.controller.signal;
      return new Promise((resolve, reject) => {
        const stop = () => { cancelAnimationFrame(frame); reject(ABORTED); };
        const frame = requestAnimationFrame(() => {
          signal.removeEventListener('abort', stop);
          try {
            draw();
            resolve(stamp());
          } catch (error) {
            reject(error);
          }
        });
        signal.addEventListener('abort', stop, {once: true});
      });
    }

    async countdown(seconds, update) {
      const end = stamp() + seconds * 1000;
      for (;;) {
        const remaining = Math.max(0, end - stamp());
        update(Math.ceil(remaining / 1000), (seconds * 1000 - remaining) / (seconds * 1000));
        if (remaining <= 0) return;
        await this.wait(Math.min(remaining, 100));
      }
    }

    setProgress(ratio, label) {
      this.progress.value = Math.min(1, Math.max(0, ratio));
      if (this.count.textContent !== label) this.count.textContent = label;
    }

    async prep(message) {
      this.body.innerHTML = `<div class="exp-run-center exp-run-prep">
        <p>${message}</p><output class="exp-run-timer">3</output>
      </div>`;
      const display = this.body.querySelector('output');
      await this.countdown(3, remaining => { display.textContent = remaining; });
    }

    static primeSound() {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return null;
      try {
        const context = new Audio();
        context.resume().catch(() => {});
        return context;
      } catch (_) {
        return null;
      }
    }

    cue(type) {
      const audio = this.audio;
      if (!audio || audio.state !== 'running') return;
      const notes = type === 'open' ? [660, 850] : type === 'closed' ? [510] : [510, 660, 850];
      const start = audio.currentTime + .02;
      notes.forEach((frequency, index) => {
        const at = start + index * .18;
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.frequency.value = frequency;
        gain.gain.setValueAtTime(.0001, at);
        gain.gain.exponentialRampToValueAtTime(.08, at + .025);
        gain.gain.exponentialRampToValueAtTime(.0001, at + .15);
        osc.connect(gain).connect(audio.destination);
        osc.start(at);
        osc.stop(at + .16);
      });
    }

    async execute() {
      if (document.hidden) { await this.stop('tab_hidden'); return; }
      if (this.run.kind === 'alpha') await this.alphaTask();
      if (this.run.kind === 'visual') await this.visualTask();
      if (this.run.kind === 'feedback') await this.feedbackTask();
    }

    async alphaTask() {
      const demo = this.run.mode === 'demo';
      const seconds = this.number('block_s', demo ? 4 : 60, 2, 600);
      const repeats = Math.round(this.number('repeats', demo ? 2 : 3, 1, 8));
      this.audio = this.sound && typeof this.sound.createOscillator === 'function' ? this.sound
        : this.sound ? ExperimentRunner.primeSound() : null;
      this.status(this.audio ? 'Два тона — открыть глаза. Один тон — закрыть.' : 'Переходы между этапами показаны на экране.');
      await this.prep('Устройтесь удобно и смотрите перед собой');
      if (this.audio && this.audio.state !== 'running') {
        this.status('Браузер заблокировал звук. Переходы между этапами показаны на экране.', 'warn');
      }
      this.body.innerHTML = `<div class="exp-run-center exp-alpha">
        <div class="exp-alpha-icon" aria-hidden="true">
          <svg viewBox="0 0 56 44"><path class="exp-eye-open" d="M5 22s8-12 23-12 23 12 23 12-8 12-23 12S5 22 5 22Z M35 22a7 7 0 1 1-14 0 7 7 0 1 1 14 0"/><path class="exp-eye-closed" d="M5 21s9 8 23 8 23-8 23-8M16 28l-3 5m15-4v6m12-7 3 5"/></svg>
        </div>
        <h3 aria-live="polite"></h3><output class="exp-run-timer"></output>
      </div>`;
      const panel = this.body.querySelector('.exp-alpha');
      const heading = panel.querySelector('h3');
      const timer = panel.querySelector('output');
      const total = 2 * repeats;
      for (let index = 0; index < total; index++) {
        const closed = index % 2 === 1;
        this.openPhase(closed ? 'eyes_closed' : 'eyes_open', index, seconds);
        panel.dataset.closed = String(closed);
        heading.textContent = closed ? 'Закройте глаза' : 'Откройте глаза';
        this.cue(closed ? 'closed' : 'open');
        await this.countdown(seconds, (remaining, progress) => {
          if (timer.textContent !== String(remaining)) timer.textContent = remaining;
          this.setProgress((index + progress) / total, `Блок ${index + 1} из ${total}`);
        });
        this.closePhase();
      }
      this.cue('complete');
    }

    visualScreen() {
      this.body.innerHTML = `
        <div class="exp-run-visual">
          <div class="exp-v-prompt" aria-live="polite">Смотрите в центр</div>
          <canvas width="240" height="240" role="img" aria-label="Площадка зрительной задачи">На этой площадке показываются наклонные решётки.</canvas>
          <div class="exp-v-answer">
            <button type="button" data-response="left"><kbd>←</kbd> Против часовой</button>
            <button type="button" data-response="right"><kbd>→</kbd> По часовой</button>
          </div>
          <button type="button" class="button text exp-v-skip">Пропустить попытку</button>
        </div>`;
      const signal = this.controller.signal;
      this.canvas = this.body.querySelector('canvas');
      this.canvasContext = this.canvas.getContext('2d', {alpha: false});
      if (!this.canvasContext) throw Error('Для зрительного теста требуется Canvas 2D.');
      this.prompt = this.body.querySelector('.exp-v-prompt');
      this.answers = Array.from(this.body.querySelectorAll('[data-response]'));
      this.skipButton = this.body.querySelector('.exp-v-skip');
      this.answers.forEach(button => {
        button.addEventListener('click', event => this.respond(button.dataset.response, 'button', event.timeStamp), {signal});
      });
      this.skipButton.addEventListener('click', event => this.respond('skip', 'button', event.timeStamp), {signal});
      window.addEventListener('keydown', event => {
        if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || !this.trial) return;
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        this.respond(event.key === 'ArrowLeft' ? 'left' : 'right', 'keyboard', event.timeStamp);
      }, {signal});
      this.fixation();
    }

    fixation() {
      const context = this.canvasContext;
      const middle = this.canvas.width / 2;
      context.fillStyle = '#808080';
      context.fillRect(0, 0, middle * 2, middle * 2);
      context.fillStyle = '#242727';
      context.fillRect(middle - 7, middle - 1, 15, 3);
      context.fillRect(middle - 1, middle - 7, 3, 15);
    }

    drawGrating(angle) {
      if (!this.gratings.has(angle)) {
        this.gratings.set(angle, grating(this.canvasContext, angle, this.canvas.width));
      }
      this.canvasContext.putImageData(this.gratings.get(angle), 0, 0);
    }

    respond(response, source, eventAt) {
      const trial = this.trial;
      if (!trial || trial.response) return;
      const received = stamp();
      const now = eventAt > 0 && eventAt <= received ? eventAt : received;
      trial.response = response;
      trial.responseSource = source;
      trial.responseTime = now;
      trial.reason = response === 'skip' ? 'skipped'
        : trial.onset === undefined || now - trial.onset < MIN_RT_MS ? 'early_response'
        : now - trial.onset > RESPONSE_MS ? 'timeout' : '';
      trial.onResponse?.();
    }

    recordTrial(trial, forcedReason = '') {
      const reason = forcedReason || trial.reason || (!trial.response ? 'timeout' : '');
      this.event('trial', {
        ...trial.parameters,
        index: trial.index,
        correct: reason ? null : trial.response === (trial.parameters.signed_offset_deg > 0 ? 'right' : 'left'),
        rt_ms: trial.onset === undefined || trial.responseTime === undefined || trial.responseTime < trial.onset
          ? null : trial.responseTime - trial.onset,
        valid: !reason,
        response: trial.response || null,
        response_source: trial.responseSource || null,
        reason: reason || null,
        onset_ms: trial.onset === undefined ? null : this.eventTime(trial.onset),
        response_at_ms: trial.responseTime === undefined ? null : this.eventTime(trial.responseTime),
        stimulus_ms: trial.offset === undefined ? null : trial.offset - trial.onset,
        reference_onset_ms: trial.reference === undefined ? null : this.eventTime(trial.reference),
        reference_ms: trial.referenceOffset === undefined ? null : trial.referenceOffset - trial.reference,
      }, trial.responseTime ?? stamp());
    }

    async awaitResponse(trial) {
      if (trial.response) return;
      const duration = Math.max(0, trial.onset + RESPONSE_MS - stamp());
      const signal = this.controller.signal;
      await new Promise((resolve, reject) => {
        const abort = () => {
          clearTimeout(timeout);
          reject(ABORTED);
        };
        const done = () => {
          signal.removeEventListener('abort', abort);
          clearTimeout(timeout);
          resolve();
        };
        const timeout = setTimeout(done, duration);
        trial.onResponse = done;
        signal.addEventListener('abort', abort, {once: true});
      });
    }

    async visualTask() {
      const training = this.run.stage === 'training';
      const demo = this.run.mode === 'demo';
      const count = Math.round(this.number('trial_count', demo ? 12 : 80, 4, 500));
      const orientation = this.number('trained_orientation', 45, 0, 179);
      const tasks = trialPlan(count, training, orientation);
      const stimulus = this.number('stimulus_ms', STIMULUS_MS, 100, 500);
      this.gratings = new Map();
      this.status('Сравните две решётки. После второй ответьте стрелкой или нажмите кнопку.');
      await this.prep('В какую сторону повернулась вторая решётка?');
      this.visualScreen();
      for (const item of tasks) {
        this.drawGrating(item.base_orientation_deg);
        this.drawGrating(item.base_orientation_deg + item.signed_offset_deg);
      }
      this.fixation();
      this.openPhase('visual_task', 0, undefined, {
        stimulus_css_px: this.canvas.getBoundingClientRect().width,
        device_pixel_ratio: window.devicePixelRatio,
        planned_reference_ms: REFERENCE_MS,
        planned_stimulus_ms: stimulus,
        response_timeout_ms: RESPONSE_MS,
      });
      for (let index = 0; index < count; index++) {
        this.setProgress(index / count, `Попытка ${index + 1} из ${count}`);
        await this.visualTrial(tasks[index], index, training, stimulus);
      }
      this.setProgress(1, `Попытка ${count} из ${count}`);
      this.closePhase();
    }

    async visualTrial(parameters, index, training, stimulus) {
      const trial = {parameters, index};
      this.trial = trial;
      this.prompt.textContent = 'Смотрите в центр';
      this.answers.forEach(answer => answer.dataset.ready = 'false');
      await this.wait(290);
      trial.reference = await this.paint(() => this.drawGrating(parameters.base_orientation_deg));
      await this.wait(REFERENCE_MS);
      trial.referenceOffset = await this.paint(() => this.fixation());
      await this.wait(INTERVAL_MS);
      trial.onset = await this.paint(() => this.drawGrating(parameters.base_orientation_deg + parameters.signed_offset_deg));
      await this.wait(stimulus);
      trial.offset = await this.paint(() => this.fixation());
      this.prompt.textContent = 'Куда повернулась решётка?';
      this.answers.forEach(answer => answer.dataset.ready = 'true');
      await this.awaitResponse(trial);
      if (!trial.reason && (trial.offset - trial.onset > stimulus + 75 ||
        trial.referenceOffset - trial.reference > REFERENCE_MS + 75)) trial.reason = 'display_timing';
      this.recordTrial(trial);
      this.trial = null;
      this.answers.forEach(answer => answer.dataset.ready = 'false');
      if (trial.reason === 'early_response') this.prompt.textContent = 'Дождитесь второй решётки';
      else if (!trial.response || trial.reason === 'timeout') this.prompt.textContent = 'Время ответа истекло';
      else if (trial.reason) this.prompt.textContent = 'Попытка пропущена';
      else if (training) this.prompt.textContent = trial.response === (parameters.signed_offset_deg > 0 ? 'right' : 'left')
        ? 'Верно' : 'Ответ неверный';
      else this.prompt.textContent = 'Ответ записан';
      await this.wait(training ? 430 : 320);
    }

    async feedbackTask() {
      const demo = this.run.mode === 'demo';
      const duration = this.number('duration_s', demo ? 15 : 120, 5, 900);
      const calibration = demo ? 0 : 10;
      const total = calibration + duration;
      if (demo) {
        this.status('Демо-сигнал');
        await this.prep('Смотрите на центр экрана');
      } else {
        await this.feedbackPhase('calibration', 0, calibration, false, 0, total);
      }
      const visible = ['live', 'control'].includes(this.run.stage);
      await this.feedbackPhase(this.run.stage, Number(!demo), duration, visible, calibration, total);
    }

    async feedbackPhase(label, index, duration, visible, elapsed, total) {
      const calibration = label === 'calibration';
      const baseline = label === 'baseline';
      this.body.innerHTML = `<div class="exp-run-center exp-feedback">
        <div class="exp-fb-field" aria-hidden="true">
          ${visible ? '<div class="exp-fb-orb"></div>' : '<span class="exp-fb-fixation">+</span>'}
        </div>
        <h3>${calibration ? 'Настройка сигнала' : visible ? 'Удерживайте взгляд на сфере' : 'Смотрите на точку фиксации'}</h3>
        <p>${calibration || baseline ? 'Держите глаза открытыми, смотрите в центр и сидите спокойно.'
          : 'Держите глаза открытыми и пробуйте менять состояние без движений.'}</p>
      </div>`;
      const orb = this.body.querySelector('.exp-fb-orb');
      this.displayed = null;
      this.status(calibration ? 'Идёт калибровка альфа-ритма' : visible
        ? 'Ожидаем первый сигнал' : 'Обратная связь в этом блоке скрыта');
      this.openPhase(label, index, duration);
      const activePhase = this.phase;
      const poll = async () => {
        const stillActive = () => !this.controller.signal.aborted && this.phase === activePhase;
        if (!stillActive()) return;
        const started = stamp();
        let data;
        try {
          data = await this.request(this.path + '/feedback', {signal: this.controller.signal});
        } catch (error) {
          if (!stillActive()) return;
          data = {valid: false, alpha_valid: false, reason: 'connection_error'};
        }
        if (!stillActive()) return;
        const displayValid = data.valid && Number.isFinite(data.value);
        if (visible && displayValid) {
          this.displayed = Math.max(0, Math.min(1, data.value));
          orb.style.setProperty('--exp-activity', this.displayed);
        }
        const alphaValid = Boolean(data.alpha_valid ?? (data.valid && Number.isFinite(data.alpha)));
        this.event('feedback', {
          label, value: visible ? this.displayed : null,
          displayed: Boolean(visible && this.displayed !== null),
          display_updated: Boolean(visible && displayValid),
          alpha: Number.isFinite(data.alpha) ? data.alpha : null,
          alpha_fixed: Number.isFinite(data.alpha_fixed) ? data.alpha_fixed : null,
          baseline_alpha: Number.isFinite(data.baseline_alpha) ? data.baseline_alpha : null,
          sample_at_ms: Number.isFinite(data.sample_at_ms) ? data.sample_at_ms : null,
          band: Array.isArray(data.band) ? data.band : null,
          band_source: data.band_source ?? null,
          valid: visible ? Boolean(displayValid) : alphaValid,
          alpha_valid: alphaValid,
          reason: data.reason ?? null,
        });
        if (visible) this.status(displayValid ? 'Обратная связь активна' :
          this.displayed === null ? 'Ожидаем пригодный сигнал' : 'Сфера удерживает последнее значение', displayValid ? 'good' : 'warn');
        this.probeTimer = setTimeout(poll, Math.max(80, 500 - (stamp() - started)));
      };
      this.drain().then(poll, () => {
        void this.checkRun();
        void poll();
      });
      await this.countdown(duration, (remaining, progress) =>
        this.setProgress((elapsed + progress * duration) / total, `${calibration ? 'Калибровка · ' : ''}${remaining} с`));
      clearTimeout(this.probeTimer);
      this.closePhase();
    }

    stop(reason = 'user_stop') {
      return this.finish(this.completion?.status || 'aborted', this.completion?.reason || reason);
    }

    finish(status, reason = '') {
      if (this.finished) return Promise.resolve(this.finished);
      if (this.finishing) return this.finishing;
      this.completion = {status, reason};
      if (!this.controller.signal.aborted) {
        const trial = this.trial;
        if (trial) this.recordTrial(trial, reason || 'interrupted');
        this.trial = null;
        this.closePhase(reason);
        this.release(status === 'completed');
      }
      if (this.stopButton) this.stopButton.disabled = true;
      this.status('Сохраняем сессию…');
      this.finishing = this.save()
        .catch(error => {
          if (this.stopButton) {
            this.stopButton.textContent = 'Повторить сохранение';
            this.stopButton.disabled = false;
          }
          this.status('Сессию пока не удалось сохранить', 'warn');
          this.onError?.(error);
        })
        .finally(() => { this.finishing = null; });
      return this.finishing;
    }

    release(complete = false) {
      this.controller.abort();
      clearInterval(this.flushTimer);
      clearInterval(this.heartbeatTimer);
      clearTimeout(this.probeTimer);
      if (this.audio && this.audio.state !== 'closed') {
        // Let the final tone play before releasing the audio context.
        const audio = this.audio;
        setTimeout(() => audio.close().catch(() => {}), complete ? 650 : 0);
      }
      this.body?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    }

    async save() {
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          await this.drain();
          this.finished = await this.post('/finish', this.completion);
          this.onFinish?.(this.finished);
          return this.finished;
        } catch (error) {
          if (!attempt) {
            await new Promise(resolve => setTimeout(resolve, 250));
            continue;
          }
          let run;
          try { run = await this.request(this.path); } catch (_) {}
          if (!run || run.status === 'running') throw error;
          this.finished = run;
          this.onFinish?.({...run, local_unsaved_events: this.pending.filter(event => event.type !== 'heartbeat').length});
          return run;
        }
      }
    }
  }

  window.NeuroExperimentRunner = ExperimentRunner;
})();

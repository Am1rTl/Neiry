/* Checkpoints belong to the recording, independent of the active view. */
(() => {
  'use strict';

  const $ = selector => document.querySelector(selector);
  const timeFormat = new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Europe/Moscow',
  });
  const drafts = new Map();
  const saving = new Set();
  let api;
  let recording = null;
  let latestRecording = null;
  let editor = null;
  let listRequest = 0;
  let restoreFocus = null;
  let creating = false;
  let toastTimer;

  const panel = () => $('#checkpoint-panel');
  const input = () => $('#checkpoint-name');
  const timeOf = checkpoint => timeFormat.format(checkpoint.ts_unix * 1000) + ' мск';

  function syncControls() {
    if (!api) return;
    $('#checkpoint-dock').hidden = !latestRecording;
    for (const button of document.querySelectorAll('[data-checkpoint-create]')) {
      button.disabled = creating || !recording;
      if (button.closest('#checkpoint-dock')) button.hidden = !recording;
    }
    for (const button of document.querySelectorAll('[data-checkpoint-list=""]')) {
      button.disabled = !latestRecording;
    }
  }

  function setRecording(value) {
    const id = typeof value === 'string' && value ? value : null;
    if (!api || id === recording) return;
    recording = id;
    if (id) latestRecording = id;
    syncControls();
  }

  function error(message = '') {
    const line = $('#checkpoint-error');
    line.hidden = !message;
    line.textContent = message;
  }

  function toast(message, failed = false) {
    const line = $('#checkpoint-toast');
    clearTimeout(toastTimer);
    line.classList.toggle('failed', failed);
    line.hidden = false;
    line.textContent = message;
    toastTimer = setTimeout(() => { line.hidden = true; }, failed ? 7000 : 2500);
  }

  function reveal(trigger = document.activeElement) {
    if (panel().hidden) restoreFocus = trigger;
    panel().hidden = false;
    $('#checkpoint-toast').hidden = true;
    error();
  }

  function close() {
    listRequest++;
    editor = null;
    panel().hidden = true;
    if (restoreFocus?.getClientRects().length) restoreFocus.focus({preventScroll: true});
    restoreFocus = null;
  }

  function syncSave() {
    const save = $('#checkpoint-save');
    const busy = saving.has(editor.checkpoint.id);
    input().readOnly = busy;
    save.disabled = busy || input().value.trim() === (editor.checkpoint.label || '');
    save.textContent = busy ? 'Сохраняем…' : 'Сохранить';
  }

  function openEditor(checkpoint, fromList = false, trigger) {
    listRequest++;
    reveal(trigger);
    editor = {checkpoint, fromList};
    $('#checkpoint-title').textContent = 'Название чекпоинта';
    $('#checkpoint-detail').textContent = checkpoint.label
      ? 'Чекпоинт в ' + timeOf(checkpoint)
      : 'Поставлен в ' + timeOf(checkpoint) + '. Название можно добавить позже.';
    $('#checkpoint-list').hidden = true;
    $('#checkpoint-form').hidden = false;
    $('[data-checkpoint-cancel]').textContent = checkpoint.label ? 'Отмена' : 'Позже';
    input().value = drafts.get(checkpoint.id) ?? checkpoint.label ?? '';
    syncSave();
    input().focus({preventScroll: true});
  }

  function renderList(list, items) {
    list.replaceChildren();
    if (!items.length) {
      list.textContent = 'В этой записи пока нет чекпоинтов.';
      return;
    }
    const rows = document.createElement('ul');
    for (const checkpoint of items.slice().sort((a, b) => b.ts_unix - a.ts_unix)) {
      const row = document.createElement('li');
      const details = document.createElement('div');
      const name = document.createElement('strong');
      const time = document.createElement('small');
      name.textContent = checkpoint.label || 'Без названия';
      time.textContent = timeOf(checkpoint);
      details.append(name, time);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button subtle';
      button.textContent = checkpoint.label ? 'Изменить' : 'Назвать';
      button.setAttribute('aria-label', button.textContent + ' чекпоинт в ' + timeOf(checkpoint)
        + (checkpoint.label ? ' — ' + checkpoint.label : ''));
      button.addEventListener('click', () => openEditor(checkpoint, true));
      row.append(details, button);
      rows.append(row);
    }
    list.append(rows);
  }

  async function openList(id = recording || latestRecording) {
    if (!id) return;
    const request = ++listRequest;
    reveal();
    editor = null;
    $('#checkpoint-title').textContent = 'Чекпоинты';
    $('#checkpoint-title').focus({preventScroll: true});
    $('#checkpoint-detail').textContent = id;
    $('#checkpoint-form').hidden = true;
    const list = $('#checkpoint-list');
    list.hidden = false;
    list.textContent = 'Загружаем чекпоинты…';
    try {
      const result = await api('/api/checkpoints?session_id=' + encodeURIComponent(id));
      if (request === listRequest) renderList(list, result.items);
    } catch (err) {
      if (request !== listRequest) return;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'button subtle';
      retry.textContent = 'Повторить';
      retry.addEventListener('click', () => openList(id));
      list.replaceChildren(retry);
      error(err.message);
    }
  }

  async function create(trigger = document.activeElement) {
    if (!recording || creating) return;
    creating = true;
    syncControls();
    try {
      const checkpoint = await api('/api/checkpoints', {method: 'POST'});
      openEditor(checkpoint, false, trigger);
    } catch (err) {
      if (panel().hidden) toast(err.message, true);
      else error(err.message);
    } finally {
      creating = false;
      syncControls();
    }
  }

  function cancel() {
    if (editor?.fromList) openList(editor.checkpoint.session_id);
    else close();
  }

  async function save(event) {
    event.preventDefault();
    const current = editor;
    if (!current || $('#checkpoint-save').disabled) return;
    const {id, session_id} = current.checkpoint;
    const label = input().value.trim() || null;
    saving.add(id);
    syncSave();
    error();
    try {
      const updated = await api('/api/checkpoints/' + encodeURIComponent(id), {
        method: 'PATCH', body: JSON.stringify({session_id, label}),
      });
      drafts.delete(id);
      if (editor === current) {
        cancel();
        toast(label ? 'Название сохранено.' : 'Название удалено.');
      } else if (editor?.checkpoint.id === id) {
        editor.checkpoint = updated;
      }
    } catch (err) {
      if (editor?.checkpoint.id === id) error(err.message);
      else toast(err.message, true);
    } finally {
      saving.delete(id);
      if (editor?.checkpoint.id === id) syncSave();
    }
  }

  function init(options) {
    if (options.preview || api) return;
    api = options.api;
    syncControls();
    $('#checkpoint-form').addEventListener('submit', save);
    input().addEventListener('input', () => {
      drafts.set(editor.checkpoint.id, input().value);
      syncSave();
    });
    document.addEventListener('click', event => {
      const target = event.target.closest('[data-checkpoint-create],[data-checkpoint-list],[data-checkpoint-close],[data-checkpoint-cancel]');
      if (!target) return;
      if ('checkpointCreate' in target.dataset) create(target);
      else if ('checkpointList' in target.dataset) openList(target.dataset.checkpointList || undefined);
      else if ('checkpointCancel' in target.dataset) cancel();
      else close();
    });
    document.addEventListener('keydown', event => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.key === 'Escape' && panel().contains(event.target) && !panel().hidden) close();
      if (!recording || !event.altKey || event.shiftKey || event.ctrlKey || event.metaKey
          || (event.code !== 'KeyM' && event.key.toLowerCase() !== 'm')) return;
      if (event.target !== input()
          && event.target.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return;
      event.preventDefault();
      if (!event.repeat) create();
    });
  }

  window.NeuroCheckpoints = {init, setRecording, syncControls};
})();

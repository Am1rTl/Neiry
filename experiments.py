"""Local experiment runner: explicit recording, timed event markers, safe lifecycle.

These are behavioural protocol markers, NOT hardware-synchronized triggers.
Never claim millisecond stimulus-locking or identify the participant implicitly.
"""
from __future__ import annotations

import math
import random
import threading
import time
import uuid
from typing import Any, Callable

DOOR_PROTOCOL = 'door_imagery'
DOOR_TRIALS = 20
DOOR_PRESENTATION_TIMEOUT_S = 1.5
DOOR_PHASES = (('fixation', 2.0), ('cue', 2.0), ('task', 5.0), ('feedback', 2.0))


def door_phases() -> list[dict[str, Any]]:
    conditions = ['imagery', 'rest'] * (DOOR_TRIALS // 2)
    random.shuffle(conditions)
    return [
        {'label': stage, 'duration_s': seconds, 'trial': trial, 'condition': condition}
        for trial, condition in enumerate(conditions, start=1)
        for stage, seconds in DOOR_PHASES
    ]


# Example templates use short blocks to be convenient during exploration.
# For a research study customize durations and repeat sufficiently.
TEMPLATES = {
    'eyes': {'title':'Глаза открыты / закрыты', 'description':'Проверка повторяемости альфа-реактивности',
             'phases':[{'label':'eyes_open','duration_s':60}, {'label':'eyes_closed','duration_s':60},
                       {'label':'eyes_open','duration_s':60}, {'label':'eyes_closed','duration_s':60}]},
    'rest': {'title':'Покой', 'description':'Неподвижное спокойное сидение',
             'phases':[{'label':'rest','duration_s':180}]},
    'task': {'title':'Задача и восстановление', 'description':'Контекст задачи задаёт исследователь',
             'phases':[{'label':'baseline','duration_s':60}, {'label':'task','duration_s':120},
                       {'label':'recovery','duration_s':120}]},
}


def validate_phases(phases: Any) -> list[dict[str, Any]]:
    if not isinstance(phases,list) or not 1 <= len(phases) <= 24:
        raise ValueError('От 1 до 24 этапов')
    out=[]
    for item in phases:
        if not isinstance(item,dict): raise ValueError('Неверный этап')
        label=item.get('label'); seconds=item.get('duration_s')
        if not isinstance(label,str) or not 1 <= len(label) <= 64 or not all(c.isalnum() or c in '-_' for c in label):
            raise ValueError('Некорректная метка этапа')
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 2 <= seconds <= 1200:
            raise ValueError('Длительность этапа от 2 до 1200 секунд')
        out.append({'label':label,'duration_s':float(seconds)})
    if sum(x['duration_s'] for x in out)>7200:raise ValueError('Протокол не может быть длиннее двух часов')
    return out


class ExperimentManager:
    def __init__(self, get_recorder: Callable[[], Any], tick_s: float = .05,
                 door_signal_ready: Callable[[], bool] | None = None):
        self._get_recorder = get_recorder
        self._door_signal_ready = door_signal_ready
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._recorder_id: str | None = None
        self._ending_session_id: str | None = None
        self._protocol_id: str | None = None
        self._phase_idx = -1
        self._started = 0.0
        self._phase_started = 0.0
        self._phases: list[dict[str, Any]] = []
        self._state = 'idle'
        self._tick_s = tick_s
        self._reason = ''
        self._phase_presented = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            running=self._state=='running'
            now=time.monotonic()
            p=self._phases[self._phase_idx] if 0 <= self._phase_idx<len(self._phases) else None
            result = {'state':self._state,'run_id':self._run_id,'protocol_id':self._protocol_id,
                    'session_id':self._recorder_id,'phase_index':self._phase_idx,
                    'phase': dict(p) if p else None, 'total_phases':len(self._phases),
                    'remaining_s':max(0.0,round(p['duration_s']-(now-self._phase_started),1)) if running and p else None,
                    'elapsed_s':round(now-self._started,1) if running else None,
                    'reason':self._reason,
                    'notice':'Маркеры записаны серверным временем; не являются синхронными аппаратными триггерами.'}
            if self._protocol_id == DOOR_PROTOCOL:
                result['total_trials'] = DOOR_TRIALS
            return result

    def _event(self, kind: str, **data: Any) -> None:
        recorder=self._get_recorder()
        if recorder is not None and recorder.active and recorder.session_id==self._recorder_id:
            recorder.event(kind, run_id=self._run_id, protocol_id=self._protocol_id, **data)

    def start(self, protocol_id: str, phases: list[dict] | None = None) -> dict[str, Any]:
        if not isinstance(protocol_id,str) or not 1 <= len(protocol_id)<=80 or not all(c.isalnum() or c in '-_' for c in protocol_id):
            raise ValueError('Некорректный идентификатор протокола')
        if protocol_id == DOOR_PROTOCOL:
            if phases is not None:
                raise ValueError('Для эксперимента с дверью используется фиксированный протокол')
            validated = door_phases()
        elif phases is None:
            if protocol_id not in TEMPLATES:raise ValueError('Неизвестный шаблон')
            validated = validate_phases(TEMPLATES[protocol_id]['phases'])
        else:
            validated = validate_phases(phases)
        with self._lock:
            if self._state=='running':raise RuntimeError('Другой протокол уже выполняется')
            rec = self._get_recorder()
            if rec is None or not rec.active or rec.session_id == self._ending_session_id:
                raise RuntimeError('Сначала начните реальную запись')
            if protocol_id == DOOR_PROTOCOL and not self._door_ready():
                raise RuntimeError('Дождитесь свежей ЭЭГ и стабильного контакта электродов')
            self._stop = threading.Event()
            self._run_id=uuid.uuid4().hex[:12]
            self._recorder_id=rec.session_id
            self._protocol_id=protocol_id
            self._phases=validated
            self._phase_idx=-1
            self._reason=''
            self._started=time.monotonic()
            self._state='running'
            metadata = {'protocol_version': 1, 'eye_state': 'open',
                        'imagery': 'kinesthetic_right_hand'} if protocol_id == DOOR_PROTOCOL else {}
            self._event('protocol_start', phases=validated, **metadata)
            self._next_phase()
            self._thread=threading.Thread(target=self._run,args=(self._stop,),name='protocol',daemon=True)
            self._thread.start()
            return self.status()

    def _next_phase(self) -> None:
        if self._phase_idx>=0:
            phase = self._phases[self._phase_idx]
            self._event('protocol_phase_end', index=self._phase_idx,
                        **{key: value for key, value in phase.items() if key != 'duration_s'})
        self._phase_idx+=1
        if self._phase_idx>=len(self._phases):
            self._event('protocol_complete')
            self._state='completed'
            self._stop.set()
            return
        self._phase_started=time.monotonic()
        self._phase_presented = False
        phase=self._phases[self._phase_idx]
        self._event('protocol_phase_start', index=self._phase_idx, **phase)

    def door_presented(self, run_id: str, phase_index: int, client_unix_ms: float) -> None:
        if (type(phase_index) is not int or type(client_unix_ms) not in (int, float)
                or not 0 < client_unix_ms < 1e14):
            raise ValueError('Некорректная метка показа')
        with self._lock:
            if (self._state != 'running' or self._protocol_id != DOOR_PROTOCOL or run_id != self._run_id
                    or phase_index != self._phase_idx):
                raise RuntimeError('Этап эксперимента уже изменился')
            if self._phase_presented:
                return
            if time.monotonic() - self._phase_started >= DOOR_PRESENTATION_TIMEOUT_S:
                self.stop('stimulus_missing')
                raise RuntimeError('Браузер не подтвердил показ вовремя')
            self._event('door_phase_presented', index=phase_index,
                        client_unix_ms=client_unix_ms, **self._phases[phase_index])
            self._phase_presented = True

    def stop(self, reason: str = 'user_stop', *, run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if run_id is not None and run_id != self._run_id:
                return self.status()
            if self._state=='running':
                self._event('protocol_aborted',reason=reason,phase_index=self._phase_idx)
                self._state='aborted'; self._reason=reason
            self._stop.set()
            return self.status()

    def _door_ready(self) -> bool:
        return self._door_signal_ready is not None and self._door_signal_ready()

    def recording_ending(self, session_id: str, reason: str) -> None:
        with self._lock:
            self._ending_session_id = session_id
            if session_id == self._recorder_id:
                self.stop(reason)

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._tick_s):
            with self._lock:
                if stop.is_set() or self._state!='running':break
                rec=self._get_recorder()
                if rec is None or not rec.active or rec.session_id!=self._recorder_id:
                    self.stop('recording_stopped');break
                elapsed = time.monotonic() - self._phase_started
                duration = self._phases[self._phase_idx]['duration_s']
                if self._protocol_id == DOOR_PROTOCOL:
                    if not self._door_ready():
                        self.stop('signal_lost')
                        break
                    if not self._phase_presented and elapsed >= min(duration, DOOR_PRESENTATION_TIMEOUT_S):
                        self.stop('stimulus_missing')
                        break
                if elapsed >= duration:
                    self._next_phase()

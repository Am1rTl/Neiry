"""Local experiment runner: explicit recording, timed event markers, safe lifecycle.

These are behavioural protocol markers, NOT hardware-synchronized triggers.
Never claim millisecond stimulus-locking or identify the participant implicitly.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from typing import Any, Callable

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
    def __init__(self, get_recorder: Callable[[], Any], tick_s: float = .05):
        self._get_recorder = get_recorder
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._recorder_id: str | None = None
        self._protocol_id: str | None = None
        self._phase_idx = -1
        self._started = 0.0
        self._phase_started = 0.0
        self._phases: list[dict[str, Any]] = []
        self._state = 'idle'
        self._tick_s = tick_s
        self._reason = ''

    def status(self) -> dict[str, Any]:
        with self._lock:
            running=self._state=='running'
            now=time.monotonic()
            p=self._phases[self._phase_idx] if 0 <= self._phase_idx<len(self._phases) else None
            return {'state':self._state,'run_id':self._run_id,'protocol_id':self._protocol_id,
                    'session_id':self._recorder_id,'phase_index':self._phase_idx,
                    'phase': p, 'total_phases':len(self._phases),
                    'remaining_s':max(0.0,round(p['duration_s']-(now-self._phase_started),1)) if running and p else None,
                    'elapsed_s':round(now-self._started,1) if running else None,
                    'reason':self._reason,
                    'notice':'Маркеры записаны серверным временем; не являются синхронными аппаратными триггерами.'}

    def _event(self, kind: str, **data: Any) -> None:
        recorder=self._get_recorder()
        if recorder is not None and recorder.active and recorder.session_id==self._recorder_id:
            recorder.event(kind, run_id=self._run_id, protocol_id=self._protocol_id, **data)

    def start(self, protocol_id: str, phases: list[dict] | None = None) -> dict[str, Any]:
        if not isinstance(protocol_id,str) or not 1 <= len(protocol_id)<=80 or not all(c.isalnum() or c in '-_' for c in protocol_id):
            raise ValueError('Некорректный идентификатор протокола')
        if phases is None:
            if protocol_id not in TEMPLATES:raise ValueError('Неизвестный шаблон')
            phases=TEMPLATES[protocol_id]['phases']
        validated=validate_phases(phases)
        rec=self._get_recorder()
        if rec is None or not rec.active: raise RuntimeError('Сначала начните реальную запись')
        with self._lock:
            if self._state=='running':raise RuntimeError('Другой протокол уже выполняется')
            self._stop.clear()
            self._run_id=uuid.uuid4().hex[:12]
            self._recorder_id=rec.session_id
            self._protocol_id=protocol_id
            self._phases=validated
            self._phase_idx=-1
            self._reason=''
            self._started=time.monotonic()
            self._state='running'
            self._event('protocol_start',phases=validated)
            self._next_phase()
            self._thread=threading.Thread(target=self._run,name='protocol',daemon=True)
            self._thread.start()
            return self.status()

    def _next_phase(self) -> None:
        if self._phase_idx>=0:
            self._event('protocol_phase_end',index=self._phase_idx,label=self._phases[self._phase_idx]['label'])
        self._phase_idx+=1
        if self._phase_idx>=len(self._phases):
            self._event('protocol_complete')
            self._state='completed'
            self._stop.set()
            return
        self._phase_started=time.monotonic()
        phase=self._phases[self._phase_idx]
        self._event('protocol_phase_start',index=self._phase_idx,label=phase['label'],duration_s=phase['duration_s'])

    def stop(self, reason: str = 'user_stop') -> dict[str, Any]:
        with self._lock:
            if self._state=='running':
                self._event('protocol_aborted',reason=reason,phase_index=self._phase_idx)
                self._state='aborted'; self._reason=reason
            self._stop.set()
            return self.status()

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            with self._lock:
                if self._state!='running':break
                rec=self._get_recorder()
                if rec is None or not rec.active or rec.session_id!=self._recorder_id:
                    self.stop('recording_stopped');break
                if time.monotonic()-self._phase_started>=self._phases[self._phase_idx]['duration_s']:
                    self._next_phase()

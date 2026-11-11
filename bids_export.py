"""Optional, explicitly unit-confirmed EEG-BIDS export via MNE-BIDS.

No silent conversion of unidentified SDK values to physical EEG voltage.
This export cannot supply missing electrode locations or hardware sync.
"""
from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def export_bids(path: Path, *, subject: str, units: str,
                confirmed: bool, task: str = 'rest') -> bytes:
    if not confirmed or units not in ('V','mV','uV'):
        raise ValueError('Подтвердите физические единицы по документации устройства: V, mV или uV')
    if not re.fullmatch('[A-Za-z0-9]{1,20}', subject):
        raise ValueError('Идентификатор участника: 1–20 латинских букв или цифр')
    if not re.fullmatch('[A-Za-z0-9]{1,30}',task):
        raise ValueError('Идентификатор задания: 1–30 латинских букв или цифр')
    try:
        import mne
        from mne_bids import BIDSPath, write_raw_bids
    except ImportError as exc:
        raise RuntimeError('Для BIDS необходимы пакеты requirements-research.txt (mne, mne-bids, edfio)') from exc
    meta=json.loads((path/'meta.json').read_text(encoding='utf-8'))
    rate=float(meta.get('sample_rates',{}).get('eeg',0))
    if not 0 < rate <= 4096:raise ValueError('В метаданных нет подтверждённой частоты ЭЭГ')
    with (path/'eeg.csv').open(newline='',encoding='utf-8-sig') as stream:
        reader=csv.DictReader(stream)
        names=[c for c in (reader.fieldnames or []) if c not in ('ts_unix','ts_iso','sample_index')]
        if not names:raise ValueError('Файл ЭЭГ не содержит каналов')
        rows=[];first_ts=None
        for rec in reader:
            try:
                row=[float(rec[name]) for name in names]
                if not all(__import__('math').isfinite(x) for x in row):
                    raise ValueError('Данные с пропусками: для BIDS требуется предварительное описание разрывов')
                if first_ts is None:first_ts=float(rec.get('ts_unix') or 0)
                rows.append(row)
            except (TypeError,ValueError,KeyError) as exc:
                raise ValueError('В ЭЭГ есть некорректные отсчёты; экспорт без подмены невозможен') from exc
            if len(rows)>1_500_000:
                raise ValueError('Сессия слишком большая для текущего экспортёра: используйте порционную конвертацию')
    if len(rows)<rate:raise ValueError('Менее одной секунды ЭЭГ')
    import numpy as np
    mult={'V':1.0,'mV':1e-3,'uV':1e-6}[units]
    info=mne.create_info(names,rate,ch_types=['eeg']*len(names))
    raw=mne.io.RawArray((np.asarray(rows).T*mult),info,verbose='ERROR')
    # The host timestamp is NOT a validated sample acquisition clock. Never
    # set meas_date from it and never fabricate electrode coordinates.
    with tempfile.TemporaryDirectory(prefix='neuroscope-bids-') as folder:
        root=Path(folder)
        bids_path=BIDSPath(root=root,subject=subject,task=task,run='01',datatype='eeg')
        write_raw_bids(raw,bids_path,format='EDF',allow_preload=True,overwrite=True,verbose='ERROR')
        (root/'NeuroScope_PROVENANCE.txt').write_text(
            f'Exported from local session: {path.name}\n'
            f'User-confirmed original SDK units: {units}; converted to V for MNE.\n'
            'Original CSV uses sample_index / nominal sample rate; packet-reception host timestamps are not hardware synchronized.\n'
            'No sensor/electrode coordinates, independent reference or verified acquisition event timing was supplied.\n'
            'The signal was not denoised or clinically interpreted. Events were not exported because old host timestamps are unreliable.\n',encoding='utf-8')
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
            for file in root.rglob('*'):
                if file.is_file():archive.write(file,arcname=file.relative_to(root).as_posix())
        return out.getvalue()

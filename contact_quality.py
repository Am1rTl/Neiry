"""Real SDK electrode-resistance readiness, never a synthetic wear detector.

The band may stream electrical values without touching the head. Good electrode
impedance is evidence of contact, not proof of identity, placement or EEG quality.
We require *consecutive real SDK callbacks*, freshness and a settle interval.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any


@dataclass(frozen=True)
class ContactOptions:
    ready_ohm: float = 1_000_000.0
    preferred_ohm: float = 500_000.0
    stable_seconds: float = 6.0
    stale_seconds: float = 4.5
    min_updates: int = 3
    min_channels: int = 2


class ElectrodeContact:
    def __init__(self, options: ContactOptions | None = None):
        self.options = options or ContactOptions()
        self.reset()

    def reset(self) -> None:
        self._values: list[dict[str, Any]] = []
        self._last_received: float | None = None
        self._good_since: float | None = None
        self._good_updates = 0
        self._connected = False

    def add(self, rows: list[dict[str, Any]], now: float | None = None) -> None:
        """Feed an SDK resistance callback; no interpolation or fake samples."""
        timestamp = time.monotonic() if now is None else float(now)
        safe = []
        # A duplicate name cannot stand in for a second physical electrode.
        unique_names = {str(row.get('channel', '')).strip() for row in rows}
        all_good = (len(rows) >= self.options.min_channels
                    and len(unique_names - {''}) >= self.options.min_channels)
        for row in rows:
            raw = row.get('value')
            try:
                ohm = float(raw)
                if not math.isfinite(ohm) or ohm <= 0:
                    ohm = None
            except (TypeError, ValueError, OverflowError):
                ohm = None
            if row.get('is_inf'):
                ohm = None
            name = str(row.get('channel', '') or 'Электрод')
            ok = ohm is not None and ohm < self.options.ready_ohm
            all_good = all_good and ok
            safe.append({'channel': name, 'ohm': ohm, 'good': ok,
                         'preferred': ohm is not None and ohm < self.options.preferred_ohm})
        # A repeated/older callback is not proof of new stable contact.
        if self._last_received is not None and timestamp <= self._last_received:
            all_good = False
        self._last_received = timestamp
        self._values = safe
        if all_good:
            if self._good_since is None:
                self._good_since = timestamp
                self._good_updates = 0
            self._good_updates += 1
        else:
            self._good_since = None
            self._good_updates = 0

    def snapshot(self, *, connected: bool, now: float | None = None) -> dict[str, Any]:
        opts = self.options
        moment = time.monotonic() if now is None else float(now)
        age = (max(0.0, moment - self._last_received)
               if self._last_received is not None else None)
        channels = self._values if connected else []
        n_good = sum(1 for row in channels if row['good'])
        enough = (len(channels) >= opts.min_channels and
                  len({row['channel'].strip() for row in channels} - {''}) >= opts.min_channels)
        fresh = age is not None and age <= opts.stale_seconds
        all_good = enough and all(row['good'] for row in channels)
        elapsed = (max(0.0, moment - self._good_since)
                   if all_good and fresh and self._good_since is not None else 0.0)
        ready = bool(connected and fresh and all_good and elapsed >= opts.stable_seconds
                     and self._good_updates >= opts.min_updates)
        if not connected:
            state, message = 'disconnected', 'Устройство не подключено.'
        elif age is None:
            state, message = 'waiting', 'Ожидаем реальные измерения сопротивления от SDK.'
        elif not fresh:
            state, message = 'stale', 'Измерения сопротивления устарели. Проверьте устройство.'
        elif not enough:
            state, message = 'incomplete', 'Получены не все измерения электродов.'
        elif not all_good:
            state, message = 'poor', 'Контакт недостаточен: поправьте повязку и волосы под электродами.'
        elif not ready:
            state, message = 'settling', 'Контакт появился. Держите повязку неподвижно до стабилизации.'
        else:
            state, message = 'ready', 'Все измеряемые электроды в допустимом диапазоне. Можно калибровать.'
        return {
            'state': state, 'ready': ready, 'connected': connected, 'message': message,
            'source': 'Capsule SDK resistance callback', 'unit': 'ohm',
            'channels': [{**row, 'kohm': row['ohm'] / 1000 if row['ohm'] is not None else None}
                         for row in channels],
            'n_channels': len(channels), 'n_good': n_good,
            'minimum_channels': opts.min_channels, 'updated_age_s': round(age, 2) if connected and age is not None else None,
            'stable_seconds': round(elapsed, 1), 'required_seconds': opts.stable_seconds,
            'remaining_seconds': round(max(0.0, opts.stable_seconds - elapsed), 1) if all_good and fresh else None,
            'sample_count': self._good_updates if all_good and fresh else 0,
            'minimum_updates': opts.min_updates,
            'ready_below_kohm': opts.ready_ohm / 1000.0,
            'preferred_below_kohm': opts.preferred_ohm / 1000.0,
            'calibration_allowed': ready,
            'notice': 'Это пороги приложения для данных сопротивления SDK, не прямой датчик надевания и не медицинская оценка качества ЭЭГ.',
        }

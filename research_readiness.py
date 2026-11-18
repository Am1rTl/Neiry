"""Evidence for a *recorded* eyes-open/closed comparison, using existing EEG QC."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


EYE_LABELS = {"eyes_open", "eyes_closed"}
EYE_SITES = {"O1", "O2"}
MIN_SDK_BLOCK_S = 30.
MAX_HOST_RESIDUAL_RANGE_S = .25
ONSET_MARGIN_S = 3.
END_MARGIN_S = 1.


def _number(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def documented_browser_clock(settings):
    return (isinstance(settings, dict) and settings.get("timing") == "browser_monotonic_software_clock"
            and _number(settings.get("clock_offset_ms")) is not None
            and _number(settings.get("clock_round_trip_ms")) is not None
            and settings["clock_round_trip_ms"] >= 0)


def sdk_supports_alpha_block(times, indices, rate, begin, end, packet_clock):
    """Only establish coarse block membership. SDK and host epochs may differ."""
    if (end - begin < MIN_SDK_BLOCK_S or indices is None or not packet_clock
            or len(times) != len(indices) or len(times) < 2):
        return False
    if (not np.all(np.isfinite(times)) or not np.all(np.isfinite(indices))
            or indices[0] < 0 or indices[0] != np.floor(indices[0])
            or not np.all(np.diff(indices) == 1)
            or times.min() < begin + ONSET_MARGIN_S or times.max() > end - END_MARGIN_S):
        return False
    residual = (times - times[0]) - (indices - indices[0]) / rate
    return bool(np.ptp(residual) <= MAX_HOST_RESIDUAL_RANGE_S and
                packet_clock.supports_window(int(indices[0]), int(indices[-1])))


@dataclass
class _Run:
    source: str
    clock: str
    completed: bool = False
    ended: bool = False
    current: tuple | None = None
    blocks: dict = field(default_factory=dict)

    def phase(self, event: dict, stamp: float | None):
        kind = event.get("type")
        if kind in ("pause", "resume"):
            self.current = None
            return
        if kind not in ("phase_start", "phase_end"):
            return
        index, label = event.get("index"), event.get("label")
        if type(index) is not int or index < 0 or not isinstance(label, str) or label not in EYE_LABELS or stamp is None:
            self.current = None
            return
        key = (index, label, event.get("phase_id"))
        if kind == "phase_start":
            self.current = (key, stamp)
            return
        opened, self.current = self.current, None
        if (opened is None or key != opened[0] or event.get("reason")
                or stamp <= opened[1]):
            return
        # Multiple completed attempts with the same index are ambiguous.
        self.blocks[index] = None if index in self.blocks else (label, opened[1], stamp)


def _paired_blocks(events: list, first: float | None, last: float | None):
    runs = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        ident, kind = event.get("run_id"), event.get("type")
        if not isinstance(ident, str) or not ident or not isinstance(kind, str):
            continue
        if kind == "protocol_start" and event.get("protocol_id") == "eyes":
            runs[ident] = _Run("protocol", "server_epoch")
        elif kind == "experiment_lab_start" and event.get("kind") == "alpha":
            settings = event.get("settings") or {}
            # The runner already applies this offset to at_ms before transmission.
            # Legacy or external exports may omit evidence of an offset estimate.
            synced = documented_browser_clock(settings)
            runs[ident] = _Run("lab", "browser_epoch_with_offset" if synced else "browser_epoch_unverified")
        run = runs.get(ident)
        if run is None or run.ended:
            continue
        if run.source == "lab":
            if kind == "experiment_lab_finish":
                run.completed = event.get("experiment_status") == "completed" and not event.get("reason")
                run.ended = True
            elif kind == "experiment_lab_event" and isinstance(event.get("event"), dict):
                inner = event["event"]
                stamp = _number(inner.get("at_ms"))
                run.phase(inner, stamp / 1000 if stamp is not None else None)
        elif event.get("protocol_id") == "eyes":
            if kind in ("protocol_complete", "protocol_aborted"):
                run.completed = kind == "protocol_complete"
                run.ended = True
            elif kind.startswith("protocol_phase_"):
                run.phase({**event, "type": kind.removeprefix("protocol_")}, _number(event.get("ts_unix")))
    pairs = []
    for run_id, run in runs.items():
        if not run.completed or first is None or last is None:
            continue
        for index, a in sorted(run.blocks.items()):
            b = run.blocks.get(index + 1)
            if (index % 2 == 0 and a and b and a[0] != b[0]
                    and first <= a[1] < a[2] <= b[1] < b[2] <= last):
                pairs.append((run_id, index, run.clock, a, b))
    return len(runs), pairs


def _covered_pairs(report, times, pairs, channels, indices, packet_clock):
    """Locate nonoverlapping usable source windows on the *actual* host clock."""
    coverage = [
        {"run_id": run_id, "phase_indices": [index, index + 1], "clock": clock, "channel_windows": {}}
        for run_id, index, clock, _, _ in pairs
    ]
    diagnostics = {"channel_windows_in_phase": 0, "channel_windows_passing_signal_qc": 0,
                   "signal_qc_windows_with_host_time_issue": 0, "sdk_supported_channel_windows": 0}
    if times is None or not pairs or not channels:
        return coverage, None
    eeg = report["eeg"]
    rate = eeg["sample_rate_hz"]
    config = report["configuration"]
    width = round(config["epoch_s"] * rate)
    hop = round(config["hop_s"] * rate)
    times = np.asarray(times, dtype=float)
    if times.ndim != 1 or width < 2 or hop < 1 or len(times) < width:
        return coverage, None
    if indices is not None:
        indices = np.asarray(indices)
        if indices.shape != times.shape:
            indices = None
    # Count bad transitions once across the stream. A bad transition before a
    # window does not invalidate a subsequent continuous stretch.
    delta = np.diff(times)
    bad = ~np.isfinite(delta) | (delta <= 0) | (delta > max(.025, 2.5 / rate))
    preceding_bad = np.concatenate(([0], np.cumsum(bad)))
    starts = np.arange(0, len(times) - width + 1, hop)
    ends = starts + width - 1
    steady = preceding_bad[ends] == preceding_bad[starts]
    for channel in channels:
        timeline = channel["timeline"]
        if len(timeline) != len(starts):
            continue
        usable = np.fromiter((w.get("usable") is True for w in timeline), dtype=bool, count=len(starts))
        for item, (_, _, clock, first, second) in zip(coverage, pairs):
            if clock == "browser_epoch_unverified":
                continue
            counts = {}
            for label, begin, end in (first, second):
                # Consider windows with both endpoints inside the phase, then
                # inspect all intervening samples. Clock failures are visible
                # separately from signal failures, even on legacy recordings.
                inside = ((times[starts] >= begin + ONSET_MARGIN_S) & (times[starts] <= end - END_MARGIN_S)
                          & (times[ends] >= begin + ONSET_MARGIN_S) & (times[ends] <= end - END_MARGIN_S))
                screened = inside & usable
                diagnostics["channel_windows_in_phase"] += int(inside.sum())
                diagnostics["channel_windows_passing_signal_qc"] += int(screened.sum())
                diagnostics["signal_qc_windows_with_host_time_issue"] += int((screened & ~steady).sum())
                sdk_ok = np.zeros(len(starts), dtype=bool)
                candidates = np.flatnonzero(screened & ~steady)
                if len(candidates) and end - begin >= MIN_SDK_BLOCK_S and indices is not None:
                    if callable(packet_clock):
                        packet_clock = packet_clock()
                    if packet_clock:
                        for j in candidates:
                            section = slice(starts[j], ends[j] + 1)
                            sdk_ok[j] = sdk_supports_alpha_block(times[section], indices[section], rate,
                                                                 begin, end, packet_clock)
                diagnostics["sdk_supported_channel_windows"] += int(sdk_ok.sum())
                chosen = _disjoint_windows(np.flatnonzero((screened & steady) | sdk_ok), starts, width)
                counts[label] = len(chosen)
                modes = item.setdefault("timing_by_channel", {}).setdefault(channel["name"], {})
                modes[label] = {"strict_host": sum(not sdk_ok[j] for j in chosen),
                                "sdk_continuity_host_block": sum(bool(sdk_ok[j]) for j in chosen)}
            item["channel_windows"][channel["name"]] = counts
    return coverage, diagnostics


def _disjoint_windows(candidates, starts, width):
    previous_end = -1
    chosen = []
    for index in candidates:
        if starts[index] > previous_end:
            chosen.append(index)
            previous_end = starts[index] + width - 1
    return chosen


def assess_research_readiness(report: dict, *, eeg_timestamps=None, eeg_indices=None, packet_clock=None) -> dict:
    """Summarize signal, timed labels and independent validation separately."""
    session = report["session"]
    eeg = report["eeg"]
    audit = session["streams"]["eeg"]
    minimum = report["configuration"]["minimum_spectral_windows"]
    channels = [ch for ch in eeg["channels"]
                if ch["quality"]["usable_windows"] >= minimum
                and ch["spectral_interpretability"] != "severely_limited"]
    occipital = [ch for ch in channels if EYE_SITES.intersection(
        site.strip() for site in ch["name"].upper().replace("–", "-").replace("—", "-").split("-"))]
    runs, pairs = _paired_blocks(session.get("events", []), audit.get("first_timestamp"),
                                 audit.get("last_timestamp"))
    coverage, window_counts = _covered_pairs(report, eeg_timestamps, pairs, occipital, eeg_indices, packet_clock)
    covered = sum(any(min(counts.values()) >= 3 for counts in item["channel_windows"].values())
                  for item in coverage)
    if not eeg["available"]:
        status = "no_eeg"
        detail = "ЭЭГ для сопоставления с заданиями недоступна."
        action = "Проверьте появление отсчётов ЭЭГ в пробной записи и затем запустите протокол «Альфа-ритм»."
    elif not runs:
        status = "no_protocol"
        detail = "Журнал не содержит запуска протокола «Альфа-ритм» или «Глаза открыты / закрыты». Одиночная отметка не задаёт границы блока."
        action = "Запишите протокол «Альфа-ритм» при стабильном контакте. Завершите обе фазы каждой пары."
    elif not pairs:
        status = "incomplete"
        detail = "В завершённых запусках нет полной пары последовательных фаз внутри временных границ ЭЭГ."
        action = "Повторите протокол до завершения и проверьте, что в журнале обеих фаз появились начало и конец."
    elif not covered:
        status = "insufficient_signal"
        detail = "В завершённых парах недостаточно пригодных окон одного затылочного отведения в обеих фазах."
        action = "Проверьте контакт и спектр по каждому отведению. Повторите размеченные блоки при стабильном сигнале."
        if all(item["clock"] == "browser_epoch_unverified" for item in coverage):
            detail = "Есть полные пары инструкций, но журнал не подтверждает сопоставление часов браузера и сервера."
            action = "Запустите протокол «Альфа-ритм» через лабораторию и сохраните оценку смещения часов и времени отклика."
        elif not occipital:
            detail += " Ни одно отведение O1/O2 не прошло общий спектральный отбор для записи."
        elif window_counts is None:
            detail += " Не хватает временных меток отсчётов для проверки окон."
        else:
            total = window_counts["channel_windows_in_phase"]
            passed = window_counts["channel_windows_passing_signal_qc"]
            clock_issues = window_counts["signal_qc_windows_with_host_time_issue"] - window_counts["sdk_supported_channel_windows"]
            detail += (f" Внутри фаз рассмотрено {total} перекрывающихся окон по отведениям. "
                       f"Скрининг сигнала прошли {passed}, из них {clock_issues} исключены по времени.")
            if clock_issues:
                action = ("Проверьте метки приёма пакетов и непрерывность индексов. "
                          "Скачок часов компьютера может исключить целое окно при сохранной форме ЭЭГ.")
            elif total == 0:
                action = "Проверьте совпадение часов ЭЭГ и журнала. Удлините фазы, оставив запас после смены инструкции."
    else:
        status = "reviewable"
        detail = f"Пары для описательного сравнения: {covered} из {len(pairs)}. В каждой фазе есть минимум три неперекрывающихся окна одного и того же затылочного отведения, прошедших скрининг."
        if window_counts["sdk_supported_channel_windows"]:
            detail += " Для части окон принадлежность длинной фазе установлена по границам времени приёма и непрерывным пакетам SDK."
        action = "Сравните мощность 8–13 Гц в парных блоках, укажите возможные артефакты и повторите протокол в другой день."
    if status in {"no_protocol", "incomplete"} and not occipital:
        action = ("Сначала проверьте наличие затылочного отведения, качество контакта и спектр "
                  "короткой пробной записи. Затем запишите обе фазы протокола «Альфа-ритм».")
    return {
        "spectral_channels": [ch["name"] for ch in channels],
        "alpha": {"status": status, "detail": detail, "runs": runs, "paired_blocks": len(pairs),
                  "pairs_with_coverage": covered, "pair_coverage": coverage, "window_counts": window_counts,
                  "method": "В каждой фазе расчёт начинается через 3 с после её начала и заканчивается за 1 с до конца. "
                            "До отбора неперекрывающихся окон счётчики суммируют окна по фазам и отведениям. "
                            "Основной критерий требует возрастающих меток приёма с шагом не более max(25 мс, 2,5 периода отсчёта). "
                            "Только для альфа-блоков длительностью от 30 с допускается второй путь. Все метки окна должны оставаться внутри блока после отступов, "
                            "индексы и пакеты SDK должны покрывать окно непрерывно, а размах отклонения времени приёма от хода индекса не должен превышать 0,25 с. "
                            "Это инженерные пороги для исследования длительных состояний. Время реакции ими не определяется. "
                            "Проверка использует оконный скрининг этой записи. В лаборатории экспериментов мощность рассчитывается отдельно по сырым участкам с проверками контакта и движения."},
        "clock": "Для серверного протокола берётся время границ на сервере, для лабораторного — метки браузера (с поправкой часов, когда она доступна). "
                 "ЭЭГ привязана к приёму пакетов. Метки описывают инструкцию. Фактическое состояние глаз и аппаратная синхронизация не подтверждены.",
        "decoder": {"status": "untested", "detail": "Оценка декодера требует размеченных команд и покоя, целого отложенного дня и учёта ложных команд. По одной записи точность переноса неизвестна."},
        "next_action": action,
    }

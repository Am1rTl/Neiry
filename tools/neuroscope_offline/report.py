"""Static Russian-language report for the offline EEG audit."""

from __future__ import annotations

from collections import Counter, defaultdict
from html import escape
from pathlib import Path, PurePosixPath

from . import spectra
from .inputs import HOST_GAP_PERIODS


CSS = """
:root{color-scheme:light;--ink:#192832;--muted:#465b67;--accent:#175861;
      --line:#d3dfe4;--paper:#fff;--soft:#f1f6f7;--warning:#6d4621}
*{box-sizing:border-box}
html{scroll-margin-top:1rem}
body{margin:0;background:#f6f9fa;color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main,.head,footer{max-width:1240px;margin:auto;padding:0 clamp(1rem,4vw,3.2rem)}
header{background:#12323e;color:#fff;padding:2.75rem 0 1.65rem;border-bottom:4px solid #5ba2a1}
header p{color:#d8e8ed;max-width:65ch}
h1,h2,h3,h4,p{margin:0 0 .75rem}
h1{font-size:clamp(2.15rem,5vw,3.6rem);line-height:1.12;letter-spacing:-.04em}
h2{font-size:clamp(1.55rem,3vw,2.05rem);line-height:1.18;letter-spacing:-.025em}
h3{font-size:1.18rem}
h4{font-size:1.05rem}
nav{display:flex;gap:.5rem 1.3rem;flex-wrap:wrap;margin-top:1.35rem}
nav a{color:#d8f1f2}
a{color:var(--accent);text-underline-offset:.18em}
a:hover{text-decoration-thickness:.12em}
a:focus-visible,summary:focus-visible{outline:2px solid #be6a37;outline-offset:3px}
section{padding:2rem 0;border-bottom:1px solid var(--line)}
section>p,li{max-width:90ch}
ul,ol{padding-left:1.3rem;margin:.55rem 0 .9rem}
li+li{margin-top:.28rem}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.7rem;margin:0 0 1.45rem}
.cards>div{border:1px solid var(--line);background:var(--paper);padding:.85rem 1rem;border-radius:.55rem}
.cards dt{color:var(--muted);font-size:.9rem}
.cards dd{font-size:1.6rem;font-weight:720;margin:0;font-variant-numeric:tabular-nums}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:.45rem;margin:.65rem 0 1.15rem}
table{border-collapse:collapse;width:100%;font-size:.9rem;background:var(--paper)}
th,td{text-align:left;vertical-align:top;padding:.55rem .72rem;border-bottom:1px solid var(--line)}
thead th{background:#e7eff1;line-height:1.3}
tbody tr:last-child td{border-bottom:0}
tbody tr:nth-child(even) td{background:#f8fbfc}
td{font-variant-numeric:tabular-nums}
td:first-child{font-weight:600}
.id{white-space:nowrap;font-size:.83rem}
.note{color:var(--muted);font-size:.92rem}
.warning{padding:.65rem .9rem;border-left:4px solid #b77b45;background:#faf3eb;color:var(--warning)}
details{background:var(--paper);margin:.55rem 0;border:1px solid var(--line);border-radius:.48rem}
details:target,article:target>details{outline:2px solid #5ba2a1;outline-offset:2px}
summary{cursor:pointer;padding:.76rem .95rem;font-weight:670;overflow-wrap:anywhere}
summary span{color:var(--muted);font-weight:400;margin-left:.45rem}
.detail{padding:0 .95rem .65rem}
.leads{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1.25rem;margin-top:1.15rem}
.lead{min-width:0}
figure{margin:.8rem 0}
figure img{width:100%;height:auto;border:1px solid var(--line);display:block}
figcaption{font-size:.84rem;color:var(--muted);margin-top:.3rem}
.method{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem 2rem}
.method>div{min-width:0}
.downloads{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.75rem 1.4rem}
code{font-size:.9em;overflow-wrap:anywhere}
pre{overflow:auto;padding:1rem;background:#e7eff1;border-radius:.45rem}
footer{padding-top:1.2rem;padding-bottom:1.2rem;color:var(--muted);font-size:.84rem}
@media(max-width:800px){.cards,.downloads{grid-template-columns:repeat(2,minmax(0,1fr))}.leads{grid-template-columns:1fr}}
@media(max-width:540px){.method,.downloads{grid-template-columns:1fr}.cards dd{font-size:1.28rem}}
@media print{body{background:white}header{background:white;color:black;padding:1rem 0}header p{color:black}nav{display:none}}
"""


BAND_NAMES = {
    "delta": "δ", "theta": "θ", "alpha": "α", "beta": "β", "gamma": "низкая γ",
}
STAGES = {"before": "До", "after": "После"}
REASONS = {
    "recorder_index_discontinuity": "разрыв индекса записи",
    "sdk_timing_discontinuity": "разрыв временной шкалы SDK",
    "nonfinite_samples": "нечисловые значения",
    "flatline": "постоянный сигнал",
    "few_discrete_levels": "мало различных значений",
    "repeated_rail_0p8": "повторение уровня ±0,8 SDK-u",
    "dominant_48_52_hz": "преобладание 48–52 Гц",
    "amplitude_outlier": "выброс RMS сигнала",
    "difference_outlier": "выброс RMS разностей",
}
MODELS = {
    "majority_baseline": "Самый частый класс",
    "nearest_centroid": "Ближайший центроид",
    "ridge_classifier": "Линейный Ridge",
}


STATUS = {
    "closed": "закрыт", "end_unconfirmed": "конец не подтверждён",
    "present": "доступен", "missing": "отсутствует", "unreadable": "не читается",
    "data": "есть строки", "header_only": "только заголовок", "empty": "пустой",
    "malformed_header": "некорректный заголовок",
}


def _status(value):
    return _t(STATUS.get(value, value))


def _t(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        return format(value, ".6g")
    return escape(str(value), quote=True)


def _pct(value):
    return "—" if value is None else f"{100 * value:.2f} %"


def _table(headers, rows):
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    if not body:
        return '<p class="note">Строк нет.</p>'
    title = "".join(f'<th scope="col">{escape(header)}</th>' for header in headers)
    return f'<div class="table-wrap"><table><thead><tr>{title}</tr></thead><tbody>{body}</tbody></table></div>'


def _list(items):
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>" if items else ""


def _link(filename, text=None):
    return f'<a href="{escape(filename, quote=True)}">{escape(text or filename)}</a>'


def _groups(rows, key):
    combined = defaultdict(list)
    for row in rows:
        combined[row.get(key)].append(row)
    return combined


def _inventory(data):
    inventory = data.get("inventory", {})
    extras = inventory.get("extras", inventory)
    archive = extras.get("archive", {})
    lab = extras.get("experiment_lab", {})
    items = []
    if archive.get("error"):
        items.append(f"Проверка records.zip завершилась ошибкой. {_t(archive['error'])}")
    elif archive.get("present"):
        matches, unmatched = archive.get("matching_files"), archive.get("unmatched_files")
        items.append(
            f"Архив records.zip сверён с каталогами сеансов по размеру и CRC32. "
            f"Совпало {_t(matches)} файлов, без совпадения {_t(unmatched)}. "
            "В расчётах использованы каталоги сеансов."
        )
    if lab.get("error"):
        items.append(f"Не удалось прочитать сводку базы экспериментов. {_t(lab['error'])}")
    elif lab.get("present"):
        runs = lab.get("runs", [])
        status_counts = Counter(str(run.get("status") or "unknown") for run in runs)
        status_list = ", ".join(f"{_t(status)} — {_t(n)}" for status, n in sorted(status_counts.items()))
        items.append(
            f"База «Эксперименты» содержит {_t(len(runs))} запусков и "
            f"{_t(lab.get('event_rows'))} событий. "
            + (f"Статусы запусков в базе — {status_list}. " if status_list else "")
            + "Основание для привязки условий к ЭЭГ берётся из событий каждого сеанса."
        )
    return _list(items)


def _catalog(sessions):
    headers = (
        "Сеанс", "Начало", "Статус записи", "Итоговый файл", "Частота ЭЭГ, Гц",
        "Строки ЭЭГ", "Номинально, с", "События", "Интервалы для оценки",
    )
    rows = []
    for index, item in enumerate(sessions, 1):
        name = f'<a class="id" href="#session-{index}">{_t(item.get("session"))}</a>'
        rows.append((
            name, _t(item.get("started_at")), _status(item.get("acquired_status")),
            _status(item.get("summary_status")), *(_t(item.get(key)) for key in (
                "eeg_hz", "eeg_rows", "eeg_nominal_s", "event_rows", "eligible_condition_rows",
            )),
        ))
    return _table(headers, rows)


def _stream_table(rows):
    fields = (
        ("Поток", "stream"), ("Файл", "status"), ("Частота, Гц", "nominal_rate_hz"),
        ("Строки", "rows"), ("Счётчик", "reported_rows"), ("Расхождение", "count_delta"),
        ("Некорректные", "malformed_rows"), ("Без метки времени", "invalid_ts"),
        ("Пропуск по индексу", "index_missing"), ("Обратные / равные шаги хоста", "host_nonincreasing"),
        ("Длинные шаги хоста", "host_long_gaps"),
    )
    active = [(header, key) for header, key in fields if any(key in row for row in rows)]
    return _table(
        [header for header, _ in active],
        ([
            _status(row.get(key)) if key == "status" else
            _t(row.get("rows_without_unix_time") if key == "invalid_ts" and key not in row else row.get(key))
            for _, key in active
        ] for row in rows),
    )


def _lead(item, rows):
    name = _t(item["channel"])
    after = item["after_windows"]
    required = item["after_minimum_windows"]
    window_stats = (
        f'<p>{_t(item["before_windows"])} окон до скрининга, {_t(after)} после '
        f'из {_t(item["window_count"])} кандидатов. Минимум для среднего после скрининга — {_t(required)}.</p>'
    )
    indicators = (
        f'<p class="note">Медиана числа различных значений в окне — {_t(item.get("median_discrete_levels"))}. '
        f'Доля соседних равных значений — {_pct(item.get("adjacent_equal_fraction"))}. '
        f'Доля 48–52 Гц в полосе 1–100 Гц — {_pct(item.get("before_line_fraction"))} до и '
        f'{_pct(item.get("after_line_fraction"))} после.</p>'
    )
    reasons = ", ".join(
        f"{escape(REASONS.get(reason, reason))} ({_t(count)})"
        for reason, count in sorted(item["reasons"].items())
    )
    reason_line = f'<p class="note">Сработавшие правила — {reasons}.</p>' if reasons else ""
    plot = item.get("plot")
    figure = ""
    if plot:
        path = PurePosixPath(plot)
        if len(path.parts) == 2 and path.parts[0] == "figures" and path.suffix == ".png":
            source = escape(path.as_posix(), quote=True)
            basis = "индексы отсчётов" if item.get("plot_axis") == "sample_index" else "порядок строк"
            figure = (
                f'<figure><a href="{source}"><img src="{source}" loading="lazy" '
                f'alt="Средняя PSD и спектрограммы до и после скрининга для {name}"></a>'
                f'<figcaption>Ось времени номинальная, по {basis}. Цветовая шкала общая '
                "для двух спектрограмм этого отведения.</figcaption></figure>"
            )
    band_rows = (
        (
            _t(STAGES.get(row["stage"], row["stage"])),
            f'{_t(BAND_NAMES.get(row["band"], row["band"]))} '
            f'{_t(row.get("low_hz"))}–{_t(row.get("high_hz"))}',
            _t(row.get("absolute_power")),
            _pct(row.get("relative_power")),
        )
        for row in rows
    )
    unit = rows[0].get("absolute_unit") if rows else None
    table = _table(("Этап", "Полоса, Гц", f"Мощность, {unit}" if unit else "Мощность", "Доля"), band_rows)
    return f'<div class="lead"><h4>{name}</h4>{window_stats}{indicators}{reason_line}{figure}{table}</div>'


def _session(index, item, streams, quality, bands, show):
    name = _t(item["session"])
    source = item.get("source")
    source = source if isinstance(source, dict) else {}
    issue_count = sum(item.get("sdk_timing_issues", {}).values())
    info = (
        f'<p>Начало {_t(item.get("started_at"))}, конец {_t(item.get("ended_at"))}. '
        f'День группы {_t(item.get("date_group"))}. Отведения {_t(item.get("eeg_channels"))}. '
        f'Номинальная частота {_t(item.get("eeg_hz"))} Гц. Единица в метаданных '
        f'<code>{_t(item.get("eeg_unit_raw"))}</code>.</p>'
        f'<p class="note">Версия SDK {_t(source.get("capsule_version"))}. Версия прошивки '
        f'{_t(source.get("firmware_version"))}. '
        f'Границы пакетов SDK {_t(item.get("sdk_timing_packets"))}, отмеченные аномалии {_t(issue_count)}.</p>'
        f'<p class="note">События {_t(item.get("event_rows"))}. Аннотации с условием '
        f'{_t(item.get("condition_rows"))}. Интервалы, допущенные к оценке '
        f'{_t(item.get("eligible_condition_rows"))}.</p>'
    )
    issues = _list([_t(issue) for issue in item.get("issues", [])])
    by_channel = _groups(bands, "channel")
    leads = "".join(_lead(row, by_channel[row["channel"]]) for row in quality)
    spectral_detail = (
        f'<div class="leads">{leads}</div>' if leads else
        '<p class="note">Спектральные окна для этого сеанса недоступны.</p>'
    )
    expanded = " open" if show else ""
    return (
        f'<article id="session-{index}"><details{expanded}><summary>{name}'
        f'<span>{_t(item.get("eeg_rows"))} строк ЭЭГ</span></summary>'
        f'<div class="detail">{info}{issues}'
        f'<h3>Полнота потоков</h3>{_stream_table(streams)}'
        f'<h3>Качество и спектры</h3>{spectral_detail}</div></details></article>'
    )


def _classification(classifiers):
    if not classifiers:
        return '<p class="warning">Нет сопоставимых размеченных серий для оценки классификаторов.</p>'
    sections = []
    for cohort, result in classifiers:
        limits = _list([_t(item) for item in result.get("limitations", ())])
        count_rows = (
            (_t(label), _t(counts["labeled_groups"]), _t(counts["usable_groups"]), _t(counts["usable_windows"]))
            for label, counts in result["condition_counts"].items()
        )
        coverage = _table(("Условие", "Размеченные группы", "Пригодные группы", "Пригодные окна"), count_rows)
        if result["status"] == "evaluated":
            rows = result["metrics"]
            metrics = _table(
                ("Модель", "Окна", "Accuracy", "Balanced accuracy", "Macro F1",
                 "Средняя accuracy по сеансам", "Средняя accuracy по группам"),
                ((
                    _t(MODELS.get(row["model"], row["model"])), _t(row["windows"]),
                    _pct(row["accuracy"]), _pct(row["balanced_accuracy"]), _pct(row["macro_f1"]),
                    _pct(row["mean_session_accuracy"]), _pct(row["mean_group_accuracy"]),
                ) for row in rows),
            )
            per_session = _table(
                ("Сеанс", "Группа", "Модель", "Окна", "Условия", "Accuracy", "Balanced accuracy", "Macro F1"),
                ((
                    _t(row["session_id"]), _t(row["group_id"]), _t(MODELS.get(row["model"], row["model"])),
                    _t(row["windows"]), _t(row["conditions"]), _pct(row["accuracy"]),
                    _pct(row["balanced_accuracy"]), _pct(row["macro_f1"]),
                ) for row in result["per_session"]),
            )
            errors_link = _link("classification_errors.csv", "Ошибки с интервалами и источниками меток")
            confusion_link = _link("classification_confusion.csv", "Матрицы ошибок")
            outcomes = (
                f"{metrics}<details><summary>Результаты по сеансам</summary>"
                f'<div class="detail">{per_session}</div></details>'
                f"<p>{errors_link} · {confusion_link}</p>"
            )
        else:
            outcomes = f'<p class="warning">{_t(result["reason"])}</p>'
        sections.append(
            f'<h3>{_t(cohort or "Доступные записи")}</h3>{outcomes}'
            f'{coverage}{limits}'
        )
    return "".join(sections)


def _annotations(rows):
    by_kind = Counter(row.get("kind") or "unknown" for row in rows)
    known = Counter(row.get("kind") or "unknown" for row in rows if row.get("condition"))
    eligible = Counter(
        row.get("kind") or "unknown" for row in rows if row.get("classifier_eligible")
    )
    return _table(
        ("Тип записи", "Всего", "С наблюдаемым условием", "Подходит для оценки"),
        ((_t(kind), _t(n), _t(known[kind]), _t(eligible[kind])) for kind, n in sorted(by_kind.items())),
    )


def _methods():
    config = spectra.CONFIG
    ranges = ", ".join(
        f"{BAND_NAMES[name]} {low:g}–{high:g}" for name, (low, high) in spectra.BANDS.items()
    )
    return f"""
    <div class="method">
      <div>
        <h3>Сигнал, монтаж и время</h3>
        <p>Сеансы читаются из каталогов рекордера. Частота берётся из <code>meta.json</code>,
        отведения сверяются с заголовком <code>eeg.csv</code>. O1−T3 и O2−T4 обозначают
        биполярные разности потенциалов, по которым нельзя локализовать источник активности.</p>
        <p><code>server.py::on_eeg</code> получает <code>get_raw_value</code> из SDK,
        а <code>recording.py::SessionRecorder</code> сохраняет эти значения без
        программной фильтрации на пути записи.
        Аппаратная и внутренняя фильтрация SDK для этих файлов неизвестна. Обозначение SDK-u
        относится к исходной шкале без подтверждённого пересчёта в вольты. Спектры записаны в
        квадрате исходной единицы на Гц, абсолютная мощность в квадрате исходной единицы.
        Преобразование в физические единицы не выполняется.</p>
        <p><code>ts_unix</code> описывает время приёма пакета на хосте с обратным распределением
        отсчётов по номинальной частоте. <code>ts_iso</code> представляет ту же шкалу. Обратные,
        равные и длинные шаги отражают в том числе джиттер доставки. Длинным считается
        шаг более {HOST_GAP_PERIODS} номинальных периодов при известной частоте. По этим шагам нельзя
        посчитать потерю ЭЭГ. <code>sample_index</code> нумерует записанные отсчёты, поэтому
        его непрерывность не выявляет пакеты, которые не дошли до рекордера. Когда есть
        <code>eeg_batch_timing</code>, временные метки SDK дают отдельные наблюдаемые признаки
        разрыва, скачка часов или несогласованной длины пакета.</p>
      </div>
      <div>
        <h3>Спектр и мощность</h3>
        <p>Окна по {config.epoch_s:g} с идут с шагом {config.hop_s:g} с в порядке отсчётов. Для каждого окна
        удаляется линейный тренд, применяется окно Ханна и вычисляется односторонняя
        периодограмма с нормировкой плотности PSD. Спектрограммы показывают PSD в центре
        окон как 10·log₁₀(PSD / 1 единицу PSD). Для обеих панелей отведения общая
        цветовая шкала определяется 5-м и 99-м процентилями конечных значений до скрининга,
        с шириной шкалы не менее 1 дБ.</p>
        <p>До скрининга в расчёт входят конечные окна без разрыва индекса и без отмеченного
        разрыва или испорченного интервала времени SDK. После скрининга остаются окна,
        прошедшие правила качества. PSD усредняется по окнам арифметически в линейной
        шкале. Для среднего после скрининга требуется минимум {config.minimum_spectral_windows} окон.
        Отдельные пригодные окна видны на спектрограмме при меньшем числе. Перекрывающиеся
        окна считаются по отдельности. Пропуски
        оставлены пустыми. В обоих расчётах отсутствуют интерполяция отсчётов и дополнение
        окна нулями.</p>
        <p>Абсолютная мощность равна интегралу средней PSD по полосе методом трапеций.
        Границы интеграла между частотными бинами линейно интерполируются. Относительная
        мощность равна этому интегралу, делённому на интеграл по 1–45 Гц, при положительном
        знаменателе. Полосы в Гц — {ranges}. Графики ограничены 100 Гц либо частотой Найквиста,
        если она ниже.</p>
      </div>
      <div>
        <h3>Правила качества</h3>
        <p>Отдельно отмечаются постоянные окна, окна с менее чем {spectra.MIN_LEVELS} различными
        значениями, доля повторов ±{spectra.RAIL_VALUE:g} SDK-u свыше {_pct(spectra.RAIL_FRACTION)}
        и доля 48–52 Гц свыше {_pct(config.line_dominance_fraction)} относительно
        мощности 1–100 Гц, когда обе полосы доступны. Повтор ±{spectra.RAIL_VALUE:g} служит
        признаком плато в данных с неизвестным масштабом. Физический порог насыщения
        оборудования по нему не устанавливается.</p>
        <p>Описательный скрининг также отмечает выбросы двух метрик на каждом отведении в
        пределах сеанса. Метрики — RMS после удаления линейного тренда и RMS соседних
        разностей. Верхняя граница каждой равна большему из медианы плюс
        {config.robust_z:g} × 1,4826 × MAD и {config.minimum_power_ratio_outlier:g} медиан.
        Эти подогнанные по сеансу границы используются только для описания и графиков.
        Синхронное исключение по MEMS и сопротивлению контакта не применяется, поскольку
        общей проверенной шкалы этих потоков и ЭЭГ нет.</p>
      </div>
      <div>
        <h3>Разметка и проверка моделей</h3>
        <p>События оператора и SDK сохраняются как наблюдаемые события. Точечные
        маркеры и калибровка сами по себе не задают интервал условия. Для классификации
        нужны согласованные границы фазы, известное условие и подходящий протокол.
        Пустое условие означает, что оно неизвестно. В аннотациях указаны метод,
        версия, основание и координаты исходных данных.</p>
        <p>Границы по времени хоста ищутся среди отсчётов в пределах ±0,1 с. Для
        браузерных событий оценка времени клиента сверяется со временем приёма
        сервером с допуском 1 с. Приблизительное сопоставление оставляет внутренний
        отступ в 1 с с каждого края фазы или в 0,25 с для подтверждённой 5-секундной
        фазы задачи «Виртуальная дверь». Аппаратная синхронизация не измерялась.</p>
        <p>Целые группы определяются до нарезки окон. Сеансы одного локального дня
        образуют группу. Если сеанс проходит через полночь, его даты объединяются.
        Группа целиком находится в одной части каждого разбиения.
        Для моделей извлекаются полные неперекрывающиеся окна по {config.epoch_s:g} с внутри
        проверенной фазы. Признаки — доли мощности пяти полос в каждом отведении.
        Применяются фиксированные локальные правила качества. Все параметры нормализации
        оцениваются на обучающих окнах, регуляризация фиксирована заранее.</p>
      </div>
    </div>
    """


def _next_experiment():
    return """
    <ol>
      <li>Начинайте каждую запись до протокола и завершайте через интерфейс, чтобы сохранить
      итоговые счётчики. Сохраните необработанный экспорт, метки начала и конца фаз, подтверждения
      показа, контакт, MEMS и журнал времени SDK. Проверьте устойчивый контакт и отсутствие выраженного
      плато или сетевой помехи по короткой контрольной записи.</li>
      <li>В пять или более разных дней проведите явный протокол покоя с открытыми и закрытыми
      глазами. В каждый день запишите не менее двух 60-секундных блоков каждого условия. Сохраняйте
      одинаковую длительность, число блоков, звуковые настройки, положение и окружение. Чередуйте
      начальное условие между прогонами, если протокол это поддерживает. Сохраняйте каждую границу
      блока и время на переход между состояниями.</li>
      <li>Выделите самостоятельный день для итоговой оценки. Сверьте наличие пригодных окон
      обоих условий в остальных днях. По журналу контакта и MEMS проверьте, сопоставимы ли
      условия по качеству записи и по движению.</li>
    </ol>
    """


def _downloads(command):
    groups = (
        ("Исходные данные и полнота", (
            ("input_manifest.json", "Снимок входов и SHA-256"), ("run.json", "Параметры и версии запуска"),
            ("sessions.csv", "Каталог сеансов"), ("streams.csv", "Полнота потоков"),
            ("stream_columns.csv", "Проверка полей потоков"), ("gaps.csv", "Разрывы и временные аномалии"),
            ("event_inventory.csv", "Типы и количество событий"),
        )),
        ("Качество и спектры", (
            ("quality_windows.csv", "Разметка окон и основания"), ("quality_summary.csv", "Сводка по отведениям"),
            ("condition_annotations.csv", "Разметка событий и фаз"),
            ("bandpower.csv", "Абсолютная и относительная мощность"), ("psd.csv", "Плотности мощности"),
        )),
        ("Оценка классификаторов", (
            ("classification.json", "Статусы, результаты и ограничения"),
            ("classification_label_coverage.csv", "Полнота меток"),
            ("classification_folds.csv", "Состав групп по разбиениям"),
            ("classification_metrics.csv", "Сводные показатели"),
            ("classification_per_session.csv", "Показатели по сеансам"),
            ("classification_confusion.csv", "Матрицы ошибок"),
            ("classification_predictions.csv", "Прогнозы по окнам"),
            ("classification_errors.csv", "Ошибочно классифицированные окна"),
        )),
    )
    columns = "".join(
        f"<div><h3>{title}</h3>{_list([_link(path, label) for path, label in links])}</div>"
        for title, links in groups
    )
    command_html = (
        f"<h3>Повторный запуск из корня проекта</h3><pre><code>{_t(command)}</code></pre>"
        if command else ""
    )
    return f'<div class="downloads">{columns}</div>{command_html}'


def render_report(path: Path, data: dict):
    """Write the report next to its relative tables and plots."""
    sessions = data["sessions"]
    streams = _groups(data["streams"], "session")
    quality = _groups(data["quality"], "session")
    bands = _groups(data["bands"], "session")
    source_groups = {row["date_group"] for row in sessions if row.get("date_group")}
    cards = (
        ("Сеансы", len(sessions)), ("Группы дат", len(source_groups)),
        ("Строки ЭЭГ", sum(row.get("eeg_rows") or 0 for row in sessions)),
        ("Интервалы для оценки", sum(row.get("eligible_condition_rows") or 0 for row in sessions)),
    )
    summary = "".join(f"<div><dt>{title}</dt><dd>{_t(value)}</dd></div>" for title, value in cards)
    observations = _list([_t(item) for item in data.get("observations", [])])
    featured = max(
        quality,
        key=lambda session: sum(item["after_windows"] for item in quality[session]),
        default=None,
    )
    session_details = "".join(
        _session(
            index, row, streams[row["session"]], quality[row["session"]], bands[row["session"]],
            show=row["session"] == featured,
        )
        for index, row in enumerate(sessions, 1)
    )
    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NeuroScope · Офлайн-анализ накопленных записей</title>
<style>{CSS}</style>
</head>
<body>
<header><div class="head">
<h1>NeuroScope · Офлайн-анализ</h1>
<p>Каталог накопленных записей, качество ЭЭГ, спектры и доступность меток условий.</p>
<nav aria-label="Разделы отчёта">
<a href="#catalog">Сеансы</a><a href="#quality">Качество и спектры</a><a href="#conditions">Разметка</a>
<a href="#classification">Классификация</a><a href="#method">Методы</a>
<a href="#next">Следующий эксперимент</a><a href="#files">Файлы</a>
</nav>
</div></header>
<main>
<section id="catalog">
<h2>Каталог сеансов</h2>
<dl class="cards">{summary}</dl>
{observations}{_inventory(data)}{_catalog(sessions)}
<p class="note">Прочерк означает, что значение недоступно.
Ссылки в первом столбце ведут к проверкам потоков и спектрам отдельных сеансов.</p>
</section>
<section id="quality">
<h2>Качество и спектры до и после скрининга</h2>
<p>Раскройте сеанс для сравнения мощностей, спектров и спектрограмм по отведениям.
Сеанс с наибольшим числом окон после скрининга раскрыт при открытии отчёта.
Одно окно может иметь несколько флагов. Разрывы, скачки времени хоста и свидетельства времени SDK
сохранены в отдельной таблице.</p>
{session_details}
</section>
<section id="conditions">
<h2>Разметка условий</h2>
{_annotations(data["conditions"])}
<p>Проверяемый журнал разметки содержит ссылки на номера строк исходных событий
и причины отклонения. Условия отражают записанную инструкцию или метку протокола.
Выполнение инструкции по этим меткам не измерялось.</p>
<p>{_link("condition_annotations.csv", "Открыть журнал аннотаций")}</p>
</section>
<section id="classification">
<h2>Сравнение классификаторов</h2>
{_classification(data["classifiers"])}
</section>
<section id="method">
<h2>Методы расчёта</h2>
{_methods()}
</section>
<section id="next">
<h2>Следующий эксперимент</h2>
{_next_experiment()}
</section>
<section id="files">
<h2>Таблицы и повторный запуск</h2>
{_downloads(data.get("command"))}
</section>
</main>
<footer>Отчёт рассчитан локально по зафиксированному снимку входных данных.</footer>
</body></html>
"""
    path.write_text(html, encoding="utf-8")

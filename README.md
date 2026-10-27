# Neiry — Capsule WebApp (совмещённая версия)

Веб-дашборд повязки Neiry на официальном Capsule SDK v2.0.72
(ЭЭГ, PPG, MEMS, эмоции, кардио, физиологические состояния, продуктивность,
NFB-ритмы, калибровка IAF) + запись всех показателей в `records/`,
перенесённая из BLE-версии Neiry Neon (`../program/neiry_web`).

Без SDK/повязки сервер тоже работает (API-only режим): отдаёт снапшот
последних данных, список записей, replay сессий и аналитику по буферам.

## Запуск

```bash
./start.sh        # http://127.0.0.1:8000  (страницы /, /charts, /more, /sessions)
./stop.sh
```

- Окружение: `WebApp/.venv` (создаётся автоматически, системный pip не используется).
  Зависимости ставятся из `requirements.txt`.
- Capsule SDK ищется сам рядом с проектом (`Capsule_and_API_*/.../CapsuleAPI/Linux`);
  можно указать вручную: `CAPSULE_SDK_DIR=/path/to/CapsuleAPI/Linux ./start.sh`.
- Сервер сам находит и подключает первую повязку, калибровка запускается из интерфейса.
- Хост/порт: `HOST=0.0.0.0 PORT=8000 ./start.sh`.

## Модули

| Модуль | Назначение |
|---|---|
| `server.py` | FastAPI-бэкенд: Capsule-поток, буферы, WS 10 Гц, REST API |
| `config.py` | Настройки из env (`Settings`), логирование, поиск SDK |
| `recording.py` | `SessionRecorder`: запись сессий в `records/` (CSV + JSONL) |
| `analytics.py` | `compute_psd` (спектр ЭЭГ, нужен numpy), `compute_hrv` (PPG) |
| `playback.py` | `PlaybackController`: replay сессии в живые буферы |
| `session_loader.py` | Чтение сессий из `records/` (stdlib, pandas опционален) |
| `edf_export.py` | Экспорт сессии в EDF (`pip install pyedflib`) |
| `examples/plot_session.py` | Сводка + график записанной сессии (нужен matplotlib) |
| `_sdk_stubs.py` | Заглушки Capsule SDK для тестов/импорта без железа |
| `diag.py` | Пошаговая проверка Capsule SDK и повязки без веба |
| `watch.py` | Консольный мониторинг WS-потока |
| `static/` | 4 страницы (`index`, `charts`, `more`, `sessions`) + `common.*` |

## Конфигурация (env, см. `config.py`)

| Переменная | Дефолт | Что значит |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | адрес uvicorn |
| `EEG_WINDOW` / `PPG_WINDOW` / `MEMS_WINDOW` / `RES_WINDOW` | `7500` / `3000` / `7500` / `256` | размеры кольцевых буферов (точек) |
| `HIST_LEN` | `256` | длина историй emotions/cardio/nfb |
| `LOST_TIMEOUT_S` | `3.0` | возраст данных, после которого устройство считается потерянным |
| `CAL_TIMEOUT_S` | `180` | таймаут ожидания ЭЭГ для калибровки |
| `RECORD_ENABLED` | `1` | автозапись сессий (ручной `/api/recording/start` работает всегда) |
| `RECORDS_DIR` | `records/` | каталог сессий (старое имя `NEIRY_RECORDS_DIR` тоже читается) |
| `MAX_SESSION_HOURS` / `MAX_CSV_MB` | `6` / `500` | ротация сессии по длительности/размеру `eeg.csv` |
| `SKIN_WARN_OHM` / `SKIN_BAD_OHM` | `500e3` / `1e6` | пороги импеданса кожи для `sensor_status` |
| `EEG_MIN_SAMPLES` / `PPG_MIN_SAMPLES` / `MEMS_MIN_SAMPLES` | `100` / `50` / `100` | сколько точек считать «есть сигнал» |
| `SNAPSHOT_INTERVAL_S` | `5` | период сохранения `state_snapshot.json` |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / `logs/webapp.log` | уровень и файл лога (ротация 5 МБ × 3) |

## API

Страницы: `/`, `/charts`, `/more`, `/sessions`. WebSocket: `/ws` (тик 10 Гц,
общий снапшот + пер-клиентские дельты `eeg_stream`/`ppg_stream`/`mems_stream`,
поля `data_age_s`/`stale`; команды `start_calibration`, `skip_calibration`,
`add_marker {label}`).

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/status` | статус подключения, устройство, калибровка |
| `GET` | `/api/health` | `ok`, uptime, свежесть данных, запись, replay |
| `GET` | `/api/buffers` | размеры буферов и последние значения |
| `GET` | `/api/records` | список сессий (новые первыми) |
| `GET` | `/api/records/{id}/download` | сессия одним zip |
| `DELETE` | `/api/records/{id}` | удалить сессию (`409`, если идёт запись) |
| `POST` | `/api/recording/start` / `/api/recording/stop` | ручное управление записью |
| `POST` | `/api/markers {label}` | маркер в события (`409`, если записи нет) |
| `GET` | `/api/spectrum?window_s=&fmax=` | спектр ЭЭГ по живому буферу |
| `GET` | `/api/hrv?window_s=` | HRV-метрики по PPG-буферу |
| `POST` | `/api/replay/{id}/start?speed=` | replay сессии (`409`, пока live-коннект со свежими данными) |
| `POST` | `/api/replay/stop` | остановить replay |
| `GET` | `/api/replay/status` | состояние replay |

## Запись в records/

Каждое подключение устройства = `records/session_<дата>_<serial>/`:

- `eeg.csv`, `ppg.csv`, `mems.csv`, `resistances.csv` — сырые потоки;
- `metrics.jsonl` — emotions / cardio / phy / productivity / nfb / battery;
- `calibration.json`, `events.jsonl`, `meta.json`, `summary.json`.

Длинные сессии ротируются автоматически (`MAX_SESSION_HOURS`/`MAX_CSV_MB`,
суффиксы `_p2`, `_p3`, …). Список сессий: `GET /api/records`.
В шапке интерфейса — индикатор `● REC`.

## Диагностика без веба

```bash
.venv/bin/python diag.py   # пошаговая проверка Capsule SDK и повязки
```

Тесты: `.venv/bin/python -m unittest test_recording test_connection test_protocol test_analytics -v`.

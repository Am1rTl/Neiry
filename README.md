# Capsule Neiry — Web Dashboard

Неоновый веб-дашборд, который подключается к гарнитуре Neiry через Bluetooth и
рисует графики в реальном времени: ЭЭГ, ФПГ (пульс), акселерометр/гироскоп,
эмоции, кардио, NFB (α/β/θ), сопротивление электродов, продуктивность и
физиологические состояния.

## Запуск

```bash
cd Linux/WebApp
./start.sh
# открыть в браузере: http://127.0.0.1:8000
# (НЕ localhost: на Kali 'localhost' резолвится в IPv6 ::1, а сервер слушает IPv4)
```

Перед запуском убедитесь, что:
- Собран `libCapsuleClient.so` (используйте `linux/build/build/CapsuleClientExample`,
  чтобы проверить, что всё работает).
- Гарнитура включена, в Bluetooth-зоне, сопряжена (`bluetoothctl`).

## Архитектура

```
   ┌─────────────────┐
   │ Headband (BLE)  │
   └────────┬────────┘
            │
   ┌────────▼────────┐
   │ libCapsuleClient│  (Neiry native lib)
   └────────┬────────┘
            │
   ┌────────▼────────┐
   │  server.py      │  FastAPI + WebSocket
   │  ├─ capsule thr │  читает колбэки Capsule API
   │  └─ asyncio     │  шлёт 10 Hz на фронтенд
   └────────┬────────┘
            │ ws://
   ┌────────▼────────┐
   │  index.html     │  Chart.js + CSS-неон
   └─────────────────┘
```

## Что показывается

| Панель | Источник | Частота |
| --- | --- | --- |
| EEG signal | `clCDevice_SetOnEEGDataEvent` | 250 Hz |
| PPG (фотоплетизмограмма) | `Cardio.set_on_ppg` | 100 Hz |
| MEMS (accel+gyro, 3 оси) | `MEMS.set_on_update` | 250 Hz |
| Cardio (HR, stress, Kaplan) | `Cardio.set_on_indexes_update` | ~1 Hz |
| Emotions (focus/chill/stress/anger/self-control) | `Emotions.set_on_states_update` | ~1 Hz |
| NFB (α/β/θ) | `NFB.set_on_user_state` (raw ctypes) | ~1 Hz |
| Resistances (T3/T4/O1/O2) | `clCDevice_SetOnResistanceUpdateEvent` | ~1 Hz |
| Productivity | `Productivity.set_on_metrics_update` | ~1 Hz |
| Physiological States | `PhysiologicalStates.set_on_states` | ~несколько минут |

## Управление

- Остановка: `Ctrl+C` в терминале.
- Повторный поиск устройства: перезапустить скрипт.

## Файлы

```
Linux/WebApp/
├── server.py        # FastAPI backend (Capsule wrapper + WS)
├── static/
│   └── index.html   # Cyberpunk dashboard (Chart.js via CDN)
├── start.sh         # Запуск с LD_LIBRARY_PATH
└── README.md
```

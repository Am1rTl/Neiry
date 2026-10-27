"""Централизованная конфигурация Capsule WebApp.

Все настройки сервера — из переменных окружения с разумными дефолтами.
Модуль намеренно не импортирует ни SDK, ни fastapi: его можно подключать
отовсюду (server.py, diag.py, тесты) без железа и веб-зависимостей.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    """Настройки из env (имена переменных — в from_env)."""

    HOST: str = "127.0.0.1"
    PORT: int = 8000
    EEG_WINDOW: int = 7500    # ~30 с при 250 Гц — запас под live+history
    PPG_WINDOW: int = 3000    # ~30 с при 100 Гц
    MEMS_WINDOW: int = 7500   # ~30 с при 250 Гц
    RES_WINDOW: int = 256
    HIST_LEN: int = 256       # emotions/cardio/nfb истории
    LOST_TIMEOUT_S: float = 3.0   # возраст данных, после которого устройство потеряно
    CAL_TIMEOUT_S: float = 180.0  # таймаут ожидания ЭЭГ для калибровки
    RECORD_ENABLED: bool = True   # автозапись сессий; ручной /api/recording/start работает всегда
    RECORDS_DIR: str = ""
    MAX_SESSION_HOURS: float = 6.0  # ротация сессии по длительности
    MAX_CSV_MB: float = 500.0       # ротация сессии по размеру eeg.csv
    SKIN_WARN_OHM: float = 500e3  # импеданс выше — канал «так себе»
    SKIN_BAD_OHM: float = 1e6     # импеданс выше — контакта нет
    EEG_MIN_SAMPLES: int = 100  # пороги sensor_status: сколько точек считать «есть сигнал»
    PPG_MIN_SAMPLES: int = 50
    MEMS_MIN_SAMPLES: int = 100
    SNAPSHOT_INTERVAL_S: float = 5.0
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        records_default = os.path.join(HERE, "records")
        return cls(
            HOST=_env_str("HOST", "127.0.0.1"),
            PORT=_env_int("PORT", 8000),
            EEG_WINDOW=_env_int("EEG_WINDOW", 7500),
            PPG_WINDOW=_env_int("PPG_WINDOW", 3000),
            MEMS_WINDOW=_env_int("MEMS_WINDOW", 7500),
            RES_WINDOW=_env_int("RES_WINDOW", 256),
            HIST_LEN=_env_int("HIST_LEN", 256),
            LOST_TIMEOUT_S=_env_float("LOST_TIMEOUT_S", 3.0),
            CAL_TIMEOUT_S=_env_float("CAL_TIMEOUT_S", 180.0),
            RECORD_ENABLED=_env_bool("RECORD_ENABLED", True),
            # RECORDS_DIR — новое имя; NEIRY_RECORDS_DIR — старое, для совместимости.
            RECORDS_DIR=(_env_str("RECORDS_DIR", "")
                         or _env_str("NEIRY_RECORDS_DIR", "") or records_default),
            MAX_SESSION_HOURS=_env_float("MAX_SESSION_HOURS", 6.0),
            MAX_CSV_MB=_env_float("MAX_CSV_MB", 500.0),
            SKIN_WARN_OHM=_env_float("SKIN_WARN_OHM", 500e3),
            SKIN_BAD_OHM=_env_float("SKIN_BAD_OHM", 1e6),
            EEG_MIN_SAMPLES=_env_int("EEG_MIN_SAMPLES", 100),
            PPG_MIN_SAMPLES=_env_int("PPG_MIN_SAMPLES", 50),
            MEMS_MIN_SAMPLES=_env_int("MEMS_MIN_SAMPLES", 100),
            SNAPSHOT_INTERVAL_S=_env_float("SNAPSHOT_INTERVAL_S", 5.0),
            LOG_LEVEL=_env_str("LOG_LEVEL", "INFO"),
            LOG_FILE=(_env_str("LOG_FILE", "")
                      or os.path.join(HERE, "logs", "webapp.log")),
        )


settings = Settings.from_env()


def setup_logging(name: str = "capsule") -> logging.Logger:
    """Логгер: консоль + RotatingFileHandler (5 МБ × 3). Идемпотентен."""
    log = logging.getLogger(name)
    if log.handlers:
        return log
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(threadName)s %(message)s",
                            datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)
        fh = RotatingFileHandler(settings.LOG_FILE, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        log.warning("cannot open log file %s — file logging disabled",
                    settings.LOG_FILE)
    log.propagate = False
    return log


def find_sdk_dir() -> str:
    """Каталог CapsuleAPI/Linux (внутри него: 'Capsule Python' и 'Lib').

    Раньше WebApp жил внутри поставки Capsule и SDK был просто родителем.
    После переезда репозитория в brain/ путь ищется: env CAPSULE_SDK_DIR ->
    соседние каталоги -> поиск поставки Capsule_and_API_* рядом с проектом.
    """
    env = os.environ.get("CAPSULE_SDK_DIR")
    candidates = []
    if env:
        candidates.append(env)
    brain = os.path.dirname(HERE)
    candidates += [
        os.path.join(brain, "CapsuleAPI", "Linux"),
        os.path.join(HERE, "CapsuleAPI", "Linux"),
        os.path.join(HERE, "sdk"),
    ]
    try:
        for entry in sorted(os.listdir(brain)):
            base = os.path.join(brain, entry)
            if not os.path.isdir(base):
                continue
            for root, dirs, _files in os.walk(base):
                if os.path.basename(root) == "Linux" and "Capsule Python" in dirs:
                    candidates.append(root)
                if root.count(os.sep) - base.count(os.sep) > 5:
                    dirs[:] = []
    except Exception:
        pass
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "Lib", "libCapsuleClient.so")):
            return cand
    return candidates[0] if candidates else ""

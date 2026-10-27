"""Пример анализа записанной сессии через session_loader.

Использование:
  .venv/bin/python examples/plot_session.py [путь_к_сессии]

Без аргумента берётся последняя сессия из records/. Текстовая сводка печатается
всегда; график ЭЭГ и сопротивлений сохраняется в session_overview.png
(для него нужен matplotlib).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_loader import list_sessions, load_session


def pick_session(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if not p.is_dir():
            raise SystemExit(f"нет такого каталога: {arg}")
        return p
    sessions = list_sessions(Path(__file__).resolve().parent.parent / "records")
    if not sessions:
        raise SystemExit("в records/ нет сессий")
    print(f"сессий: {len(sessions)}, беру последнюю: {sessions[-1].name}")
    return sessions[-1]


def describe(sess: dict) -> None:
    meta = sess.get("meta") or {}
    print(f"session_id: {meta.get('session_id', sess['path'])}")
    print(f"device: {meta.get('device')} serial: {meta.get('serial')}")
    print(f"channels: {meta.get('channels')}")
    for name in ("eeg", "ppg", "mems", "resistances"):
        stream = sess.get(name)
        if stream is None:
            print(f"{name}: нет данных")
            continue
        n = len(stream["ts"])
        dur = stream["ts"][-1] - stream["ts"][0] if n > 1 else 0.0
        print(f"{name}: {n} строк, {dur:.1f} c, колонки: {list(stream['columns'])}")
    print(f"metrics: {len(sess.get('metrics', []))} строк, "
          f"events: {len(sess.get('events', []))} строк")
    if sess.get("calibration"):
        print(f"calibration: {sess['calibration']}")


def plot(sess: dict, out: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("для графиков нужен matplotlib: pip install matplotlib",
              file=sys.stderr)
        raise SystemExit(1)
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    eeg = sess.get("eeg")
    if eeg is not None and eeg["columns"]:
        ts, cols = eeg["ts"], eeg["columns"]
        step = max(1, len(ts) // 5000)  # прореживаем длинные записи
        t0 = ts[0]
        for ch, vals in cols.items():
            axes[0].plot([t - t0 for t in ts[::step]], vals[::step], label=ch, linewidth=0.7)
        axes[0].legend(loc="upper right", fontsize=8)
    axes[0].set_title(f"EEG — {Path(sess['path']).name}")
    axes[0].set_ylabel("мкВ")
    res = sess.get("resistances")
    if res is not None and res["columns"]:
        t0 = res["ts"][0]
        for ch, vals in res["columns"].items():
            axes[1].plot([t - t0 for t in res["ts"]], vals, label=ch, marker=".", markersize=3)
        axes[1].legend(loc="upper right", fontsize=8)
    axes[1].set_title("Сопротивления")
    axes[1].set_xlabel("секунды от начала потока")
    axes[1].set_ylabel("кОм")
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    print(f"график: {out}")


def main(argv: list[str]) -> None:
    session_dir = pick_session(argv[1] if len(argv) > 1 else None)
    sess = load_session(session_dir)
    describe(sess)
    plot(sess, Path.cwd() / "session_overview.png")


if __name__ == "__main__":
    main(sys.argv)

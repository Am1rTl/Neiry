"""Exploratory classification of verified, session-relative condition intervals."""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import math

import numpy as np


MIN_GROUPS_PER_CONDITION = 3
MIN_WINDOWS_PER_CONDITION = 6
RIDGE_ALPHA = 1.0
BOUNDARY_TOLERANCE_S = 1e-7
PREDICTION_FIELDS = (
    "model", "group_id", "session_id", "start_s", "end_s", "label_source",
    "condition", "prediction", "correct",
)
TABLE_FIELDS = {
    "label_coverage": (
        "group_id", "session_id", "condition", "interval_count", "labeled_seconds",
        "usable_windows",
    ),
    "folds": (
        "held_out_group", "train_groups", "train_sessions", "test_sessions",
        "train_windows", "test_windows", "status",
    ),
    "metrics": (
        "model", "windows", "conditions", "accuracy", "balanced_accuracy", "macro_f1",
        "mean_session_accuracy", "mean_group_accuracy",
    ),
    "per_session": (
        "model", "group_id", "session_id", "windows", "conditions", "accuracy",
        "balanced_accuracy", "macro_f1",
    ),
    "confusion": ("model", "condition", "prediction", "windows"),
    "predictions": PREDICTION_FIELDS,
    "errors": PREDICTION_FIELDS,
}


@dataclass(frozen=True)
class ConditionInterval:
    session_id: str
    group_id: str
    condition: str | None
    start_s: float
    end_s: float
    source: str


@dataclass(frozen=True)
class FeatureWindow:
    interval: ConditionInterval
    start_s: float
    end_s: float
    features: Sequence[float]


def evaluate_conditions(
    intervals: Iterable[ConditionInterval],
    extract_features: Callable[[tuple[ConditionInterval, ...]], Iterable[FeatureWindow]],
) -> dict:
    """Fix group membership and folds before requesting any classification windows.

    Times are seconds relative to sample_index 0 with exclusive right bounds.
    The callback receives the verified intervals of one whole group at a time.
    It supplies only usable EEG windows and computes each feature locally in the
    window. The models learn feature normalization within each training fold.
    """
    intervals = tuple(intervals)
    known = [
        interval for interval in intervals
        if interval.condition is not None and interval.condition.strip().casefold()
        not in {"", "unknown"}
    ]
    result = {table: [] for table in TABLE_FIELDS}
    result.update(
        status="skipped",
        reason_code="no_verified_conditions",
        reason="Нет проверенных интервалов с метками условий.",
        known_interval_count=len(known),
        unknown_interval_count=len(intervals) - len(known),
        condition_counts={},
        settings={
            "split": "leave_one_group_out",
            "minimum_groups_per_condition": MIN_GROUPS_PER_CONDITION,
            "minimum_windows_per_condition": MIN_WINDOWS_PER_CONDITION,
            "tie_break": "first_condition_in_sorted_order",
            "ridge_alpha": RIDGE_ALPHA,
            "models": (
                "majority_baseline", "nearest_centroid", "ridge_classifier",
            ),
        },
        limitations=(
            "Сравнение разведочное. Окна одного сеанса могут быть зависимы.",
            "Метки условий должны повторяться в независимых группах. Если условие "
            "всегда занимает одну и ту же фазу или позицию в протоколе, модель "
            "может распознавать порядок, дрейф сигнала и качество контакта.",
            "Оценка переноса на новых участников требует разделения по участникам.",
            "Balanced accuracy по сеансу усредняет полноту по условиям, которые "
            "в этом сеансе представлены. Средние accuracy по сеансам и группам "
            "дают каждому сеансу и каждой группе одинаковый вес соответственно.",
            "Ridge использует фиксированную L2-регуляризацию. Центрирование "
            "и масштабирование признаков оцениваются отдельно на каждой "
            "обучающей части. Подбор гиперпараметров отсутствует.",
        ),
    )
    if not known:
        return result

    by_group = defaultdict(list)
    by_session = defaultdict(list)
    coverage = defaultdict(list)
    labeled_groups = defaultdict(set)
    for interval in known:
        by_group[interval.group_id].append(interval)
        by_session[interval.session_id].append(interval)
        coverage[interval.group_id, interval.session_id, interval.condition].append(interval)
        labeled_groups[interval.condition].add(interval.group_id)

    for session_id, session_intervals in sorted(by_session.items()):
        previous_end = 0.0
        if len({interval.group_id for interval in session_intervals}) != 1:
            return _skip(result, "inconsistent_group", f"Сеанс {session_id} отнесён к нескольким группам.")
        for interval in sorted(session_intervals, key=lambda item: (item.start_s, item.end_s)):
            if (
                not interval.session_id.strip()
                or not interval.group_id.strip()
                or not interval.source.strip()
                or not math.isfinite(interval.start_s)
                or not math.isfinite(interval.end_s)
                or interval.start_s < 0
                or interval.end_s <= interval.start_s
            ):
                return _skip(
                    result, "invalid_condition_interval",
                    f"У сеанса {session_id} есть некорректный интервал или метка без источника.",
                )
            if interval.start_s < previous_end:
                return _skip(
                    result, "overlapping_condition_intervals",
                    f"Интервалы условий сеанса {session_id} пересекаются.",
                )
            previous_end = interval.end_s

    result["label_coverage"] = [
        {
            "group_id": group_id,
            "session_id": session_id,
            "condition": condition,
            "interval_count": len(items),
            "labeled_seconds": sum(item.end_s - item.start_s for item in items),
            "usable_windows": None,
        }
        for (group_id, session_id, condition), items in sorted(coverage.items())
    ]
    conditions = sorted(labeled_groups)
    result["condition_counts"] = {
        condition: {
            "labeled_groups": len(labeled_groups[condition]),
            "usable_groups": None,
            "usable_windows": None,
        }
        for condition in conditions
    }
    if len(conditions) < 2:
        return _skip(result, "single_condition", "Для сравнения требуется хотя бы два условия.")
    if any(len(labeled_groups[condition]) < MIN_GROUPS_PER_CONDITION for condition in conditions):
        return _skip(
            result, "insufficient_labeled_groups",
            f"Каждое условие должно встречаться минимум в {MIN_GROUPS_PER_CONDITION} независимых группах.",
        )

    groups = sorted(by_group)
    folds = [(group_id, tuple(item for item in groups if item != group_id)) for group_id in groups]
    group_windows = {
        group_id: _make_windows(by_group[group_id], extract_features) for group_id in groups
    }
    window_counts = Counter(
        (group_id, window.interval.session_id, window.interval.condition)
        for group_id, windows in group_windows.items()
        for window in windows
    )
    usable_groups = defaultdict(set)
    usable_windows = Counter()
    for (group_id, _, condition), count in window_counts.items():
        usable_groups[condition].add(group_id)
        usable_windows[condition] += count
    for row in result["label_coverage"]:
        row["usable_windows"] = window_counts[row["group_id"], row["session_id"], row["condition"]]
    for condition, counts in result["condition_counts"].items():
        counts.update(
            usable_groups=len(usable_groups[condition]),
            usable_windows=usable_windows[condition],
        )
    enough_windows = all(
        counts["usable_groups"] >= MIN_GROUPS_PER_CONDITION
        and counts["usable_windows"] >= MIN_WINDOWS_PER_CONDITION
        for counts in result["condition_counts"].values()
    )

    condition_indices = {condition: index for index, condition in enumerate(conditions)}
    for group_id, train_groups in folds:
        train = [window for training_group in train_groups for window in group_windows[training_group]]
        test = group_windows[group_id]
        row = {
            "held_out_group": group_id,
            "train_groups": " | ".join(train_groups),
            "train_sessions": _session_ids(by_group, train_groups),
            "test_sessions": _session_ids(by_group, (group_id,)),
            "train_windows": len(train),
            "test_windows": len(test),
            "status": "evaluated",
        }
        result["folds"].append(row)
        if not enough_windows:
            row["status"] = "insufficient_usable_groups_or_windows"
        elif not test:
            row["status"] = "no_usable_test_windows"
        else:
            result["predictions"].extend(
                _predict_fold(group_id, train, test, conditions, condition_indices)
            )

    if not enough_windows:
        return _skip(
            result, "insufficient_usable_groups_or_windows",
            f"После контроля качества каждому условию требуется минимум "
            f"{MIN_WINDOWS_PER_CONDITION} полных окон из "
            f"{MIN_GROUPS_PER_CONDITION} независимых групп.",
        )

    _summarize(result, conditions)
    result.update(status="evaluated", reason_code="", reason="")
    return result


def _skip(result, reason_code, reason):
    result.update(reason_code=reason_code, reason=reason)
    return result


def _session_ids(by_group, groups):
    return " | ".join(sorted({item.session_id for group in groups for item in by_group[group]}))


def _make_windows(intervals, extract_features):
    requested = tuple(sorted(intervals, key=lambda item: (item.session_id, item.start_s)))
    eligible = set(requested)
    seen = set()
    windows = []
    for window in extract_features(requested):
        interval = window.interval
        if (
            interval not in eligible
            or not (interval.start_s - BOUNDARY_TOLERANCE_S <= window.start_s
                    < window.end_s <= interval.end_s + BOUNDARY_TOLERANCE_S)
        ):
            raise ValueError("Feature window lies outside its verified condition interval.")
        key = (interval.session_id, window.start_s, window.end_s)
        if key in seen:
            raise ValueError("Feature callback returned a duplicate window.")
        seen.add(key)
        features = np.asarray(window.features, dtype=float)
        if features.ndim != 1 or not features.size or not np.isfinite(features).all():
            raise ValueError("Feature windows require a nonempty vector of finite values.")
        windows.append(window)
    return sorted(windows, key=lambda item: (item.interval.session_id, item.start_s, item.end_s))


def _predict_fold(group_id, train, test, conditions, condition_indices):
    train_features = np.asarray([item.features for item in train], dtype=float)
    test_features = np.asarray([item.features for item in test], dtype=float)
    train_labels = np.asarray([condition_indices[item.interval.condition] for item in train])
    n_classes = len(conditions)

    mean = train_features.mean(axis=0)
    scale = train_features.std(axis=0)
    scale[scale == 0] = 1.0
    train_scaled = (train_features - mean) / scale
    test_scaled = (test_features - mean) / scale

    majority = np.bincount(train_labels, minlength=n_classes).argmax()
    centroids = np.stack([train_scaled[train_labels == idx].mean(axis=0) for idx in range(n_classes)])
    distances = np.stack([np.square(test_scaled - centroid).sum(axis=1) for centroid in centroids])
    target = np.eye(n_classes)[train_labels]
    target_mean = target.mean(axis=0)
    design_covariance = (train_scaled.T @ train_scaled) / len(train)
    weights = np.linalg.solve(
        design_covariance + RIDGE_ALPHA * np.eye(train_scaled.shape[1]),
        (train_scaled.T @ (target - target_mean)) / len(train),
    )
    predictions = {
        "majority_baseline": np.full(len(test), majority),
        "nearest_centroid": distances.argmin(axis=0),
        "ridge_classifier": (test_scaled @ weights + target_mean).argmax(axis=1),
    }
    for model, predicted in predictions.items():
        for window, index in zip(test, predicted, strict=True):
            actual = window.interval.condition
            yield {
                "model": model,
                "group_id": group_id,
                "session_id": window.interval.session_id,
                "start_s": window.start_s,
                "end_s": window.end_s,
                "label_source": window.interval.source,
                "condition": actual,
                "prediction": conditions[index],
                "correct": actual == conditions[index],
            }


def _summarize(result, conditions):
    by_model = defaultdict(list)
    for row in result["predictions"]:
        by_model[row["model"]].append(row)
        if not row["correct"]:
            result["errors"].append(row)

    for model, rows in by_model.items():
        by_session = defaultdict(list)
        by_group = defaultdict(list)
        for row in rows:
            by_session[row["session_id"]].append(row)
            by_group[row["group_id"]].append(row)

        session_scores = []
        for session_id, items in sorted(by_session.items()):
            scores = _scores(items)
            session_scores.append(scores["accuracy"])
            result["per_session"].append({
                "model": model, "group_id": items[0]["group_id"], "session_id": session_id, **scores,
            })

        result["metrics"].append({
            "model": model,
            **_scores(rows),
            "mean_session_accuracy": sum(session_scores) / len(session_scores),
            "mean_group_accuracy": sum(_accuracy(items) for items in by_group.values()) / len(by_group),
        })
        counts = Counter((item["condition"], item["prediction"]) for item in rows)
        result["confusion"].extend({
            "model": model, "condition": actual, "prediction": predicted,
            "windows": counts[actual, predicted],
        } for actual in conditions for predicted in conditions)


def _accuracy(rows):
    return sum(item["correct"] for item in rows) / len(rows)


def _scores(rows):
    actual = Counter(item["condition"] for item in rows)
    predicted = Counter(item["prediction"] for item in rows)
    correct = Counter(item["condition"] for item in rows if item["correct"])
    f1_labels = sorted(actual.keys() | predicted.keys())
    return {
        "windows": len(rows),
        "conditions": len(actual),
        "accuracy": _accuracy(rows),
        "balanced_accuracy": sum(correct[label] / actual[label] for label in actual) / len(actual),
        "macro_f1": (
            sum(2 * correct[label] / (actual[label] + predicted[label]) for label in f1_labels)
            / len(f1_labels)
        ),
    }

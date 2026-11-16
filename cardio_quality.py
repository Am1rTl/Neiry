"""Quality gates for Capsule heart-rate and cardio index reports."""

from typing import Any, Mapping


def sdk_cardio_issues(report: Mapping[str, Any]) -> list[str]:
    """List reasons to withhold a report from charts of accepted SDK values."""
    issues = []
    if report.get("skinContact") is False:
        issues.append("Оптический датчик не подтвердил контакт с кожей.")
    if report.get("motionArtifacts") is True:
        issues.append("Capsule обнаружил движение.")
    if report.get("hasArtifacts") is True:
        issues.append("Capsule обнаружил артефакты ФПГ.")
    if report.get("metricsAvailable") is False:
        issues.append("Capsule пометил кардиопоказатели как недоступные.")
    if any(type(report.get(flag)) is not bool for flag in (
        "skinContact", "motionArtifacts", "hasArtifacts", "metricsAvailable",
    )):
        issues.append("Нет полного набора флагов качества Capsule.")
    hr = report.get("heartRate")
    if type(hr) not in (int, float) or not 30 <= hr <= 220:
        issues.append("Пульс вне диапазона отображения.")
    return issues

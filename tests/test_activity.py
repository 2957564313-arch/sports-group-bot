"""activity 服务：固定活动可配置（WEEKLY_EVENTS）+ 月活动合并。"""

from datetime import date

from app.config import WeeklyEvent
from app.models.activity import Activity
from app.services import activity


def _manual(y: int, m: int, d: int, name: str = "周末拉练") -> Activity:
    return Activity(kind="manual", name=name, date=date(y, m, d), time="", location="")


def _cancel(y: int, m: int, d: int, name: str = "例训") -> Activity:
    return Activity(kind="cancel", name=name, date=date(y, m, d), time="", location="")


def test_weekly_events_empty_by_default(monkeypatch):
    """未配置 WEEKLY_EVENTS = 无固定活动。"""
    monkeypatch.setattr(activity.settings, "weekly_events", [])
    assert activity.weekly_events() == {}


def test_weekly_events_from_config(monkeypatch):
    """WEEKLY_EVENTS 配置的固定活动按 weekday 展开。"""
    monkeypatch.setattr(
        activity.settings,
        "weekly_events",
        [WeeklyEvent(weekday=2, name="例训", time="16:00", location="田径场")],
    )
    ev = activity.weekly_events()
    assert ev[2].name == "例训"
    assert ev[2].time == "16:00"
    assert ev[2].location == "田径场"


def test_resolve_month_events_manual_overrides_weekly(monkeypatch):
    """同日手动活动覆盖固定活动。2026-10-30 是周五。"""
    monkeypatch.setattr(
        activity.settings,
        "weekly_events",
        [WeeklyEvent(weekday=4, name="例跑", time="20:00", location="田径场")],
    )
    ev = activity.resolve_month_events(2026, 10, [_manual(2026, 10, 30, "接力")], [])
    assert ev[30].name == "接力"


def test_resolve_month_events_cancel_removes_weekly(monkeypatch):
    """取消标记让当天不显示固定活动。"""
    monkeypatch.setattr(
        activity.settings,
        "weekly_events",
        [WeeklyEvent(weekday=4, name="例跑", time="20:00", location="田径场")],
    )
    ev = activity.resolve_month_events(2026, 10, [], [_cancel(2026, 10, 30, "例跑")])
    assert 30 not in ev

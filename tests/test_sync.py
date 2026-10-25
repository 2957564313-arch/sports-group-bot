"""截图里程累加 + 跨周清零 + 单次运动累加（DailyStats 版）测试。"""

import datetime
from datetime import date

from sqlalchemy import select

from app.models.checkin_log import CheckinLog
from app.models.daily_record import DailyRecord
from app.models.manual_distance import ManualDistance
from app.models.member import Member
from app.services.providers.base import DailyStats
from app.services.sync import (
    add_manual_distance,
    find_duplicate_workout,
    log_checkin,
    record_manual_activity,
    undo_checkin_by_id,
    undo_last_checkin,
)


def test_add_manual_distance_accumulates(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    assert add_manual_distance(m, 5.0, db_session) == 5.0
    assert add_manual_distance(m, 3.0, db_session) == 8.0

    rec = db_session.get(ManualDistance, "111")
    assert rec.total_distance_km == 8.0
    assert rec.week_distance_km == 8.0


def test_add_manual_distance_resets_across_week(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()
    add_manual_distance(m, 5.0, db_session)

    # 伪装成上一周的累计，触发跨周清零
    rec = db_session.get(ManualDistance, "111")
    rec.week_start = rec.week_start - datetime.timedelta(days=7)
    db_session.commit()

    assert add_manual_distance(m, 2.0, db_session) == 7.0  # 总累计 5 + 2
    rec = db_session.get(ManualDistance, "111")
    assert rec.week_distance_km == 2.0  # 本周从 2.0 重计


def test_record_manual_activity_accumulates(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    d = date.today()
    record_manual_activity(
        m,
        d,
        DailyStats(
            date=d,
            distance_km=5.0,
            ascent_meters=50.0,
            calories=400,
            active_minutes=30,
            avg_pace_sec_per_km=300.0,
            avg_hr=150,
        ),
        db_session,
    )
    record_manual_activity(
        m,
        d,
        DailyStats(date=d, distance_km=3.0, ascent_meters=20.0, calories=200, active_minutes=20),
        db_session,
    )

    rec = db_session.execute(select(DailyRecord).where(DailyRecord.member_qq == "111")).scalar_one()
    assert rec.distance_km == 8.0
    assert rec.ascent_meters == 70.0
    assert rec.calories == 600
    assert rec.active_minutes == 50
    assert rec.activities_count == 2
    assert rec.max_activity_distance_km == 5.0  # max(5.0, 3.0)
    assert rec.avg_pace_sec_per_km == 300.0  # 第二次无配速，保持第一次
    assert rec.avg_hr == 150


def test_undo_last_checkin_reverts_last(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    d = date.today()
    # 两次截图打卡
    add_manual_distance(m, 5.0, db_session)
    record_manual_activity(m, d, DailyStats(date=d, distance_km=5.0), db_session)
    log_checkin("111", d, 5.0, db_session)

    add_manual_distance(m, 3.0, db_session)
    record_manual_activity(m, d, DailyStats(date=d, distance_km=3.0), db_session)
    log_checkin("111", d, 3.0, db_session)

    # 撤销最近一次（3.0）
    dist, dd = undo_last_checkin("111", db_session)
    assert dist == 3.0
    assert dd == d

    md = db_session.get(ManualDistance, "111")
    assert md.total_distance_km == 5.0
    assert md.week_distance_km == 5.0

    rec = db_session.execute(
        select(DailyRecord).where(DailyRecord.member_qq == "111", DailyRecord.platform == "manual")
    ).scalar_one()
    assert rec.distance_km == 5.0
    assert rec.activities_count == 1

    logs = (
        db_session.execute(select(CheckinLog).where(CheckinLog.member_qq == "111")).scalars().all()
    )
    assert len(logs) == 1
    assert logs[0].distance_km == 5.0


def test_undo_last_checkin_removes_record_when_zero(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    d = date.today()
    add_manual_distance(m, 5.0, db_session)
    record_manual_activity(m, d, DailyStats(date=d, distance_km=5.0), db_session)
    log_checkin("111", d, 5.0, db_session)

    dist, _ = undo_last_checkin("111", db_session)
    assert dist == 5.0

    md = db_session.get(ManualDistance, "111")
    assert md.total_distance_km == 0.0

    rec = db_session.execute(
        select(DailyRecord).where(DailyRecord.member_qq == "111", DailyRecord.platform == "manual")
    ).scalar_one_or_none()
    assert rec is None  # 当日 manual 明细归零后整行删除

    assert undo_last_checkin("111", db_session) == (0.0, None)  # 已无明细


def test_undo_checkin_by_id_reverts_specific(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    d = date.today()
    # 三次截图打卡，分别 5 / 3 / 4 km
    for km in (5.0, 3.0, 4.0):
        add_manual_distance(m, km, db_session)
        record_manual_activity(m, d, DailyStats(date=d, distance_km=km), db_session)
        log_checkin("111", d, km, db_session)

    logs = (
        db_session.execute(
            select(CheckinLog).where(CheckinLog.member_qq == "111").order_by(CheckinLog.id)
        )
        .scalars()
        .all()
    )
    assert len(logs) == 3
    middle_id = logs[1].id  # 删除中间那条 3 km

    dist, dd = undo_checkin_by_id("111", middle_id, db_session)
    assert dist == 3.0
    assert dd == d

    md = db_session.get(ManualDistance, "111")
    assert md.total_distance_km == 9.0  # 5 + 4

    rec = db_session.execute(
        select(DailyRecord).where(DailyRecord.member_qq == "111", DailyRecord.platform == "manual")
    ).scalar_one()
    assert rec.distance_km == 9.0
    assert rec.activities_count == 2
    assert rec.max_activity_distance_km == 5.0  # 剩余 5 与 4，重建 max

    remaining = (
        db_session.execute(
            select(CheckinLog).where(CheckinLog.member_qq == "111").order_by(CheckinLog.id)
        )
        .scalars()
        .all()
    )
    assert [lg.distance_km for lg in remaining] == [5.0, 4.0]


def test_undo_checkin_by_id_not_found(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()
    d = date.today()
    add_manual_distance(m, 5.0, db_session)
    record_manual_activity(m, d, DailyStats(date=d, distance_km=5.0), db_session)
    log_checkin("111", d, 5.0, db_session)

    assert undo_checkin_by_id("111", 9999, db_session) is None  # 不存在
    assert undo_checkin_by_id("222", 1, db_session) is None  # 不属于该成员


def test_member_display_name_priority(db_session):
    m = Member(qq="111", nickname="QQ昵称", custom_nickname="")
    assert m.display_name == "QQ昵称"  # 未设自定义 → 回落到 QQ 昵称
    m.custom_nickname = "自定义"
    assert m.display_name == "自定义"  # 设了自定义 → 优先
    m.nickname = ""
    assert m.display_name == "自定义"
    m.custom_nickname = ""
    assert m.display_name == "111"  # 都空 → 回落到 QQ 号


# ---------------------------------------------------------------------------
# 同日数据指纹判重：同一场运动换张截图不重复累计，两场真实运动各自记录
# ---------------------------------------------------------------------------


def _log(dist, minutes, kcal, start_minute=None):
    return CheckinLog(
        member_qq="111",
        record_date=date.today(),
        distance_km=dist,
        active_minutes=minutes,
        calories=kcal,
        start_minute=start_minute,
    )


def test_find_duplicate_workout_matches():
    # 数值指纹一致（含时间）→ 判为同一场运动
    log = _log(5.02, 35, 340, start_minute=7 * 60 + 32)
    data = {"distance_km": 5.03, "active_minutes": 36, "calories": 344}
    assert find_duplicate_workout(data, 7 * 60 + 32, [log]) is log


def test_find_duplicate_workout_diff_values():
    # 数值不同 → 不同场（同一天两场真实运动各自记录）
    log = _log(5.02, 35, 340, start_minute=7 * 60 + 32)
    data = {"distance_km": 8.1, "active_minutes": 52, "calories": 520}
    assert find_duplicate_workout(data, 18 * 60 + 0, [log]) is None


def test_find_duplicate_workout_time_conflict():
    # 数值恰好相同但开始时间相差 >20 分钟 → 两场不同运动
    log = _log(5.02, 35, 340, start_minute=7 * 60 + 32)
    data = {"distance_km": 5.03, "active_minutes": 36, "calories": 344}
    assert find_duplicate_workout(data, 18 * 60 + 5, [log]) is None


def test_find_duplicate_workout_missing_time_still_matches():
    # 任一侧没识别出时间 → 时间不参与判定，仍按数值判重
    log = _log(5.02, 35, 340)
    data = {"distance_km": 5.03, "active_minutes": 36, "calories": 344}
    assert find_duplicate_workout(data, None, [log]) is log


def test_find_duplicate_workout_no_logs():
    assert find_duplicate_workout({"distance_km": 5.02}, None, []) is None


def test_log_checkin_stores_start_minute(db_session):
    # 截图识别的开始时间随打卡明细一起落库（供判重与「今日明细」时段展示）
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()
    d = date.today()
    add_manual_distance(m, 5.0, db_session)
    record_manual_activity(m, d, DailyStats(date=d, distance_km=5.0), db_session)
    log_checkin("111", d, 5.0, db_session, start_minute=7 * 60 + 32)
    db_session.commit()

    lg = db_session.execute(select(CheckinLog).where(CheckinLog.member_qq == "111")).scalars().one()
    assert lg.start_minute == 7 * 60 + 32

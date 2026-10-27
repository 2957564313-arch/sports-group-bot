from datetime import date, timedelta

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from ..config import settings
from ..db import engine, get_session
from ..models.checkin_log import CheckinLog
from ..models.daily_record import DailyRecord
from . import timeutil


def _wal_checkpoint() -> None:
    """清理删除大量行后顺手 checkpoint，避免 SQLite WAL 文件长期膨胀（仅 SQLite 生效）。

    每日清理过期明细会删掉一整天的记录，不做 checkpoint 的话 -wal 文件会把删除的
    旧页一直带在身上，长期运行下占满磁盘；TRUNCATE 模式在 WAL 下安全（没有并发写者）。
    失败静默：checkpoint 只是空间优化，不该影响清理主流程。
    """
    if not settings.db_url.startswith("sqlite"):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:
        pass


def cleanup_daily(before: date | None = None) -> int:
    """删除 record_date < before 的原始明细，返回删除行数。

    默认按 settings.retention_days（=365）滚动保留最近一年，超过一年的明细在每日
    播报前清理时删除。retention_days <= 0 表示永久保留、不清理（如需停用可改 .env）。
    before 省略时按 retention_days 自动计算截止日期（以北京时间为准）。
    """
    if settings.retention_days <= 0:
        return 0
    if before is None:
        before = timeutil.today() - timedelta(days=settings.retention_days)
    session: Session = get_session()
    try:
        result = session.execute(delete(DailyRecord).where(DailyRecord.record_date < before))
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()
        _wal_checkpoint()


def cleanup_checkin_logs(before: date | None = None) -> int:
    """删除 record_date < before 的截图打卡明细，返回删除行数。

    与 daily_record 共用同一 retention_days 窗口：明细只用于「删除打卡」精确回退，
    保留与聚合数据同样长的一年已足够——更早的记录 daily_record 也已清理、本就无法回退。
    retention_days <= 0 表示永久保留、不清理。
    """
    if settings.retention_days <= 0:
        return 0
    if before is None:
        before = timeutil.today() - timedelta(days=settings.retention_days)
    session: Session = get_session()
    try:
        result = session.execute(delete(CheckinLog).where(CheckinLog.record_date < before))
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()
        _wal_checkpoint()

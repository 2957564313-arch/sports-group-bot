"""定时任务：每日排行播报 + 过期明细清理。

从 app/scheduler.py 归位到服务层。原「周聚合」任务与周汇总中间表已一并删除——
周榜/月榜直接由 daily_record 聚合，无需中间表。
过期明细清理从「每周一」改为「每日播报前」执行，幂等、更及时。
"""

import asyncio
import datetime

from nonebot import get_driver
from nonebot.log import logger

from ..config import settings
from ..db import get_session
from ..models.member import Member
from . import activity, alert, checkin, notify, ranking, retention, sync, timeutil

driver = get_driver()


def _lottery_if_due() -> tuple[int, list[str]] | None:
    """全群累计打卡突破抽奖阈值时开奖，返回 (阈值, 中奖者昵称列表)；否则 None。

    状态表（lottery:<阈值>）保证只触发一次。阻塞 DB 调用，放线程执行。
    """
    session = get_session()
    try:
        t = checkin.check_lottery(session)
        if t is None:
            return None
        winners = checkin.draw_lottery(session)
        names = []
        for w in winners:
            m = session.get(Member, w)
            names.append(m.display_name if m else w)
        session.commit()
        return t, names
    finally:
        session.close()


async def _run_broadcast() -> None:
    """计算并播报排行：每日今日榜；周日追加周榜、月末追加月榜，逐段独立发送。"""
    today = timeutil.today()
    day = datetime.timedelta(days=1)

    # 播报前清理过期明细（retention_days<=0 时永久保留、自动跳过），幂等、每日执行
    try:
        await asyncio.to_thread(retention.cleanup_daily)
    except Exception as e:
        logger.warning(f"清理过期明细失败（不影响播报）: {e}")
    try:
        await asyncio.to_thread(retention.cleanup_checkin_logs)
    except Exception as e:
        logger.warning(f"清理过期打卡明细失败（不影响播报）: {e}")

    # 播报前先同步所有已绑定成员的今日数据，否则绑定平台后榜单可能因当天未入库而为空
    try:
        await asyncio.to_thread(sync.sync_today_all)
    except Exception as e:
        logger.warning(f"同步今日数据失败（仍用已有数据播报）: {e}")
        alert.record("sync_today_all", f"同步今日数据失败：{e}")

    # (scope, 标签, 起始, 结束) 三段榜单，按需追加
    periods: list[tuple[str, str, datetime.date, datetime.date]] = [
        ("day", "今日", today, today + day)
    ]
    if today.weekday() == 6:  # 周日
        this_monday = today - datetime.timedelta(days=6)
        periods.append(("week", "本周", this_monday, today + day))
    if (today + day).month != today.month:  # 月末（明天进入下月）
        month_start = today.replace(day=1)
        periods.append(("month", "本月", month_start, today + day))

    messages: list[str] = []
    for scope, label, start, end in periods:
        try:
            r = await asyncio.to_thread(ranking.compute_range_rankings, start, end, scope=scope)
            messages.append(ranking.format_leaderboards(r, _rank_title(label, start, end)))
        except Exception as e:
            logger.exception(f"{label}排行计算失败: {e}")
            alert.record(f"ranking:{scope}", f"{label}排行计算失败：{e}")
    if not messages:
        return

    # 解析播报目标：bots 按 self_id 去重、群按白名单过滤（见 activity.resolve_targets）
    bots, groups = await activity.resolve_targets()

    # 运维告警：把本次积累的关键故障（平台接口异常 / 同步失败 / 排行失败）私聊管理员，
    # 先于群播报发出，让管理员第一时间察觉「数据可能悄悄缺失」。同 key 去重见 services.alert。
    alerts = alert.drain()
    if alerts and bots:
        text = f"⚠️ {settings.bot_name}运维提醒\n" + "\n".join(alerts)
        for su in get_driver().config.superusers:
            for bot in bots:
                try:
                    await bot.send_private_msg(user_id=int(su), message=text)
                    break  # 一个 bot 发成功即可，避免多 bot 重复
                except Exception as e:
                    logger.warning(f"发送运维告警失败: {e}")

    if not bots:
        logger.warning("没有已连接的 OneBot Bot，跳过排行播报")
        return
    if not groups:
        logger.warning("没有可播报的群，跳过排行播报")
        return

    for bot in bots:
        for gid in groups:
            for text in messages:
                try:
                    await bot.send_group_msg(group_id=gid, message=text)
                    logger.info(f"已向群 {gid} 播报一段排行")
                except Exception as e:
                    logger.warning(f"向群 {gid} 播报失败: {e}")

    # 群抽奖兜底：全群累计打卡突破阈值时开奖，公告到所有播报群（状态表保证只触发一次）
    try:
        lot = await asyncio.to_thread(_lottery_if_due)
    except Exception as e:
        logger.warning(f"群抽奖检查失败: {e}")
        lot = None
    if lot:
        t, winners = lot
        text = (
            f"🎉 全群累计打卡突破 {t} 天！群抽奖开奖：{'、'.join(winners)} "
            f"获得{checkin.LOTTERY_PRIZE}～恭喜这位跑友！"
        )
        for bot in bots:
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=text)
                    logger.info(f"已向群 {gid} 发送群抽奖公告")
                except Exception as e:
                    logger.warning(f"向群 {gid} 发送群抽奖公告失败: {e}")

    # 活动提醒：明天有活动则提醒（搭 23:00 榜单播报时刻，幂等保证只提醒一次）
    try:
        await activity.remind_tomorrow(today)
    except Exception as e:
        logger.warning(f"活动提醒失败: {e}")

    # 下月跑团日历：今天是本月最后一天 → 生成并发送下月日历图（同月只发一次）
    if (today + day).month != today.month:
        next_first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        try:
            await activity.send_next_month_calendar(next_first)
        except Exception as e:
            logger.warning(f"发送下月跑团日历失败: {e}")


def _rank_title(label: str, start: datetime.date, end: datetime.date) -> str:
    if label == "今日":
        return ranking.daily_title(start)
    if label == "本周":
        last = end - datetime.timedelta(days=1)
        return ranking.weekly_title(start, last)
    return ranking.monthly_title(start)


async def _daily_broadcast_loop() -> None:
    hour = settings.broadcast_hour
    minute = settings.broadcast_minute
    while True:
        try:
            now = timeutil.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await _run_broadcast()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 常驻循环绝不允许被未预期异常杀死（否则每日播报静默失效、且无任何告警）。
            # 登记运维告警（同 key 去重限频）后 60 秒重试，恢复后循环照常运转。
            logger.exception(f"每日播报循环异常: {e}")
            alert.record("broadcast_loop", f"每日播报循环异常：{e}")
            await asyncio.sleep(60)


_started = False
# 持有播报循环任务的强引用，避免常驻协程被 GC 回收导致每日播报静默失效
_broadcast_task: asyncio.Task | None = None
# 成员提醒循环任务引用（同上）
_notify_task: asyncio.Task | None = None


async def _member_notify_loop() -> None:
    """周期抽取 services.notify 里的成员提醒并私聊发送（同 qq 24h 去重）。

    独立于每日播报循环：授权失效等账号级故障需要尽快通知成员本人——
    等 23:00 播报搭车太迟（当天数据已经缺失，成员越晚重新绑定缺得越多）。
    无可用 Bot 或发送失败时放回队列重试（限次），不静默丢弃。
    """
    while True:
        await asyncio.sleep(30)
        try:
            items = notify.drain()
            if not items:
                continue
            try:
                bots, _ = await activity.resolve_targets()
            except Exception as e:
                logger.warning(f"解析成员通知发送目标失败: {e}")
                bots = []
            if not bots:
                # 没有已连接的 Bot：放回队列，等连接恢复后再发
                for qq, text, left in items:
                    notify.retry(qq, text, left)
                continue
            for qq, text, left in items:
                try:
                    await bots[0].send_private_msg(user_id=int(qq), message=text)
                    logger.info(f"已私聊通知成员 {qq}（账号级提醒）")
                except Exception as e:
                    logger.warning(f"私聊通知成员 {qq} 失败: {e}")
                    notify.retry(qq, text, left)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 同播报循环：成员提醒循环也不允许静默死亡（授权失效通知会随之中断）。
            logger.exception(f"成员提醒循环异常: {e}")
            alert.record("notify_loop", f"成员提醒循环异常：{e}")


def start_scheduler() -> None:
    """在 driver on_startup 阶段调用，启动每日排行播报循环与成员提醒循环。

    幂等：若 on_startup 被重复触发，也只创建一次循环任务，避免每天 23:00 重复播报。
    """
    global _started, _broadcast_task, _notify_task
    if _started:
        logger.warning("调度器已启动，跳过重复启动")
        return
    _started = True
    loop = asyncio.get_running_loop()
    _broadcast_task = loop.create_task(_daily_broadcast_loop())
    _notify_task = loop.create_task(_member_notify_loop())
    logger.info(
        f"每日排行播报已启动（每天 {settings.broadcast_hour:02d}:{settings.broadcast_minute:02d}）"
    )

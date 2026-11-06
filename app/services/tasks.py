"""定时任务：每日排行播报 + 过期明细清理 + 成员提醒。

从 app/scheduler.py 归位到服务层。原「周聚合」任务与周汇总中间表已一并删除——
周榜/月榜直接由 daily_record 聚合，无需中间表。
过期明细清理从「每周一」改为「每日播报前」执行，幂等、更及时。

调度纪律（见 CLAUDE.md）：榜单计算失败、Bot 掉线、某个群发送失败，都不得连带
跳过后面的群抽奖 / 活动提醒 / 下月日历——它们各自独立 try，且「今日已播报」用
checkin_state 打标记，重启或休眠错过时刻后可自动补播。
"""

import asyncio
import datetime

from nonebot import get_driver
from nonebot.log import logger

from ..config import settings
from ..db import get_session
from ..models.checkin_state import CheckinState
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


def _broadcast_key(day: datetime.date) -> str:
    return f"broadcast:{day.isoformat()}"


def _broadcast_done(day: datetime.date) -> bool:
    """今天是否已经完成过一次播报（用于错过时刻后补播的幂等判据）。"""
    session = get_session()
    try:
        return session.get(CheckinState, _broadcast_key(day)) is not None
    finally:
        session.close()


def _mark_broadcast_done(day: datetime.date) -> None:
    session = get_session()
    try:
        if session.get(CheckinState, _broadcast_key(day)) is None:
            session.add(CheckinState(key=_broadcast_key(day)))
            session.commit()
    finally:
        session.close()


async def _send_private_alerts(alerts: list[tuple[str, str]], bots) -> None:
    """把运维告警私聊给管理员；一条都没送达就把告警放回队列，下一轮再发。"""
    if not alerts:
        return
    if not bots:
        for key, msg in alerts:
            alert.requeue(key, msg)
        logger.warning("没有已连接的 Bot，运维告警暂存待重发")
        return
    text = f"⚠️ {settings.bot_name}运维提醒\n" + "\n".join(msg for _, msg in alerts)
    for su in get_driver().config.superusers:
        try:
            user_id = int(su)
        except (TypeError, ValueError):
            continue
        for bot in bots:
            try:
                await bot.send_private_msg(user_id=user_id, message=text)
                return  # 一个 bot 发成功即可，避免多 bot 重复
            except Exception as e:
                logger.warning(f"发送运维告警失败（bot={getattr(bot, 'self_id', '?')}）: {e}")
    for key, msg in alerts:
        alert.requeue(key, msg)
    logger.warning("运维告警全部发送失败，已放回队列待重发")


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
            messages.append(
                ranking.format_leaderboards(
                    r, _rank_title(label, start, end), with_active_days=(scope == "month")
                )
            )
        except Exception as e:
            logger.exception(f"{label}排行计算失败: {e}")
            alert.record(f"ranking:{scope}", f"{label}排行计算失败：{e}")

    # 解析播报目标：bots 按 self_id 去重、群按白名单过滤（见 activity.resolve_targets）。
    # 解析失败也只是「这一轮发不出去」，后面的抽奖/提醒/日历照样各自尝试。
    try:
        bots, groups = await activity.resolve_targets()
    except Exception as e:
        logger.warning(f"解析播报目标失败: {e}")
        alert.record("resolve_targets", f"解析播报目标失败：{e}")
        bots, groups = [], []

    # 运维告警先于群播报发出（发送失败会放回队列，不会被 drain 掉）
    await _send_private_alerts(alert.drain(), bots)

    if bots and groups and messages:
        for bot in bots:
            for gid in groups:
                for text in messages:
                    try:
                        await bot.send_group_msg(group_id=gid, message=text)
                        logger.info(f"已向群 {gid} 播报一段排行")
                    except Exception as e:
                        logger.warning(f"向群 {gid} 播报失败: {e}")
                        alert.record(f"broadcast:{gid}", f"向群 {gid} 播报失败：{e}")
    else:
        logger.warning(
            "跳过排行播报（bots=%d, groups=%d, 文案段落=%d）", len(bots), len(groups), len(messages)
        )

    # 群抽奖兜底：全群累计打卡突破阈值时开奖，公告到所有播报群（状态表保证只触发一次）。
    # 只在有群可公告时才开奖——否则状态被打上标记，公告就永远发不出去了。
    if bots and groups:
        try:
            lot = await asyncio.to_thread(_lottery_if_due)
        except Exception as e:
            logger.warning(f"群抽奖检查失败: {e}")
            alert.record("lottery", f"群抽奖检查失败：{e}")
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

    # 活动提醒：明天有活动则提醒（搭播报时刻，按 (活动, 群) 幂等，见 activity.remind_tomorrow）
    try:
        await activity.remind_tomorrow(today)
    except Exception as e:
        logger.warning(f"活动提醒失败: {e}")
        alert.record("remind_tomorrow", f"活动提醒失败：{e}")

    # 下月跑团日历：今天是本月最后一天 → 生成并发送下月日历图（同月只发一次）
    if (today + day).month != today.month:
        next_first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        try:
            await activity.send_next_month_calendar(next_first)
        except Exception as e:
            logger.warning(f"发送下月跑团日历失败: {e}")
            alert.record("send_calendar", f"发送下月跑团日历失败：{e}")

    # 标记「今天已播报」：错过时刻补播、重启重跑都靠它判重（放在最后，中途异常则不标记）
    try:
        await asyncio.to_thread(_mark_broadcast_done, today)
    except Exception as e:
        logger.warning(f"标记今日播报状态失败: {e}")


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
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # 补播：今天的时刻已过但还没打「已播报」标记（重启/休眠/上次失败）→ 立即补跑
            if now >= scheduled and not await asyncio.to_thread(_broadcast_done, now.date()):
                logger.info("今日播报时刻已过且无完成标记，立即补播")
                await _run_broadcast()
                continue
            if now >= scheduled:
                scheduled += datetime.timedelta(days=1)
            await asyncio.sleep(max(1.0, (scheduled - now).total_seconds()))
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
    等播报搭车太迟（当天数据已经缺失，成员越晚重新绑定缺得越多）。
    无可用 Bot 或发送失败时放回队列重试（限次）；成功送达才登记 24h 去重时间戳。
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
                    notify.mark_sent(qq)
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

    幂等：若 on_startup 被重复触发，也只创建一次循环任务，避免每天重复播报。
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

"""成员提醒：后台同步发现账号级故障（如平台授权被官方吊销）时，主动私聊通知成员本人。

与 services.alert 的区别：alert 面向管理员（搭每日播报私聊发出），notify 面向普通成员，
需要尽快送达——成员看到提醒才会重新绑定/修复，否则数据一直悄悄缺失。
内存态 + 去重限频（同 qq 24 小时只发一次，**成功送达后才开始计时**），重启即清空；
由 tasks._member_notify_loop 周期性抽取并私聊发送，发送失败自动放回队列重试
（限 _MAX_RETRIES 次）。
"""

import logging
import threading
import time
from collections import deque

_log = logging.getLogger(__name__)

_DEDUP_SEC = 24 * 3600
_MAX_RETRIES = 5

_lock = threading.Lock()
# 待发队列：(qq, text, 剩余重试次数)
_pending: deque[tuple[str, str, int]] = deque()
# 已在队列中的 qq（避免同一成员重复排队刷屏）
_queued: set[str] = set()
# qq -> 最近一次「成功送达」时间（24h 去重；失败不占用名额）
_sent_at: dict[str, float] = {}


def enqueue(qq: str, text: str) -> bool:
    """登记一条成员通知（同 qq 24 小时内成功发送过、或已在队列中则跳过）。"""
    now = time.time()
    with _lock:
        if now - _sent_at.get(qq, 0.0) < _DEDUP_SEC:
            return False
        if qq in _queued:
            return False
        _queued.add(qq)
        _pending.append((qq, text, _MAX_RETRIES))
        return True


def drain() -> list[tuple[str, str, int]]:
    """取走并清空待发通知；发送失败时调用 ``retry`` 放回，成功时调用 ``mark_sent``。"""
    with _lock:
        items = list(_pending)
        _pending.clear()
        _queued.clear()
        return items


def mark_sent(qq: str) -> None:
    """发送成功后登记去重时间戳（24h 内不再重复提醒）。"""
    with _lock:
        _sent_at[qq] = time.time()
        _queued.discard(qq)


def retry(qq: str, text: str, attempts_left: int) -> None:
    """发送失败放回队列（重试次数递减，耗尽则告警丢弃，避免永久空转）。"""
    if attempts_left <= 1:
        _log.warning("成员提醒发送失败已达上限，放弃：qq=%s", qq)
        with _lock:
            _queued.discard(qq)
        return
    with _lock:
        _queued.add(qq)
        _pending.appendleft((qq, text, attempts_left - 1))

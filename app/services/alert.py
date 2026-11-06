"""运维告警：把关键故障从「静默吞掉」升级为「可感知」。

provider 解析 / 定时任务里原本只 logger.warning 的关键失败（平台核心接口异常、
播报/同步失败等），除记日志外再调 ``alert.record()`` 登记一条；每日排行播报搭车
把这些告警私聊发给管理员（superuser），避免「数据悄悄缺失」却无人察觉。

内存态 + 去重限频：同一 key 一个窗口期内只记一次，避免同一故障反复刷屏；
重启即清空，不做持久化（告警是「提醒」性质，不是审计）。

注意：去重时间戳与待发消息分开存放——``drain`` 只清待发消息，不重置去重窗口；
否则「取走即清空」会让同一故障在每轮播报里重复刷屏。发送失败时由调用方放回队列，
不会丢失（``drain`` 之后 ``requeue`` 可恢复）。
"""

import threading
import time

# 同一告警 key 的去重窗口（秒）：同一故障 6 小时内只提醒一次
_DEDUP_SEC = 6 * 3600

_lock = threading.Lock()
# key -> 去重截止时间戳（与待发消息解耦，drain 不影响它）
_dedup: dict[str, float] = {}
# key -> 待发消息（发送成功后移除）
_pending: dict[str, str] = {}


def record(key: str, message: str) -> None:
    """登记一条告警（同 key 去重限频）。同步函数、线程安全，可在 provider 解析路径调用。"""
    now = time.time()
    with _lock:
        if now < _dedup.get(key, 0.0):
            return
        _dedup[key] = now + _DEDUP_SEC
        _pending[key] = message


def drain() -> list[tuple[str, str]]:
    """取走并清空所有待发告警，返回 ``(key, message)`` 列表（可能为空）。

    返回 key 是为了发送失败时能用 ``requeue`` 原样放回，避免告警被静默吃掉。
    """
    with _lock:
        items = list(_pending.items())
        _pending.clear()
        return items


def requeue(key: str, message: str) -> None:
    """发送失败时把告警放回待发队列（不重置去重窗口）。"""
    with _lock:
        _pending.setdefault(key, message)


def reset() -> None:
    """清空全部状态（仅供测试）。"""
    with _lock:
        _dedup.clear()
        _pending.clear()

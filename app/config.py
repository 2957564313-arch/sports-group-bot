import json
import logging
import re

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)


def _parse_json_list(value, field: str):
    """把 .env 里的 JSON 数组字符串解析成 list；非法时告警并返回空列表。

    这类配置写错会让 Settings 在 import 阶段抛 ValidationError、机器人整个起不来，
    而它们都只是「增强 / 限制」性质的可选项，留空完全可运行——宁可带告警降级。
    """
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            _log.warning(f"配置 {field} 不是合法 JSON（{e}），已退回默认值；请检查 .env")
            return []
        if not isinstance(parsed, list):
            _log.warning(
                f"配置 {field} 应为 JSON 数组（当前是 {type(parsed).__name__}），"
                "已退回默认值；请检查 .env"
            )
            return []
        return parsed
    return value


def _clean_group_ids(raw, field: str) -> list[int]:
    """群号列表逐项校验：非整数 / 布尔 / 非正数一律丢弃并告警。

    只做「整个数组是否合法」的 JSON 校验不够——`[1, 2, "oops"]` 会让 pydantic 在
    元素级抛 ValidationError，照样把机器人拦在启动阶段。
    """
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            _log.warning(f"配置 {field} 含非法群号 {item!r}（布尔），已忽略；请检查 .env")
            continue
        try:
            gid = int(item)
        except (TypeError, ValueError):
            _log.warning(f"配置 {field} 含非法群号 {item!r}，已忽略；请检查 .env")
            continue
        if gid <= 0:
            _log.warning(f"配置 {field} 含非法群号 {gid}（必须为正整数），已忽略")
            continue
        if gid not in out:
            out.append(gid)
    return out


def _clean_weekly_events(raw, field: str) -> list[dict]:
    """固定活动逐项校验：weekday 必须在 0..6、name 必须非空，坏项丢弃并告警。"""
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            _log.warning(f"配置 {field} 含非对象元素 {item!r}，已忽略；请检查 .env")
            continue
        name = str(item.get("name") or "").strip()
        try:
            weekday = int(item.get("weekday"))
        except (TypeError, ValueError):
            _log.warning(f"配置 {field} 含非法 weekday {item.get('weekday')!r}，已忽略")
            continue
        if not name or not 0 <= weekday <= 6:
            _log.warning(f"配置 {field} 元素不完整或 weekday 越界（{item!r}），已忽略")
            continue
        out.append({**item, "weekday": weekday, "name": name})
    return out


def _clean_llm_fallbacks(raw, field: str) -> list[dict]:
    """备用端点逐项校验：model 与 http(s) base_url 缺一不可，坏项丢弃并告警。"""
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            _log.warning(f"配置 {field} 含非对象元素 {item!r}，已忽略；请检查 .env")
            continue
        model = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        if not model or not base_url.startswith(("http://", "https://")):
            _log.warning(f"配置 {field} 元素缺少 model/base_url（{item!r}），已忽略")
            continue
        out.append({**item, "model": model, "base_url": base_url})
    return out


class LlmEndpoint(BaseModel):
    """模型池中的一个 OpenAI 兼容端点（用于故障转移）。

    主端点仍由 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY / LLM_REASONING_EFFORT 决定；
    本模型只描述「备用端点」，通过 LLM_FALLBACKS（JSON 数组）配置。
    """

    model: str
    base_url: str
    # 端点密钥。本地 Ollama 可填任意非空值（如 "ollama"）；留空 = 视为未配置，直接跳过。
    api_key: str = ""
    # 该端点的思考强度；留空 = 不下发 reasoning_effort
    reasoning_effort: str = ""
    # 该端点读超时（秒）；留空 = 用 LLM_TIMEOUT
    timeout: float | None = None
    # 是否支持图片输入。视觉兜底只路由到 vision=True 的端点，纯文本模型不会被发图。
    vision: bool = False


class WeeklyEvent(BaseModel):
    """WEEKLY_EVENTS 中的一个固定活动：每周星期几 + 名称/时间/地点。"""

    weekday: int  # 0=周一 … 6=周日
    name: str
    time: str = ""
    location: str = ""


class Settings(BaseSettings):
    """从 .env 读取配置，字段名与 .env 中变量名大小写不敏感对应。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 机器人在群里的称呼（帮助菜单 / LLM 人设 / 月历标题等处统一使用），
    # 各群可自行改成自己的名字（如跑团名），代码里不再硬编码机器人名。
    bot_name: str = "运动搭子"

    # 数据库连接
    db_url: str = "sqlite:///data/bot.db"

    # 每日运动排行播报（默认每晚 23:00，本机本地时间即北京时间）
    broadcast_hour: int = 23
    broadcast_minute: int = 0
    # 原始训练明细保留天数（365 = 保留最近一年，超过自动清除）。历史明细供个人复盘/历史查询。
    retention_days: int = 365
    # 显式指定播报群号列表（JSON 数组）；留空 = 自动播报到机器人出现过的所有群
    broadcast_groups: list[int] = []
    # 群使用白名单（群号列表）：仅这些群的成员可触发命令 / 被每日播报。
    # 留空 = 不限制（全群可用）。需要限制时在 .env 用 ALLOWED_GROUPS 填写（JSON 数组）。
    allowed_groups: list[int] = []

    # 打卡计数起点（YYYY-MM-DD）：此日期之前的记录不统计打卡天数。
    # 默认 1970-01-01 = 统计全部历史；按学期运行的群可改为开学日等自定义起点。
    semester_start: str = "1970-01-01"

    # 打卡体系文案（可配置，去掉硬编码的群专属内容）：
    gift_name: str = "惊喜小礼物"  # 第 GIFT_DAYS 天礼物的名称
    lottery_prize: str = "惊喜奖品"  # 群抽奖奖品名称

    # 每周固定活动（JSON 数组）：自动进入跑团月历；weekday 0=周一 … 6=周日。
    # 例：WEEKLY_EVENTS=[{"weekday":2,"name":"例训","time":"16:00","location":"田径场"}]
    # 留空 = 无固定活动，月历只显示管理员手动添加的活动。
    weekly_events: list[WeeklyEvent] = []

    # 活动体系起始年月（YYYY-MM）：早于该月的「月历」请求直接提示无数据，
    # 不再把 2026-10 这类群专属时间硬编码在插件里。
    activity_start_month: str = "2026-10"

    # Garmin 佳明（每用户凭据绑定，此处仅区域开关；中国区账号 is_cn=true）
    garmin_is_cn: bool = True

    # COROS 高驰（官方 MCP + OAuth 授权，无需账号密码；region 决定接口区域）
    coros_region: str = "cn"  # cn / us

    # 大模型（OpenAI 兼容；可选增强层：数据解读/总结/问答/点评；留空 = 自动禁用）
    llm_api_key: str = ""
    # 推荐模型：智谱 GLM-5.3-Flash —— 原生多模态（文本 + 图片）、1M 上下文、
    # 约 0.8/2.8 元每百万 token（本项目低频短输出，月成本约 2 元）。
    # 换回其它 OpenAI 兼容服务时，LLM_MODEL / LLM_BASE_URL 必须成对修改。
    llm_model: str = "glm-5.3-flash"
    # 大模型接口地址（OpenAI 兼容 chat/completions 全路径）
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    # 思考强度（low / medium / max）。GLM-5.3-Flash 的 thinking 常开，短文案任务用 low
    # 可显著减少思考 token，避免 max_tokens 被吃掉后 content 为空（→ 误降级模板）。
    # 留空 = 不下发该参数，保证对不支持 reasoning_effort 的服务商（旧中转站等）完全兼容。
    llm_reasoning_effort: str = ""
    # 单次调用读超时（秒）。直连 GLM（200 tok/s）建议 20~25，失败更快降级；
    # 走慢速中转站时保持较大值（45）。
    llm_timeout: float = 45.0
    # 识图隐私开关（默认开启）：本地 OCR 未识别出距离时，允许把截图发给视觉模型兜底识别；
    # 关闭后只用本地 OCR（RapidOCR，数据不出本机），截图绝不上传云端。
    llm_vision_enabled: bool = True

    # 模型池故障转移（可选）：按顺序尝试，主端点失败后依次切到备用端点，全部失败才降级模板。
    # JSON 数组，元素字段见 LlmEndpoint。示例（本地 Ollama，无需任何云端 key）：
    # LLM_FALLBACKS=[{"model":"qwen3:4b","base_url":"http://127.0.0.1:11434/v1/chat/completions","api_key":"ollama"}]
    llm_fallbacks: list[LlmEndpoint] = []

    @field_validator("llm_timeout", mode="before")
    @classmethod
    def _blank_timeout_to_default(cls, v):
        """容错：.env 里写成空的 ``LLM_TIMEOUT=`` 时退回 45s，而不是启动即崩溃。

        其余数值型配置沿用原行为不变；这里单独加固是因为 LLM_TIMEOUT 是新增项，
        用户很容易照抄成空值，而 pydantic 对空字符串转 float 会抛 ValidationError，
        导致整个机器人起不来——宁可退回默认值也不能让服务挂掉。
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return 45.0
        return v

    @field_validator(
        "broadcast_groups", "allowed_groups", "llm_fallbacks", "weekly_events", mode="before"
    )
    @classmethod
    def _lenient_json_list(cls, v, info):
        """容错：JSON 数组整体或单个元素写错时，丢弃坏项并告警，不让启动崩溃。

        只校验「整体是不是数组」不够：`[1, 2, "oops"]`、`[{"model": "x"}]` 这类
        元素级错误同样会在 import 阶段抛 ValidationError，把机器人拦在启动之外。
        """
        raw = _parse_json_list(v, info.field_name)
        if not isinstance(raw, list):
            return raw  # 交给 pydantic 报出更明确的类型错误
        if info.field_name in {"broadcast_groups", "allowed_groups"}:
            return _clean_group_ids(raw, info.field_name)
        if info.field_name == "weekly_events":
            return _clean_weekly_events(raw, info.field_name)
        return _clean_llm_fallbacks(raw, info.field_name)

    @field_validator("activity_start_month", mode="before")
    @classmethod
    def _clean_activity_month(cls, v):
        """容错：ACTIVITY_START_MONTH 写成 2026-10 / 2026.10 / 2026年10月 均可。

        解析失败退回 2026-10 并告警，避免一个手滑的 .env 值让机器人启动即崩。
        """
        m = re.fullmatch(r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*月?", str(v or "").strip())
        if not m:
            if v not in (None, ""):
                _log.warning(f"配置 activity_start_month={v!r} 无法解析，已退回 2026-10")
            return "2026-10"
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            _log.warning(f"配置 activity_start_month={v!r} 月份越界，已退回 2026-10")
            return "2026-10"
        return f"{y:04d}-{mo:02d}"

    @field_validator("broadcast_hour", "broadcast_minute", mode="before")
    @classmethod
    def _clamp_broadcast_time(cls, v, info):
        """播报时刻越界（如 25 点、-1 分）时夹到合法区间并告警，避免启动即崩。"""
        limit = 23 if info.field_name == "broadcast_hour" else 59
        try:
            n = int(v)
        except (TypeError, ValueError):
            default = 23 if info.field_name == "broadcast_hour" else 0
            _log.warning(
                f"配置 {info.field_name} 不是整数（{v!r}），已退回默认值 {default}；请检查 .env"
            )
            return default
        if n < 0 or n > limit:
            clamped = min(max(n, 0), limit)
            _log.warning(
                f"配置 {info.field_name}={n} 越界（0~{limit}），已夹到 {clamped}；请检查 .env"
            )
            return clamped
        return n


settings = Settings()

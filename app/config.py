import json
import logging

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)


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
        """容错：.env 里 JSON 数组写错（漏逗号 / 尾逗号 / 手误）时退回空列表并告警。

        这类配置写错会导致 Settings 在 import 阶段抛 ValidationError，机器人整个起不来；
        而它们都只是「增强/限制」性质的可选项，留空完全可运行——宁可带告警降级，
        也不能让一行手误的配置把服务打挂。
        """
        if v is None or v == "" or v == []:
            return []
        if isinstance(v, (list, tuple)):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as e:
                _log.warning(
                    f"配置 {info.field_name} 不是合法 JSON（{e}），已退回默认值；请检查 .env"
                )
                return []
            # JSON 合法但形状不对（如对象而非数组）同样视为配置错误，降级默认值
            if not isinstance(parsed, list):
                _log.warning(
                    f"配置 {info.field_name} 应为 JSON 数组（当前是对象），已退回默认值；请检查 .env"
                )
                return []
            return parsed
        return v


settings = Settings()

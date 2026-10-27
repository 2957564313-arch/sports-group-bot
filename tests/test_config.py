"""配置层稳定性测试：.env 写错时不能导致启动失败，可选项应降级为默认值。

全部用例用 ``_env_file=None`` 隔离项目根目录的真实 .env（测试不应依赖本机配置）。
"""

from app.config import Settings, WeeklyEvent


def _S(**kw) -> Settings:
    """构造不读 .env 的 Settings（隔离本机配置，测试可复现）。"""
    return Settings(_env_file=None, **kw)


def test_blank_llm_timeout_falls_back_to_default():
    """`.env` 里写成空的 LLM_TIMEOUT= 要退回 45s，而不是启动即 ValidationError。"""
    assert _S(llm_timeout="").llm_timeout == 45.0
    assert _S(llm_timeout="   ").llm_timeout == 45.0


def test_valid_llm_timeout_is_kept():
    assert _S(llm_timeout="25").llm_timeout == 25.0
    assert _S(llm_timeout=30).llm_timeout == 30.0


def test_default_bot_name_is_generic():
    """默认机器人名是通用称呼（不硬编码任何群专属名）。"""
    assert _S().bot_name == "运动搭子"
    assert _S(bot_name="我的跑团").bot_name == "我的跑团"


def test_invalid_json_list_falls_back_to_empty():
    """JSON 数组写错（漏逗号/手误/形状不对）时退回空列表，而不是启动即崩。"""
    assert _S(broadcast_groups="[1, 2, oops]").broadcast_groups == []
    assert _S(allowed_groups="不是数组").allowed_groups == []
    assert _S(llm_fallbacks='{"model": 1}').llm_fallbacks == []
    assert _S(weekly_events="[{broken").weekly_events == []


def test_valid_json_lists_are_parsed():
    """合法 JSON 数组正常解析（含 nested 对象）。"""
    assert _S(broadcast_groups="[123, 456]").broadcast_groups == [123, 456]
    assert _S(allowed_groups=[7]).allowed_groups == [7]
    events = _S(
        weekly_events='[{"weekday": 2, "name": "例训", "time": "16:00", "location": "田径场"}]'
    ).weekly_events
    assert events == [WeeklyEvent(weekday=2, name="例训", time="16:00", location="田径场")]


def test_blank_lists_are_empty():
    """空字符串 / None / 空列表一律视为未配置。"""
    assert _S(broadcast_groups="").broadcast_groups == []
    assert _S(weekly_events=None).weekly_events == []
    assert _S(llm_fallbacks=[]).llm_fallbacks == []


def test_gift_texts_configurable():
    """礼物/奖品文案可配置，默认是通用文案（不含群专属内容）。"""
    s = _S()
    assert s.gift_name == "惊喜小礼物"
    assert s.lottery_prize == "惊喜奖品"
    assert _S(gift_name="纪念徽章").gift_name == "纪念徽章"


def test_semester_start_default_counts_everything():
    """默认打卡起点 = 1970-01-01（统计全部历史），各群可自行改为学期起始日。"""
    assert _S().semester_start == "1970-01-01"

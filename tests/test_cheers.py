"""鼓励语库纯函数测试：时段称呼 / 当日多次运动文案 / 时段开场。"""

from app.services.cheers import day_period, multi_session_note, opener


def test_day_period_boundaries():
    assert day_period(None) == ""
    assert day_period(4 * 60) == "清晨"
    assert day_period(8 * 60 + 59) == "清晨"
    assert day_period(9 * 60) == "上午"
    assert day_period(12 * 60) == "中午"
    assert day_period(14 * 60) == "下午"
    assert day_period(18 * 60) == "傍晚"
    assert day_period(21 * 60) == "晚上"
    assert day_period(2 * 60) == "深夜"


def test_multi_session_note():
    assert multi_session_note(1) == ""
    assert "第 2 次" in multi_session_note(2)
    assert "第 3 次" in multi_session_note(3)
    assert "第 4 次" in multi_session_note(4)


def test_opener_with_period_and_distance():
    # 有开始时段 + 距离 → 优先用时段开场（「清晨的 5.2 km，收下啦！」）
    text = opener({"distance_km": 5.2}, period="清晨")
    assert "清晨" in text and "5.2" in text
    # 无距离时时段开场不生效（退回通用开场池）
    assert "清晨" not in opener({"steps": 8000}, period="清晨")

"""输入安全加固：清理提问文本 + 拦截提示词注入（纯函数，无 nonebot/config 依赖）。

@机器人 收到的群友原话与多轮上下文，都可能被用来污染/注入 LLM。这里在进入 LLM 前
加一道本地、确定性、可单测的防线：不可见字符清洗、Unicode 归一化、超长截断、指令覆盖类注入拦截。

加固要点（避免「插空格 / 插零宽字符」绕过）：
- 先按 Unicode 类别删除 Cf（零宽、方向控制、软连字符、BOM）与 Cc（控制符，换成空格）；
- 再做 NFKC 归一化，把全角拉丁字母、全角括号、兼容字符折叠成标准形式；
- 注入判定同时匹配「原样」与「去掉全部空白」两种形态。
"""

import re
import unicodedata

# 控制字符：替换为空格，避免把换行两侧的词粘在一起。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# 允许保留的可见空白：普通空格已在上面统一折叠。
_WS_RE = re.compile(r"\s+")

# 指令覆盖/越权类注入模式（大小写不敏感）。目标是把机器人「带偏」成别的角色、
# 或套取系统提示词/设定。刻意不拦「你是谁 / 你是什么模型」——那走 _PERSONA +
# _guard_identity 正常应答。
_INJECTION_PATTERNS = (
    r"忽略(?:之前|以上|所有|一切|你的)?(?:的)?(?:指令|提示|设定|规则|要求|之前的话)",
    r"无视(?:之前|以上|所有|你的)?(?:的)?(?:指令|提示|设定|规则)",
    r"(?:忘掉|忘记|清除|清空|删除)(?:之前|你|你的)?(?:的)?(?:指令|提示|设定|规则|记忆|对话)",
    r"扮演|假装|伪装成|role\s*play|pretend|act\s+as|you\s+are\s+now|你就是",
    r"系统提示(?:词)?|system\s*prompt|你的提示词|你的设定|你的角色",
    r"开发者模式|developer\s*mode|jail\s*break|越狱",
    r"(?:泄露|泄漏|告诉我|说出|显示|打印|吐出)(?:你的|系统)?(?:提示词|设定|指令|prompt|秘密)",
    r"ignore\s+(?:all|previous|above|the|your)?\s*(?:previous|above|all|the|your)?\s*"
    r"(?:instructions|prompt|directives|rules)",
    r"disregard|override\s+your",
    r"(?:现在|从今(?:天|以后)|从此)起你是|你不再是",
)

# 常见不可见字符（除了按 Unicode 类别清理外，再兜一层显式清单）。
_INVISIBLE_CHARS = (
    "\u200b\u200c\u200d\u200e\u200f"  # 零宽空格/连接符/方向控制
    "\u202a\u202b\u202c\u202d\u202e"  # 双向控制
    "\u2060\u2061\u2062\u2063\u2064"  # 词连接符、不可见运算符
    "\u00ad"  # 软连字符
    "\u180e"  # 蒙古文元音分隔符
    "\u3164"  # 韩文填充符
    "\ufeff"  # BOM
    "\ufe00\ufe01\ufe0f"  # 变体选择符
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")


def _strip_invisibles(text: str) -> str:
    """删除不可见字符，把控制符换成空格（按 Unicode 类别 + 显式清单双重清理）。"""
    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cf":  # 零宽 / 方向控制 / 软连字符 / BOM
            continue
        if cat == "Cc":  # 控制字符（含 \n \t \x00）
            out.append(" ")
            continue
        out.append(ch)
    return _INVISIBLE_RE.sub("", "".join(out))


def clean_question(text: str, max_len: int = 400) -> str:
    """清洗群友提问：剔不可见字符、折叠空白、超长截断。

    这里刻意不做 NFKC：NFKC 会把中文全角标点（「？！」「（）」）折成半角，
    改变用户原话语义；注入判定另走 ``_normalized_variants`` 做归一化。
    """
    if not text:
        return ""
    t = _strip_invisibles(str(text))
    t = _CONTROL_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > max_len:
        t = t[:max_len].rstrip()
    return t


def _normalized_variants(text: str) -> tuple[str, str]:
    """返回（原样小写, 去空白小写）两种形态，供注入模式双形态匹配。"""
    t = unicodedata.normalize("NFKC", _strip_invisibles(text or "")).strip()
    return t.lower(), _WS_RE.sub("", t).lower()


def looks_like_injection(text: str) -> bool:
    """本地判定提问是否是指令覆盖/越权类注入（命中任一模式返回 True）。

    同时检查原样与「去掉全部空白」两种形态：`忽 略 之 前 的 指 令`、
    `忽\\u200b略之前的指令` 这类插空绕过都会在归一化后命中。
    """
    if not text:
        return False
    variants = _normalized_variants(text)
    if not variants[0]:
        return False
    return any(re.search(p, v, re.IGNORECASE) for p in _INJECTION_PATTERNS for v in variants)

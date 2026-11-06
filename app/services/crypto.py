"""统一凭据加密：Fernet 对称加密 + ``data/secret.key`` 自动生成。

所有敏感落盘数据（佳明邮箱+密码、COROS OAuth token）统一经此模块加密。
设计要点：
  - 密钥固定放在 **仓库根目录下的 data/secret.key**（``data/`` 已在 .gitignore），
    不再依赖进程工作目录——从别处启动机器人时，相对路径会「悄悄生成一把新钥匙」，
    导致所有已绑定成员永久无法解密；
  - 密钥缺失且磁盘上已有加密凭据时**拒绝自动生成**并抛错，逼运维从备份恢复，
    而不是静默清空全部绑定；
  - 明文兜底读取 + 一次性迁移，保证老数据不中断；
  - 解密失败（密钥被换 / 密文损坏）返回 None，宁可判「未绑定/未授权」也不吐错误凭据。
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger(__name__)

# 仓库根 = app/services/crypto.py 上溯两级
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_KEY_FILE = _DATA_DIR / "secret.key"
_fernet: Optional[Fernet] = None


def _accounts_dir() -> Path:
    """凭据目录跟随密钥目录（测试里 monkeypatch ``_KEY_FILE`` 时会自动隔离）。"""
    return _KEY_FILE.parent / "accounts"


def _has_encrypted_accounts() -> bool:
    """磁盘上是否已有用 Fernet 加密的凭据（用于判断「密钥丢失」是否致命）。"""
    d = _accounts_dir()
    if not d.exists():
        return False
    for p in d.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and "_enc" in raw:
            return True
    return False


def _create_key(path: Path) -> bytes:
    """原子生成密钥：O_EXCL 抢占，避免两个进程同时启动时互相覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # 另一个进程刚建好，直接复用它的
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _get_fernet() -> Fernet:
    """读取或自动生成密钥，返回 Fernet 实例（模块级缓存）。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    if _KEY_FILE.exists():
        key = _KEY_FILE.read_bytes().strip()
    elif _has_encrypted_accounts():
        raise RuntimeError(
            f"密钥文件 {_KEY_FILE} 不存在，但 data/accounts 下已有加密凭据——"
            "此时自动生成新密钥会让所有已绑定成员永久无法解密。"
            "请从备份恢复 data/secret.key（该文件丢失=全部凭据永久作废）。"
        )
    else:
        key = _create_key(_KEY_FILE)
        _log.warning("首次运行：已生成凭据加密密钥 %s，请立即备份（丢失则全部凭据作废）", _KEY_FILE)
    try:
        _fernet = Fernet(key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            f"密钥文件 {_KEY_FILE} 内容非法（{e}）：请从备份恢复，不要手动新建空文件。"
        ) from e
    return _fernet


def encrypt_json(data: dict) -> dict:
    """把 dict 加密成 ``{"_enc": "<token>"}`` 结构，可直接 json.dumps 落盘。"""
    token = _get_fernet().encrypt(json.dumps(data, ensure_ascii=False).encode())
    return {"_enc": token.decode()}


def decrypt_json(raw: dict) -> Optional[dict]:
    """解密封装结构；明文老数据原样返回（幂等迁移）。

    返回 None 表示无法读取（密钥被换 / 密文损坏 / 字段类型异常），
    由调用方视为「未绑定/未授权」。
    """
    if not isinstance(raw, dict):
        return raw
    if "_enc" not in raw:
        return raw  # 旧明文，兼容返回
    token = raw["_enc"]
    if not isinstance(token, str):
        _log.warning("凭据密文字段 _enc 不是字符串（%r），按损坏处理", type(token).__name__)
        return None
    try:
        return json.loads(_get_fernet().decrypt(token.encode()).decode("utf-8"))
    except (InvalidToken, ValueError):
        return None


def migrate_encrypt() -> int:
    """把 ``data/accounts/*.json`` 下旧明文凭据升级为加密格式，返回迁移文件数（幂等）。"""
    accounts_dir = _accounts_dir()
    if not accounts_dir.exists():
        return 0
    n = 0
    for p in accounts_dir.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(raw, dict) and "_enc" not in raw:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(encrypt_json(raw), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, p)
            n += 1
    return n

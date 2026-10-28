"""凭据加密 —— API Key 的静态加密与掩码。

设计要点:

- 用 Fernet(对称 AES-128-CBC + HMAC)做静态加密,只解决「数据库文件被拿走
  时明文不外泄」,不替代传输加密。
- 主密钥来源优先级:环境变量 ``VERIDROP_SECRET_KEY`` > 密钥文件
  (``VERIDROP_SECRET_FILE``,默认 ``web_data/secret.key``)。生产环境建议
  显式配置环境变量,文件只作为本地开发的零配置兜底。
- Fernet 实例模块级缓存:每次请求都重新构造会重复解析密钥,且无法在测试
  里替换;``reset_cipher()`` 供测试清缓存。
- 解密失败(密文损坏、换了主密钥)统一抛 ``CredentialDecryptError``,路由层
  据此返回 503,而不是把底层异常细节漏给前端。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from relay_detector.models import mask_api_key


logger = logging.getLogger("veridrop")

DEFAULT_SECRET_FILE = Path("web_data") / "secret.key"


class CredentialDecryptError(Exception):
    """凭据无法解密(密文损坏或主密钥不匹配)。"""


_cipher: Fernet | None = None


def _secret_file_path() -> Path:
    """密钥文件路径;环境变量为空时用默认的 web_data/secret.key。"""
    raw = (os.environ.get("VERIDROP_SECRET_FILE") or "").strip()
    return Path(raw) if raw else DEFAULT_SECRET_FILE


def _load_key() -> bytes:
    """取 Fernet key:优先环境变量,否则读文件(不存在则生成)。"""
    env_key = (os.environ.get("VERIDROP_SECRET_KEY") or "").strip()
    if env_key:
        return env_key.encode("ascii")

    path = _secret_file_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        # 文件存在但读不了(权限/被占用)时无法兜底,直接上抛交给调用方。
        raise CredentialDecryptError(f"cannot read secret file: {exc}") from exc

    if existing:
        return existing.encode("ascii")

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_text(key.decode("ascii"), encoding="utf-8")
    try:
        # Windows 上 chmod 基本是 no-op,失败也不影响功能。
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.warning(
        "未配置 VERIDROP_SECRET_KEY，已自动生成本地密钥文件 %s，"
        "生产建议配置 VERIDROP_SECRET_KEY",
        path,
    )
    return key


def _get_cipher() -> Fernet:
    """懒加载并缓存 Fernet 实例。"""
    global _cipher
    if _cipher is None:
        try:
            _cipher = Fernet(_load_key())
        except (ValueError, UnicodeError) as exc:
            # 环境变量不是合法的 44 字符 Fernet key 时同样视为不可用。
            raise CredentialDecryptError(f"invalid secret key: {exc}") from exc
    return _cipher


def reset_cipher() -> None:
    """清空缓存的主密钥(测试用;改了环境变量后需调用)。"""
    global _cipher
    _cipher = None


def encrypt_api_key(plain: str) -> str:
    """明文 → Fernet token(ASCII 字符串,可直接入库)。"""
    return _get_cipher().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    """Fernet token → 明文;失败抛 CredentialDecryptError。"""
    try:
        return _get_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise CredentialDecryptError("credential cannot be decrypted") from exc


def mask_key(key: str) -> str:
    """复用检测器的掩码规则,保证报告与凭据库展示一致。"""
    return mask_api_key(key)

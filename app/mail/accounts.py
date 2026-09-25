# -*- coding: utf-8 -*-
"""邮箱账户配置 config/mail.json（只存元数据：地址、服务商、开关；**密码/令牌不在这里**）。

config/mail.json 含个人邮箱地址，已加入 .gitignore；模板见 config/mail.example.json。
"""
import json
import re
from pathlib import Path

from . import credentials
from .providers import PRESETS, guess_provider

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "mail.json"

DEFAULT_CONFIG = {
    "poll_min": 5,          # 轮询间隔（分钟）
    "recent_count": 10,     # 每个账户缓存最近几封
    "notify_new": True,     # 有新未读时冒气泡
    "accounts": [],
    "oauth": {
        "microsoft": {"client_id": "", "tenant": "common"},
        "google": {"client_id": "", "client_secret": ""},
    },
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k == "oauth" and isinstance(v, dict):
                    for kind, spec in v.items():
                        cfg["oauth"].setdefault(kind, {}).update(spec)
                else:
                    cfg[k] = v
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def enabled_accounts(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load()
    return [a for a in cfg.get("accounts", []) if a.get("enabled", True) and a.get("address")]


def _make_id(address: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", address.lower()).strip("_")[:40] or "mail"
    aid, n = base, 2
    while aid in existing:
        aid, n = f"{base}_{n}", n + 1
    return aid


def add_account(address: str, provider: str | None = None, secret: str | None = None,
                name: str = "", **overrides) -> dict:
    """新增账户；secret（授权码）直接进凭据管理器。返回账户 dict。"""
    address = address.strip()
    if "@" not in address:
        raise ValueError("邮箱地址不对")
    provider = provider or guess_provider(address) or "custom"
    if provider != "custom" and provider not in PRESETS:
        raise ValueError(f"不支持的服务商：{provider}")
    cfg = load()
    if any(a["address"].lower() == address.lower() for a in cfg["accounts"]):
        raise ValueError(f"{address} 已经添加过了")
    acc = {"id": _make_id(address, {a["id"] for a in cfg["accounts"]}),
           "address": address, "provider": provider, "name": name, "enabled": True}
    acc.update({k: v for k, v in overrides.items() if v not in (None, "")})
    cfg["accounts"].append(acc)
    save(cfg)
    if secret:
        credentials.set_secret(credentials.password_key(acc["id"]), secret, username=address)
    return acc


def remove_account(account_id: str) -> None:
    cfg = load()
    cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != account_id]
    save(cfg)
    for key in (credentials.password_key(account_id), credentials.token_key(account_id)):
        try:
            credentials.delete_secret(key)
        except OSError:
            pass


def find(ref: str, cfg: dict | None = None) -> dict | None:
    """按 id、完整地址或地址前缀/序号（1 起）找账户。"""
    accs = (cfg or load()).get("accounts", [])
    ref = (ref or "").strip().lower()
    if ref.isdigit() and 1 <= int(ref) <= len(accs):
        return accs[int(ref) - 1]
    for a in accs:
        if ref in (a["id"].lower(), a["address"].lower()):
            return a
    for a in accs:
        if a["address"].lower().startswith(ref):
            return a
    return None

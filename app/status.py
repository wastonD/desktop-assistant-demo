# -*- coding: utf-8 -*-
"""data/status.json：各后台模块写入的小状态（unread_mail、news_ready…），看板/面板读取。

多个线程都会写，这里加锁 + 读改写 + 原子替换，避免互相覆盖或读到半截文件。
"""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "data" / "status.json"
_lock = threading.Lock()


def read() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update(**fields) -> dict:
    """合并写入；值为 None 的键会被删除。返回写入后的完整状态。"""
    with _lock:
        data = read()
        for k, v in fields.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATUS_PATH)
        return data

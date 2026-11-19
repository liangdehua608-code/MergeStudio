"""极小的 JSON 落盘工具：原子写 + 容错读。

队列、配置、运行记录都只是几十 KB 的字典，不值得上数据库；但直接 write_text
在崩溃/断电时会留下半截文件，下次启动解析失败就等于把数据丢了。所以统一走这里：
先写 `.tmp`，再 `os.replace` 原子替换，读失败一律返回默认值。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def read_json(path, default=None):
    p = Path(path)
    try:
        if not p.is_file():
            return default
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data) -> bool:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False

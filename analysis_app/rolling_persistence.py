"""rolling-interval 状态存盘与恢复。

状态 JSON 与报告 JSON 同样走临时文件 + 原子替换；
恢复后继续跑的结果必须与不中断运行逐字段一致（由测试保证）。
"""
import json
import os
import tempfile
from pathlib import Path

from .rolling_engine import RollingIntervalEngine


def save_state(path, engine):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.rolling-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(engine.to_dict(), handle, ensure_ascii=False,
                      sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path):
    with Path(path).open(encoding='utf-8') as handle:
        payload = json.load(handle)
    return RollingIntervalEngine.from_dict(payload)

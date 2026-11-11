import csv
import hashlib
import json
import math
from pathlib import Path

from .schema import FIELDS, REQUIRED


def load_records(path):
    converters = {'str': str, 'float': float, 'int': int}
    rows = []
    seen = set()
    with Path(path).open(encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError('缺少字段：' + ','.join(sorted(missing)))
        for position, raw in enumerate(reader, 2):
            row = {}
            for name, kind in FIELDS.items():
                value = raw[name]
                if value is None or value == '':
                    if name in REQUIRED:
                        raise ValueError(f'第{position}行必填字段为空：{name}')
                    row[name] = None
                    continue
                try:
                    converted = converters[kind](value)
                except (ValueError, TypeError):
                    raise ValueError(f'第{position}行字段类型不符：{name}') from None
                if kind == 'float' and not math.isfinite(converted):
                    raise ValueError(f'第{position}行字段不是有限数：{name}')
                row[name] = converted
            if row['observation_id'] in seen:
                raise ValueError('重复观测编号：' + row['observation_id'])
            seen.add(row['observation_id'])
            rows.append(row)
    return rows


def fingerprint(records):
    canonical = sorted(records, key=lambda row: row['observation_id'])
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()

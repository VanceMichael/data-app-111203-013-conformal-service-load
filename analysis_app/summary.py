import statistics
from collections import defaultdict

from .domain import describe
from .io import fingerprint
from .schema import ENTITY, GROUP, MEASURE, PRODUCT, UNIT


def numeric_summary(values):
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return {'observed': 0, 'mean': None, 'minimum': None, 'maximum': None, 'median': None}
    return {'observed': len(clean), 'mean': statistics.fmean(clean),
            'minimum': clean[0], 'maximum': clean[-1], 'median': statistics.median(clean)}


def summarize(records):
    ordered = sorted(records, key=lambda row: row['observation_id'])
    groups = defaultdict(list)
    for row in ordered:
        groups[row[GROUP]].append(row)
    result = []
    for key in sorted(groups):
        members = groups[key]
        result.append({'group': key, 'rows': len(members),
                       'entities': len({row[ENTITY] for row in members}),
                       'measurement': numeric_summary([row[MEASURE] for row in members]),
                       'domain': describe(members)})
    return {'product': PRODUCT, 'schema_version': 1, 'unit': UNIT,
            'rows': len(records), 'entities': len({row[ENTITY] for row in records}),
            'input_fingerprint': fingerprint(ordered), 'groups': result}

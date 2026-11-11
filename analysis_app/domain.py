import math
import statistics
from collections import Counter


def describe(records):
    capacity = math.fsum(row['capacity'] for row in records)
    load = math.fsum(row['load'] for row in records)
    return {'load_total': load, 'capacity_total': capacity, 'utilization': load / capacity if capacity else None}

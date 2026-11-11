FIRST_OVERRIDE = {'load': 2, 'capacity': 4}
SECOND_OVERRIDE = {'load': 4, 'capacity': 8}
METRIC = 'utilization'
EXPECTED = 0.5

import copy
import csv
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis_app.cli import save_json
from analysis_app.domain import describe
from analysis_app.io import fingerprint, load_records
from analysis_app.schema import ENTITY, FIELDS, GROUP, MEASURE
from analysis_app.summary import numeric_summary, summarize


class RecordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_records('data/observations.csv')

    def write_csv(self, directory, rows, fields=None):
        path = Path(directory) / 'input.csv'
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields or FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_fixture_size_and_entities(self):
        self.assertEqual(len(self.records), 288)
        self.assertGreaterEqual(len({row[ENTITY] for row in self.records}), 24)

    def test_group_counts_are_conserved(self):
        result = summarize(self.records)
        self.assertEqual(sum(group['rows'] for group in result['groups']), 288)
        self.assertEqual(result['entities'], len({row[ENTITY] for row in self.records}))

    def test_missing_measurements_have_explicit_denominator(self):
        self.assertEqual(numeric_summary([None, 2, 4]), {'observed': 2, 'mean': 3, 'minimum': 2, 'maximum': 4, 'median': 3})

    def test_empty_numeric(self):
        self.assertIsNone(numeric_summary([None])['mean'])
        self.assertEqual(numeric_summary([])['observed'], 0)

    def test_empty_dataset(self):
        result = summarize([])
        self.assertEqual(result['rows'], 0)
        self.assertEqual(result['groups'], [])

    def test_input_order_does_not_change_result(self):
        shuffled = copy.deepcopy(self.records)
        random.Random(42).shuffle(shuffled)
        self.assertEqual(summarize(shuffled), summarize(self.records))

    def test_fingerprint_reflects_value_changes(self):
        changed = copy.deepcopy(self.records)
        changed[0][MEASURE] = 12345.0
        self.assertNotEqual(fingerprint(changed), fingerprint(self.records))

    def test_missing_column_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [], ['observation_id'])
            with self.assertRaisesRegex(ValueError, '缺少字段'):
                load_records(path)

    def test_duplicate_observation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [self.records[0], self.records[0]])
            with self.assertRaisesRegex(ValueError, '重复观测'):
                load_records(path)

    def test_nonfinite_measure_rejected(self):
        record = dict(self.records[0], **{MEASURE: 'NaN'})
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [record])
            with self.assertRaisesRegex(ValueError, '有限数'):
                load_records(path)

    def test_empty_entity_rejected(self):
        record = dict(self.records[0], **{ENTITY: ''})
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [record])
            with self.assertRaisesRegex(ValueError, '必填'):
                load_records(path)

    def test_bad_type_rejected(self):
        record = dict(self.records[0], **{MEASURE: '错误数值'})
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(directory, [record])
            with self.assertRaisesRegex(ValueError, '类型不符'):
                load_records(path)

    def test_domain_accounting(self):
        first, second = copy.deepcopy(self.records[:2])
        first.update(FIRST_OVERRIDE)
        second.update(SECOND_OVERRIDE)
        self.assertAlmostEqual(describe([first, second])[METRIC], EXPECTED)

    def test_atomic_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested' / 'result.json'
            save_json(path, {'value': 1})
            save_json(path, {'value': 2})
            self.assertEqual(json.loads(path.read_text()), {'value': 2})
            self.assertEqual([item.name for item in path.parent.iterdir()], ['result.json'])

    def test_cli_matches_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            run = subprocess.run([sys.executable, '-m', 'analysis_app', 'summary', '--output', str(path)], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), summarize(self.records))
            self.assertEqual(json.loads(path.read_text()), json.loads(run.stdout))

    def test_cli_failure_has_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            run = subprocess.run([sys.executable, '-m', 'analysis_app', 'summary', '--input', str(path.parent / 'missing.csv'), '--output', str(path)], capture_output=True)
            self.assertEqual(run.returncode, 2)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()

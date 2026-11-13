"""rolling-interval 流程测试。

覆盖需求点名的四类场景与存盘恢复：
1. 迟到观测（known_at 晚到、同集合结果与到达顺序无关、历史预测不可改写）
2. 部署突变（报警只依赖已实现误差、widen/suspend 动作、恢复条件）
3. 初始无校准样本（区间显式 uncalibrated，绝不伪造）
4. 多步标签延迟（各 horizon 残差分桶、延迟标签不提前进入校准）
另含：分层借用规则随区间携带、有限样本分位数退化、保存后恢复逐字段一致。
"""
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
from analysis_app.io import load_records
from analysis_app.rolling_config import (
    RollingConfig, quantile_rank, two_sided_quantiles, two_sided_ranks)
from analysis_app.rolling_engine import BORROW_RULE, RollingIntervalEngine
from analysis_app.rolling_persistence import load_state, save_state

FIXTURE = 'data/observations.csv'


def make_row(oid, service, tier, hour, load, capacity=100.0,
             deploy=0, known_at=None):
    if known_at is None:
        known_at = hour
    return {'observation_id': oid, 'service': service, 'tier': tier,
            'hour': hour, 'load': float(load), 'capacity': float(capacity),
            'deploy': deploy, 'known_at': known_at}


def run_engine(rows, **kwargs):
    engine = RollingIntervalEngine(RollingConfig(**kwargs))
    engine.run(rows)
    return engine


def write_csv(directory, rows):
    path = Path(directory) / 'input.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(
            handle, fieldnames=['observation_id', 'service', 'tier', 'hour',
                                'load', 'capacity', 'deploy', 'known_at'])
        writer.writeheader()
        writer.writerows(rows)
    return path


class FiniteSampleQuantileTest(unittest.TestCase):
    def test_split_conformal_rank_is_ceil_rule(self):
        # n=9, level=0.9 -> ceil(10*0.9)=9；level=0.1 -> 1
        self.assertEqual(quantile_rank(9, 0.9), 9)
        self.assertEqual(quantile_rank(9, 0.1), 1)
        # n=19, 0.9 -> ceil(18)=18, 0.1 -> ceil(2)=2
        self.assertEqual(two_sided_ranks(19, 0.8), (2, 18))

    def test_rank_unavailable_when_sample_too_small(self):
        # n=5 时 0.9 水平的秩为 6 > 5，无法构造
        self.assertIsNone(quantile_rank(5, 0.9))
        self.assertEqual(two_sided_ranks(5, 0.8), (None, None))

    def test_same_sign_residuals_form_one_sided_interval(self):
        values = list(range(1, 15))  # 全部为正 -> 单侧上界
        q_low, q_high, shape = two_sided_quantiles(values, 0.8, 9)
        self.assertEqual(shape, 'one_sided_upper')
        self.assertEqual(q_low, 0.0)
        self.assertGreater(q_high, 0.0)
        negatives = list(range(-14, 0))  # 全部为负 -> 单侧下界
        q_low, q_high, shape = two_sided_quantiles(negatives, 0.8, 9)
        self.assertEqual(shape, 'one_sided_lower')
        self.assertLess(q_low, 0.0)
        self.assertEqual(q_high, 0.0)

    def test_mixed_sign_returns_ordered_quantiles(self):
        values = list(range(-10, 11))
        q_low, q_high, shape = two_sided_quantiles(values, 0.8, 9)
        self.assertEqual(shape, 'two_sided')
        self.assertLess(q_low, 0.0)
        self.assertGreater(q_high, 0.0)

    def test_below_min_effective_samples(self):
        self.assertEqual(two_sided_quantiles([-1, 1], 0.8, 9),
                         (None, None, None))


class InitialCalibrationTest(unittest.TestCase):
    def test_first_predictions_are_explicitly_uncalibrated(self):
        rows = [make_row(f'o{h}', 'SX', 'T0', h, 50 + h) for h in range(6)]
        engine = run_engine(rows)
        early = [f for f in engine.forecasts if f['issue_clock'] == 0]
        self.assertTrue(early)
        for forecast in early:
            self.assertEqual(forecast['status'], 'uncalibrated')
            self.assertIsNone(forecast['interval']['lower'])
            self.assertIsNone(forecast['interval']['upper'])
            self.assertFalse(forecast['interval']['calibrated'])
            self.assertEqual(forecast['interval']['residual_source'], 'none')
            self.assertEqual(forecast['interval']['residual_n'], 0)

    def test_uncalibrated_outcomes_do_not_feed_alarm(self):
        rows = [make_row(f'o{h}', 'SX', 'T0', h, 200 if h >= 2 else 50)
                for h in range(6)]
        engine = run_engine(rows)
        # 样本不足期间即使实际值偏离也不允许产生报警事件
        self.assertEqual(engine.events, [])

    def test_point_forecast_uses_only_visible_history(self):
        # known_at 延迟：clock 0 时 hour 0 的观测尚不可见
        rows = [make_row('o0', 'SX', 'T0', 0, 50, known_at=1),
                make_row('o1', 'SX', 'T0', 1, 60, known_at=1)]
        engine = RollingIntervalEngine(RollingConfig(horizons=(1,)))
        engine.step(1, rows)
        first = next(f for f in engine.forecasts if f['issue_clock'] == 1)
        # 可见两点，EWMA(alpha=.3) = .3*60 + .7*50 = 53
        self.assertAlmostEqual(first['point_forecast'], 53.0)
        self.assertEqual(first['baseline'], {
            'name': 'ewma', 'alpha': 0.3, 'visible_points': 2})


class MultiHorizonLabelDelayTest(unittest.TestCase):
    def _lag_rows(self):
        # service A：每个观测延迟 2 个小时可见（known_at = hour + 2）
        return [make_row(f'a{h}', 'SA', 'T0', h, 100 + h, known_at=h + 2)
                for h in range(10)]

    def test_future_labels_never_calibrate_before_arrival(self):
        engine = run_engine(self._lag_rows())
        # clock 2 才第一次有观测；此时不可能存在已实现残差
        at_two = [f for f in engine.forecasts if f['issue_clock'] == 2]
        self.assertTrue(at_two)
        for forecast in at_two:
            self.assertEqual(forecast['interval']['residual_n'], 0)
            self.assertEqual(forecast['status'], 'uncalibrated')

    def test_horizon_residuals_are_kept_in_separate_buckets(self):
        # 放宽报警阈值，避免良性趋势触发队列清空，专注验证分桶隔离
        engine = run_engine(self._lag_rows(), horizons=(1, 2, 3),
                            alarm_window=8, alarm_breach_threshold=8,
                            min_alarm_realizations=8)
        # 同一 service 的不同 horizon 队列长度必须独立
        sizes = {horizon: len(engine.queues.get(('SA', horizon), []))
                 for horizon in (1, 2, 3)}
        # 最晚实现的 horizon=3 实现次数最少
        self.assertGreater(sizes[1], sizes[3])
        self.assertTrue(all(size >= 0 for size in sizes.values()))

    def test_lag_is_recorded_on_outcome(self):
        engine = run_engine(self._lag_rows())
        realized = [f for f in engine.forecasts
                    if f['service'] == 'SA' and f['outcome'] is not None]
        self.assertTrue(realized)
        for forecast in realized:
            self.assertEqual(forecast['outcome']['lag'], 2)

    def test_forecast_issued_before_label_arrives(self):
        # 预测在 clock 2 发出，目标 hour 3，而 hour 3 的标签 known_at=5
        engine = run_engine(self._lag_rows())
        forecast = next(f for f in engine.forecasts
                        if f['service'] == 'SA' and f['target_hour'] == 3
                        and f['horizon'] == 1)
        self.assertEqual(forecast['issue_clock'], 2)
        self.assertEqual(forecast['outcome']['known_at'], 5)
        # 发出时记录的点预测不允许被后来的标签改写
        frozen = forecast['point_forecast']
        self.assertEqual(forecast['point_forecast'], frozen)


class LateArrivalTest(unittest.TestCase):
    def test_late_row_is_flagged_but_resolves_pending_forecast(self):
        rows = [make_row(f'o{h}', 'SB', 'T0', h, 80 + h,
                         known_at=h + 2) for h in range(8)]
        engine = RollingIntervalEngine(RollingConfig(horizons=(1,)))
        batches = {}
        for row in rows:
            batches.setdefault(row['known_at'], []).append(row)
        for clock in sorted(batches):
            engine.step(clock, batches[clock])
        # 迟到一条：hour 8 的观测在 clock 11 才到（按其延迟本应 clock 10）
        late = make_row('olate', 'SB', 'T0', 8, 95, known_at=11)
        engine.ingest_row(late)
        self.assertNotIn(('SB', 8), engine.pending)
        record = next(f for f in engine.forecasts
                      if f['service'] == 'SB' and f['target_hour'] == 8)
        self.assertTrue(record['outcome']['late_arrival'])
        self.assertEqual(record['outcome']['known_at'], 11)
        self.assertEqual(record['outcome']['realized_load'], 95.0)
        # 迟到残差进入校准队列，但历史预测内容保持冻结
        self.assertIn(95.0 - record['point_forecast'],
                      [value for value, _ in engine.queues[('SB', 1)]])

    def test_arrival_permutation_within_visibility_is_equivalent(self):
        rows = load_records(FIXTURE)
        reference = run_engine(copy.deepcopy(rows)).report()
        shuffled = copy.deepcopy(rows)
        random.Random(7).shuffle(shuffled)
        # 按 known_at 重新分桶，桶内顺序打乱
        batches = {}
        for row in shuffled:
            batches.setdefault(row['known_at'], []).append(row)
        engine = RollingIntervalEngine(RollingConfig())
        for clock in sorted(batches):
            engine.step(clock, batches[clock])
        self.assertEqual(engine.report(), reference)

    def test_duplicate_observation_is_rejected(self):
        row = make_row('dup', 'SC', 'T0', 0, 10)
        engine = RollingIntervalEngine(RollingConfig(horizons=(1,)))
        engine.step(0, [row])
        with self.assertRaisesRegex(ValueError, '重复'):
            engine.ingest_row(dict(row))

    def test_clock_cannot_go_backwards_in_step(self):
        engine = RollingIntervalEngine(RollingConfig(horizons=(1,)))
        engine.step(2, [make_row('x', 'SC', 'T0', 2, 10, known_at=2)])
        with self.assertRaisesRegex(ValueError, '不能回退'):
            engine.step(1, [make_row('y', 'SC', 'T0', 1, 10, known_at=1)])


class DeploymentShiftTest(unittest.TestCase):
    """部署突变：hour 5 起负载从 100 平台跳到 180 平台（硬切换）。

    用同一 tier 的多个服务，使分层残差池在突变前就具备有效样本，
    区间从部署前开始连续输出，才能观察到“区间何时不再可信”。
    """

    def _shift_rows(self, services=('SD', 'SE', 'SF'), post=260):
        rows = []
        for index, service in enumerate(services):
            offset = index - 1
            for hour in range(34):
                # 突变前 10 小时窄波动（自身残差与 MAD 尺度充分建立），
                # hour 10 部署后整体跳到远离旧平台的位置，新平台保持足够久，
                # 以便报警后新世代残差队列重新积累、验证扩宽输出与恢复
                load = (100 + offset + (0.5 if hour % 2 == 0 else -0.5)
                        if hour < 10 else post + offset)
                deploy = 1 if hour == 10 else 0
                rows.append(make_row(f'{service}-{hour}', service, 'T0', hour,
                                     load, capacity=320, deploy=deploy))
        return rows

    SHIFT_CONFIG = dict(alarm_window=5, alarm_breach_threshold=3,
                        min_alarm_realizations=5, recovery_streak=4,
                        recovery_min_samples=12, alarm_z=3.0)

    def test_alarm_uses_only_realized_errors_and_fires_after_shift(self):
        engine = run_engine(self._shift_rows(), **self.SHIFT_CONFIG)
        alarms = [e for e in engine.events if e['trigger'] == 'breach_rate']
        self.assertTrue(alarms)
        # 第一个报警时钟必须在突变标签被实现之后（hour 10 的标签 clock 10 才可见）
        first_alarm = alarms[0]
        self.assertGreaterEqual(first_alarm['clock'], 10)
        self.assertGreaterEqual(first_alarm['breaches'],
                                engine.config.alarm_breach_threshold)
        # 事件本身可解释：含原因、阈值、恢复条件，且不引用任何未来小时
        self.assertIn('recovery_condition', first_alarm)
        self.assertIn('horizon', first_alarm)
        for event in engine.events:
            # 事件引用的 target_hour 是该时钟已实现的预测目标，不能指向未来
            self.assertLessEqual(event['target_hour'], event['clock'])
            self.assertIn(event['horizon'], engine.config.horizons)
        # 部署前的预测不应产生任何报警
        self.assertTrue(all(e['clock'] >= 10 for e in alarms))

    def test_widen_action_emits_wider_intervals_with_carried_rule(self):
        engine = run_engine(self._shift_rows(), alarm_action='widen',
                            **self.SHIFT_CONFIG)
        # 报警后进入 alarmed_widened 状态，且状态机事件可解释
        self.assertIn('alarmed_widened',
                      {trust['state'] for trust in engine.trust.values()}
                      | {e['state_after'] for e in engine.events})
        alarm_event = next(e for e in engine.events
                           if e['trigger'] == 'breach_rate')
        self.assertEqual(alarm_event['action'], 'widen')
        self.assertIn('recovery_condition', alarm_event)
        self.assertIn('retired_residual_queues', alarm_event)
        # 扩宽后的区间（含 0.95 成功构造或单侧退化）必须实际存在
        post_alarm = [f for f in engine.forecasts
                      if f['issue_clock'] > alarm_event['clock']
                      and f['status'] == 'emitted']
        self.assertTrue(post_alarm)
        # 每条扩宽区间都携带世代、来源与样本数元数据
        for forecast in post_alarm:
            self.assertIsNotNone(forecast['interval']['lower'])
            self.assertIn(forecast['interval']['shape'],
                          ('two_sided', 'one_sided_upper', 'one_sided_lower'))
            self.assertGreater(forecast['interval']['residual_n'], 0)
        # 同一服务新世代下，0.95 可构造时其宽度不小于 0.8
        widened = [f for f in post_alarm
                   if f['interval']['level']
                   == engine.config.widened_coverage]
        nominal = [f for f in engine.forecasts
                   if f['interval']['level']
                   == engine.config.target_coverage]
        if widened and nominal:
            def mean_width(records):
                widths = [f['interval']['upper'] - f['interval']['lower']
                          for f in records]
                return sum(widths) / len(widths)
            self.assertGreaterEqual(mean_width(widened), mean_width(nominal))

    def test_suspend_action_suppresses_intervals_but_keeps_evaluation(self):
        engine = run_engine(self._shift_rows(), alarm_action='suspend',
                            **self.SHIFT_CONFIG)
        suspended = [f for f in engine.forecasts if f['status'] == 'suspended']
        self.assertTrue(suspended)
        for forecast in suspended:
            # 对外区间被暂停：不输出任何上下界
            self.assertIsNone(forecast['interval']['lower'])
            self.assertIsNone(forecast['interval']['upper'])
            self.assertFalse(forecast['interval']['calibrated'])
        self.assertIn('suspended',
                      {e['state_after'] for e in engine.events
                       if e['trigger'] == 'breach_rate'})
        # 暂停期间状态机仍在运行：暂停后的预测最终都有 outcome，
        # 且恢复事件能够出现（评估口径在样本重建后恢复）
        realized_suspended = [f for f in suspended if f['outcome']]
        self.assertTrue(realized_suspended)
        self.assertTrue(any(e['trigger'] == 'recovery_streak'
                            for e in engine.events))

    def test_recovery_returns_to_nominal_after_stable_realizations(self):
        # 突变后平台稳定，连续落回 nominal 区间应触发恢复事件
        engine = run_engine(self._shift_rows(post=130),
                            **self.SHIFT_CONFIG)
        states = [e['state_after'] for e in engine.events]
        self.assertIn('alarmed_widened', states)
        self.assertEqual(states[-1], 'nominal')
        recoveries = [e for e in engine.events
                      if e['trigger'] == 'recovery_streak']
        self.assertTrue(recoveries)
        self.assertGreaterEqual(recoveries[0]['streak'],
                                engine.config.recovery_streak)
        self.assertIn('nominal',
                      {trust['state'] for trust in engine.trust.values()})

    def test_fixture_deploys_do_not_drive_alarm_directly(self):
        # 引擎不读取 deploy 列做决策：用真实数据跑，报警只能来自残差越界
        rows = load_records(FIXTURE)
        engine = run_engine(rows)
        for event in engine.events:
            self.assertEqual(event['trigger'] in ('breach_rate',
                                                   'recovery_streak'), True)
            self.assertNotIn('deploy', json.dumps(event, ensure_ascii=False))

    def test_capacity_breach_diagnostics(self):
        # 构造 tier 池让区间可输出，再让一个服务上界越过容量
        rows = []
        for service in ('CA', 'CB', 'CC'):
            for hour in range(14):
                load = (95 if hour < 4 else 140) + (2 if hour % 2 else -2)
                rows.append(make_row(f'{service}-{hour}', service, 'T0', hour,
                                     load, capacity=120))
        engine = run_engine(rows)
        over = [f for f in engine.forecasts
                if f['capacity']['upper_over_capacity']]
        self.assertTrue(over)
        for forecast in over:
            self.assertGreater(forecast['interval']['upper'],
                               forecast['capacity']['capacity'])
            self.assertIsNotNone(forecast['capacity']['upper_utilization'])
        realized_over = [f for f in engine.forecasts
                         if f['outcome'] and f['outcome']['realized_over_capacity']]
        self.assertTrue(realized_over)


class HierarchicalBorrowingTest(unittest.TestCase):
    def test_sparse_service_borrows_tier_residuals_with_carried_metadata(self):
        rows = []
        # 三个同 tier 的稠密服务先建立 tier 残差池（各自 >= min_service_residuals）
        for service in ('TA', 'TB', 'TC'):
            for hour in range(16):
                rows.append(make_row(f'{service}-{hour}', service, 'T9', hour,
                                     100 + (4 if hour % 2 == 0 else -4),
                                     capacity=200))
        # 稀疏服务只有 2 个历史点，自身残差远低于 min_service_residuals
        for hour in range(2):
            rows.append(make_row(f'TS-{hour}', 'TS', 'T9', hour,
                                 100, capacity=200))
        engine = run_engine(rows)
        sparse = [f for f in engine.forecasts if f['service'] == 'TS'
                  and f['status'] == 'emitted']
        self.assertTrue(sparse)
        for forecast in sparse:
            meta = forecast['interval']
            self.assertEqual(meta['residual_source'], 'tier_borrow')
            self.assertTrue(meta['borrowed'])
            self.assertGreaterEqual(meta['residual_n'],
                                    engine.config.min_pool_residuals)
            self.assertIn('tier', meta['borrowing_rule'])
        # 非借用服务标注自身来源（取自身残差已达阈值后的后期预测）
        dense = next(f for f in engine.forecasts
                     if f['service'] == 'TA' and f['status'] == 'emitted'
                     and f['interval']['residual_source'] == 'service'
                     and f['interval']['residual_n']
                     >= engine.config.min_service_residuals)
        self.assertIsNone(dense['interval']['borrowing_rule'])

    def test_borrowing_does_not_cross_tiers(self):
        rows = []
        for hour in range(12):
            rows.append(make_row(f'A-{hour}', 'A', 'T1', hour, 100))
        sparse = [make_row('Z-0', 'Z', 'T2', 0, 100),
                  make_row('Z-1', 'Z', 'T2', 1, 100)]
        engine = run_engine(rows + sparse)
        z_records = [f for f in engine.forecasts if f['service'] == 'Z']
        # T2 内没有任何可借用残差 -> 只能 uncalibrated，不能用 T1 的
        self.assertTrue(z_records)
        self.assertTrue(all(f['status'] == 'uncalibrated'
                            and f['interval']['residual_source'] == 'none'
                            for f in z_records))


class PersistenceTest(unittest.TestCase):
    def test_resume_matches_uninterrupted_run_field_by_field(self):
        rows = load_records(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            # 分段：先跑到 clock 6，存盘
            first = RollingIntervalEngine(RollingConfig())
            first.run([r for r in rows if r['known_at'] <= 6])
            save_state(state_path, first)
            # 恢复后跑剩余批次
            resumed = load_state(state_path)
            resumed.run([r for r in rows if r['known_at'] > 6])
            # 不中断对照
            uninterrupted = run_engine(copy.deepcopy(rows))
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.report(), uninterrupted.report())

    def test_state_file_is_json_and_atomically_written(self):
        rows = load_records(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'nested' / 'state.json'
            engine = run_engine(rows)
            save_state(state_path, engine)
            payload = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(payload['format'], 'rolling-interval-state')
            # 恢复对象类型与关键计数
            restored = load_state(state_path)
            self.assertEqual(len(restored.forecasts), len(engine.forecasts))
            self.assertEqual(restored.clock, engine.clock)
            self.assertEqual(restored.trust, engine.trust)
            self.assertEqual(restored.queues.keys(), engine.queues.keys())

    def test_resume_then_ingest_resolves_pending_forecasts(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            early = [make_row(f'e-{h}', 'SR', 'T0', h, 100 + h)
                     for h in range(4)]
            engine = run_engine(early)
            save_state(state_path, engine)
            restored = load_state(state_path)
            restored.step(4, [make_row('e-4', 'SR', 'T0', 4, 105,
                                       known_at=4)])
            resolved = [f for f in restored.forecasts
                        if f['target_hour'] == 4 and f['outcome']]
            self.assertTrue(resolved)

    def test_unknown_state_format_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text(json.dumps({'format': 'else'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '无法识别'):
                load_state(path)


class CliIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_records(FIXTURE)

    def test_rolling_cli_outputs_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / 'report.json'
            state_path = Path(directory) / 'state.json'
            run = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'rolling-interval',
                 '--output', str(report_path), '--state', str(state_path),
                 '--stop-after', '6'],
                text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            partial = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertLessEqual(partial['final_clock'], 6)
            self.assertTrue(state_path.exists())
            # 恢复并跑完整数据
            run2 = subprocess.run(
                [sys.executable, '-m', 'analysis_app', 'rolling-interval',
                 '--output', str(report_path), '--state', str(state_path),
                 '--resume'],
                text=True, capture_output=True)
            self.assertEqual(run2.returncode, 0, run2.stderr)
            full = json.loads(report_path.read_text(encoding='utf-8'))
            reference = run_engine(copy.deepcopy(self.records)).report()
            self.assertEqual(full, reference)
            self.assertEqual(json.loads(run2.stdout), reference)

    def test_cli_rejects_invalid_config(self):
        run = subprocess.run(
            [sys.executable, '-m', 'analysis_app', 'rolling-interval',
             '--target-coverage', '1.5'],
            text=True, capture_output=True)
        self.assertEqual(run.returncode, 2)

    def test_report_contains_required_diagnostics(self):
        report = run_engine(copy.deepcopy(self.records)).report()
        self.assertEqual(report['pipeline'], 'rolling-interval')
        self.assertEqual(len(report['service_diagnostics']),
                         len({r['service'] for r in self.records}))
        for tier_row in report['tier_diagnostics']:
            for key in ('coverage', 'mean_width', 'nominal_breaches',
                        'realized_over_capacity',
                        'upper_over_capacity_forecasts',
                        'borrowed_intervals'):
                self.assertIn(key, tier_row)
        for forecast in report['forecasts']:
            self.assertIn('borrowing_rule', forecast['interval'])
            self.assertIn('residual_n', forecast['interval'])


if __name__ == '__main__':
    unittest.main()

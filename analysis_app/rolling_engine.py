"""rolling-interval 在线回放引擎。

因果约定（任何分支都不能破坏）：
1. 时钟 clock 取当前批次的 known_at；只有 known_at <= clock 的观测才算可见。
2. 新观测到达之前，其标签不参与任何校准；残差只在预测被实现时入队。
3. 训练（EWMA 基线）、校准（残差分位数）、待预测窗口严格按时间排列，
   预测一旦发出即冻结，迟到数据只能影响之后的预测。
4. 分布变化报警只消费“已实现”的 nominal 区间越界记录，不读取任何未来标签。
"""
from collections import deque

from .rolling_config import RollingConfig, two_sided_quantiles

BORROW_RULE = (
    'service 自身同 horizon 残差数 < min_service_residuals 时，'
    '借用同 tier 全部 service 的同 horizon 残差构造分位数；'
    '借用后仍少于 min_pool_residuals 则区间不可用。'
    '残差来源与样本数随每条区间携带。'
)


class RollingIntervalEngine:
    def __init__(self, config):
        config.validate()
        self.config = config
        self.clock = None
        # service -> {'tier', 'points': [(hour, load, capacity, known_at)], 'ewma', 'capacity'}
        self.series = {}
        self.queues = {}          # (service, horizon) -> deque[(residual, generation)]
        self.generations = {}     # service -> 当前校准世代（报警时 +1）
        self.pending = {}         # (service, target_hour) -> [forecast record]
        self.forecasts = []       # 全部预测记录（发出后不可变，实现时补 outcome）
        self.issued_keys = set()  # (service, target_hour, horizon)
        self.trust = {}           # service -> 信任状态
        self.events = []
        self.processed = set()    # 已摄入 observation_id
        self.next_forecast_id = 1

    # ------------------------------------------------------------------ 摄入
    def step(self, clock, observations):
        """处理一个 known_at=clock 的批次，随后发出该时钟下的全部预测。

        批次内按 (service, hour) 排序后摄入，保证同批到达顺序不改变结果。
        """
        if self.clock is not None and clock < self.clock:
            raise ValueError(f'clock 不能回退：{clock} < {self.clock}')
        self.clock = clock
        ordered = sorted(observations,
                         key=lambda row: (row['service'], row['hour']))
        for row in ordered:
            if row['known_at'] != clock:
                raise ValueError(
                    f"批次 known_at={clock} 与观测 known_at={row['known_at']} 不一致")
            self.ingest_row(row)
        # 整个批次的残差都入队后，再统一做一次信任状态迁移判定，
        # 保证同一时钟内一个 service 至多产生一条事件
        self._flush_trust_transitions()
        self._issue_forecasts()

    def ingest_row(self, row):
        """摄入单条观测（允许迟到：known_at 小于当前 clock）。

        迟到观测照常更新可见序列并解析对应预测，但不会改写历史预测。
        """
        oid = row['observation_id']
        if oid in self.processed:
            raise ValueError(f'观测已摄入，拒绝重复：{oid}')
        # 迟到判定：观测在其目标小时的一个步长之后才可见，或时钟已越过其可见时刻
        horizon = min(self.config.horizons)
        late = (row['known_at'] > row['hour'] + horizon
                or (self.clock is not None and row['known_at'] < self.clock))
        self.processed.add(oid)

        service, tier = row['service'], row['tier']
        self.generations.setdefault(service, 0)
        bucket = self.series.setdefault(
            service, {'tier': tier, 'points': [], 'ewma': None, 'capacity': None})
        bucket['tier'] = tier
        bucket['capacity'] = row['capacity']
        bucket['points'].append(
            (row['hour'], row['load'], row['capacity'], row['known_at']))
        bucket['points'].sort(key=lambda item: item[0])
        bucket['ewma'] = self._recompute_ewma(bucket['points'])
        if service not in self.trust:
            self.trust[service] = self._new_trust(tier)

        self._resolve(service, row, late)

    def _recompute_ewma(self, points):
        # 按负载发生小时排序重算，保证同一可见集合下结果与到达顺序无关
        alpha = self.config.ewma_alpha
        ewma = points[0][1]
        for _, load, _, _ in points[1:]:
            ewma = alpha * load + (1.0 - alpha) * ewma
        return ewma

    # ------------------------------------------------------------- 预测实现化
    def _resolve(self, service, row, late):
        target_hour = row['hour']
        waiting = self.pending.pop((service, target_hour), None)
        if not waiting:
            return
        actual = row['load']
        # 第一步：全部 horizon 的残差先入队、outcome 先落定（不触发状态迁移）
        for record in waiting:
            point = record['point_forecast']
            residual = actual - point
            horizon = record['horizon']
            queue = self.queues.setdefault(
                (service, horizon),
                deque(maxlen=self.config.calibration_window))
            # 残差携带当前世代：报警后旧世代残差不再参与区间构造，但仍留档
            queue.append((residual, self.generations[service]))

            eval_bounds = record['evaluation']
            nominal_covered = None
            if eval_bounds['lower'] is not None:
                nominal_covered = eval_bounds['lower'] <= actual <= eval_bounds['upper']

            emitted = record['status'] == 'emitted'
            emitted_covered = None
            if emitted and record['interval']['lower'] is not None:
                interval = record['interval']
                emitted_covered = interval['lower'] <= actual <= interval['upper']

            record['outcome'] = {
                'target_hour': target_hour,
                'known_at': row['known_at'],
                'lag': row['known_at'] - target_hour,
                'late_arrival': late,
                'realized_load': actual,
                'residual': residual,
                'nominal_covered': nominal_covered,
                'emitted_covered': emitted_covered,
                'realized_over_capacity': actual > row['capacity'],
            }
        # 第二步：信任状态迁移延迟到整个时钟批次结束后统一处理
        for record in sorted(waiting, key=lambda item: item['horizon']):
            horizon_state = self.trust[service]['horizons'][record['horizon']]
            horizon_state['breach_window'].append(
                record['outcome']['nominal_covered'] is False)
            horizon_state['realizations'] += 1

    def _flush_trust_transitions(self):
        """每个时钟批次结束后调用一次：按 service、horizon 顺序评估状态迁移。"""
        for service in sorted(self.trust):
            trust = self.trust[service]
            for horizon in sorted(trust['horizons']):
                horizon_state = trust['horizons'][horizon]
                cfg = self.config
                if trust['state'] == 'nominal':
                    window = list(horizon_state['breach_window'])
                    if (horizon_state['realizations'] >= cfg.min_alarm_realizations
                            and len(window) == cfg.alarm_window
                            and sum(window) >= cfg.alarm_breach_threshold):
                        self._fire_alarm(service, trust, horizon, horizon_state,
                                         window)
                        break  # 一个 service 一个时钟至多一条报警事件
                else:
                    if self._check_recovery(service, trust, horizon,
                                            horizon_state):
                        break

    def _realized_target_hour(self, service, horizon):
        """该 service/horizon 最近一次实现的预测目标小时（无则 None）。"""
        candidates = [f['target_hour'] for f in self.forecasts
                      if f['service'] == service and f['horizon'] == horizon
                      and f['outcome'] is not None]
        return max(candidates) if candidates else None

    def _fire_alarm(self, service, trust, horizon, horizon_state, window):
        cfg = self.config
        new_state = ('suspended' if cfg.alarm_action == 'suspend'
                     else 'alarmed_widened')
        trust['state'] = new_state
        for entry in trust['horizons'].values():
            entry['recovery_streak'] = 0
        trust['last_change_clock'] = self.clock
        # 旧分布的残差已不可信：校准世代 +1。旧世代残差留在队列里留档，
        # 但区间构造只使用新世代残差，新残差随滑动窗口自然积累。
        previous_generation = self.generations.get(service, 0)
        queue_snapshot = {
            str(item): [round(value, 6) for value, generation in
                        list(self.queues.get((service, item), ()))
                        if generation == previous_generation]
            for item in cfg.horizons}
        self.generations[service] = previous_generation + 1
        self.events.append({
            'service': service,
            'tier': trust['service_tier'],
            'clock': self.clock,
            'target_hour': self._realized_target_hour(service, horizon),
            'horizon': horizon,
            'trigger': 'breach_rate',
            'state_before': 'nominal',
            'state_after': new_state,
            'alarm_window': cfg.alarm_window,
            'breaches': sum(window),
            'threshold': cfg.alarm_breach_threshold,
            'action': cfg.alarm_action,
            'generation_before': previous_generation,
            'generation_after': self.generations[service],
            'retired_residual_queues': queue_snapshot,
            'reason': (f'horizon={horizon} 最近 {cfg.alarm_window} 个已实现预测中 '
                       f'{sum(window)} 次越出 nominal 评估区间，'
                       f'达到阈值 {cfg.alarm_breach_threshold}（已实现 '
                       f'{horizon_state["realizations"]} 个，越过冷启动护栏 '
                       f'{cfg.min_alarm_realizations}）；'
                       '报警只使用该时钟前已实现的误差，未读取任何未来标签'),
            'recovery_condition': (
                f'任一 horizon 连续 {cfg.recovery_streak} 个已实现预测'
                '落回 nominal 评估区间，且新残差队列重新积累完成'),
        })

    def _check_recovery(self, service, trust, horizon, horizon_state):
        cfg = self.config
        window = list(horizon_state['breach_window'])
        # 恢复双重条件：
        # 1. 最近 recovery_streak 个已实现预测全部落回评估区间；
        # 2. 新世代已积累到最小有效样本量，恢复后能立刻重新发出有效区间。
        current_generation = self.generations.get(service, 0)
        generation_residuals = [
            value for value, generation
            in self.queues.get((service, horizon), ())
            if generation == current_generation]
        enough_samples = len(generation_residuals) >= cfg.recovery_min_samples
        stable = (len(window) >= cfg.recovery_streak
                  and not any(window[-cfg.recovery_streak:]))
        if stable and enough_samples:
            state_before = trust['state']
            trust['state'] = 'nominal'
            for entry in trust['horizons'].values():
                entry['breach_window'].clear()
                entry['recovery_streak'] = 0
            trust['last_change_clock'] = self.clock
            self.events.append({
                'service': service,
                'tier': trust['service_tier'],
                'clock': self.clock,
                'target_hour': self._realized_target_hour(service, horizon),
                'horizon': horizon,
                'trigger': 'recovery_streak',
                'state_before': state_before,
                'state_after': 'nominal',
                'streak': cfg.recovery_streak,
                'reason': (f'horizon={horizon} 最近 {cfg.recovery_streak} 个'
                           '已实现预测全部落回 nominal 评估区间，恢复常规输出'),
            })
            return True
        return False

    def _new_trust(self, tier):
        return {
            'service_tier': tier,
            'state': 'nominal',
            'horizons': {
                horizon: {
                    'breach_window': deque(maxlen=self.config.alarm_window),
                    'recovery_streak': 0,
                    'realizations': 0,
                }
                for horizon in self.config.horizons
            },
            'last_change_clock': None,
        }


    # ----------------------------------------------------------------- 发预测
    def _issue_forecasts(self):
        cfg = self.config
        for service in sorted(self.series):
            bucket = self.series[service]
            point = bucket['ewma']
            tier = bucket['tier']
            trust_state = self.trust[service]['state']
            for horizon in cfg.horizons:
                target_hour = self.clock + horizon
                key = (service, target_hour, horizon)
                if key in self.issued_keys:
                    continue
                self.issued_keys.add(key)
                own = self._current_residuals(service, horizon)
                source, pool = self._select_pool(service, tier, horizon, own)
                residual_n = len(pool)
                # nominal 评估区间只使用 service 自身残差：可信度判断不能被
                # 分层借用稀释（借用只用于输出区间，且元数据随区间携带）。
                eval_result = self._evaluation_bounds(point, own)
                eval_lo, eval_hi = eval_result[0], eval_result[1]
                level = (cfg.widened_coverage
                         if trust_state == 'alarmed_widened'
                         else cfg.target_coverage)
                interval_shape = None
                if trust_state == 'suspended':
                    status, lower, upper, used_level = 'suspended', None, None, None
                elif source == 'none':
                    status, lower, upper, used_level = 'uncalibrated', None, None, None
                else:
                    lower, upper, interval_shape = self._bounds(point, pool, level)
                    if lower is None:
                        # 该 level（如 0.95）在当前样本量下无法构造有效秩区间
                        status, used_level = 'uncalibrated', None
                    else:
                        status, used_level = 'emitted', level

                record = {
                    'forecast_id': self._next_id(),
                    'service': service,
                    'tier': tier,
                    'horizon': horizon,
                    'issue_clock': self.clock,
                    'target_hour': target_hour,
                    'point_forecast': round(point, 6),
                    'baseline': {
                        'name': 'ewma',
                        'alpha': cfg.ewma_alpha,
                        'visible_points': len(bucket['points']),
                    },
                    'status': status,
                    'interval': {
                        'lower': None if lower is None else round(lower, 6),
                        'upper': None if upper is None else round(upper, 6),
                        'level': used_level,
                        'nominal_level': cfg.target_coverage,
                        'shape': interval_shape,
                        'one_sided_note': (
                            '残差同号导致双侧分位数退化，区间在该侧收缩到点预测，'
                            '宽度信息受限') if interval_shape
                            and interval_shape != 'two_sided' else None,
                        'calibrated': status == 'emitted',
                        'residual_source': source,
                        'residual_n': residual_n if source != 'none' else 0,
                        'borrowed': source == 'tier_borrow',
                        'borrowing_rule': BORROW_RULE if source != 'service' else None,
                    },
                    'evaluation': {
                        'lower': None if eval_lo is None else round(eval_lo, 6),
                        'upper': None if eval_hi is None else round(eval_hi, 6),
                        'level': cfg.target_coverage,
                        'method': 'mad_standardized_self_residuals',
                    },
                    'capacity': self._capacity_view(bucket, point, upper),
                    'outcome': None,
                }
                self.forecasts.append(record)
                self.pending.setdefault((service, target_hour), []).append(record)

    def _next_id(self):
        value = f'F{self.next_forecast_id:06d}'
        self.next_forecast_id += 1
        return value

    def _current_residuals(self, service, horizon):
        """返回该 service/horizon 当前校准世代的残差（已排序）。"""
        generation = self.generations.get(service, 0)
        entries = self.queues.get((service, horizon), ())
        return sorted(value for value, gen in entries if gen == generation)

    def _select_pool(self, service, tier, horizon, own):
        cfg = self.config
        if len(own) >= cfg.min_service_residuals:
            return 'service', own
        pooled = list(own)
        for other, bucket in self.series.items():
            if other == service or bucket['tier'] != tier:
                continue
            pooled.extend(self._current_residuals(other, horizon))
        pooled = sorted(pooled)
        if len(pooled) >= cfg.min_pool_residuals:
            return 'tier_borrow', pooled
        return 'none', pooled

    def _bounds(self, point, pool, level):
        q_low, q_high, shape = two_sided_quantiles(
            pool, level, self.config.min_effective_samples)
        if q_low is None:
            return None, None, None
        # 加法区间：残差低分位（负）加在点预测左侧，高分位（正）加在右侧
        return point + q_low, point + q_high, shape

    def _evaluation_bounds(self, point, own_residuals):
        """状态机专用评估区间：标准化残差（残差 / 稳健尺度）越界判据。

        与对外区间解耦——对外区间必须满足有限样本分位数的有效秩口径，
        而报警只需要一个基于近期、自身、已实现误差的漂移判据：
        - 尺度取最近 alarm_window 个残差的 MAD，单点突变不会瞬间把尺度撑大；
        - 评估半径 = 尺度 × alarm_z，半径随新分布自适应但响应慢于点预测；
        - 残差不足或 MAD 退化为 0（冷启动同号/恒定残差）时返回 (None, None)，
          该预测不参与报警统计，避免除零与冷启动误报。
        """
        if not own_residuals:
            return None, None
        # 新世代冷启动期间样本不足 alarm_window 时，用世代内全部残差，
        # 尺度随新分布快速重建；世代内至少要有 3 个点才能估计 MAD
        if len(own_residuals) < 3:
            return None, None
        recent = list(own_residuals)[-self.config.alarm_window:]
        scale = _median_absolute_deviation(recent)
        if scale <= 0.0:
            return None, None
        radius = scale * self.config.alarm_z
        return point - radius, point + radius

    def _capacity_view(self, bucket, point, upper):
        capacity = bucket['capacity']
        point_util = point / capacity if capacity else None
        upper_util = (upper / capacity) if (capacity is not None and upper is not None) else None
        return {
            'capacity': capacity,
            'point_utilization': None if point_util is None else round(point_util, 6),
            'upper_utilization': None if upper_util is None else round(upper_util, 6),
            'point_over_capacity': bool(capacity is not None and point > capacity),
            'upper_over_capacity': bool(upper is not None and capacity is not None
                                        and upper > capacity),
        }

    # ------------------------------------------------------------------- 回放
    def run(self, records):
        """按 known_at 分批完整回放；同批顺序不影响结果。

        恢复场景下自动跳过不晚于当前 clock 的批次（其 observation_id 也已
        在 processed 中），只回放新到达的时钟，保证可断点续跑。
        """
        batches = {}
        for row in records:
            batches.setdefault(row['known_at'], []).append(row)
        for clock in sorted(batches):
            if self.clock is not None and clock <= self.clock:
                continue
            self.step(clock, batches[clock])
        return self

    # --------------------------------------------------------------- 诊断输出
    def report(self):
        config_view = self.config.to_dict()
        # JSON 口径统一为 list，保证“存盘后恢复再报告”与不中断运行逐字段一致
        config_view['horizons'] = list(config_view['horizons'])
        return {
            'product': 'conformal-service-load',
            'pipeline': 'rolling-interval',
            'schema_version': 1,
            'config': config_view,
            'final_clock': self.clock,
            'forecasts': [dict(item) for item in sorted(
                self.forecasts,
                key=lambda r: (r['issue_clock'], r['service'], r['horizon']))],
            'trust_events': self.events,
            'service_diagnostics': self._service_diagnostics(),
            'tier_diagnostics': self._tier_diagnostics(),
            'trust_summary': self._trust_summary(),
        }

    def _service_diagnostics(self):
        cfg = self.config
        result = []
        for service in sorted(self.series):
            records = [f for f in self.forecasts if f['service'] == service]
            realized = [f for f in records if f['outcome'] is not None]
            emitted = [f for f in realized if f['status'] == 'emitted']
            covered = [f for f in emitted if f['outcome']['emitted_covered']]
            widths = [f['interval']['upper'] - f['interval']['lower'] for f in emitted]
            result.append({
                'service': service,
                'tier': self.series[service]['tier'],
                'trust_state': self.trust[service]['state'],
                'horizon_trust': [
                    {'horizon': horizon,
                     'realizations': entry['realizations'],
                     'recent_breaches': sum(entry['breach_window']),
                     'breach_window_filled': len(entry['breach_window']) == cfg.alarm_window,
                     'recovery_streak': entry['recovery_streak']}
                    for horizon, entry in sorted(self.trust[service]['horizons'].items())
                ],
                'issued': len(records),
                'realized': len(realized),
                'emitted': len(emitted),
                'uncalibrated': sum(f['status'] == 'uncalibrated' for f in records),
                'suspended': sum(f['status'] == 'suspended' for f in records),
                'borrowed_intervals': sum(
                    f['interval']['residual_source'] == 'tier_borrow' for f in records),
                'coverage': (len(covered) / len(emitted)) if emitted else None,
                'mean_width': (sum(widths) / len(widths)) if widths else None,
                'nominal_breaches': sum(
                    f['outcome'] is not None and f['outcome']['nominal_covered'] is False
                    for f in records),
                'realized_over_capacity': sum(
                    f['outcome'] is not None and f['outcome']['realized_over_capacity']
                    for f in records),
                'upper_over_capacity_forecasts': sum(
                    f['capacity']['upper_over_capacity'] for f in records),
            })
        return result

    def _tier_diagnostics(self):
        result = []
        for tier in sorted({bucket['tier'] for bucket in self.series.values()}):
            records = [f for f in self.forecasts if f['tier'] == tier]
            realized = [f for f in records if f['outcome'] is not None]
            emitted = [f for f in realized if f['status'] == 'emitted']
            covered = [f for f in emitted if f['outcome']['emitted_covered']]
            widths = [f['interval']['upper'] - f['interval']['lower'] for f in emitted]
            result.append({
                'tier': tier,
                'services': sorted(
                    s for s, b in self.series.items() if b['tier'] == tier),
                'issued': len(records),
                'emitted': len(emitted),
                'uncalibrated': sum(f['status'] == 'uncalibrated' for f in records),
                'suspended': sum(f['status'] == 'suspended' for f in records),
                'borrowed_intervals': sum(
                    f['interval']['residual_source'] == 'tier_borrow' for f in records),
                'coverage': (len(covered) / len(emitted)) if emitted else None,
                'mean_width': (sum(widths) / len(widths)) if widths else None,
                'nominal_breaches': sum(
                    f['outcome'] is not None and f['outcome']['nominal_covered'] is False
                    for f in records),
                'realized_over_capacity': sum(
                    f['outcome'] is not None and f['outcome']['realized_over_capacity']
                    for f in records),
                'upper_over_capacity_forecasts': sum(
                    f['capacity']['upper_over_capacity'] for f in records),
            })
        return result

    def _trust_summary(self):
        summary = {}
        for trust in self.trust.values():
            summary[trust['state']] = summary.get(trust['state'], 0) + 1
        return {'services': len(self.trust), 'states': summary,
                'alarm_events': sum(e['trigger'] == 'breach_rate' for e in self.events),
                'recovery_events': sum(e['trigger'] == 'recovery_streak'
                                       for e in self.events)}

    # --------------------------------------------------------------- 存盘恢复
    def to_dict(self):
        return {
            'format': 'rolling-interval-state',
            'format_version': 2,
            'config': self.config.to_dict(),
            'clock': self.clock,
            'generations': dict(self.generations),
            'series': {service: {'tier': b['tier'], 'points': b['points'],
                                 'ewma': b['ewma'], 'capacity': b['capacity']}
                       for service, b in self.series.items()},
            'queues': {f'{service}|{horizon}': [[value, generation]
                                                for value, generation in values]
                       for (service, horizon), values in self.queues.items()},
            'pending': [[f['forecast_id'] for f in records]
                        for records in self.pending.values()],
            'forecasts': self.forecasts,
            'issued_keys': [list(item) for item in sorted(self.issued_keys)],
            'trust': {service: {
                'service_tier': t['service_tier'],
                'state': t['state'],
                'horizons': {
                    str(horizon): {
                        'breach_window': list(entry['breach_window']),
                        'recovery_streak': entry['recovery_streak'],
                        'realizations': entry['realizations'],
                    }
                    for horizon, entry in t['horizons'].items()},
                'last_change_clock': t['last_change_clock'],
            } for service, t in self.trust.items()},
            'events': self.events,
            'processed': sorted(self.processed),
            'next_forecast_id': self.next_forecast_id,
        }

    @classmethod
    def from_dict(cls, payload):
        if payload.get('format') != 'rolling-interval-state':
            raise ValueError('无法识别的 rolling-interval 状态文件')
        if payload.get('format_version') != 2:
            raise ValueError('不支持的状态版本：%r' % payload.get('format_version'))
        cfg = _config_from(payload['config'])
        engine = cls(cfg)
        engine.clock = payload['clock']
        engine.generations = dict(payload['generations'])
        engine.series = {
            service: {'tier': b['tier'],
                      'points': [tuple(item) for item in b['points']],
                      'ewma': b['ewma'], 'capacity': b['capacity']}
            for service, b in payload['series'].items()}
        engine.queues = {}
        for key, values in payload['queues'].items():
            service, horizon = key.split('|')
            engine.queues[(service, int(horizon))] = deque(
                ((value, generation) for value, generation in values),
                maxlen=cfg.calibration_window)
        id_to_record = {f['forecast_id']: f for f in payload['forecasts']}
        engine.forecasts = payload['forecasts']
        engine.pending = {}
        for ids in payload['pending']:
            records = [id_to_record[fid] for fid in ids]
            key = (records[0]['service'], records[0]['target_hour'])
            engine.pending[key] = records
        engine.issued_keys = {tuple(item) for item in payload['issued_keys']}
        engine.trust = {service: {
            'service_tier': t['service_tier'],
            'state': t['state'],
            'horizons': {
                int(horizon): {
                    'breach_window': deque(entry['breach_window'],
                                           maxlen=cfg.alarm_window),
                    'recovery_streak': entry['recovery_streak'],
                    'realizations': entry['realizations'],
                }
                for horizon, entry in t['horizons'].items()},
            'last_change_clock': t['last_change_clock'],
        } for service, t in payload['trust'].items()}
        engine.events = payload['events']
        engine.processed = set(payload['processed'])
        engine.next_forecast_id = payload['next_forecast_id']
        return engine


def _config_from(data):
    allowed = RollingConfig.__dataclass_fields__
    kwargs = {key: tuple(value) if key == 'horizons' else value
              for key, value in data.items() if key in allowed}
    return RollingConfig(**kwargs)


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _median_absolute_deviation(values):
    """MAD 尺度估计；常数 1.4826 使其对正态数据与标准差同尺度。"""
    center = _median(values)
    deviations = [abs(value - center) for value in values]
    return 1.4826 * _median(deviations)

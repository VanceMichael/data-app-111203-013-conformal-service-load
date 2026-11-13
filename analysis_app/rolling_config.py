"""rolling-interval 流程的配置与有限样本分位数工具。

口径要点：
- 所有阈值都是显式参数，默认值只负责可复现，不追求漂亮的平均覆盖率。
- 有限样本分位数按 split conformal 的 ceil((n+1)*q) 秩选取（1 基），
  样本不足以支撑该秩时由上层判定为未校准，而不是偷偷放宽名义水平。
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RollingConfig:
    # 预测步长（单位：小时），每个步长各自维护残差队列
    horizons: tuple = (1, 2, 3)
    # EWMA 点预测基线的平滑系数
    ewma_alpha: float = 0.3
    # 名义双侧覆盖率
    target_coverage: float = 0.8
    # 校准滑动窗口容量（每个 service/tier/horizon 队列）
    calibration_window: int = 200
    # 本 service 自身残差少于该数量时允许向 tier 分层借用
    min_service_residuals: int = 9
    # 分层借用后至少需要的残差数，否则区间不可用
    min_pool_residuals: int = 9
    # 最小有效样本量：双侧区间要求 10% 与 90% 次序统计量是不同的秩，
    # n<9 时有限样本分位数退化为同一个点（甚至上下界颠倒），一律判未校准。
    # 注意：可信度报警的评估窗口有独立的最小样本护栏，不受该分位数门槛限制。
    min_effective_samples: int = 9
    # 报警：每个 horizon 各自维护最近 N 个已实现预测的越界窗口，
    # 任一 horizon 达到阈值即触发（不同 horizon 的残差不互相稀释）
    alarm_window: int = 6
    alarm_breach_threshold: int = 4
    # 冷启动护栏：某 horizon 的已实现预测少于该数量时不允许报警，
    # 避免极少残差下的分位数失真把正常波动误判成分布变化
    min_alarm_realizations: int = 6
    # 标准化残差报警阈值：评估区间半径 = MAD 尺度 × alarm_z。
    # 该阈值只决定“何时不再可信”，不参与对外区间的覆盖率口径。
    alarm_z: float = 3.0
    # 报警后动作：widen（按分位数扩宽）或 suspend（暂停输出区间）
    alarm_action: str = 'widen'
    # 扩宽时使用的更保守覆盖率
    widened_coverage: float = 0.95
    # 恢复条件：连续 N 个已实现预测落回（按点预测的）名义区间内。
    # 报警时校准世代切换，恢复至少要等新分布下重新积累这么多样本。
    recovery_streak: int = 4
    # 恢复所需的新世代残差数：高于最小有效样本量，保证恢复前扩宽/暂停状态
    # 已经在新分布下实际运行过一段时间，而不是刚够发区间就立刻解除报警
    recovery_min_samples: int = 12

    def to_dict(self):
        return asdict(self)

    def validate(self):
        if not self.horizons or any(not isinstance(h, int) or h <= 0 for h in self.horizons):
            raise ValueError('horizons 必须为正整数序列')
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError('horizons 不能重复')
        if not 0.0 < self.ewma_alpha <= 1.0:
            raise ValueError('ewma_alpha 必须在 (0, 1]')
        for name, value in (('target_coverage', self.target_coverage),
                            ('widened_coverage', self.widened_coverage)):
            if not 0.0 < value < 1.0:
                raise ValueError(f'{name} 必须在 (0, 1)')
        if self.widened_coverage <= self.target_coverage:
            raise ValueError('widened_coverage 必须高于 target_coverage')
        if self.calibration_window <= 0 or self.min_service_residuals <= 0 \
                or self.min_pool_residuals <= 0 or self.min_effective_samples <= 0 \
                or self.alarm_window <= 0 or self.min_alarm_realizations <= 0 \
                or self.alarm_breach_threshold <= 0 or self.recovery_streak <= 0:
            raise ValueError('窗口与阈值必须为正整数')
        if self.alarm_breach_threshold > self.alarm_window:
            raise ValueError('alarm_breach_threshold 不能大于 alarm_window')
        if self.min_alarm_realizations < self.alarm_window:
            raise ValueError('min_alarm_realizations 不能小于 alarm_window，'
                             '否则冷启动护栏失效')
        if self.recovery_min_samples < self.min_effective_samples:
            raise ValueError('recovery_min_samples 不能小于 min_effective_samples')
        if self.alarm_z <= 0:
            raise ValueError('alarm_z 必须为正数')
        if self.min_pool_residuals < self.min_effective_samples:
            raise ValueError('min_pool_residuals 不能小于 min_effective_samples，'
                             '否则分位数会退化')
        if self.alarm_action not in ('widen', 'suspend'):
            raise ValueError("alarm_action 只能是 'widen' 或 'suspend'")


def quantile_rank(sample_size, level):
    """split conformal 有限样本秩（1 基，向上取整）。

    返回 None 表示样本量不足以在该 level 下给出有效秩，
    调用方必须把区间标记为未校准，不能自行降级。
    """
    if sample_size <= 0 or not 0.0 < level < 1.0:
        return None
    rank = int(_ceil((sample_size + 1) * level))
    if rank > sample_size:
        return None
    return rank


def _ceil(value):
    # 避开浮点边界：误差在 1e-12 内视为整数
    rounded = round(value)
    if abs(value - rounded) <= 1e-12:
        return rounded
    return int(value) + 1 if value > 0 else int(value)


def order_statistic(sorted_values, level):
    """对已排序序列取 level 分位的次序统计量；不可用时返回 None。"""
    rank = quantile_rank(len(sorted_values), level)
    if rank is None:
        return None
    return sorted_values[rank - 1]


def two_sided_ranks(sample_size, level):
    """双侧区间的 (下秩, 上秩)（1 基）；要求两秩不同，否则返回 (None, None)。

    有限样本下 n 太小时两个分位数退化为同一秩，这种“区间”没有信息量，
    调用方必须标记为未校准，而不是发出宽度为零或上下颠倒的区间。
    """
    lower = quantile_rank(sample_size, (1.0 - level) / 2.0)
    upper = quantile_rank(sample_size, 1.0 - (1.0 - level) / 2.0)
    if lower is None or upper is None or lower >= upper:
        return None, None
    return lower, upper


def two_sided_quantiles(sorted_values, level, min_effective_samples):
    """返回 (低分位, 高分位)，构造加法区间要求 q_low <= 0 <= q_high。

    残差本身可正可负。若样本残差在该 level 下同号（例如部署突变后 EWMA
    单向追赶、残差全部为正），严格异号会让区间长期无法构造。此时退化为
    单侧区间：同号侧分位置零，调用方通过返回的 third 元素得知是否退化。
    返回 (None, None, None) 表示该 level 下连有效秩都无法构造。
    """
    if len(sorted_values) < min_effective_samples:
        return None, None, None
    rank_low, rank_high = two_sided_ranks(len(sorted_values), level)
    if rank_low is None:
        return None, None, None
    q_low = sorted_values[rank_low - 1]
    q_high = sorted_values[rank_high - 1]
    if q_low > 0.0:
        # 残差全部偏正：下界退化为点预测（单侧上界区间）
        return 0.0, q_high, 'one_sided_upper'
    if q_high < 0.0:
        # 残差全部偏负：上界退化为点预测（单侧下界区间）
        return q_low, 0.0, 'one_sided_lower'
    return q_low, q_high, 'two_sided'

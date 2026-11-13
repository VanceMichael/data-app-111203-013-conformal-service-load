import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .io import load_records
from .rolling_config import RollingConfig
from .rolling_engine import RollingIntervalEngine
from .rolling_persistence import load_state, save_state
from .summary import summarize


def save_json(path, result):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_horizons(text):
    horizons = tuple(int(part) for part in text.split(','))
    if not horizons or any(h <= 0 for h in horizons):
        raise argparse.ArgumentTypeError('horizons 必须是逗号分隔的正整数')
    if len(set(horizons)) != len(horizons):
        raise argparse.ArgumentTypeError('horizons 不能重复')
    return tuple(sorted(horizons))


def _build_rolling_config(args):
    return RollingConfig(
        horizons=args.horizons,
        ewma_alpha=args.ewma_alpha,
        target_coverage=args.target_coverage,
        calibration_window=args.calibration_window,
        min_service_residuals=args.min_service_residuals,
        min_pool_residuals=args.min_pool_residuals,
        alarm_window=args.alarm_window,
        alarm_breach_threshold=args.alarm_breach_threshold,
        min_alarm_realizations=args.min_alarm_realizations,
        alarm_z=args.alarm_z,
        alarm_action=args.alarm_action,
        widened_coverage=args.widened_coverage,
        recovery_streak=args.recovery_streak,
        recovery_min_samples=args.recovery_min_samples,
    )


def _run_rolling(args):
    records = load_records(args.input)
    if args.resume:
        if not args.state or not Path(args.state).exists():
            raise ValueError('--resume 需要已存在的 --state 文件')
        engine = load_state(args.state)
    else:
        engine = RollingIntervalEngine(_build_rolling_config(args))
    if args.stop_after is not None:
        records = [row for row in records if row['known_at'] <= args.stop_after]
    engine.run(records)
    if args.state:
        save_state(args.state, engine)
    return engine.report()


def main():
    parser = argparse.ArgumentParser(description='本地观测数据分析')
    commands = parser.add_subparsers(dest='command', required=True)

    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)

    rolling = commands.add_parser(
        'rolling-interval',
        help='按 known_at 因果回放的滚动区间预测、分层借用与可信度报警')
    rolling.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    rolling.add_argument('--output', type=Path)
    rolling.add_argument('--state', type=Path, help='模型与校准队列的存盘路径')
    rolling.add_argument('--resume', action='store_true', help='从 --state 恢复后继续跑')
    rolling.add_argument('--stop-after', type=int, default=None,
                         help='只回放 known_at <= 该值的观测，用于分段恢复演示')
    rolling.add_argument('--horizons', type=_parse_horizons, default=(1, 2, 3))
    rolling.add_argument('--ewma-alpha', type=float, default=0.3)
    rolling.add_argument('--target-coverage', type=float, default=0.8)
    rolling.add_argument('--calibration-window', type=int, default=200)
    rolling.add_argument('--min-service-residuals', type=int, default=9)
    rolling.add_argument('--min-pool-residuals', type=int, default=9)
    rolling.add_argument('--alarm-window', type=int, default=6)
    rolling.add_argument('--alarm-breach-threshold', type=int, default=4)
    rolling.add_argument('--min-alarm-realizations', type=int, default=6)
    rolling.add_argument('--alarm-z', type=float, default=3.0)
    rolling.add_argument('--alarm-action', choices=('widen', 'suspend'), default='widen')
    rolling.add_argument('--widened-coverage', type=float, default=0.95)
    rolling.add_argument('--recovery-streak', type=int, default=4)
    rolling.add_argument('--recovery-min-samples', type=int, default=12)

    args = parser.parse_args()
    try:
        if args.command == 'summary':
            result = summarize(load_records(args.input))
        else:
            result = _run_rolling(args)
        if args.output:
            save_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# rolling-interval 流程口径

本流程回答的问题不是“平均覆盖率好不好看”，而是“**这个区间在当前时刻还可不可信**”。
所有预测都按 `known_at` 因果回放：训练（点预测基线）、校准（残差队列）、
待预测窗口严格按时间排列，未到达的观测绝不提前进入校准。

## 1. 时间因果与回放

- 引擎时钟 `clock` 取当前批次的 `known_at`；只有 `known_at <= clock` 的观测可见。
- 同一 `known_at` 批次按 `(service, hour)` 排序后摄入，批次内到达顺序不改变结果。
- 预测在发出时冻结（点预测、区间、残差来源、样本数、信任状态全部留痕），
  迟到观测只能解析挂起预测并影响之后的预测，不能改写历史。
- 每个预测时刻对配置的多个步长 `horizons`（默认 1/2/3 小时）分别发预测，
  每个 `(service, horizon)` 维护独立残差队列，互不混用。

## 2. 点预测基线

显式基线为 EWMA（`ewma_alpha` 默认 0.3），按负载发生小时排序重算，
保证同一可见集合下结果与到达顺序无关。每条预测的 `baseline` 字段
记录基线名称、系数与可见点数。

## 3. 有限样本分位数区间

- 残差定义为 `actual - point_forecast`，在预测被实现时才入滑动校准窗
  （`calibration_window`），新观察未到达前不可用于校准。
- 双侧区间采用 split conformal 的 `ceil((n+1)q)` 秩（1 基）。
- 最小有效样本量 `min_effective_samples`（默认 9）：n 过小时两个分位数
  退化为同一秩，区间判为 `uncalibrated`，不伪造区间。
- 残差同号（如部署突变后 EWMA 单向追赶）时退化为单侧区间
  （`shape=one_sided_upper/lower`），并在 `one_sided_note` 标注宽度信息受限。
- 区间状态：`emitted`（有效输出）、`uncalibrated`（样本不足）、
  `suspended`（报警暂停）。

## 4. 分层残差借用

稀疏服务自身同 horizon 残差少于 `min_service_residuals` 时，
借用同 `tier` 全部服务的同 horizon 当前世代残差；借用后仍少于
`min_pool_residuals` 则区间不可用。借用不跨 tier。
每条区间携带 `residual_source`（service / tier_borrow / none）、
`residual_n`、`borrowed` 与完整 `borrowing_rule`。

## 5. 可信度报警（只依赖已实现误差）

报警状态机不读取 `deploy` 列，也不看任何未来标签：

1. 每个 `(service, horizon)` 维护最近 `alarm_window` 个已实现预测的
   标准化残差越界窗口。评估区间只使用该服务**自身**残差的 MAD 尺度
   （`alarm_z` 阈值），不被分层借用稀释。
2. 冷启动护栏 `min_alarm_realizations`：已实现预测不足时不允许报警。
3. 越界数达到 `alarm_breach_threshold` 触发报警，状态转为
   `alarmed_widened`（按 `widened_coverage` 扩宽）或 `suspended`（暂停输出）。
4. 报警时校准**世代（generation）+1**：旧分布残差留在队列与事件快照中
   留档（`retired_residual_queues`），但区间构造只用新世代残差。
5. 恢复条件双重：最近 `recovery_streak` 个预测全部落回评估区间，
   且新世代残差数达到 `recovery_min_samples`。事件含原因、阈值与恢复条件。

## 6. 容量越界诊断

按 service 与 tier 输出覆盖率、平均宽度、nominal 越界数、
实际负载越容量次数，以及预测上界越容量（`upper_over_capacity`）
与利用率（`upper_utilization`）。

## 7. 存盘与恢复

状态（EWMA 模型、带世代的校准队列、挂起预测、信任状态机、事件）
序列化为版本化 JSON，临时文件原子替换。恢复后续跑结果与不中断运行
逐字段一致（由 `tests/test_rolling.py` 保证）。

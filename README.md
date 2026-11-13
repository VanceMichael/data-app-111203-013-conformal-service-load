# conformal-service-load

容量团队汇总服务负载与容量利用率，逐小时记录同时保留被观测到的时间。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json
```

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。

## rolling-interval 滚动区间流程

面向容量值班的因果区间预测：每个预测时刻按 `known_at` 只取当时可见的负载，
训练、校准与待预测窗口严格按时间排列；多步长残差分别维护，
重点输出“区间何时不再可信”，而非追求平均覆盖率。

```bash
# 完整回放，输出报告 JSON 与可恢复的状态文件
python3 -m analysis_app rolling-interval \
  --output output/rolling.json --state output/rolling.state.json
# 分段运行后从状态恢复（结果与一次不中断运行逐字段一致）
python3 -m analysis_app rolling-interval --state output/rolling.state.json \
  --output output/part1.json --stop-after 6
python3 -m analysis_app rolling-interval --state output/rolling.state.json \
  --resume --output output/part2.json
# 报警动作可配置为扩宽或暂停
python3 -m analysis_app rolling-interval --alarm-action suspend
```

区间状态为 `emitted` / `uncalibrated` / `suspended`；初始无校准样本、
有限样本分位数退化时显式标记不可用，不伪造区间。稀疏服务可借用同 tier
残差，借用规则与样本数随每条区间携带。分布变化报警只消费已实现的
MAD 标准化残差，支持扩宽或暂停，并带可解释事件与恢复条件。
完整口径见 `docs/rolling_interval.md`。

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t conformal-service-load .
docker run --rm conformal-service-load
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试和 summary 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：当前业务核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/rolling_config.py`：rolling-interval 配置、参数校验与有限样本分位数。
- `analysis_app/rolling_engine.py`：因果回放引擎、分层借用、可信度状态机与诊断。
- `analysis_app/rolling_persistence.py`：模型与校准队列的版本化存盘恢复。
- `analysis_app/cli.py`：本地命令及原子输出。
- `docs/data.md`：数据契约。
- `docs/rolling_interval.md`：rolling-interval 流程口径。

演示数据全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。

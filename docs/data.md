# 数据契约

容量团队汇总服务负载与容量利用率，逐小时记录同时保留被观测到的时间。

主测量 `load` 的单位为请求数，主体字段是 `service`，核算维度是 `tier`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `service` | str | 必填 |
| `tier` | str | 必填 |
| `hour` | int | 必填 |
| `load` | float | 必填 |
| `capacity` | float | 必填 |
| `deploy` | int | 必填 |
| `known_at` | int | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

load 与 capacity 单位都是该小时请求数，deploy 表示部署事件。hour 是负载发生小时，known_at 是观测可用小时。service 可重复出现，tier 是服务等级。

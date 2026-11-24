# 道路场景缺口分析与车队采集排程

这是用于本地分析开发的合成车辆运营资料与工具链。所有车辆、地区、道路、调查参与者及事件标识均为虚构值。

车队数据团队用它回答三个问题：哪些道路场景缺少可用证据（代表性缺口）、有限的采集机会应排给哪些车辆与路线（联合计划）、计划执行与外部条件变化后覆盖目标如何变化（计划维护）。软件宣传中提到的交通标志、小动物、散落物、链条、坑洼、深积水等场景仅作为采集需求来源；本工具不对产品功能状态作任何结论。

## 环境准备

Python 3.11 至 3.14，无第三方依赖。

```sh
python3 -m venv .venv
. .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .            # 可选；安装后可直接使用 road-scenario 命令
```

不安装也可以，用 `PYTHONPATH=src python3 -m road_scenario_collection_planning ...` 运行，下文统一写作 `road-scenario`。

## 快速开始（演示流程）

```sh
# 1. 导入基线批次（data/ 即第一个投递目录）
road-scenario --state state --today 2026-11-24 ingest data

# 2. 查看代表性缺口：小动物/坑洼全是待复核、R-W 只有夜间有证据……
road-scenario --state state --today 2026-11-24 gaps

# 3. 生成联合采集计划（草稿），区分新增覆盖/重复采样/不可执行
road-scenario --state state --today 2026-11-24 plan --profile 均衡

# 4. 数据负责人比较不同优先级方案下的覆盖改善
road-scenario --state state --today 2026-11-24 compare

# 5. 车队接受计划，任务开始占用对应车辆和时段
road-scenario --state state --today 2026-11-24 accept PLAN-20261124-01

# 6. 生成并导入第二批合成资料：标注复核结论、新采集片段与回执、
#    窗口取消、传感器配置变更、许可登记、新窗口
python3 -m road_scenario_collection_planning.synthetic --out drops   # 需 PYTHONPATH=src 或已安装
road-scenario --state state --today 2026-11-24 ingest drops/round2

# 7. 对账：只撤销仍可调整的任务，并解释哪些覆盖目标因此变成缺口
road-scenario --state state --today 2026-11-24 reconcile

# 8. 车队协调员看板：冲突、待确认、执行中、已完成、已撤销
road-scenario --state state --today 2026-11-24 board

# 9. 再看缺口：复核与新片段已改变覆盖数与未覆盖范围
road-scenario --state state --today 2026-11-24 gaps
```

`--today` 是业务日期（默认系统当天），用于判断窗口是否过期、许可是否覆盖窗口；演示数据窗口在 2026-12 月，建议如上显式指定。状态目录 `--state`（默认 `./state`）保存全部导入结果与计划，删除即可重来。

## 命令参考

| 命令 | 作用 | 主要使用者 |
| --- | --- | --- |
| `init` | 初始化状态目录（其他命令会自动初始化，通常不必单独执行） | — |
| `ingest <目录>` | 导入一个投递目录的 CSV 批次；按内容哈希幂等，重复导入无效果 | 数据负责人 |
| `gaps` | 代表性缺口报告：逐场景总览、未达标单元格、待复核提示、已接受计划投影 | 数据负责人 |
| `plan --profile <方案>` | 生成联合采集计划（草稿）：新增覆盖任务、重复采样、冲突、不可执行原因、残余缺口 | 数据负责人 |
| `compare [--profiles a,b]` | 同一现状下比较不同优先级方案的任务数、新增覆盖、加权得分 | 数据负责人 |
| `accept <计划号>` | 接受草稿计划；存在车辆时段冲突时拒绝并列出冲突 | 车队协调员 |
| `confirm <任务号...>` | 确认导入的待确认任务（逐个做占用冲突检查） | 车队协调员 |
| `reconcile` | 对账：窗口取消/封闭、许可过期、传感器变更时只撤销仍可调整的任务，并解释暴露的缺口 | 车队协调员 |
| `receipt --task <任务> --segments <片段...>` | 登记采集回执，回报实际采集范围（计划内命中/计划外命中/未命中） | 车队协调员 |
| `board` | 协调看板：车辆时段冲突、待确认、执行中（含逾期未回执）、已完成、已撤销 | 车队协调员 |
| `scenarios` | 列出场景、传感器需求、传感器配置与优先级方案 | — |

## 数据契约

`docs/资料契约.md` 说明四个基线文件的字段与记录边界。工具在此之上支持两个**可选**投递文件：

| 文件 | 字段 | 含义 |
| --- | --- | --- |
| `permits.csv` | `permit_id`, `valid_from`, `valid_to` | 许可有效期登记；登记后按有效期判断，未登记的许可按 `permit_id` 字面规则判断 |
| `receipts.csv` | `receipt_id`, `task_id`, `segment_id`, `collected_at` | 采集回执，一行一个片段；导入后任务转为已完成 |

导入语义：

- **幂等**：按文件内容哈希去重，同一文件重复导入不产生变化。
- **版本化**：同一主键的记录内容变化时，旧记录进入历史并升版本；已采集片段当时的版本与条件始终可查，回执会记录片段当时的版本号。
- **复核**：同一 `label_id` 的新记录视为复核结论，覆盖数与未覆盖范围随之改变。
- **容错**：单行不合法只跳过该行并在报告中列出，不中断批次。
- **保护**：本地已完成/已撤销的任务不会被导入的旧状态覆盖。

## 关键口径与规则

- **覆盖单元格** = 场景 × 地区 × 时段（白天/黄昏/夜间）。每格需 `min_exposures_per_cell` 次独立暴露（默认 2）才算覆盖。
- **独立暴露**：同一单元格内来自同一行程（`trip_id`）的若干片段只计一次——相邻片段同车同路，不构成独立暴露；不同行程才分别计数。
- **有效证据**：只有复核通过的标注（`confirmed`/`revised`）计入覆盖；`pending` 单独提示为待复核。
- **联合规划**：一个窗口生成一个候选任务，可同时命中多个场景×时段单元格；相同车辆和时段只能被一个任务占用，按"加权新增覆盖"贪心选择，因此不会把各场景各自可行的计划简单相加。
- **占用**：被接受（`accepted`）和已完成（`done`）的任务占用对应车辆和时段；窗口时段为左闭右开区间。
- **可执行性**：`closed` 路线一律不可执行；`open`/`urban-only` 需要有效许可（`P-0`/空视为未办理，`P-EXPIRED` 视为已过期，`permits.csv` 登记的有效期优先）；车辆传感器配置须满足场景需求（见 `scenarios` 命令）。
- **撤销**：`reconcile` 只撤销仍可调整（`proposed`/`accepted`）的任务；已完成任务及其已采集片段保持当时记录，仅提示窗口事实已变化。

## 配置

首次运行后，配置写入 `state/store.json` 的 `config` 节，可直接编辑：

- `min_exposures_per_cell`：每单元格达标所需独立暴露数。
- `sensor_configs` / `scenario_requirements`：传感器配置清单与场景传感器需求。
- `permit_required_route_states` / `no_permit_ids` / `expired_permit_ids`：许可规则。
- `priority_profiles`：优先级方案（场景 → 权重，未列出的场景权重为 0）。默认含`均衡`、`安全优先`、`设施优先`，可自行增删。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖：导入幂等与版本化、复核改变覆盖、独立暴露口径、传感器/许可/封闭约束、车辆时段互斥、多场景命中、重复采样识别、优先级方案比较、接受占用与冲突拒绝、对账撤销与缺口解释、回执版本保留，以及基于 `data/` + 合成第二批次的端到端流程。`tools/check_materials.py` 仍可用于基线资料完整性检查。

## 项目结构

```
data/                  基线合成批次（第一个投递目录）
docs/资料契约.md        资料契约
src/road_scenario_collection_planning/
  contract.py          契约定义、枚举、默认配置、行级校验
  store.py             带版本的本地 JSON 状态仓库
  ingest.py            持续导入（幂等、容错、引用提示）
  coverage.py          独立暴露与代表性缺口
  planning.py          联合贪心规划与优先级比较
  lifecycle.py         接受/确认/对账撤销/回执/看板
  report.py            中文报告格式化
  cli.py               命令行入口
  synthetic.py         合成演示批次生成器（round2）
tests/                 单元测试与端到端测试
```

# 危险废液暂存隔离复核 API

实验室把废液桶临时并入暂存柜时，**标签相同也未必能共用托盘**：酸与含氰废液
泄漏混合会放出 HCN 等剧毒气体，氧化剂与有机废液可剧烈反应。本服务用
Python 标准库（`http.server` / `json` / `sqlite3`）实现入库、移位、合并
全过程的相容性复核，规则矩阵版本化并冻结，修订结果仅追加、不可改写。

## 运行

```bash
python3 -m hazwaste.server --host 127.0.0.1 --port 8080 --db hazwaste.db
# --db 默认 :memory:；指定文件则事件/修订/矩阵持久化，重启可复核旧结果
```

启动时自动冻结内置矩阵 **v1.0（基线）** 与 **v2.0（收紧版，演示版本比较）**。

## 核心概念

| 概念 | 说明 |
|---|---|
| 冻结矩阵 `matrix` | 成分浓度阈值→危害类别、成对反应规则、材质适配、通风等级、装填/盛漏参数。带内容指纹（sha256），同版本号内容不同则拒绝（409）。 |
| 设施 `facility` | 柜体 cabinet（含通风方式）→ 分区 zone（含坐标 `position_xy`，用于隔离距离）→ 盛漏托盘 tray（公称/有效容积）。设施定义版本化追加。 |
| 容器 `container` | 成分及**浓度范围** `conc_min/conc_max`、声明危害类别、材质、公称容量、当前装量、当前位置。 |
| 事件 `event` | 带时标的 `intake 入库 / move 移位 / merge 合并 / correct 成分更正 / repack 换桶`，只追加；时标必须单调不减。 |
| 修订 `revision` | 每个事件后全量重放出的不可变复核结果（父子链）。含完整布局状态、逐桶明细与计算依据。 |
| 试排 / 预检 `trial` | 不落事件的假设布局计算，结果同样冻结入库，可用于确认时交叉核对。 |

### 判定与“待处置”

- 成分浓度范围跨越矩阵阈值带时，类别同时有**确定集**与**可能集**（多结论）；
  基于可能集的禁配按“可能禁配”报告。多结论本身即阻断项。
- 出现以下任一情形，布局结论为 `pending`（待处置），禁止确认：
  - 确定/可能禁配物共置于规则禁止的层级（同柜 / 同分区 / 同托盘）；
  - 规则冲突、矩阵未收录成分、声明类别与成分推断冲突；
  - 材质不适配、装填率超限、托盘盛漏容积不足；
  - 隔离距离不足、通风等级不足、位置引用无效。
- 全部通过才为 `disposable`，方可 `confirm`（确认只新增确认记录，不改写修订）。
- 每个问题都返回 `code`、涉事 `containers`、各自 `positions` 与 `basis`
  （命中规则、阈值、计算公式与中间量）。

## 主要端点

```
GET  /health
GET  /matrices                        GET  /matrices/{ver}
POST /matrices                        # 冻结新版本
GET  /matrices/{old}/diff/{new}       # 矩阵版本比较（参数/阈值/规则差异）
PUT  /facilities/{fid}                # 注册/升级设施（新版本追加）
GET  /facilities/{fid}?version=

POST /facilities/{fid}/events         # 单个 event 或 events 批量；每个事件出新修订
GET  /events?facility_id={fid}

POST /facilities/{fid}/layout/try           # 试排（proposal: moves/merges/repacks）
POST /facilities/{fid}/layout/move-check    # 单桶移动预检（给 allowed 结论）
GET  /trials/{tid}

GET  /revisions?facility_id={fid}
GET  /revisions/latest?facility_id={fid}
GET  /revisions/{rid}
GET  /revisions/{rid}/disposal?container_id=C   # 逐桶处置单（全部计算明细+整改建议）
POST /revisions/{rid}/recalc?container_id=C     # 用冻结矩阵全量复算并核对指纹
GET  /revisions/{a}/diff/{b}                    # 修订版本比较
POST /revisions/{rid}/confirm                   # 仅当无阻断项；可带 trial_id 交叉核对
```

所有计算类端点接受 `"matrix_version"`；省略时使用最新冻结版本。
试排、移动预检、确认、版本比较必须引用已冻结矩阵，确认时还会校验
试排与修订的矩阵指纹、布局指纹一致。

## 计算明细

- **装填率**：`volume_l / capacity_l ≤ max_fill_ratio`（v1.0=0.90，v2.0=0.85）
- **托盘盛漏**：`有效容积 ≥ Σ装量 × tray_capacity_ratio`
  （v1.0 倍数 1.10；有效容积未声明时按公称 × 0.90 折减）
- **隔离距离**：仅“禁同分区”规则（如 R3 酸碱）允许同柜，分区间欧氏距离须
  ≥ 规则下限（v1.0 为 1.0m），且柜体通风满足规则附加要求。
- **混合浓度**：合并按体积守恒加权传播每个成分的浓度区间。
- **结果指纹**：对 `矩阵版本+指纹+设施+各桶输入` 做 canonical JSON 哈希，
  复算指纹一致才 `verified: true`。

## 内置反应规则（v1.0 摘要）

| 规则 | 类别对 | 后果 | 共置策略 |
|---|---|---|---|
| R1 | 酸 × 含氰 | 释放 HCN 剧毒气体 | 同柜禁止 |
| R2 | 酸 × 硫化物 | 释放 H₂S | 同柜禁止 |
| R3 | 酸 × 碱 | 中和放热 | 禁同分区，同柜≥1m 且机械通风 |
| R4 | 氧化剂 × 有机物 | 剧烈反应/燃爆 | 同柜禁止 |
| R5 | 氧化剂 × 碱 | 不相容 | 禁同托盘 |
| R6 | 次卤酸盐 × 酸 | 释放 Cl₂ | 同柜禁止 |
| R7 | 次卤酸盐 × 有机物 | 剧烈反应 | 同柜禁止 |
| R8（v2.0 新增） | 酸 × 重金属 | 放热/有毒酸雾 | 禁同分区，≥1m |

## 测试与演示

```bash
python3 -m unittest discover -s tests -v   # 34 个用例（引擎 + HTTP 端到端）
python3 demo_smoke.py http://127.0.0.1:8080
```

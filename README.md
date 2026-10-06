# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。

## 库存对账（全层 · 层页 · 顶条）

`lots` 是唯一业务事实表；`shelf_projection` 是它的**只读投影**，全层页、
各层页（上/中/下）、顶条预警三个页面都从这一张投影导出。业务写路由
（入库 / FEFO 消费 / 过期下架）在**同一数据库事务**内随业务改动一起重建投影，
因此任何已提交状态下三方天然一致。

- 对账在**单一事务快照**内交叉核对三个范围：全层总量、每层层页过滤之和、
  顶条 `lot_id` 集合，并逐批比对（余量 / 层归属 / 顶条标记 / 幽灵批）。
  分叉报告同时带 `layer` 与 `lot_id`。
- **默认只报告，不改任何数据**：`POST /api/reconcile`（`reproject=false`），
  连跑任意次计数与数据都稳定。CLI：`python -m app.engines.reconcile`
  （有分叉退出码 1）。
- 只有显式打开开关才重投影：`{"reproject": true}`
  （CLI 加 `--reproject`）。在单写事务内以 `lots` 重建投影并在同一快照复核。
  **修复路径永不写、永不清零业务 `lots`**（报告用 `lots_checksum_*` 与
  `business_lots_modified` 自证；若触碰 lots，CLI 退出码 3）。
- 修复后连跑两次必须都为 0；第二次仍非 0 视为失败（重投影后第二次为幂等
  no-op，`rebuilt=false`）。
- 进行中（未提交）的消费对对账快照不可见，不会被误报为分叉；WAL +
  `busy_timeout` 让读对账不阻塞写消费。
- 不允许把「直接改 SQLite 文件 / 清零 lots」作为业务路由里的常规修复；
  唯一修复动作是重建只读投影。

测试：`cd backend && python -m pytest app/tests/`。


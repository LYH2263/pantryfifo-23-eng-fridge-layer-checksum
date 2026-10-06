# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。

## 三视图对账（全层 / 层页 / 顶条）

`GET /api/audit`：默认**只报告，不改任何批次**。以 `consumptions` 流水重投影
每批应有余量，与全层页 `/fridge`、层页 `/fridge?layer=`、顶条 `/alerts` 三个
视口的实际展示逐一核对，分叉条目含 `layer` 与 `lot_id`。

`POST /api/audit/reproject`：**显式开关**，才按流水精准回写分叉批
（复活批置 consumed、错量批回写期望值、漏报批复活）。只做带主键谓词的
UPDATE，不 DELETE、不清零无关批、不动 items/settings。

- 默认两次连跑结果逐字节一致且零写入；两种模式各自稳定在同一选择。
- 对账在单个 `BEGIN IMMEDIATE` 事务快照内完成，并发的消费要么整笔提交后
  可见、要么在锁后提交，进行中一笔不会被当成分叉。
- 验收闸门：修复后连跑两次 `open_count` 都必须为 0，第二次仍非 0 即失败。

CLI：`python -m app.audit`（只报告）/ `python -m app.audit --reproject`。

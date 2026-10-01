# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/mobilization.py`：动员流程纯规则（时间窗重叠、同窗资源分配、回执三项对账、幂等指纹）。
- `src/mob_store.py`：资源目录、动员单、占用、回执与动员事件的事务访问。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：动员调度台演示页面（占用、缺口、待复核、时间线）。
- `tests/`：完整流程、规则计算、失败场景与动员并发/对账/重算测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 动员流程（故障单 → 资源占用 → 到货回执）

把调度员各自改单的做法收敛为一条可恢复流程。新增角色：`dispatcher`（调度员）、`external_org`（外单位回传）。

动员单状态：`draft`（容量不足的草稿，带缺口明细）→ `reserved`（船机/班组/备缆同窗预留）→ `pending_review`（回执对不上）→ `confirmed`（复核通过）→ `loaded`（已装船）；勘察变更时旧单转为 `voided` 并生成 `-R1/-R2…` 后继单。

- `POST /api/resources`（dispatcher）：登记船机、班组、备缆批次（备缆需 `batch_no`/`capacity_km`/`checksum`）。
- `GET /api/resources?kind=vessel|crew|spare_lot`：资源目录。
- `POST /api/records/{id}/mobilizations`（dispatcher）：确认动员，体为 `{"mob_no":"MOB-1","expected_version":1(可选),"data":{"window_start":"...","window_end":"...","vessel_count":1,"crew_count":1,"spare_km":15.75,"vessel_tags":[]}}`。
  - 同一时间窗一次性预留船机、接续班组和备缆；任一容量不足则落为 **draft**，返回 `shortfalls` 写清缺口，已能锁定的部分仍保留。
  - **两名调度员同时确认**：每个故障单至多一张进行中动员单（数据库部分唯一索引 + `BEGIN IMMEDIATE`），并发下一处成功、另一处 409。
  - **写入失败后按同一 `mob_no` 重试**：已生效确认原样返回（已占资源不重复）；草稿续作会按最新资源重新试算，`(mob_no,resource_id)` 唯一约束防止重复占用。请求内容指纹变化时拒绝混用编号。
- `POST /api/mobilizations/{mob_no}/receipt`（external_org/dispatcher）：回传到货单，`items:[{batch_no,quantity,checksum}]`。批次、数量、校验值逐项对账，任一不符进入 **pending_review** 并记录来源单位与不符明细。
- `POST /api/mobilizations/{mob_no}/review`（dispatcher）：`{"approve":true|false,"note":"..."}`；复核前 `load` 一律 409，驳回保持待复核。
- `POST /api/mobilizations/{mob_no}/load`（dispatcher）：复核通过后安排装船，占用转为 `consumed`。
- `POST /api/records/{id}/actions/survey_revise`（cable_engineer，`surveyed/mobilized` 可用）：勘察结果变更（可带新的 `required_spare_km`）。该故障单**未接续（未装船）的占用立即失效**（holds → `released`，动员单 → `voided`），并沿用原时间窗自动重算后继单，可能落为新草稿。
- `GET /api/mobilizations?record_id=&state=`、`GET /api/mobilizations/{mob_no}`、`GET /api/mobilizations/{mob_no}/audit`：动员单与事件时间线。
- `GET /api/console`：调度台汇总 `active_holds`（占用，含资源/时间窗/状态）、`shortfalls`（缺口，来源 dispatch/survey）、`pending_review`（待复核，来源 receipt）、`events`（最近动员事件时间线）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及动员预留/缺口草稿/同窗并发唯一生效/同编号幂等续作/回执三项对账与复核闸门/勘察变更失效重算/调度台汇总和HTTP端到端。

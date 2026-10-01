# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（含演示资源种子）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：故障单SQLite建表、事务和查询。
- `src/mobilization.py`：动员资源规划（船机/班组/备缆原子预留）与到货回执对账的纯领域逻辑。
- `src/resource_repository.py`：资源库存、时间窗占用、动员单、回执与动员事件表，全部写入使用`BEGIN IMMEDIATE`串行化。
- `src/mobilization_service.py`：可恢复动员用例编排（草稿/确认/对账/复核/装船/重算）。
- `src/service.py`：故障单用例编排；在勘察、接续、取消动作上挂钩动员子系统。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与动员并发/对账/重算测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并写入演示资源（船机CS-1/CS-2、班组CREW-A/CREW-B、备缆批次BATCH-01~03）。

## 动员流程（可恢复）

1. 调度员按故障单创建动员草稿：`POST /api/mobilizations`，带`mobilization_no`（客户端生成、重试不变）、时间窗、船机、班组与优先备缆批次。
2. 确认：`POST /api/mobilizations/{id}/confirm`。同一写事务内重算容量并原子预留船机、接续班组、备缆批次（全有或全无）。容量不足时保持`draft`，响应与调度台中`gaps`写清缺口类型、缺口量、被谁占用；不留下任何占用。
3. 两个调度员同时确认只有一处生效（SQLite写事务串行化 + 部分唯一索引去重）；另一处幂等返回同一张已确认单。写入失败后用**同一动员编号**重试：`POST /api/mobilizations`同号返回原单，确认不重复占位。
4. 外单位到货回执：`POST /api/mobilizations/{id}/receipts`。系统按已确认备缆占用生成应到货清单（批次、数量、`批次|数量`的校验值），批次/数量/校验值任一不符即进入`receipt_pending`（待复核），差异明细和来源单位（`X-Org`）进入时间线。
5. 待复核期间装船被拒（`POST .../load`返回409）。复核接口`POST /api/receipts/{id}/review`：批准后`ready_to_load`方可装船；驳回回到`confirmed`重新对账。
6. 勘察结果变化（`survey`或`survey_update`）：该故障单**未接续**的活跃占用立即释放（`released`），动员单退回`draft`并按新需求在锁内重算缺口；待复核回执作废。接续完成（`splice`）后占用转为`consumed`、动员单`completed`；故障单取消则释放占用并作废动员单。

## 主要接口

故障单（原有）：

- `GET /health`、`GET /`
- `GET /api/records`（`state`、`limit`）、`GET /api/records/{id}`、`GET /api/stats`
- `POST /api/records`：`{"reference":"...","data":{...}}`
- `POST /api/records/{id}/actions/{action}`：`{"expected_version":1,"data":{...}}`，动作含`approve/mobilize/survey/survey_update/splice/test/restore/cancel`
- `GET /api/records/{id}/audit`：故障单时间线；`?merged=1`合并动员事件（占用、缺口、待复核来源）

动员与资源：

- `POST /api/resources` / `GET /api/resources`：登记/查看船机`vessel`、班组`crew`、备缆批次`cable`（`capacity_qty`为批次库存公里数，可带`window_start/window_end`可用窗口）
- `POST /api/mobilizations`：`{"record_id":1,"mobilization_no":"MOB-...","data":{...}}`
- `POST /api/mobilizations/{id}/draft`：修改草稿（需`expected_version`）
- `POST /api/mobilizations/{id}/confirm`：确认预留（幂等续作）
- `POST /api/mobilizations/{id}/receipts`：登记到货回执
- `POST /api/receipts/{id}/review`：`{"decision":"approve|reject"}`
- `POST /api/mobilizations/{id}/load`：装船（仅对平或复核批准后可用）
- `GET /api/mobilizations/{id}`、`GET /api/mobilizations/by-no/{mobilization_no}`、`GET /api/mobilizations/{id}/timeline`
- `GET /api/board`：调度台视图（状态、活跃占用、缺口、最新待复核回执来源）

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，回执来源单位取`X-Org`。角色：`dispatcher`（编排/确认/资源/对账）、`repair_manager`（批准/复核）、`vessel_master`（装船）、`cable_engineer`（勘察/接续）、`noc_operator`（创建/测试/恢复）、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及动员的原子预留与缺口、并发确认只一处生效、同号幂等续作、回执不符拦截装船、勘察失效重算、接续消耗与调度台/审计时间线。

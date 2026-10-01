"""动员流程的 SQLite 表结构与事务访问。

关键不变量：
- mobilizations.mob_no 唯一：同一动员编号的重试是幂等续作；
- 每个故障单至多一张未结束动员单（部分唯一索引），两名调度员同时确认只能一处生效；
- mob_holds(mob_no, resource_id) 唯一：续作不重复占用；
- 写占用前对动员单 BEGIN IMMEDIATE 串行化，并发确认一个成功一个冲突。
"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .repository import _now
from .mobilization import KIND_LABELS, VOIDED, successor_no


def _loads(value: str, default: Any) -> Any:
    return json.loads(value) if value else default


class MobilizationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    batch_no TEXT NOT NULL DEFAULT '',
                    capacity REAL NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    checksum TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mobilizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mob_no TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    demand TEXT NOT NULL,
                    shortfalls TEXT NOT NULL DEFAULT '[]',
                    fingerprint TEXT NOT NULL DEFAULT '',
                    successor_of TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mob_holds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mob_no TEXT NOT NULL,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    kind TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'held',
                    created_at TEXT NOT NULL,
                    UNIQUE(mob_no, resource_id)
                );
                CREATE TABLE IF NOT EXISTS mob_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mob_no TEXT NOT NULL,
                    supplier TEXT NOT NULL DEFAULT '',
                    items TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mismatches TEXT NOT NULL DEFAULT '[]',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mob_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mob_no TEXT NOT NULL DEFAULT '',
                    record_id INTEGER,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mobs_record ON mobilizations(record_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mobs_active_record
                    ON mobilizations(record_id) WHERE state != 'voided';
                CREATE INDEX IF NOT EXISTS idx_holds_resource ON mob_holds(resource_id, state);
                CREATE INDEX IF NOT EXISTS idx_receipts_mob ON mob_receipts(mob_no, id);
                CREATE INDEX IF NOT EXISTS idx_mob_events_time ON mob_events(id);
                CREATE INDEX IF NOT EXISTS idx_mob_events_record ON mob_events(record_id);
                """
            )

    # ---------- 基础读写 ----------
    @staticmethod
    def _mob(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["demand"] = _loads(item["demand"], {})
        item["shortfalls"] = _loads(item["shortfalls"], [])
        return item

    @staticmethod
    def _hold(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["items"] = _loads(item["items"], [])
        item["mismatches"] = _loads(item["mismatches"], [])
        return item

    def create_resource(self, kind: str, name: str, batch_no: str, capacity: float, tags: List[str],
                        checksum: str = "") -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO resources(kind,name,batch_no,capacity,tags,checksum,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (kind, name, batch_no, float(capacity), json.dumps(tags, ensure_ascii=False), checksum, now),
            )
            row = connection.execute("SELECT * FROM resources WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return dict(row)

    def list_resources(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                rows = connection.execute("SELECT * FROM resources WHERE active=1 AND kind=? ORDER BY id", (kind,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM resources WHERE active=1 ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_mob(self, connection: sqlite3.Connection, mob_no: str) -> Optional[sqlite3.Row]:
        return connection.execute("SELECT * FROM mobilizations WHERE mob_no=?", (mob_no,)).fetchone()

    def get_mob_or_404(self, mob_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = self.get_mob(connection, mob_no)
        if row is None:
            raise NotFound("动员单不存在")
        return self._mob(row)

    def holds(self, connection: sqlite3.Connection, mob_no: str, state: Optional[str] = None) -> List[Dict[str, Any]]:
        if state:
            rows = connection.execute("SELECT * FROM mob_holds WHERE mob_no=? AND state=? ORDER BY id", (mob_no, state)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM mob_holds WHERE mob_no=? ORDER BY id", (mob_no,)).fetchall()
        return [self._hold(row) for row in rows]

    def latest_receipt(self, connection: sqlite3.Connection, mob_no: str) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM mob_receipts WHERE mob_no=? ORDER BY id DESC LIMIT 1", (mob_no,)).fetchone()
        return self._receipt(row) if row else None

    def _bundle(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        mob = self._mob(row)
        mob["holds"] = self.holds(connection, mob["mob_no"])
        receipt = self.latest_receipt(connection, mob["mob_no"])
        mob["receipt"] = receipt
        return mob

    def bundle(self, mob_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = self.get_mob(connection, mob_no)
            if row is None:
                raise NotFound("动员单不存在")
            return self._bundle(connection, row)

    def list_mobs(self, record_id: Optional[int] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM mobilizations WHERE 1=1"
        args: List[Any] = []
        if record_id is not None:
            sql += " AND record_id=?"
            args.append(record_id)
        if state:
            sql += " AND state=?"
            args.append(state)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._mob(row) for row in rows]

    # ---------- 确认（预留）事务 ----------
    def confirm(self, mob_no: str, record_id: int, window_start: str, window_end: str,
                demand: Dict[str, Any], fingerprint: str, planner, actor_id: str,
                expected_version: Optional[int]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self.get_mob(connection, mob_no)
                if existing is None:
                    # 新单：每个故障单至多一张未结束动员单（含草稿）
                    active = connection.execute(
                        "SELECT mob_no FROM mobilizations WHERE record_id=? AND state != 'voided'",
                        (record_id,),
                    ).fetchone()
                    if active is not None:
                        raise Conflict("故障单已有进行中的动员单 %s，请基于该编号续作" % active["mob_no"])
                else:
                    # 同一动员编号重试续作
                    if existing["state"] == VOIDED:
                        raise Conflict("动员单 %s 已因勘察变更失效，请使用后继动员编号续作" % mob_no)
                    if expected_version is not None and int(existing["version"]) != int(expected_version):
                        raise Conflict("动员单版本冲突，请读取最新版本后续作")
                    if existing["fingerprint"] and existing["fingerprint"] != fingerprint:
                        raise Conflict("动员编号 %s 已用于另一份确认内容，不能混用" % mob_no)
                    if existing["state"] in ("reserved", "pending_review", "confirmed", "loaded"):
                        # 已生效确认的重试：原样返回，已占资源不重复
                        return self._bundle(connection, existing)

                # 在写锁内读取资源与占用快照（续作时排除本单旧占用），再统一分配
                resource_rows = connection.execute("SELECT * FROM resources WHERE active=1").fetchall()
                resources = [dict(row) for row in resource_rows]
                exclude = mob_no if existing is not None else None
                if exclude:
                    hold_rows = connection.execute(
                        "SELECT * FROM mob_holds WHERE state != 'released' AND mob_no != ?", (exclude,)).fetchall()
                else:
                    hold_rows = connection.execute("SELECT * FROM mob_holds WHERE state != 'released'").fetchall()
                plan = planner(resources, [self._hold(row) for row in hold_rows])
                allocations, shortfalls = plan["allocations"], plan["shortfalls"]
                derived_state = "draft" if shortfalls else "reserved"

                if existing is None:
                    connection.execute(
                        "INSERT INTO mobilizations(mob_no,record_id,state,version,window_start,window_end,"
                        "demand,shortfalls,fingerprint,created_by,updated_by,created_at,updated_at) "
                        "VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?)",
                        (mob_no, record_id, derived_state, window_start, window_end,
                         json.dumps(demand, ensure_ascii=False, sort_keys=True),
                         json.dumps(shortfalls, ensure_ascii=False, sort_keys=True),
                         fingerprint, actor_id, actor_id, now, now),
                    )
                    event_kind = "mobilization_draft" if shortfalls else "mobilization_reserved"
                    event_details = {"window_start": window_start, "window_end": window_end,
                                     "allocations": allocations, "shortfalls": shortfalls}
                else:
                    # draft 续作：清掉上一轮试算占用，按最新资源情况重新预留
                    connection.execute("DELETE FROM mob_holds WHERE mob_no=?", (mob_no,))
                    new_version = int(existing["version"]) + 1
                    connection.execute(
                        "UPDATE mobilizations SET state=?,version=?,window_start=?,window_end=?,demand=?,"
                        "shortfalls=?,fingerprint=?,updated_by=?,updated_at=? WHERE mob_no=?",
                        (derived_state, new_version, window_start, window_end,
                         json.dumps(demand, ensure_ascii=False, sort_keys=True),
                         json.dumps(shortfalls, ensure_ascii=False, sort_keys=True),
                         fingerprint, actor_id, now, mob_no),
                    )
                    event_kind = "mobilization_retry_draft" if shortfalls else "mobilization_retry_reserved"
                    event_details = {"version": new_version, "allocations": allocations, "shortfalls": shortfalls}

                for alloc in allocations:
                    connection.execute(
                        "INSERT INTO mob_holds(mob_no,resource_id,kind,quantity,window_start,window_end,state,created_at) "
                        "VALUES(?,?,?,?,?,?, 'held', ?)",
                        (mob_no, alloc["resource_id"], alloc["kind"], float(alloc["quantity"]),
                         window_start, window_end, now),
                    )
                connection.execute(
                    "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_no, record_id, event_kind, "dispatch", actor_id,
                     json.dumps(event_details, ensure_ascii=False, sort_keys=True), now),
                )
                result = self.get_mob(connection, mob_no)
                connection.commit()
                return self._bundle(connection, result)
            except Exception:
                connection.rollback()
                raise

    # ---------- 回执 ----------
    def submit_receipt(self, mob_no: str, supplier: str, items: List[Dict[str, Any]],
                       status: str, mismatches: List[Dict[str, Any]], actor_id: str, source: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.get_mob(connection, mob_no)
                if row is None:
                    raise NotFound("动员单不存在")
                state = row["state"]
                if state not in ("reserved", "pending_review"):
                    raise Conflict("动员单当前状态(%s)不能提交回执" % state)
                cursor = connection.execute(
                    "INSERT INTO mob_receipts(mob_no,supplier,items,status,mismatches,created_at) VALUES(?,?,?,?,?,?)",
                    (mob_no, supplier, json.dumps(items, ensure_ascii=False, sort_keys=True),
                     status, json.dumps(mismatches, ensure_ascii=False, sort_keys=True), now),
                )
                new_state = "pending_review" if mismatches else "confirmed"
                if state != new_state:
                    connection.execute(
                        "UPDATE mobilizations SET state=?,version=version+1,updated_by=?,updated_at=? WHERE mob_no=?",
                        (new_state, actor_id, now, mob_no),
                    )
                connection.execute(
                    "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_no, row["record_id"],
                     "receipt_mismatch" if mismatches else "receipt_matched",
                     source, actor_id,
                     json.dumps({"supplier": supplier, "items": items, "mismatches": mismatches,
                                 "to": new_state}, ensure_ascii=False, sort_keys=True),
                     now),
                )
                connection.commit()
                receipt = self._receipt(connection.execute(
                    "SELECT * FROM mob_receipts WHERE id=?", (int(cursor.lastrowid),)).fetchone())
            except Exception:
                connection.rollback()
                raise
        return receipt

    def review_receipt(self, mob_no: str, approve: bool, note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.get_mob(connection, mob_no)
                if row is None:
                    raise NotFound("动员单不存在")
                if row["state"] != "pending_review":
                    raise Conflict("动员单不在待复核状态")
                receipt_row = connection.execute(
                    "SELECT * FROM mob_receipts WHERE mob_no=? ORDER BY id DESC LIMIT 1", (mob_no,)).fetchone()
                receipt = self._receipt(receipt_row)
                new_state = "confirmed" if approve else "pending_review"
                connection.execute(
                    "UPDATE mob_receipts SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                    ("approved" if approve else "rejected", actor_id, now, receipt["id"]),
                )
                if approve:
                    connection.execute(
                        "UPDATE mobilizations SET state=?,version=version+1,updated_by=?,updated_at=? WHERE mob_no=?",
                        (new_state, actor_id, now, mob_no),
                    )
                connection.execute(
                    "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_no, row["record_id"],
                     "receipt_review_approved" if approve else "receipt_review_rejected",
                     "audit", actor_id,
                     json.dumps({"note": note, "to": new_state}, ensure_ascii=False, sort_keys=True), now),
                )
                result = self.get_mob(connection, mob_no)
                connection.commit()
                return self._bundle(connection, result)
            except Exception:
                connection.rollback()
                raise

    def mark_loaded(self, mob_no: str, actor_id: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.get_mob(connection, mob_no)
                if row is None:
                    raise NotFound("动员单不存在")
                if row["state"] != "confirmed":
                    raise Conflict("仅复核通过的动员单可以装船，当前状态为 %s" % row["state"])
                connection.execute("UPDATE mobilizations SET state='loaded',version=version+1,updated_by=?,updated_at=? WHERE mob_no=?",
                                   (actor_id, now, mob_no))
                connection.execute("UPDATE mob_holds SET state='consumed' WHERE mob_no=? AND state='held'", (mob_no,))
                connection.execute(
                    "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_no, row["record_id"], "mobilization_loaded", "dispatch", actor_id,
                     json.dumps({"note": note}, ensure_ascii=False), now),
                )
                result = self.get_mob(connection, mob_no)
                connection.commit()
                return self._bundle(connection, result)
            except Exception:
                connection.rollback()
                raise

    # ---------- 勘察变更：未接续占用立即失效重算 ----------
    def recalculate_after_survey(self, record_id: int, new_demand: Dict[str, Any], new_window_start: str,
                                 new_window_end: str, planner, fingerprint: str,
                                 actor_id: str, survey_summary: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                actives = connection.execute(
                    "SELECT * FROM mobilizations WHERE record_id=? AND state != 'voided' ORDER BY id", (record_id,)).fetchall()
                if not actives:
                    connection.rollback()
                    raise Conflict("故障单尚无动员单，无需重算")
                base_no = actives[-1]["mob_no"]
                existing_numbers = self.mob_numbers_for_record(connection, record_id)
                successor = successor_no(existing_numbers, base_no)
                for old in actives:
                    old_no = old["mob_no"]
                    # 未接续（未装船）的占用立即失效；已装船消耗的不动
                    connection.execute("UPDATE mob_holds SET state='released' WHERE mob_no=? AND state!='consumed'", (old_no,))
                    connection.execute(
                        "UPDATE mobilizations SET state='voided',version=version+1,updated_by=?,updated_at=? WHERE mob_no=?",
                        (actor_id, now, old_no),
                    )
                    connection.execute(
                        "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                        (old_no, record_id, "mobilization_voided", "survey", actor_id,
                         json.dumps({"successor": successor, "survey_summary": survey_summary}, ensure_ascii=False), now),
                    )
                # 旧占用已释放，按新勘察结果在同一时间窗重算
                resources = [dict(row) for row in connection.execute(
                    "SELECT * FROM resources WHERE active=1").fetchall()]
                hold_rows = connection.execute(
                    "SELECT * FROM mob_holds WHERE state != 'released' AND mob_no NOT IN (%s)" %
                    ",".join("?" for _ in actives),
                    [old["mob_no"] for old in actives],
                ).fetchall()
                plan = planner(resources, [self._hold(row) for row in hold_rows])
                derived_state = "draft" if plan["shortfalls"] else "reserved"
                connection.execute(
                    "INSERT INTO mobilizations(mob_no,record_id,state,version,window_start,window_end,"
                    "demand,shortfalls,fingerprint,successor_of,created_by,updated_by,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
                    (successor, record_id, derived_state, new_window_start, new_window_end,
                     json.dumps(new_demand, ensure_ascii=False, sort_keys=True),
                     json.dumps(plan["shortfalls"], ensure_ascii=False, sort_keys=True),
                     fingerprint, base_no, actor_id, actor_id, now, now),
                )
                for alloc in plan["allocations"]:
                    connection.execute(
                        "INSERT INTO mob_holds(mob_no,resource_id,kind,quantity,window_start,window_end,state,created_at) "
                        "VALUES(?,?,?,?,?,?, 'held', ?)",
                        (successor, alloc["resource_id"], alloc["kind"], float(alloc["quantity"]),
                         new_window_start, new_window_end, now),
                    )
                connection.execute(
                    "INSERT INTO mob_events(mob_no,record_id,kind,source,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (successor, record_id,
                     "mobilization_recalculated_draft" if plan["shortfalls"] else "mobilization_recalculated_reserved",
                     "survey", actor_id,
                     json.dumps({"predecessors": [old["mob_no"] for old in actives],
                                 "allocations": plan["allocations"], "shortfalls": plan["shortfalls"]},
                                ensure_ascii=False, sort_keys=True), now),
                )
                result = self.get_mob(connection, successor)
                connection.commit()
                return self._bundle(connection, result)
            except Exception:
                connection.rollback()
                raise

    def mob_numbers_for_record(self, connection: sqlite3.Connection, record_id: int) -> List[str]:
        rows = connection.execute("SELECT mob_no FROM mobilizations WHERE record_id=?", (record_id,)).fetchall()
        return [row["mob_no"] for row in rows]

    # ---------- 调度台与时间线 ----------
    def console(self) -> Dict[str, Any]:
        with self._connect() as connection:
            hold_rows = connection.execute(
                "SELECT h.*, r.name AS resource_name, r.kind AS rkind, r.batch_no AS batch_no, m.record_id AS record_id "
                "FROM mob_holds h JOIN resources r ON r.id = h.resource_id "
                "JOIN mobilizations m ON m.mob_no = h.mob_no "
                "WHERE h.state IN ('held','consumed') ORDER BY h.window_start, h.id").fetchall()
            shortfall_rows = connection.execute(
                "SELECT mob_no,record_id,state,shortfalls,updated_at FROM mobilizations WHERE shortfalls != '[]' ORDER BY id DESC").fetchall()
            review_rows = connection.execute(
                "SELECT m.mob_no,m.record_id,m.state,m.updated_at,rc.id AS receipt_id,rc.supplier,rc.mismatches,rc.created_at AS receipt_at "
                "FROM mobilizations m JOIN mob_receipts rc ON rc.id=("
                "SELECT id FROM mob_receipts WHERE mob_no=m.mob_no ORDER BY id DESC LIMIT 1) "
                "WHERE m.state='pending_review' ORDER BY rc.id").fetchall()
            event_rows = connection.execute(
                "SELECT * FROM mob_events ORDER BY id DESC LIMIT 50").fetchall()
        active_holds = []
        for row in hold_rows:
            item = dict(row)
            active_holds.append(item)
        gaps = []
        for row in shortfall_rows:
            gaps.append({
                "mob_no": row["mob_no"], "record_id": row["record_id"], "state": row["state"],
                "updated_at": row["updated_at"], "shortfalls": _loads(row["shortfalls"], []),
                "source": "dispatch",
            })
        pending = []
        for row in review_rows:
            pending.append({
                "mob_no": row["mob_no"], "record_id": row["record_id"], "receipt_id": row["receipt_id"],
                "supplier": row["supplier"], "receipt_at": row["receipt_at"],
                "mismatches": _loads(row["mismatches"], []), "source": "receipt",
            })
        events = []
        for row in event_rows:
            item = dict(row)
            item["details"] = _loads(item["details"], {})
            events.append(item)
        events.reverse()
        return {"active_holds": active_holds, "shortfalls": gaps, "pending_review": pending, "events": events}

    def mob_timeline(self, mob_no: str) -> List[Dict[str, Any]]:
        self.get_mob_or_404(mob_no)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mob_events WHERE mob_no=? ORDER BY id", (mob_no,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item["details"], {})
            result.append(item)
        return result

    def record_mob_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mob_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item["details"], {})
            result.append(item)
        return result

    def spare_expected(self, mob_no: str) -> Dict[str, Dict[str, Any]]:
        """取预留中备缆批次的 {batch_no: {quantity, checksum}}，校验值以批次登记值为准。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.batch_no, h.quantity, r.checksum FROM mob_holds h JOIN resources r ON r.id=h.resource_id "
                "WHERE h.mob_no=? AND h.kind='spare_lot' AND h.state='held'", (mob_no,)).fetchall()
        expected: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            expected[row["batch_no"]] = {"quantity": float(row["quantity"]), "checksum": str(row["checksum"] or "")}
        return expected

    def kind_labels(self) -> Dict[str, str]:
        return dict(KIND_LABELS)

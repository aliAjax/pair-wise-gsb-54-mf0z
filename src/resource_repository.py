"""动员子系统持久化：资源库存、时间窗占用、动员单、到货回执与时间线。

并发安全要点：
- 所有写入使用 BEGIN IMMEDIATE，SQLite 写事务全局串行化；
- 同一动员单的活跃占用有部分唯一索引兜底，重试续作不会重复占位；
- mobilizations 有 version 乐观锁与状态判定，两个调度员同时确认只有一处生效。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .mobilization import (
    ACTIVE,
    CONSUMED,
    DRAFT,
    RELEASED,
    plan_resources,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw: str) -> Any:
    return json.loads(raw) if raw else None


class ResourceRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rid TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    capacity_qty REAL,
                    window_start TEXT,
                    window_end TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mobilizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mobilization_no TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    vessel_rid TEXT NOT NULL,
                    crew_rid TEXT NOT NULL,
                    required_spare_km REAL NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    preferred_batches TEXT NOT NULL DEFAULT '[]',
                    gaps TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mobilization_id INTEGER NOT NULL REFERENCES mobilizations(id) ON DELETE CASCADE,
                    resource_rid TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    qty REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mobilization_id INTEGER NOT NULL REFERENCES mobilizations(id) ON DELETE CASCADE,
                    external_org TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    reported TEXT NOT NULL,
                    diffs TEXT NOT NULL DEFAULT '[]',
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mob_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mobilization_id INTEGER NOT NULL REFERENCES mobilizations(id) ON DELETE CASCADE,
                    record_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'dispatch',
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_holds_active
                    ON holds(resource_rid, window_start, window_end) WHERE status='active';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_active_dedup
                    ON holds(mobilization_id, resource_rid, window_start, window_end)
                    WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_mobs_record ON mobilizations(record_id);
                CREATE INDEX IF NOT EXISTS idx_mobs_state ON mobilizations(state);
                CREATE INDEX IF NOT EXISTS idx_receipts_mob ON receipts(mobilization_id, id);
                CREATE INDEX IF NOT EXISTS idx_mob_events_mob ON mob_events(mobilization_id, id);
                CREATE INDEX IF NOT EXISTS idx_mob_events_record ON mob_events(record_id, id);
                """
            )

    # ---------- 资源 ----------
    def upsert_resource(self, rid: str, kind: str, name: str,
                        capacity_qty: Optional[float], window_start: Optional[str],
                        window_end: Optional[str]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO resources(rid,kind,name,capacity_qty,window_start,window_end,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(rid) DO UPDATE SET
                    kind=excluded.kind, name=excluded.name,
                    capacity_qty=excluded.capacity_qty,
                    window_start=excluded.window_start, window_end=excluded.window_end,
                    updated_at=excluded.updated_at
                """,
                (rid, kind, name, capacity_qty, window_start, window_end, now),
            )
            row = connection.execute("SELECT * FROM resources WHERE rid=?", (rid,)).fetchone()
        return dict(row)

    def list_resources(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                rows = connection.execute("SELECT * FROM resources WHERE kind=? ORDER BY rid", (kind,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM resources ORDER BY kind, rid").fetchall()
        return [dict(row) for row in rows]

    # ---------- 动员单 ----------
    @staticmethod
    def _mob_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["preferred_batches"] = _loads(item["preferred_batches"])
        item["gaps"] = _loads(item["gaps"])
        return item

    def get_mobilization(self, mobilization_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
        if row is None:
            raise NotFound("动员单不存在")
        return self._mob_row(row)

    def get_mobilization_by_no(self, mobilization_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM mobilizations WHERE mobilization_no=?", (mobilization_no,)).fetchone()
        return self._mob_row(row) if row is not None else None

    def create_mobilization(self, mobilization_no: str, record_id: int, vessel_rid: str, crew_rid: str,
                            required_spare_km: float, window_start: str, window_end: str,
                            preferred_batches: List[str], gaps: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO mobilizations(mobilization_no,record_id,state,version,vessel_rid,crew_rid,
                        required_spare_km,window_start,window_end,preferred_batches,gaps,
                        created_by,updated_by,created_at,updated_at)
                    VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (mobilization_no, record_id, DRAFT, vessel_rid, crew_rid, required_spare_km,
                     window_start, window_end, json.dumps(preferred_batches, ensure_ascii=False),
                     json.dumps(gaps, ensure_ascii=False), actor_id, actor_id, now, now),
                )
                mob_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_id, record_id, "draft_created", actor_id, "dispatch",
                     json.dumps({"gaps": gaps}, ensure_ascii=False), now),
                )
                row = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mob_id,)).fetchone()
        except sqlite3.IntegrityError:
            # 并发提交同一动员编号：首个写入生效，其余按同号续作返回（不新建、不重复占位）。
            existing = self.get_mobilization_by_no(mobilization_no)
            if existing is not None:
                return existing
            raise Conflict("动员编号已存在")
        return self._mob_row(row)

    def update_draft(self, mobilization_id: int, expected_version: int, fields: Dict[str, Any],
                     actor_id: str, gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version,state FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("动员单不存在")
            if row["state"] != DRAFT:
                connection.rollback()
                raise Conflict("动员单已确认，不能修改草稿")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新草稿后重试")
            connection.execute(
                """
                UPDATE mobilizations SET vessel_rid=?,crew_rid=?,required_spare_km=?,window_start=?,
                    window_end=?,preferred_batches=?,gaps=?,version=?,updated_by=?,updated_at=? WHERE id=?
                """,
                (fields["vessel_rid"], fields["crew_rid"], fields["required_spare_km"],
                 fields["window_start"], fields["window_end"],
                 json.dumps(fields["preferred_batches"], ensure_ascii=False),
                 json.dumps(gaps, ensure_ascii=False), int(row["version"]) + 1, actor_id, now, mobilization_id),
            )
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (mobilization_id, None, "draft_updated", actor_id, "dispatch",
                 json.dumps({"gaps": gaps}, ensure_ascii=False), now),
            )
            result = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            connection.commit()
        return self._mob_row(result)

    def _active_holds(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT h.*, m.mobilization_no
            FROM holds h JOIN mobilizations m ON m.id=h.mobilization_id
            WHERE h.status='active'
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_holds(self, connection: sqlite3.Connection, mobilization_id: int,
                      holds: List[Dict[str, Any]], now: str) -> None:
        try:
            connection.executemany(
                "INSERT INTO holds(mobilization_id,resource_rid,kind,window_start,window_end,qty,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                [(mobilization_id, h["resource_rid"], h["kind"], h["window_start"], h["window_end"], h["qty"], ACTIVE, now) for h in holds],
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源已被其他动员单占用，请按同一动员编号重试续作") from exc

    def confirm_mobilization(self, mobilization_id: int, actor_id: str) -> Dict[str, Any]:
        """原子确认：同一事务内重算容量并占用船机/班组/备缆。

        已确认（含后续状态）的动员单按同一动员编号直接返回，不重复占位。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("动员单不存在")
            mob = self._mob_row(row)
            if mob["state"] == "cancelled":
                connection.rollback()
                raise Conflict("动员单已作废，不能确认")
            if mob["state"] != DRAFT:
                connection.rollback()
                return mob
            resources = [dict(r) for r in connection.execute("SELECT * FROM resources").fetchall()]
            plan = plan_resources(resources, self._active_holds(connection), mob)
            if plan.gaps:
                connection.execute(
                    "UPDATE mobilizations SET gaps=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                    (json.dumps(plan.gaps, ensure_ascii=False), mob["version"] + 1, actor_id, now, mobilization_id),
                )
                connection.execute(
                    "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mobilization_id, mob["record_id"], "gap_recorded", actor_id, "dispatch",
                     json.dumps({"gaps": plan.gaps}, ensure_ascii=False), now),
                )
                result = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
                connection.commit()
                return self._mob_row(result)
            self._insert_holds(connection, mobilization_id, plan.holds, now)
            connection.execute(
                "UPDATE mobilizations SET state='confirmed',gaps='[]',version=?,updated_by=?,updated_at=? WHERE id=?",
                (mob["version"] + 1, actor_id, now, mobilization_id),
            )
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (mobilization_id, mob["record_id"], "confirmed", actor_id, "dispatch",
                 json.dumps({"holds": plan.holds}, ensure_ascii=False), now),
            )
            result = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            connection.commit()
        return self._mob_row(result)

    def holds_for(self, mobilization_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM holds WHERE mobilization_id=? AND status=? ORDER BY id", (mobilization_id, status)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM holds WHERE mobilization_id=? ORDER BY id", (mobilization_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 到货回执 ----------
    def latest_receipt(self, mobilization_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipts WHERE mobilization_id=? ORDER BY id DESC LIMIT 1", (mobilization_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("expected", "reported", "diffs"):
            item[key] = _loads(item[key])
        return item

    def register_receipt(self, mobilization_id: int, external_org: str, expected: List[Dict[str, Any]],
                         reported: List[Dict[str, Any]], diffs: List[Dict[str, Any]], status: str,
                         actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,record_id FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("动员单不存在")
            if row["state"] not in ("confirmed", "receipt_pending", "ready_to_load"):
                connection.rollback()
                raise Conflict("仅已确认且未接续的动员单可登记到货回执")
            cursor = connection.execute(
                """
                INSERT INTO receipts(mobilization_id,external_org,status,expected,reported,diffs,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (mobilization_id, external_org, status,
                 json.dumps(expected, ensure_ascii=False), json.dumps(reported, ensure_ascii=False),
                 json.dumps(diffs, ensure_ascii=False), actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            if diffs:
                connection.execute("UPDATE mobilizations SET state='receipt_pending' WHERE id=?", (mobilization_id,))
            else:
                connection.execute("UPDATE mobilizations SET state='ready_to_load' WHERE id=?", (mobilization_id,))
            action = "receipt_pending" if diffs else "receipt_matched"
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (mobilization_id, row["record_id"], action, actor_id, "receipt:%s" % external_org,
                 json.dumps({"receipt_id": receipt_id, "diffs": diffs, "reported": reported}, ensure_ascii=False), now),
            )
            result = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            mob = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            connection.commit()
        item = dict(result)
        for key in ("expected", "reported", "diffs"):
            item[key] = _loads(item[key])
        item["mobilization"] = self._mob_row(mob)
        return item

    def review_receipt(self, receipt_id: int, decision: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("回执不存在")
            receipt = dict(row)
            mob = connection.execute("SELECT * FROM mobilizations WHERE id=?", (receipt["mobilization_id"],)).fetchone()
            if mob is None:
                connection.rollback()
                raise NotFound("动员单不存在")
            if mob["state"] != "receipt_pending" or receipt["status"] != "pending":
                connection.rollback()
                raise Conflict("该回执不在待复核状态")
            new_receipt_status = "approved" if decision == "approve" else "rejected"
            new_mob_state = "ready_to_load" if decision == "approve" else "confirmed"
            connection.execute("UPDATE receipts SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                               (new_receipt_status, actor_id, now, receipt_id))
            connection.execute("UPDATE mobilizations SET state=? WHERE id=?", (new_mob_state, receipt["mobilization_id"]))
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (receipt["mobilization_id"], mob["record_id"], "receipt_reviewed", actor_id, "review",
                 json.dumps({"receipt_id": receipt_id, "decision": decision}, ensure_ascii=False), now),
            )
            result = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        item = dict(result)
        for key in ("expected", "reported", "diffs"):
            item[key] = _loads(item[key])
        return item

    def mark_loaded(self, mobilization_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,record_id FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("动员单不存在")
            if row["state"] != "ready_to_load":
                connection.rollback()
                raise Conflict("复核通过前不安排装船")
            connection.execute("UPDATE mobilizations SET state='loaded',version=version+1,updated_by=?,updated_at=? WHERE id=?",
                               (actor_id, now, mobilization_id))
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (mobilization_id, row["record_id"], "loaded", actor_id, "dispatch", json.dumps({}, ensure_ascii=False), now),
            )
            result = connection.execute("SELECT * FROM mobilizations WHERE id=?", (mobilization_id,)).fetchone()
            connection.commit()
        return self._mob_row(result)

    def complete_for_record(self, record_id: int, actor_id: str) -> None:
        """接续完成：该故障单全部活跃占用转为已消耗。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mobs = connection.execute("SELECT id,record_id FROM mobilizations WHERE record_id=? AND state='loaded'", (record_id,)).fetchall()
            for mob in mobs:
                connection.execute("UPDATE holds SET status=? WHERE mobilization_id=? AND status='active'", (CONSUMED, mob["id"]))
                connection.execute("UPDATE mobilizations SET state='completed',version=version+1,updated_by=?,updated_at=? WHERE id=?",
                                   (actor_id, now, mob["id"]))
                connection.execute(
                    "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob["id"], record_id, "completed", actor_id, "splice",
                     json.dumps({"holds_consumed": True}, ensure_ascii=False), now),
                )
            connection.commit()

    def cancel_for_record(self, record_id: int, actor_id: str) -> None:
        """故障单取消：未接续的活跃占用立即释放，未关闭的动员单作废。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mobs = connection.execute(
                "SELECT id FROM mobilizations WHERE record_id=? AND state != 'completed' ORDER BY id",
                (record_id,),
            ).fetchall()
            for mob in mobs:
                connection.execute("UPDATE holds SET status=? WHERE mobilization_id=? AND status='active'", (RELEASED, mob["id"]))
                connection.execute("UPDATE receipts SET status='superseded' WHERE mobilization_id=? AND status IN ('pending','approved')", (mob["id"],))
                connection.execute("UPDATE mobilizations SET state='cancelled',version=version+1,updated_by=?,updated_at=? WHERE id=?",
                                   (actor_id, now, mob["id"]))
                connection.execute(
                    "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob["id"], record_id, "mobilization_cancelled", actor_id, "record_cancel",
                     json.dumps({"released_holds": True}, ensure_ascii=False), now),
                )
            connection.commit()

    def invalidate_for_record(self, record_id: int, new_required_spare_km: Optional[float],
                              actor_id: str, reason: str) -> List[int]:
        """勘察结果变化：未接续（活跃）占用立即失效，动员单退回草稿重算。

        已装船但尚未接续的同样失效（资源尚未消耗），待复核回执作废。
        返回受影响的动员单id列表。
        """
        now = _now()
        affected: List[int] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mobs = connection.execute(
                "SELECT * FROM mobilizations WHERE record_id=? AND state != 'completed' ORDER BY id",
                (record_id,),
            ).fetchall()
            for row in mobs:
                mob = self._mob_row(row)
                mob_id = mob["id"]
                active = [dict(r) for r in connection.execute("SELECT * FROM holds WHERE mobilization_id=? AND status='active'", (mob_id,)).fetchall()]
                if active:
                    connection.execute("UPDATE holds SET status=? WHERE mobilization_id=? AND status='active'", (RELEASED, mob_id))
                connection.execute("UPDATE receipts SET status='superseded' WHERE mobilization_id=? AND status IN ('pending','approved')", (mob_id,))
                if new_required_spare_km is not None:
                    connection.execute("UPDATE mobilizations SET required_spare_km=? WHERE id=?", (float(new_required_spare_km), mob_id))
                # 退回后按新需求预计算一次缺口，供调度台直接展示；确认时仍会在锁内重算。
                reloaded = self._mob_row(connection.execute("SELECT * FROM mobilizations WHERE id=?", (mob_id,)).fetchone())
                resources = [dict(r) for r in connection.execute("SELECT * FROM resources").fetchall()]
                remaining_holds = self._active_holds(connection)
                plan = plan_resources(resources, remaining_holds, reloaded)
                connection.execute("UPDATE mobilizations SET state='draft',gaps=?,version=version+1,updated_by=?,updated_at=? WHERE id=?",
                                   (json.dumps(plan.gaps, ensure_ascii=False), actor_id, now, mob_id))
                connection.execute(
                    "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mob_id, record_id, "holds_invalidated", actor_id, "survey",
                     json.dumps({"reason": reason, "released_holds": active,
                                 "new_required_spare_km": new_required_spare_km,
                                 "recalculated_gaps": plan.gaps}, ensure_ascii=False), now),
                )
                affected.append(mob_id)
            connection.commit()
        return affected

    # ---------- 调度台与时间线 ----------
    def board(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mobilizations ORDER BY id DESC LIMIT 200").fetchall()
            items = []
            for row in rows:
                mob = self._mob_row(row)
                mob["active_holds"] = [dict(r) for r in connection.execute(
                    "SELECT id,resource_rid,kind,window_start,window_end,qty,status FROM holds WHERE mobilization_id=? AND status='active' ORDER BY id",
                    (mob["id"],),
                ).fetchall()]
                receipt = connection.execute("SELECT id,external_org,status FROM receipts WHERE mobilization_id=? ORDER BY id DESC LIMIT 1", (mob["id"],),).fetchone()
                mob["latest_receipt"] = dict(receipt) if receipt is not None else None
                items.append(mob)
        return items

    def mob_timeline(self, mobilization_id: int) -> List[Dict[str, Any]]:
        self.get_mobilization(mobilization_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mob_events WHERE mobilization_id=? ORDER BY id", (mobilization_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item["details"])
            result.append(item)
        return result

    def record_mob_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM mob_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item["details"])
            result.append(item)
        return result

    def add_mob_event(self, mobilization_id: int, record_id: Optional[int], action: str,
                      actor_id: str, source: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO mob_events(mobilization_id,record_id,action,actor_id,source,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (mobilization_id, record_id, action, actor_id, source,
                 json.dumps(details, ensure_ascii=False), _now()),
            )

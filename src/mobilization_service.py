"""可恢复动员流程用例：草稿预留、确认占资源、回执对账、勘察重算。"""
import re
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, number, text
from .mobilization import (
    CABLE,
    CREW,
    RESOURCE_KINDS,
    VESSEL,
    cable_manifest,
    line_checksum,
    match_receipt,
    parse_window,
    plan_resources,
)
from .resource_repository import ResourceRepository

DISPATCH_ROLES = {"dispatcher", "admin"}
REVIEW_ROLES = {"repair_manager", "admin"}
RECEIPT_ROLES = {"dispatcher", "repair_manager", "admin"}
LOAD_ROLES = {"dispatcher", "vessel_master", "admin"}

MOB_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,40}$")


class MobilizationService:
    def __init__(self, repository: Any, resource_repository: ResourceRepository) -> None:
        self.repository = repository
        self.resources = resource_repository

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    def _validate_resources(self, vessel_rid: str, crew_rid: str) -> None:
        kinds = {r["rid"]: r["kind"] for r in self.resources.list_resources()}
        if kinds.get(vessel_rid) != VESSEL:
            raise ValidationError("vessel_rid必须指向已登记的船机")
        if kinds.get(crew_rid) != CREW:
            raise ValidationError("crew_rid必须指向已登记的接续班组")

    def _draft_fields(self, data: Dict[str, Any], required_spare_km: Optional[float] = None) -> Dict[str, Any]:
        vessel_rid = text(data, "vessel_rid")
        crew_rid = text(data, "crew_rid")
        window_start = text(data, "window_start")
        window_end = text(data, "window_end")
        start = parse_window(window_start, "window_start")
        end = parse_window(window_end, "window_end")
        if end <= start:
            raise ValidationError("时间窗结束必须晚于开始")
        preferred = data.get("preferred_batches", [])
        if not isinstance(preferred, list) or any(not isinstance(x, str) or not x.strip() for x in preferred):
            raise ValidationError("preferred_batches必须是文本列表")
        fields = {
            "vessel_rid": vessel_rid,
            "crew_rid": crew_rid,
            "window_start": window_start,
            "window_end": window_end,
            "preferred_batches": [x.strip() for x in preferred],
        }
        if required_spare_km is not None:
            fields["required_spare_km"] = float(required_spare_km)
        else:
            fields["required_spare_km"] = number(data, "required_spare_km", 0)
        self._validate_resources(vessel_rid, crew_rid)
        return fields

    def _plan_gaps(self, fields: Dict[str, Any]) -> List[Dict[str, Any]]:
        resources = self.resources.list_resources()
        plan = plan_resources(resources, [], fields)
        # 草稿创建时不看已有占用（仅校验窗口与资源登记），真实容量在确认时锁内重算；
        # 但其他动员单已占导致的缺口必须在确认时体现，草稿只保留资源自身缺口提示。
        return plan.gaps

    def create_draft(self, actor: Actor, mobilization_no: str, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, DISPATCH_ROLES)
        mobilization_no = text({"mobilization_no": mobilization_no}, "mobilization_no")
        if not MOB_NO_RE.match(mobilization_no):
            raise ValidationError("动员编号需为3-40位字母数字及_-")
        record = self.repository.get(record_id)
        if record["state"] not in {"approved", "mobilized", "surveyed"}:
            raise Conflict("故障单需先批准才能编排动员")
        fields = self._draft_fields(data or {}, required_spare_km=record["payload"].get("required_spare_km"))
        existing = self.resources.get_mobilization_by_no(mobilization_no)
        if existing is not None:
            # 写入失败后按同一动员编号重试：不新建单据、不重复占用，返回原单续作。
            if existing["record_id"] != record_id:
                raise Conflict("动员编号已被其他故障单使用")
            if existing["state"] == "cancelled":
                raise Conflict("动员单已随故障单作废，请使用新编号")
            return existing
        gaps = self._plan_gaps(fields)
        return self.resources.create_mobilization(
            mobilization_no=mobilization_no, record_id=record_id,
            vessel_rid=fields["vessel_rid"], crew_rid=fields["crew_rid"],
            required_spare_km=fields["required_spare_km"],
            window_start=fields["window_start"], window_end=fields["window_end"],
            preferred_batches=fields["preferred_batches"], gaps=gaps, actor_id=actor.user_id,
        )

    def update_draft(self, actor: Actor, mobilization_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, DISPATCH_ROLES)
        fields = self._draft_fields(data or {})
        gaps = self._plan_gaps(fields)
        return self.resources.update_draft(mobilization_id, int(expected_version), fields, actor.user_id, gaps)

    def confirm(self, actor: Actor, mobilization_id: int) -> Dict[str, Any]:
        """确认动员：同一事务预留船机、班组、备缆。

        容量不足保留草稿并写清缺口；已确认时按同一动员编号幂等续作。
        """
        actor = self._actor(actor)
        self._require(actor, DISPATCH_ROLES)
        return self.resources.confirm_mobilization(mobilization_id, actor.user_id)

    def get(self, actor: Actor, mobilization_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        return self._detail(self.resources.get_mobilization(mobilization_id))

    def get_by_no(self, actor: Actor, mobilization_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        mob = self.resources.get_mobilization_by_no(text({"mobilization_no": mobilization_no}, "mobilization_no"))
        if mob is None:
            raise NotFound("动员单不存在")
        return self._detail(mob)

    def _detail(self, mob: Dict[str, Any]) -> Dict[str, Any]:
        mob = dict(mob)
        mob["holds"] = self.resources.holds_for(mob["id"])
        receipt = self.resources.latest_receipt(mob["id"])
        if receipt is not None:
            expected = cable_manifest([h for h in mob["holds"] if h["status"] == "active"])
            receipt["expected_current"] = expected
        mob["latest_receipt"] = receipt
        return mob

    def register_receipt(self, actor: Actor, mobilization_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """登记外单位到货回执：批次/数量/校验值对不上即停在待复核。"""
        actor = self._actor(actor)
        self._require(actor, RECEIPT_ROLES)
        external_org = text(data or {}, "external_org")
        lines = (data or {}).get("lines")
        if not isinstance(lines, list) or not lines:
            raise ValidationError("lines至少包含一条到货明细")
        reported = []
        for line in lines:
            if not isinstance(line, dict):
                raise ValidationError("到货明细必须是对象")
            batch = text(line, "batch")
            qty = number(line, "qty", 0)
            checksum = line.get("checksum")
            if not isinstance(checksum, str) or not checksum.strip():
                checksum = line_checksum(batch, qty)
            else:
                checksum = checksum.strip()
            reported.append({"batch": batch, "qty": round(float(qty), 3), "checksum": checksum})
        mob = self.resources.get_mobilization(mobilization_id)
        active_holds = self.resources.holds_for(mobilization_id, "active")
        expected = cable_manifest(active_holds)
        if not any(h["kind"] == CABLE for h in active_holds):
            raise Conflict("动员单无有效备缆占用，无法对账")
        diffs = match_receipt(expected, reported)
        status = "pending" if diffs else "matched"
        result = self.resources.register_receipt(
            mobilization_id, external_org, expected, reported, diffs, status,
            actor.organization or actor.user_id,
        )
        return result

    def review_receipt(self, actor: Actor, receipt_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, REVIEW_ROLES)
        decision = text(data or {}, "decision")
        if decision not in {"approve", "reject"}:
            raise ValidationError("decision只能是approve或reject")
        return self.resources.review_receipt(receipt_id, decision, actor.user_id)

    def load(self, actor: Actor, mobilization_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, LOAD_ROLES)
        return self.resources.mark_loaded(mobilization_id, actor.user_id)

    def board(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.resources.board()

    def timeline(self, actor: Actor, mobilization_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.resources.mob_timeline(mobilization_id)

    def merged_timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        """审计时间线：故障单事件 + 动员事件（占用、缺口、待复核来源）合并。"""
        actor = self._actor(actor)
        record_events = self.repository.audit_timeline(record_id)
        merged = []
        for event in record_events:
            merged.append({
                "stream": "record",
                "record_id": record_id,
                "mobilization_id": None,
                "action": event["action"],
                "actor_id": event["actor_id"],
                "source": "record",
                "details": event["details"],
                "created_at": event["created_at"],
            })
        for event in self.resources.record_mob_timeline(record_id):
            merged.append({
                "stream": "mobilization",
                "record_id": record_id,
                "mobilization_id": event["mobilization_id"],
                "action": event["action"],
                "actor_id": event["actor_id"],
                "source": event["source"],
                "details": event["details"],
                "created_at": event["created_at"],
            })
        merged.sort(key=lambda item: item["created_at"])
        return merged

    def upsert_resource(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require(actor, DISPATCH_ROLES)
        rid = text(data or {}, "rid")
        kind = text(data or {}, "kind")
        if kind not in RESOURCE_KINDS:
            raise ValidationError("kind只能是%s" % "/".join(RESOURCE_KINDS))
        name = text(data or {}, "name")
        window_start = (data or {}).get("window_start")
        window_end = (data or {}).get("window_end")
        if window_start or window_end:
            if not window_start or not window_end:
                raise ValidationError("window_start与window_end必须同时提供")
            if parse_window(str(window_end), "window_end") <= parse_window(str(window_start), "window_start"):
                raise ValidationError("资源可用窗口结束必须晚于开始")
        if kind == CABLE:
            capacity_qty = number(data or {}, "capacity_qty", 0)
        else:
            capacity_qty = 1.0
        return self.resources.upsert_resource(rid, kind, name, capacity_qty,
                                              window_start or None, window_end or None)

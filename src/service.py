"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, integer, number, text, text_list
from .mobilization import (
    KIND_LABELS,
    compare_receipt,
    parse_window,
    plan_allocation,
    request_fingerprint,
)
from .mob_store import MobilizationStore
from .repository import Repository
from .rules import DomainRules


# 允许发起动员确认的故障单状态（尚未接续）
PRE_SPLICE_STATES = {"approved", "mobilized", "surveyed"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 mob_store: MobilizationStore = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.mob_store = mob_store or MobilizationStore(repository.db_path)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _is_dispatcher(self, actor: Actor) -> bool:
        return actor.role in ("dispatcher", "admin")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "survey_revise":
            # 勘察结果一变：该故障单所有未接续占用立即失效并重算
            self._recalculate_after_survey(actor, record_id, new_payload, data or {})
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ==================== 动员流程 ====================
    def create_resource(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_dispatcher(actor):
            raise PermissionDenied("仅调度员可登记资源")
        data = data or {}
        kind = text(data, "kind")
        if kind not in KIND_LABELS:
            raise ValidationError("kind只能是 vessel/crew/spare_lot")
        name = text(data, "name")
        batch_no = data.get("batch_no", "")
        if not isinstance(batch_no, str):
            raise ValidationError("batch_no必须是文本")
        batch_no = batch_no.strip()
        if kind == "spare_lot":
            if not batch_no:
                raise ValidationError("备缆批次必须填写batch_no")
            capacity = number(data, "capacity_km", 0)
            checksum = text(data, "checksum")
        else:
            capacity = float(integer(data, "capacity", minimum=1))
            checksum = ""
        tags = data.get("vessel_tags", [])
        if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
            raise ValidationError("vessel_tags必须是文本列表")
        return self.mob_store.create_resource(kind, name, batch_no, capacity,
                                              [item.strip() for item in tags if item.strip()], checksum)

    def list_resources(self, actor: Actor, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if kind is not None and kind not in KIND_LABELS:
            raise ValidationError("kind只能是 vessel/crew/spare_lot")
        return self.mob_store.list_resources(kind)

    @staticmethod
    def _build_demand(record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        vessel_count = integer(data, "vessel_count", minimum=1) if "vessel_count" in data else 1
        crew_count = integer(data, "crew_count", minimum=1) if "crew_count" in data else 1
        spare_km = number(data, "spare_km", minimum=0) if "spare_km" in data else float(record["payload"]["required_spare_km"])
        tags = text_list(data, "vessel_tags") if "vessel_tags" in data else []
        return {"vessel_count": vessel_count, "crew_count": crew_count, "spare_km": spare_km, "vessel_tags": tags}

    def confirm_mobilization(self, actor: Actor, record_id: int, mob_no: str, data: Dict[str, Any],
                             expected_version: Optional[int] = None) -> Dict[str, Any]:
        """确认动员：按同一时间窗预留船机、班组、备缆。容量不足则保留草稿并写清缺口。

        同一动员编号重试为幂等续作：已占资源不重复。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_dispatcher(actor):
            raise PermissionDenied("仅调度员可确认动员")
        record = self.repository.get(record_id)
        if record["state"] not in PRE_SPLICE_STATES:
            raise Conflict("故障单当前状态(%s)不能动员：须在批准后、接续前" % record["state"])
        mob_no = text({"mob_no": mob_no}, "mob_no")
        data = data or {}
        window_start, window_end = parse_window(
            text(data, "window_start"), text(data, "window_end"))
        demand = self._build_demand(record, data)
        fingerprint = request_fingerprint(record_id, window_start, window_end, demand)

        def planner(resources, holds):
            return plan_allocation(resources, holds,
                                   {"window_start": window_start, "window_end": window_end, **demand})

        return self.mob_store.confirm(mob_no, record_id, window_start, window_end, demand,
                                      fingerprint, planner, actor.user_id, expected_version)

    def get_mobilization(self, actor: Actor, mob_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.mob_store.bundle(text({"mob_no": mob_no}, "mob_no"))

    def list_mobilizations(self, actor: Actor, record_id: Optional[int] = None,
                           state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.mob_store.list_mobs(record_id=record_id, state=state, limit=limit)

    def mobilization_timeline(self, actor: Actor, mob_no: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.mob_store.mob_timeline(text({"mob_no": mob_no}, "mob_no"))

    # ---------- 到货回执 ----------
    @staticmethod
    def _validate_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("items必须是非空列表")
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("回执条目必须是对象")
            batch_no = text(item, "batch_no")
            quantity = number(item, "quantity", 0)
            checksum = text(item, "checksum")
            cleaned.append({"batch_no": batch_no, "quantity": quantity, "checksum": checksum})
        return cleaned

    def submit_receipt(self, actor: Actor, mob_no: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """外单位回传到货回执：批次/数量/校验值任一不符即停在待复核，复核前不安排装船。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in ("external_org", "dispatcher", "admin"):
            raise PermissionDenied("仅外单位或调度台可提交回执")
        mob_no = text({"mob_no": mob_no}, "mob_no")
        data = data or {}
        items = self._validate_items(data)
        supplier = data.get("supplier", actor.organization or actor.user_id)
        if not isinstance(supplier, str) or not supplier.strip():
            supplier = actor.user_id
        expected = self.mob_store.spare_expected(mob_no)
        if not expected:
            raise Conflict("动员单无已预留备缆批次，无法对账")
        mismatches = compare_receipt(expected, items)
        status = "matched" if not mismatches else "mismatch"
        receipt = self.mob_store.submit_receipt(
            mob_no, supplier.strip(), items, status, mismatches,
            actor.user_id, "external_org" if actor.role == "external_org" else "dispatch")
        return {"mob_no": mob_no, "receipt": receipt,
                "state": "confirmed" if not mismatches else "pending_review", "mismatches": mismatches}

    def review_receipt(self, actor: Actor, mob_no: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_dispatcher(actor):
            raise PermissionDenied("仅调度员可复核回执")
        data = data or {}
        if "approve" not in data or not isinstance(data["approve"], bool):
            raise ValidationError("approve必须是布尔值")
        approve = data["approve"]
        note = str(data.get("note", ""))
        return self.mob_store.review_receipt(text({"mob_no": mob_no}, "mob_no"), approve, note, actor.user_id)

    def load_mobilization(self, actor: Actor, mob_no: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self._is_dispatcher(actor):
            raise PermissionDenied("仅调度员可安排装船")
        data = data or {}
        note = str(data.get("note", ""))
        return self.mob_store.mark_loaded(text({"mob_no": mob_no}, "mob_no"), actor.user_id, note)

    # ---------- 勘察变更重算 ----------
    def _recalculate_after_survey(self, actor: Actor, record_id: int, payload: Dict[str, Any], input_data: Dict[str, Any]) -> None:
        actives = [item for item in self.mob_store.list_mobs(record_id=record_id, limit=50)
                   if item["state"] != "voided"]
        if not actives:
            return
        latest = sorted(actives, key=lambda item: item["id"])[-1]
        window_start, window_end = latest["window_start"], latest["window_end"]
        old_demand = latest["demand"]
        new_demand = {
            "vessel_count": int(old_demand.get("vessel_count", 1)),
            "crew_count": int(old_demand.get("crew_count", 1)),
            "spare_km": float(payload["required_spare_km"]),
            "vessel_tags": list(old_demand.get("vessel_tags", [])),
        }
        fingerprint = request_fingerprint(record_id, window_start, window_end, new_demand)
        survey_summary = "fault_location_km=%s, revision=%s" % (
            input_data.get("fault_location_km"), payload.get("survey_revision"))

        def planner(resources, holds):
            return plan_allocation(resources, holds,
                                   {"window_start": window_start, "window_end": window_end, **new_demand})

        self.mob_store.recalculate_after_survey(
            record_id, new_demand, window_start, window_end,
            planner, fingerprint, actor.user_id, survey_summary)

    # ---------- 调度台 ----------
    def console(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.mob_store.console()

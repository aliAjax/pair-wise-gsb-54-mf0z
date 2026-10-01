"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .mobilization_service import MobilizationService
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 mobilization_service: MobilizationService = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.mobilization_service = mobilization_service

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

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
        self._after_transition(actor, updated, action, data or {})
        return updated

    def _after_transition(self, actor: Actor, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> None:
        """动员子系统挂钩（无动员单时为空操作）。"""
        if self.mobilization_service is None or self.mobilization_service.resources is None:
            return
        resources = self.mobilization_service.resources
        try:
            if action in ("survey", "survey_update"):
                # 勘察结果确定/变化：未接续的占用立即失效，动员单退回草稿按新需求重算。
                required = record["payload"].get("required_spare_km")
                affected = resources.invalidate_for_record(record["id"], required, actor.user_id, action)
                if affected:
                    self.audit.note(record["id"], actor.user_id, "mobilization_invalidated",
                                    {"affected_mobilization_ids": affected, "reason": action,
                                     "new_required_spare_km": required})
            elif action == "splice":
                # 接续完成：备缆已消耗，占用转为 consumed，动员单关闭。
                resources.complete_for_record(record["id"], actor.user_id)
            elif action == "cancel":
                # 故障单取消：未接续占用立即释放，动员单作废。
                resources.cancel_for_record(record["id"], actor.user_id)
        except Exception:
            # 挂钩失败不能回滚已提交的故障单状态；审计中可见原状态推进。
            if action in ("survey", "survey_update", "splice", "cancel"):
                self.audit.note(record["id"], actor.user_id, "mobilization_hook_failed", {"action": action})
            raise

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

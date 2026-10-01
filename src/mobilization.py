"""动员资源规划与到货回执对账的纯领域逻辑。

本模块不访问数据库，全部函数可独立测试：
- plan_resources：按同一时间窗原子规划船机、班组、备缆批次，容量不足返回结构化缺口。
- match_receipt：按批次、数量、校验值核对到货回执。
- line_checksum：外单位回传校验值的约定算法。
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .domain import ValidationError

VESSEL = "vessel"
CREW = "crew"
CABLE = "cable"
RESOURCE_KINDS = (VESSEL, CREW, CABLE)

# 动员单状态：草稿 -> 已确认 -> (待复核) -> 可装船 -> 已装船；勘察变化后回到草稿。
DRAFT = "draft"
CONFIRMED = "confirmed"
RECEIPT_PENDING = "receipt_pending"
READY_TO_LOAD = "ready_to_load"
LOADED = "loaded"
COMPLETED = "completed"
CANCELLED = "cancelled"
CONFIRMED_FAMILY = {CONFIRMED, RECEIPT_PENDING, READY_TO_LOAD, LOADED}

ACTIVE = "active"
CONSUMED = "consumed"
RELEASED = "released"

QTY_EPS = 1e-6


def parse_window(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % field_name)
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO时间" % field_name) from exc
    if parsed.tzinfo is None:
        raise ValidationError("%s必须带时区" % field_name)
    return parsed


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return parse_window(value, "资源可用窗口")


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _covers(resource: Dict[str, Any], start: datetime, end: datetime) -> bool:
    r_start = _parse(resource.get("window_start"))
    r_end = _parse(resource.get("window_end"))
    if r_start is None or r_end is None:
        return True
    return r_start <= start and end <= r_end


def line_checksum(batch_rid: str, qty: float) -> str:
    """备缆批次到货校验值：批次与数量（公里，三位小数）的摘要前12位。"""
    payload = "%s|%.3f" % (batch_rid, float(qty))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class Plan:
    holds: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"holds": self.holds, "gaps": self.gaps}


def _unit_gap(kind: str, rid: str, resources: Dict[str, Dict[str, Any]],
              active_holds: List[Dict[str, Any]], mob: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
    """船机/班组为不可分割单元：同一时间窗内只能被一个动员单占用。"""
    start = parse_window(mob["window_start"], "window_start")
    end = parse_window(mob["window_end"], "window_end")
    resource = resources.get(rid)
    if resource is None or resource.get("kind") != kind:
        return {"type": "%s_not_found" % kind, "resource": rid, "label": label}
    if not _covers(resource, start, end):
        return {
            "type": "%s_window_unavailable" % kind,
            "resource": rid,
            "label": label,
            "window": [mob["window_start"], mob["window_end"]],
            "resource_window": [resource.get("window_start"), resource.get("window_end")],
        }
    for hold in active_holds:
        if hold["resource_rid"] != rid:
            continue
        h_start = parse_window(hold["window_start"], "hold.window_start")
        h_end = parse_window(hold["window_end"], "hold.window_end")
        if _overlaps(start, end, h_start, h_end):
            return {
                "type": "%s_occupied" % kind,
                "resource": rid,
                "label": label,
                "window": [hold["window_start"], hold["window_end"]],
                "held_by_mobilization_id": hold["mobilization_id"],
                "held_by_mobilization_no": hold.get("mobilization_no"),
            }
    return None


def plan_resources(resources: List[Dict[str, Any]], active_holds: List[Dict[str, Any]],
                   mob: Dict[str, Any]) -> Plan:
    """对船机、班组、备缆做同一时间窗的一次性原子规划。

    任意一类不满足都不产生预留，缺口全部列出（全有或全无）。
    备缆按 preferred_batches 指定顺序、再按批次编号顺序贪心切分。
    """
    by_rid = {str(r["rid"]): r for r in resources}
    start = parse_window(mob["window_start"], "window_start")
    end = parse_window(mob["window_end"], "window_end")
    if end <= start:
        raise ValidationError("时间窗结束必须晚于开始")

    gaps: List[Dict[str, Any]] = []
    vessel_gap = _unit_gap(VESSEL, mob["vessel_rid"], by_rid, active_holds, mob, "船机")
    if vessel_gap:
        gaps.append(vessel_gap)
    crew_gap = _unit_gap(CREW, mob["crew_rid"], by_rid, active_holds, mob, "接续班组")
    if crew_gap:
        gaps.append(crew_gap)

    required = float(mob["required_spare_km"])
    preferred = [str(x) for x in (mob.get("preferred_batches") or [])]
    occupied: Dict[str, float] = {}
    for hold in active_holds:
        if hold.get("kind") != CABLE:
            continue
        h_start = parse_window(hold["window_start"], "hold.window_start")
        h_end = parse_window(hold["window_end"], "hold.window_end")
        if _overlaps(start, end, h_start, h_end):
            occupied[hold["resource_rid"]] = occupied.get(hold["resource_rid"], 0.0) + float(hold["qty"])

    available: Dict[str, float] = {}
    for rid, resource in by_rid.items():
        if resource.get("kind") != CABLE or not _covers(resource, start, end):
            continue
        free = float(resource["capacity_qty"]) - occupied.get(rid, 0.0)
        if free > QTY_EPS:
            available[rid] = free

    ordered: List[str] = []
    for rid in preferred + sorted(available):
        if rid in available and rid not in ordered:
            ordered.append(rid)

    cable_holds: List[Dict[str, Any]] = []
    remaining = required
    for rid in ordered:
        if remaining <= QTY_EPS:
            break
        take = min(available[rid], remaining)
        if take > QTY_EPS:
            cable_holds.append({
                "resource_rid": rid, "kind": CABLE,
                "window_start": mob["window_start"], "window_end": mob["window_end"],
                "qty": round(take, 3),
            })
            remaining -= take
    shortfall = round(remaining, 3)
    if shortfall > QTY_EPS:
        gaps.append({
            "type": "cable_capacity",
            "label": "备缆",
            "required_km": round(required, 3),
            "shortfall_km": shortfall,
            "preferred_batches": preferred,
            "available": [{"batch": rid, "free_km": round(qty, 3)} for rid, qty in sorted(available.items())],
        })

    if gaps:
        return Plan(holds=[], gaps=gaps)

    holds = [{
        "resource_rid": mob["vessel_rid"], "kind": VESSEL,
        "window_start": mob["window_start"], "window_end": mob["window_end"], "qty": 1.0,
    }, {
        "resource_rid": mob["crew_rid"], "kind": CREW,
        "window_start": mob["window_start"], "window_end": mob["window_end"], "qty": 1.0,
    }]
    holds.extend(cable_holds)
    return Plan(holds=holds, gaps=[])


def cable_manifest(active_holds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """根据已确认的备缆占用生成应到货物资清单。"""
    by_batch: Dict[str, float] = {}
    for hold in active_holds:
        if hold.get("kind") == CABLE:
            by_batch[hold["resource_rid"]] = by_batch.get(hold["resource_rid"], 0.0) + float(hold["qty"])
    return [{"batch": rid, "qty": round(qty, 3), "checksum": line_checksum(rid, qty)}
            for rid, qty in sorted(by_batch.items())]


def match_receipt(expected: List[Dict[str, Any]], reported: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """逐批次核对批次、数量、校验值，返回差异明细；无差异即对平。"""
    diffs: List[Dict[str, Any]] = []
    expected_by_batch = {line["batch"]: line for line in expected}
    reported_by_batch = {}
    for line in reported:
        reported_by_batch.setdefault(line["batch"], []).append(line)

    for batch, expected_line in expected_by_batch.items():
        reported_lines = reported_by_batch.pop(batch, [])
        if not reported_lines:
            diffs.append({"batch": batch, "reasons": ["batch_missing"],
                          "expected_qty": expected_line["qty"], "reported_qty": None,
                          "expected_checksum": expected_line["checksum"], "reported_checksum": None})
            continue
        reported_line = reported_lines[0]
        reasons = []
        reported_qty = reported_line.get("qty")
        if reported_qty is None or abs(float(reported_qty) - float(expected_line["qty"])) > QTY_EPS:
            reasons.append("qty_mismatch")
        if reported_line.get("checksum") != expected_line["checksum"]:
            reasons.append("checksum_mismatch")
        if reasons:
            diffs.append({"batch": batch, "reasons": reasons,
                          "expected_qty": expected_line["qty"], "reported_qty": reported_qty,
                          "expected_checksum": expected_line["checksum"],
                          "reported_checksum": reported_line.get("checksum")})
    for batch, extra_lines in reported_by_batch.items():
        for line in extra_lines:
            diffs.append({"batch": batch, "reasons": ["unexpected_batch"],
                          "expected_qty": None, "reported_qty": line.get("qty"),
                          "expected_checksum": None, "reported_checksum": line.get("checksum")})
    return diffs

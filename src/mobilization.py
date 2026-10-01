"""动员流程的纯领域规则：时间窗、资源分配、到货回执对账与幂等指纹。

本模块不访问数据库，全部为可单测的纯函数。
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .domain import ValidationError

# 动员单状态
DRAFT = "draft"                      # 草稿：容量不足，记录缺口
RESERVED = "reserved"                # 已预留：船机/班组/备缆同一时间窗锁定
PENDING_REVIEW = "pending_review"    # 待复核：回执批次/数量/校验值对不上
CONFIRMED = "confirmed"              # 已确认：复核通过，可以装船
LOADED = "loaded"                    # 已装船
VOIDED = "voided"                    # 已失效：勘察变更后被后继单取代

ACTIVE_STATES = (DRAFT, RESERVED, PENDING_REVIEW, CONFIRMED, LOADED)
RESOURCE_KINDS = ("vessel", "crew", "spare_lot")
KIND_LABELS = {"vessel": "船机", "crew": "接续班组", "spare_lot": "备缆批次"}


def parse_window(start: str, end: str) -> Tuple[str, str]:
    """校验并把时间窗归一化为 UTC 的可比较字符串（YYYY-MM-DDTHH:MM:SSZ）。"""
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValidationError("时间窗必须是ISO时间文本")

    def _parse(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("时间格式无效: %s" % value) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    start_dt, end_dt = _parse(start), _parse(end)
    if not start_dt < end_dt:
        raise ValidationError("时间窗开始必须早于结束")
    fmt = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return fmt(start_dt), fmt(end_dt)


def windows_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """归一化后的时间窗可直接按字符串比较。"""
    return start_a < end_b and start_b < end_a


def plan_allocation(resources: List[Dict[str, Any]], holds: List[Dict[str, Any]], demand: Dict[str, Any]) -> Dict[str, Any]:
    """在同一时间窗内统筹分配船机、班组和备缆批次。

    resources: 资源目录行；holds: 其它动员单未释放的占用行。
    返回 {"allocations": [...], "shortfalls": [...]}，两者可同时非空（部分满足）。
    """
    window_start = demand["window_start"]
    window_end = demand["window_end"]
    allocations: List[Dict[str, Any]] = []
    shortfalls: List[Dict[str, Any]] = []

    def busy_in_window(resource_id: int) -> bool:
        for hold in holds:
            if int(hold["resource_id"]) != int(resource_id) or hold.get("state") == "released":
                continue
            if windows_overlap(window_start, window_end, hold["window_start"], hold["window_end"]):
                return True
        return False

    # 船机、班组：同一时间窗不可重叠占用，标签必须满足调度要求
    required_tags = set(demand.get("vessel_tags") or [])
    for kind, count_key in (("vessel", "vessel_count"), ("crew", "crew_count")):
        required = int(demand[count_key])
        candidates = [
            item for item in resources
            if item["kind"] == kind and int(item.get("active", 1)) == 1
            and required_tags.issubset(set(json.loads(item.get("tags") or "[]")) if kind == "vessel" else set())
        ]
        free = [item for item in candidates if not busy_in_window(int(item["id"]))]
        take = min(required, len(free))
        for item in free[:take]:
            allocations.append({"resource_id": int(item["id"]), "kind": kind, "quantity": 1})
        gap = required - take
        if gap > 0:
            shortfalls.append({
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "required": required,
                "available": len(free),
                "gap": gap,
                "required_tags": sorted(required_tags),
                "message": "时间窗 %s~%s 缺少%d个%s" % (window_start, window_end, gap, KIND_LABELS[kind]),
            })

    # 备缆：可消耗库存，可用量 = 批次容量 - 其它动员单未释放占用
    required_spare = round(float(demand["spare_km"]), 3)
    if required_spare > 0:
        remaining = required_spare
        lots = [item for item in resources if item["kind"] == "spare_lot" and int(item.get("active", 1)) == 1]
        lots = sorted(lots, key=lambda item: (item.get("batch_no") or "", int(item["id"])))
        for lot in lots:
            used = sum(float(h["quantity"]) for h in holds
                       if int(h["resource_id"]) == int(lot["id"]) and h.get("state") != "released")
            available = round(float(lot["capacity"]) - used, 3)
            if available <= 0 or remaining <= 0:
                continue
            take = round(min(available, remaining), 3)
            allocations.append({"resource_id": int(lot["id"]), "kind": "spare_lot", "quantity": take})
            remaining = round(remaining - take, 3)
        if remaining > 0:
            total_available = round(sum(
                max(0.0, float(lot["capacity"]) - sum(
                    float(h["quantity"]) for h in holds
                    if int(h["resource_id"]) == int(lot["id"]) and h.get("state") != "released"))
                for lot in lots
            ), 3)
            shortfalls.append({
                "kind": "spare_lot",
                "kind_label": KIND_LABELS["spare_lot"],
                "required": required_spare,
                "available": total_available,
                "gap": remaining,
                "message": "备缆缺口 %.3f 公里（需 %.3f，时间窗内可用 %.3f）" % (remaining, required_spare, total_available),
            })

    return {"allocations": allocations, "shortfalls": shortfalls}


def compare_receipt(expected: Dict[str, Dict[str, Any]], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对账：逐项核对回执的批次、数量与校验值，返回不符明细（空列表表示完全相符）。"""
    mismatches: List[Dict[str, Any]] = []
    actual = {str(item["batch_no"]): item for item in items}
    if len(actual) != len(items):
        mismatches.append({"batch_no": "", "field": "batch", "expected": None, "actual": None,
                           "message": "回执内存在重复批次"})
    for batch_no, expect in expected.items():
        received = actual.get(batch_no)
        if received is None:
            mismatches.append({"batch_no": batch_no, "field": "batch", "expected": batch_no, "actual": None,
                               "message": "回执缺少预留批次 %s" % batch_no})
            continue
        if abs(float(received["quantity"]) - float(expect["quantity"])) > 1e-6:
            mismatches.append({"batch_no": batch_no, "field": "quantity",
                               "expected": float(expect["quantity"]), "actual": float(received["quantity"]),
                               "message": "批次 %s 数量不符：应收 %s，实收 %s" % (batch_no, expect["quantity"], received["quantity"])})
        if str(received.get("checksum", "")) != str(expect["checksum"]):
            mismatches.append({"batch_no": batch_no, "field": "checksum",
                               "expected": str(expect["checksum"]), "actual": str(received.get("checksum", "")),
                               "message": "批次 %s 校验值不符" % batch_no})
    for batch_no, received in actual.items():
        if batch_no not in expected:
            mismatches.append({"batch_no": batch_no, "field": "batch", "expected": None, "actual": batch_no,
                               "message": "回执出现未预留批次 %s" % batch_no})
    return mismatches


def request_fingerprint(record_id: int, window_start: str, window_end: str, demand: Dict[str, Any]) -> str:
    """同一动员编号重试续作时，用来识别“是不是同一笔确认”。"""
    payload = json.dumps(
        {"record_id": int(record_id), "window_start": window_start, "window_end": window_end,
         "demand": {key: demand[key] for key in sorted(demand)}},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def successor_no(existing: List[str], base: str) -> str:
    """为勘察变更后的重算单生成 -R1/-R2 递增编号（沿原始编号前缀继续递增）。"""
    import re
    prefix = re.sub(r"-R\d+$", "", base)
    seq = 1
    while True:
        candidate = "%s-R%d" % (prefix, seq)
        if candidate not in existing:
            return candidate
        seq += 1

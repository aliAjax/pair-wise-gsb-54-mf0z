import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
DISPATCHER = Actor("disp-1", "dispatcher")
WINDOW = {"window_start": "2026-10-05T08:00:00Z", "window_end": "2026-10-06T08:00:00Z"}
WINDOW_2 = {"window_start": "2026-10-06T09:00:00Z", "window_end": "2026-10-07T09:00:00Z"}


def approved_record(service, reference):
    record = service.create(Actor("creator", "noc_operator"), reference, CREATE_DATA)
    record = service.act(Actor("rm", "repair_manager"), record["id"], record["version"],
                         "approve", {"repair_manager": "RM-2"})
    return record


def seed_resources(service):
    service.create_resource(DISPATCHER, {"kind": "vessel", "name": "CS-1", "capacity": 1})
    service.create_resource(DISPATCHER, {"kind": "vessel", "name": "CS-2", "capacity": 1})
    service.create_resource(DISPATCHER, {"kind": "crew", "name": "TEAM-A", "capacity": 1})
    service.create_resource(DISPATCHER, {"kind": "spare_lot", "name": "备缆B100",
                                         "batch_no": "B-100", "capacity_km": 20.0, "checksum": "sha256-B100"})
    service.create_resource(DISPATCHER, {"kind": "spare_lot", "name": "备缆B200",
                                         "batch_no": "B-200", "capacity_km": 20.0, "checksum": "sha256-B200"})


class MobilizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        seed_resources(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_confirm_reserves_all_in_one_window(self):
        record = approved_record(self.service, "CABLE-30001")
        mob = self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-1", dict(WINDOW))
        self.assertEqual(mob["state"], "reserved")
        self.assertEqual(mob["shortfalls"], [])
        kinds = sorted(hold["kind"] for hold in mob["holds"])
        self.assertEqual(kinds, ["crew", "spare_lot", "vessel"])
        spare = [h for h in mob["holds"] if h["kind"] == "spare_lot"][0]
        self.assertAlmostEqual(spare["quantity"], 15.75, places=2)
        timeline = self.service.mobilization_timeline(DISPATCHER, "MOB-1")
        self.assertEqual(timeline[0]["kind"], "mobilization_reserved")
        self.assertEqual(timeline[0]["source"], "dispatch")

    def test_capacity_gap_keeps_draft_then_retry_succeeds_without_double_hold(self):
        record = approved_record(self.service, "CABLE-30002")
        mob = self.service.confirm_mobilization(
            DISPATCHER, record["id"], "MOB-2", {**WINDOW, "spare_km": 50.0})
        self.assertEqual(mob["state"], "draft")
        self.assertEqual(len(mob["shortfalls"]), 1)
        self.assertEqual(mob["shortfalls"][0]["kind"], "spare_lot")
        self.assertAlmostEqual(mob["shortfalls"][0]["gap"], 10.0, places=2)
        # 船机班组仍已部分预留
        self.assertGreaterEqual(len(mob["holds"]), 2)
        # 缺口补齐后按同一动员编号重试续作
        self.service.create_resource(DISPATCHER, {"kind": "spare_lot", "name": "备缆B300",
                                                  "batch_no": "B-300", "capacity_km": 15.0, "checksum": "sha256-B300"})
        mob = self.service.confirm_mobilization(
            DISPATCHER, record["id"], "MOB-2", {**WINDOW, "spare_km": 50.0})
        self.assertEqual(mob["state"], "reserved")
        self.assertEqual(mob["version"], 2)
        # 同一资源在一张动员单上只有一条占用，不重复
        resource_ids = [h["resource_id"] for h in mob["holds"]]
        self.assertEqual(len(resource_ids), len(set(resource_ids)))
        total_spare = sum(h["quantity"] for h in mob["holds"] if h["kind"] == "spare_lot")
        self.assertAlmostEqual(total_spare, 50.0, places=2)

    def test_two_dispatchers_concurrently_only_one_wins(self):
        record = approved_record(self.service, "CABLE-30003")
        barrier = threading.Barrier(2)
        results = []

        def confirm(mob_no):
            try:
                barrier.wait(timeout=10)
                self.service.confirm_mobilization(
                    Actor(mob_no.lower(), "dispatcher"), record["id"], mob_no, dict(WINDOW_2))
                results.append("ok")
            except Conflict:
                results.append("conflict")
            except Exception as exc:  # pragma: no cover - 暴露意外错误
                results.append("other:%r" % exc)

        threads = [threading.Thread(target=confirm, args=("MOB-3A",)),
                   threading.Thread(target=confirm, args=("MOB-3B",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])

    def test_retry_after_failure_with_same_number_is_idempotent(self):
        record = approved_record(self.service, "CABLE-30004")
        # 船机先被另一张动员单在同一时间窗占掉，MOB-4 只能是草稿
        rec2 = self.service.create(Actor("creator", "noc_operator"), "CABLE-30099", dict(CREATE_DATA, segment="S9"))
        rec2 = self.service.act(Actor("rm", "repair_manager"), rec2["id"], rec2["version"],
                                "approve", {"repair_manager": "RM-2"})
        self.service.confirm_mobilization(DISPATCHER, rec2["id"], "MOB-BUSY",
                                          {**WINDOW, "spare_km": 1.0})
        mob = self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-4", dict(WINDOW))
        self.assertEqual(mob["state"], "draft")
        # 重复点确认：仍是草稿且占用不叠加
        mob_again = self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-4", dict(WINDOW))
        self.assertEqual(mob_again["state"], "draft")
        counts = {}
        for hold in mob_again["holds"]:
            counts[hold["resource_id"]] = counts.get(hold["resource_id"], 0) + 1
        self.assertTrue(all(count == 1 for count in counts.values()))
        # 已生效的确认重复提交：原样返回，不新增占用
        rec3 = self.service.create(Actor("creator", "noc_operator"), "CABLE-30100",
                                   dict(CREATE_DATA, segment="S8"))
        rec3 = self.service.act(Actor("rm", "repair_manager"), rec3["id"], rec3["version"],
                                "approve", {"repair_manager": "RM-2"})
        first = self.service.confirm_mobilization(DISPATCHER, rec3["id"], "MOB-5", dict(WINDOW_2))
        second = self.service.confirm_mobilization(DISPATCHER, rec3["id"], "MOB-5", dict(WINDOW_2))
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(len(first["holds"]), len(second["holds"]))

    def test_same_mob_number_with_different_content_rejected(self):
        record = approved_record(self.service, "CABLE-30005")
        self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-6", dict(WINDOW))
        with self.assertRaises(Conflict):
            self.service.confirm_mobilization(
                DISPATCHER, record["id"], "MOB-6", {**WINDOW, "spare_km": 99.0})

    def test_receipt_match_confirms_and_loads(self):
        record = approved_record(self.service, "CABLE-30006")
        self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-7", dict(WINDOW))
        result = self.service.submit_receipt(
            Actor("sup-1", "external_org", "海洋备缆厂"), "MOB-7",
            {"supplier": "海洋备缆厂",
             "items": [{"batch_no": "B-100", "quantity": 15.75, "checksum": "sha256-B100"}]})
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["mismatches"], [])
        loaded = self.service.load_mobilization(DISPATCHER, "MOB-7", {"note": "吊装完成"})
        self.assertEqual(loaded["state"], "loaded")
        self.assertTrue(all(h["state"] == "consumed" for h in loaded["holds"] if h["kind"] == "spare_lot"))

    def test_receipt_mismatch_holds_for_review_and_blocks_loading(self):
        record = approved_record(self.service, "CABLE-30007")
        self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-8", dict(WINDOW))
        # 校验值不符
        result = self.service.submit_receipt(
            Actor("sup-2", "external_org", "外单位X"), "MOB-8",
            {"items": [{"batch_no": "B-100", "quantity": 15.75, "checksum": "wrong"}]})
        self.assertEqual(result["state"], "pending_review")
        fields = {item["field"] for item in result["mismatches"]}
        self.assertEqual(fields, {"checksum"})
        with self.assertRaises(Conflict):
            self.service.load_mobilization(DISPATCHER, "MOB-8", {})
        # 复核驳回：仍停留在待复核
        reviewed = self.service.review_receipt(DISPATCHER, "MOB-8", {"approve": False, "note": "要求重发"})
        self.assertEqual(reviewed["state"], "pending_review")
        # 数量不符再收一单，复核通过后才放行
        result2 = self.service.submit_receipt(
            Actor("sup-2", "external_org"), "MOB-8",
            {"items": [{"batch_no": "B-100", "quantity": 10.0, "checksum": "sha256-B100"}]})
        self.assertEqual(result2["state"], "pending_review")
        self.assertIn("quantity", {item["field"] for item in result2["mismatches"]})
        reviewed = self.service.review_receipt(DISPATCHER, "MOB-8", {"approve": True, "note": "差异已说明"})
        self.assertEqual(reviewed["state"], "confirmed")
        loaded = self.service.load_mobilization(DISPATCHER, "MOB-8", {})
        self.assertEqual(loaded["state"], "loaded")

    def test_survey_revision_voids_holds_and_recalculates(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30008", CREATE_DATA)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"],
                                  "approve", {"repair_manager": "RM-2"})
        record = self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"],
                                  "mobilize", {"weather_window_hours": 40, "available_spare_km": 18,
                                               "vessel_name": "CS-1"})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"],
                                  "survey", {"survey_complete": True, "fault_location_km": 128})
        mob = self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-9", dict(WINDOW))
        self.assertEqual(mob["state"], "reserved")
        # 勘察结果变更：故障点移动、备缆需求变到 40km（超过现有40km可用量边界，恰好=40 够）
        record = self.service.act(
            Actor("eng", "cable_engineer"), record["id"], record["version"], "survey_revise",
            {"fault_location_km": 130, "required_spare_km": 40.0})
        old = self.service.get_mobilization(DISPATCHER, "MOB-9")
        self.assertEqual(old["state"], "voided")
        self.assertTrue(all(h["state"] == "released" for h in old["holds"]))
        successor = self.service.get_mobilization(DISPATCHER, "MOB-9-R1")
        self.assertEqual(successor["state"], "reserved")
        self.assertEqual(successor["successor_of"], "MOB-9")
        total_spare = sum(h["quantity"] for h in successor["holds"] if h["kind"] == "spare_lot")
        self.assertAlmostEqual(total_spare, 40.0, places=2)
        # 再变一次：新缺口出现（要50，可用40），且旧-R1占用已释放可被重算
        record = self.service.act(
            Actor("eng", "cable_engineer"), record["id"], record["version"], "survey_revise",
            {"fault_location_km": 131, "required_spare_km": 50.0})
        self.assertEqual(self.service.get_mobilization(DISPATCHER, "MOB-9-R1")["state"], "voided")
        r2 = self.service.get_mobilization(DISPATCHER, "MOB-9-R2")
        self.assertEqual(r2["state"], "draft")
        self.assertAlmostEqual(r2["shortfalls"][0]["gap"], 10.0, places=2)

    def test_console_shows_holds_gaps_and_review_sources(self):
        record = approved_record(self.service, "CABLE-30010")
        # 缺口草稿
        rec_gap = self.service.create(Actor("creator", "noc_operator"), "CABLE-30011",
                                      dict(CREATE_DATA, segment="S7"))
        rec_gap = self.service.act(Actor("rm", "repair_manager"), rec_gap["id"], rec_gap["version"],
                                   "approve", {"repair_manager": "RM-2"})
        self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-10", dict(WINDOW))
        self.service.confirm_mobilization(DISPATCHER, rec_gap["id"], "MOB-11",
                                          {**WINDOW, "spare_km": 80.0})
        self.service.submit_receipt(
            Actor("sup", "external_org"), "MOB-10",
            {"items": [{"batch_no": "B-100", "quantity": 1, "checksum": "bad"}]})
        console = self.service.console(DISPATCHER)
        hold_mobs = {h["mob_no"] for h in console["active_holds"]}
        self.assertIn("MOB-10", hold_mobs)
        gap_mobs = {g["mob_no"] for g in console["shortfalls"]}
        self.assertIn("MOB-11", gap_mobs)
        self.assertEqual(console["shortfalls"][0]["source"], "dispatch")
        review = {p["mob_no"]: p for p in console["pending_review"]}["MOB-10"]
        self.assertEqual(review["source"], "receipt")
        self.assertTrue(review["mismatches"])
        kinds = {e["kind"] for e in console["events"]}
        self.assertIn("mobilization_reserved", kinds)
        self.assertIn("receipt_mismatch", kinds)

    def test_permissions(self):
        record = approved_record(self.service, "CABLE-30012")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_mobilization(Actor("x", "noc_operator"), record["id"], "MOB-X", dict(WINDOW))
        mob = self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-12", dict(WINDOW))
        with self.assertRaises(PermissionDenied):
            self.service.submit_receipt(Actor("x", "vessel_master"), "MOB-12",
                                        {"items": [{"batch_no": "B-100", "quantity": 15.75, "checksum": "sha256-B100"}]})
        with self.assertRaises(PermissionDenied):
            self.service.review_receipt(Actor("x", "external_org"), "MOB-12", {"approve": True})

    def test_cannot_confirm_after_splice(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30013", CREATE_DATA)
        for action, role, data in [
            ("approve", "repair_manager", {"repair_manager": "RM-2"}),
            ("mobilize", "vessel_master", {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"}),
            ("survey", "cable_engineer", {"survey_complete": True, "fault_location_km": 128}),
            ("splice", "cable_engineer", {"splice_loss_db": 0.12, "spare_used_km": 16}),
        ]:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
        with self.assertRaises(Conflict):
            self.service.confirm_mobilization(DISPATCHER, record["id"], "MOB-13", dict(WINDOW))

import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.mobilization import VESSEL, CREW, CABLE, line_checksum, match_receipt, plan_resources
from src.mobilization_service import MobilizationService


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
W1 = {"window_start": "2026-10-05T02:00:00+00:00", "window_end": "2026-10-06T06:00:00+00:00"}
W2 = {"window_start": "2026-10-06T08:00:00+00:00", "window_end": "2026-10-07T08:00:00+00:00"}
DISPATCHER = Actor("disp-1", "dispatcher")


def approved(service):
    record = service.create(Actor("creator", "noc_operator"), "CABLE-40001", CREATE_DATA)
    return service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})


def mob_data(**overrides):
    data = {"vessel_rid": "CS-1", "crew_rid": "CREW-A", "preferred_batches": ["BATCH-01"]}
    data.update(W1)
    data.update(overrides)
    return data


class MobilizationFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.mobs: MobilizationService = self.service.mobilization_service
        self.record = approved(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def _draft(self, no="MOB-001", record_id=None, **overrides):
        return self.mobs.create_draft(DISPATCHER, no, record_id or self.record["id"], mob_data(**overrides))

    def test_confirm_reserves_all_three_in_one_window(self):
        draft = self._draft()
        self.assertEqual(draft["state"], "draft")
        confirmed = self.mobs.confirm(DISPATCHER, draft["id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["gaps"], [])
        holds = self.mobs.get(DISPATCHER, draft["id"])["holds"]
        kinds = sorted(h["kind"] for h in holds if h["status"] == "active")
        self.assertEqual(kinds, sorted([VESSEL, CABLE, CREW]))
        self.assertEqual([h["window_start"] for h in holds], [W1["window_start"]] * len(holds))
        # 备缆按优先批次切分，需要 15.75km
        cable = [h for h in holds if h["kind"] == CABLE]
        self.assertEqual(len(cable), 1)
        self.assertAlmostEqual(cable[0]["qty"], 15.75, places=3)
        self.assertEqual(cable[0]["resource_rid"], "BATCH-01")

    def test_insufficient_capacity_keeps_draft_with_gaps_and_no_holds(self):
        # 船机/班组：第二张同窗口单撞 MOB-A；备缆：同窗口用另一套船机班组吃满批次制造缺口。
        self._draft("MOB-A")
        first = self.mobs.get_by_no(DISPATCHER, "MOB-A")
        self.mobs.confirm(DISPATCHER, first["id"])

        def make_record(ref, segment):
            data = dict(CREATE_DATA)
            data["segment"] = segment
            rec = self.service.create(Actor("creator", "noc_operator"), ref, data)
            return self.service.act(Actor("rm", "repair_manager"), rec["id"], rec["version"], "approve", {"repair_manager": "RM-X"})

        # CS-2/CREW-B 同窗口占走 BATCH-02(15) 与 BATCH-03(0.75)
        rec = make_record("CABLE-40010", "S20")
        d = self.mobs.create_draft(DISPATCHER, "MOB-CAP", rec["id"], mob_data(
            vessel_rid="CS-2", crew_rid="CREW-B", preferred_batches=["BATCH-02", "BATCH-03"]))
        self.mobs.confirm(DISPATCHER, d["id"])
        # 同窗口可用备缆：BATCH-01 余 4.25 + BATCH-03 余 9.25 = 13.5 < 需 15.75
        record2 = make_record("CABLE-40002", "S4")
        draft2 = self.mobs.create_draft(DISPATCHER, "MOB-B", record2["id"], mob_data(preferred_batches=[]))
        result = self.mobs.confirm(DISPATCHER, draft2["id"])
        self.assertEqual(result["state"], "draft")
        gap_types = {g["type"] for g in result["gaps"]}
        self.assertIn("vessel_occupied", gap_types)
        self.assertIn("crew_occupied", gap_types)
        self.assertIn("cable_capacity", gap_types)
        # 全有或全无：失败不得留下任何占用
        holds = self.mobs.get(DISPATCHER, draft2["id"])["holds"]
        self.assertEqual(holds, [])
        vessel_gap = next(g for g in result["gaps"] if g["type"] == "vessel_occupied")
        self.assertEqual(vessel_gap["held_by_mobilization_no"], "MOB-A")
        cable_gap = next(g for g in result["gaps"] if g["type"] == "cable_capacity")
        self.assertAlmostEqual(cable_gap["shortfall_km"], 2.25, places=2)

    def test_non_overlapping_window_can_both_confirm(self):
        draft1 = self._draft("MOB-A")
        self.mobs.confirm(DISPATCHER, draft1["id"])
        other_data = dict(CREATE_DATA)
        other_data["segment"] = "S4"
        record2 = self.service.create(Actor("creator", "noc_operator"), "CABLE-40002", other_data)
        record2 = self.service.act(Actor("rm", "repair_manager"), record2["id"], record2["version"], "approve", {"repair_manager": "RM-3"})
        draft2 = self.mobs.create_draft(DISPATCHER, "MOB-B", record2["id"], mob_data(**W2))
        result = self.mobs.confirm(DISPATCHER, draft2["id"])
        self.assertEqual(result["state"], "confirmed")

    def test_concurrent_confirm_only_one_takes_effect(self):
        draft = self._draft()
        outcomes = []

        def confirm():
            try:
                outcomes.append(self.mobs.confirm(Actor("disp-%d" % threading.get_ident(), "dispatcher"), draft["id"]))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 2)
        # 两处调用都应得到同一张已确认动员单（幂等续作），不会重复占位
        self.assertTrue(all(isinstance(o, dict) and o["state"] == "confirmed" for o in outcomes), outcomes)
        self.assertEqual(len({o["id"] for o in outcomes}), 1)
        holds = self.mobs.get(DISPATCHER, draft["id"])["holds"]
        rids = [(h["resource_rid"], h["window_start"]) for h in holds if h["status"] == "active"]
        self.assertEqual(len(rids), len(set(rids)))
        self.assertEqual(len([h for h in holds if h["kind"] == VESSEL]), 1)

    def test_retry_with_same_mobilization_no_resumes_and_holds_are_idempotent(self):
        draft = self._draft("MOB-RESUME")
        first = self.mobs.confirm(DISPATCHER, draft["id"])
        # 写入“失败”后客户端拿同一编号重试：不新建、不重复占资源
        again = self.mobs.create_draft(DISPATCHER, "MOB-RESUME", self.record["id"], mob_data())
        self.assertEqual(again["id"], first["id"])
        reconfirmed = self.mobs.confirm(DISPATCHER, again["id"])
        self.assertEqual(reconfirmed["state"], "confirmed")
        holds = self.mobs.get(DISPATCHER, draft["id"])["holds"]
        self.assertEqual(len([h for h in holds if h["status"] == "active"]), 3)

    def test_receipt_mismatch_holds_pending_and_blocks_load(self):
        draft = self._draft()
        self.mobs.confirm(DISPATCHER, draft["id"])
        good = self.mobs.register_receipt(Actor("disp-1", "dispatcher", "外部物流A"), draft["id"], {
            "external_org": "外部物流A",
            "lines": [{"batch": "BATCH-01", "qty": 15.75}],
        })
        self.assertEqual(good["status"], "matched")
        self.assertEqual(good["mobilization"]["state"], "ready_to_load")

        other_data = dict(CREATE_DATA)
        other_data["segment"] = "S9"
        record2 = self.service.create(Actor("creator", "noc_operator"), "CABLE-40099", other_data)
        record2 = self.service.act(Actor("rm", "repair_manager"), record2["id"], record2["version"], "approve", {"repair_manager": "RM-9"})
        draft2 = self.mobs.create_draft(Actor("d2", "dispatcher"), "MOB-X", record2["id"], mob_data(**W2))
        self.mobs.confirm(Actor("d2", "dispatcher"), draft2["id"])
        bad = self.mobs.register_receipt(Actor("d2", "dispatcher", "外部物流B"), draft2["id"], {
            "external_org": "外部物流B",
            "lines": [{"batch": "BATCH-01", "qty": 10.0, "checksum": "deadbeefdead"}],
        })
        self.assertEqual(bad["status"], "pending")
        mob = self.mobs.get(Actor("d2", "dispatcher"), draft2["id"])
        self.assertEqual(mob["state"], "receipt_pending")
        self.assertEqual(mob["latest_receipt"]["diffs"][0]["reasons"], ["qty_mismatch", "checksum_mismatch"])
        bad_receipt = bad["id"]
        with self.assertRaises(Conflict):
            self.mobs.load(Actor("d2", "dispatcher"), draft2["id"])
        # 复核拒绝 -> 回 confirmed 重对账；重新登记对平后才能装船
        self.mobs.review_receipt(Actor("rm", "repair_manager"), bad_receipt, {"decision": "reject"})
        self.assertEqual(self.mobs.get(Actor("d2", "dispatcher"), draft2["id"])["state"], "confirmed")
        good2 = self.mobs.register_receipt(Actor("d2", "dispatcher", "外部物流B"), draft2["id"], {
            "external_org": "外部物流B",
            "lines": [{"batch": "BATCH-01", "qty": 15.75}],
        })
        self.assertEqual(good2["status"], "matched")
        loaded = self.mobs.load(Actor("vm", "vessel_master"), draft2["id"])
        self.assertEqual(loaded["state"], "loaded")

    def test_survey_change_invalidates_unconsumed_holds_and_recalculates(self):
        draft = self._draft()
        self.mobs.confirm(DISPATCHER, draft["id"])
        holds_before = [h for h in self.mobs.get(DISPATCHER, draft["id"])["holds"] if h["status"] == "active"]
        self.assertEqual(len(holds_before), 3)
        # 推进到勘察：勘察点变化（此处 required_spare_km 保持），未接续占用应全部失效
        self.service.act(Actor("vm", "vessel_master"), self.record["id"], self.record["version"], "mobilize",
                         {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"})
        record = self.service.get_record(Actor("c", "noc_operator"), self.record["id"])
        self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "survey",
                         {"survey_complete": True, "fault_location_km": 128})
        mob = self.mobs.get(DISPATCHER, draft["id"])
        self.assertEqual(mob["state"], "draft")
        active = [h for h in mob["holds"] if h["status"] == "active"]
        self.assertEqual(active, [])
        self.assertTrue(all(h["status"] == "released" for h in mob["holds"]))
        # 同窗口资源已释放：另一张单现在可以占用同一船机
        other_data = dict(CREATE_DATA)
        other_data["segment"] = "S5"
        record2 = self.service.create(Actor("creator", "noc_operator"), "CABLE-40005", other_data)
        record2 = self.service.act(Actor("rm", "repair_manager"), record2["id"], record2["version"], "approve", {"repair_manager": "RM-5"})
        draft2 = self.mobs.create_draft(DISPATCHER, "MOB-C", record2["id"], mob_data())
        self.assertEqual(self.mobs.confirm(DISPATCHER, draft2["id"])["state"], "confirmed")
        # 原动员单按同号续作：确认时在锁内重算，不会重复占用
        renewed = self.mobs.confirm(DISPATCHER, draft["id"])
        # CS-1 又被 MOB-C 占走，原单应保留草稿并给出船机缺口
        self.assertEqual(renewed["state"], "draft")
        self.assertIn("vessel_occupied", {g["type"] for g in renewed["gaps"]})

    def test_splice_consumes_holds_and_completes_mobilization(self):
        # 时序：船机动员 -> 勘察（占用失效点）-> 调度确认动员 -> 回执对平 -> 装船 -> 接续消耗
        self.service.act(Actor("vm", "vessel_master"), self.record["id"], self.record["version"], "mobilize",
                         {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"})
        record = self.service.get_record(Actor("c", "noc_operator"), self.record["id"])
        self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "survey",
                         {"survey_complete": True, "fault_location_km": 128})
        draft = self._draft()
        self.mobs.confirm(DISPATCHER, draft["id"])
        self.mobs.register_receipt(Actor("d", "dispatcher", "物流"), draft["id"], {
            "external_org": "物流", "lines": [{"batch": "BATCH-01", "qty": 15.75}]})
        self.mobs.load(Actor("vm", "vessel_master"), draft["id"])
        record = self.service.get_record(Actor("c", "noc_operator"), self.record["id"])
        self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "splice",
                         {"splice_loss_db": 0.1, "spare_used_km": 16})
        mob = self.mobs.get(DISPATCHER, draft["id"])
        self.assertEqual(mob["state"], "completed")
        self.assertTrue(all(h["status"] == "consumed" for h in mob["holds"]))

    def test_cancel_record_releases_holds_and_cancels_mobilization(self):
        draft = self._draft()
        self.mobs.confirm(DISPATCHER, draft["id"])
        self.service.act(Actor("rm", "repair_manager"), self.record["id"], self.record["version"],
                         "cancel", {"cancel_reason": "误报"})
        mob = self.mobs.get(DISPATCHER, draft["id"])
        self.assertEqual(mob["state"], "cancelled")
        self.assertTrue(all(h["status"] == "released" for h in mob["holds"]))
        # 资源释放后同窗口可被新单占用
        other_data = dict(CREATE_DATA)
        other_data["segment"] = "S6"
        rec2 = self.service.create(Actor("creator", "noc_operator"), "CABLE-40006", other_data)
        rec2 = self.service.act(Actor("rm", "repair_manager"), rec2["id"], rec2["version"], "approve", {"repair_manager": "RM-6"})
        draft2 = self.mobs.create_draft(DISPATCHER, "MOB-D", rec2["id"], mob_data())
        self.assertEqual(self.mobs.confirm(DISPATCHER, draft2["id"])["state"], "confirmed")

    def test_board_and_merged_timeline_show_holds_gaps_and_pending_source(self):
        draft = self._draft()
        self.mobs.confirm(DISPATCHER, draft["id"])
        self.mobs.register_receipt(Actor("d", "dispatcher", "外部物流Z"), draft["id"], {
            "external_org": "外部物流Z",
            "lines": [{"batch": "BATCH-02", "qty": 1.0, "checksum": "000000000000"}],
        })
        board = self.mobs.board(DISPATCHER)
        entry = next(b for b in board if b["mobilization_no"] == "MOB-001")
        self.assertEqual(entry["state"], "receipt_pending")
        self.assertEqual(entry["latest_receipt"]["external_org"], "外部物流Z")
        self.assertTrue(entry["active_holds"])
        timeline = self.mobs.merged_timeline(DISPATCHER, self.record["id"])
        actions = [e["action"] for e in timeline]
        self.assertIn("confirmed", actions)
        self.assertIn("receipt_pending", actions)
        pending = next(e for e in timeline if e["action"] == "receipt_pending")
        self.assertEqual(pending["source"], "receipt:外部物流Z")
        self.assertTrue(pending["details"]["diffs"])


class PlannerPureTest(unittest.TestCase):
    def test_match_receipt_diff_reasons(self):
        expected = [{"batch": "B1", "qty": 5.0, "checksum": line_checksum("B1", 5.0)}]
        diffs = match_receipt(expected, [{"batch": "B1", "qty": 5.0, "checksum": line_checksum("B1", 5.0)}])
        self.assertEqual(diffs, [])
        diffs = match_receipt(expected, [{"batch": "B1", "qty": 4.0, "checksum": "x"}])
        self.assertEqual(set(diffs[0]["reasons"]), {"qty_mismatch", "checksum_mismatch"})
        diffs = match_receipt(expected, [])
        self.assertEqual(diffs[0]["reasons"], ["batch_missing"])

    def test_plan_spills_across_batches(self):
        resources = [
            {"rid": "V", "kind": VESSEL, "window_start": None, "window_end": None},
            {"rid": "C", "kind": CREW, "window_start": None, "window_end": None},
            {"rid": "B1", "kind": CABLE, "capacity_qty": 10.0, "window_start": None, "window_end": None},
            {"rid": "B2", "kind": CABLE, "capacity_qty": 10.0, "window_start": None, "window_end": None},
        ]
        mob = {"vessel_rid": "V", "crew_rid": "C", "required_spare_km": 15.75, **W1, "preferred_batches": ["B1"]}
        plan = plan_resources(resources, [], mob)
        self.assertEqual(plan.gaps, [])
        cable = {(h["resource_rid"]): h["qty"] for h in plan.holds if h["kind"] == CABLE}
        self.assertAlmostEqual(cable["B1"], 10.0)
        self.assertAlmostEqual(cable["B2"], 5.75, places=3)

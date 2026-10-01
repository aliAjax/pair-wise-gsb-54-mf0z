import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import build_service
from src.http_api import create_server


def request(server, method, path, body=None, headers=None):
    host, port = server.server_address[:2]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request("http://%s:%s%s" % (host, port, path), data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


CREATE_DATA = {'cable': 'SEA-9', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}


class HttpSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path("/workspace/static"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_full_mobilization_over_http(self):
        dispatcher = {"X-User-Id": "d1", "X-Role": "dispatcher"}
        # 资源登记
        for payload in (
            {"kind": "vessel", "name": "CS-1", "capacity": 1},
            {"kind": "crew", "name": "TEAM-A", "capacity": 1},
            {"kind": "spare_lot", "name": "备缆", "batch_no": "B-100", "capacity_km": 20.0, "checksum": "CK-100"},
        ):
            status, _ = request(self.server, "POST", "/api/resources", payload, dispatcher)
            self.assertEqual(status, 201)
        # 故障单 + 批准
        status, record = request(self.server, "POST", "/api/records",
                                 {"reference": "CABLE-HTTP-1", "data": CREATE_DATA},
                                 {"X-User-Id": "c1", "X-Role": "noc_operator"})
        self.assertEqual(status, 201)
        status, record = request(self.server, "POST",
                                 "/api/records/%s/actions/approve" % record["id"],
                                 {"expected_version": record["version"], "data": {"repair_manager": "RM-1"}},
                                 {"X-User-Id": "r1", "X-Role": "repair_manager"})
        self.assertEqual(status, 200)
        # 确认动员
        status, mob = request(self.server, "POST",
                              "/api/records/%s/mobilizations" % record["id"],
                              {"mob_no": "MOB-HTTP-1",
                               "data": {"window_start": "2026-10-05T08:00Z", "window_end": "2026-10-06T08:00Z"}},
                              dispatcher)
        self.assertEqual(status, 200)
        self.assertEqual(mob["state"], "reserved")
        # 无身份被拒
        status, err = request(self.server, "GET", "/api/console")
        self.assertEqual(status, 403)
        # 回执校验值不符 -> 待复核，装船被拒
        status, result = request(self.server, "POST", "/api/mobilizations/MOB-HTTP-1/receipt",
                                 {"items": [{"batch_no": "B-100", "quantity": 15.75, "checksum": "BAD"}]},
                                 {"X-User-Id": "s1", "X-Role": "external_org", "X-Org": "supplier-x"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "pending_review")
        status, err = request(self.server, "POST", "/api/mobilizations/MOB-HTTP-1/load", {}, dispatcher)
        self.assertEqual(status, 409)
        # 正确回执 -> 确认 -> 装船
        status, result = request(self.server, "POST", "/api/mobilizations/MOB-HTTP-1/receipt",
                                 {"items": [{"batch_no": "B-100", "quantity": 15.75, "checksum": "CK-100"}]},
                                 {"X-User-Id": "s1", "X-Role": "external_org"})
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "confirmed")
        status, mob = request(self.server, "POST", "/api/mobilizations/MOB-HTTP-1/load", {}, dispatcher)
        self.assertEqual(status, 200)
        self.assertEqual(mob["state"], "loaded")
        # 调度台与动员时间线
        status, console = request(self.server, "GET", "/api/console", None, dispatcher)
        self.assertEqual(status, 200)
        self.assertTrue(console["active_holds"])
        kinds = {event["kind"] for event in console["events"]}
        self.assertIn("receipt_mismatch", kinds)
        self.assertIn("mobilization_loaded", kinds)
        status, timeline = request(self.server, "GET", "/api/mobilizations/MOB-HTTP-1/audit", None, dispatcher)
        self.assertEqual(status, 200)
        self.assertEqual(timeline["items"][0]["kind"], "mobilization_reserved")
        # 演示页
        req = urllib.request.Request("http://%s:%s/" % self.server.server_address[:2])
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("动员调度台", response.read().decode("utf-8"))

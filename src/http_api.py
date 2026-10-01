"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
MOBILIZE_RE = re.compile(r"^/api/records/(\d+)/mobilizations$")
MOB_RE = re.compile(r"^/api/mobilizations/([A-Za-z0-9_.\-]+)$")
MOB_AUDIT_RE = re.compile(r"^/api/mobilizations/([A-Za-z0-9_.\-]+)/audit$")
MOB_ACTION_RE = re.compile(r"^/api/mobilizations/([A-Za-z0-9_.\-]+)/(receipt|review|load)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "subsea-cable-repair/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "subsea-cable-repair", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if parsed.path == "/api/resources":
                    query = parse_qs(parsed.query)
                    kind = query.get("kind", [None])[0]
                    self._send(200, {"items": service.list_resources(self._actor(), kind)})
                    return
                if parsed.path == "/api/mobilizations":
                    query = parse_qs(parsed.query)
                    record_id = query.get("record_id", [None])[0]
                    state = query.get("state", [None])[0]
                    limit = int(query.get("limit", ["100"])[0])
                    items = service.list_mobilizations(
                        self._actor(),
                        record_id=int(record_id) if record_id is not None else None,
                        state=state, limit=limit)
                    self._send(200, {"items": items})
                    return
                match = MOB_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.mobilization_timeline(self._actor(), match.group(1))})
                    return
                match = MOB_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_mobilization(self._actor(), match.group(1)))
                    return
                if parsed.path == "/api/console":
                    self._send(200, service.console(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/resources":
                    resource = service.create_resource(self._actor(), body)
                    self._send(201, resource)
                    return
                match = MOBILIZE_RE.match(parsed.path)
                if match:
                    mob_no = body.get("mob_no", "")
                    version = body.get("expected_version")
                    if version is not None and not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    mob = service.confirm_mobilization(
                        self._actor(), int(match.group(1)), mob_no, body.get("data", {}),
                        expected_version=version if isinstance(version, int) else None)
                    self._send(200, mob)
                    return
                match = MOB_ACTION_RE.match(parsed.path)
                if match:
                    mob_no, sub = match.group(1), match.group(2)
                    if sub == "receipt":
                        result = service.submit_receipt(self._actor(), mob_no, body)
                    elif sub == "review":
                        result = service.review_receipt(self._actor(), mob_no, body)
                    else:
                        result = service.load_mobilization(self._actor(), mob_no, body)
                    self._send(200, result)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))

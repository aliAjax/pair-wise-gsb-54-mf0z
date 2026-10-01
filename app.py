"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path

from src.audit import AuditRecorder
from src.http_api import create_server
from src.mobilization_service import MobilizationService
from src.repository import Repository
from src.resource_repository import ResourceRepository
from src.rules import DomainRules
from src.service import Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "subsea-cable-repair.db"
DEFAULT_PORT = 8330

SEED_RESOURCES = [
    {"rid": "CS-1", "kind": "vessel", "name": "海缆船一号", "capacity_qty": 1.0},
    {"rid": "CS-2", "kind": "vessel", "name": "海缆船二号", "capacity_qty": 1.0},
    {"rid": "CREW-A", "kind": "crew", "name": "接续班组甲", "capacity_qty": 1.0},
    {"rid": "CREW-B", "kind": "crew", "name": "接续班组乙", "capacity_qty": 1.0},
    {"rid": "BATCH-01", "kind": "cable", "name": "备缆批次01", "capacity_qty": 20.0},
    {"rid": "BATCH-02", "kind": "cable", "name": "备缆批次02", "capacity_qty": 15.0},
    {"rid": "BATCH-03", "kind": "cable", "name": "备缆批次03", "capacity_qty": 10.0},
]


def build_service(db_path: str, seed: bool = True) -> Service:
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    resource_repository = ResourceRepository(db_path)
    mobilization_service = MobilizationService(repository, resource_repository)
    service = Service(repository, DomainRules(), audit, mobilization_service)
    if seed:
        for item in SEED_RESOURCES:
            resource_repository.upsert_resource(
                item["rid"], item["kind"], item["name"], item["capacity_qty"], None, None,
            )
    return service


def parse_args():
    parser = argparse.ArgumentParser(description="跨海光缆故障与抢修协调")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    service = build_service(args.db)
    server = create_server(args.host, args.port, service, BASE_DIR / "static")
    print("跨海光缆故障与抢修协调 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

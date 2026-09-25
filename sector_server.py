"""扇区编排台 HTTP 层。

只做请求解析、调用规则层（sector_rules）与存储层（sector_store）、返回 JSON。
排区判断与状态流转分别由规则层、存储层负责。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import sector_rules as rules
from sector_store import DEFAULT_DB, ROOT, SectorStore, StoreError

PAGE = ROOT / "static" / "sectors.html"


class SectorHandler(BaseHTTPRequestHandler):
    store: SectorStore

    # ---------- 基础 ----------
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self, data: dict[str, Any] | None = None) -> str:
        # 中文姓名放请求头会被 latin-1 限制，故优先支持请求体 by 字段
        header = self.headers.get("X-User", "").strip()
        if header:
            return header
        return str((data or {}).get("by", "")).strip()

    def _json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StoreError("Content-Length 无效") from exc
        if length > 1_000_000:
            raise StoreError("请求体过大", 413)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError("请求体不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise StoreError("JSON 请求体必须是对象")
        return data

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [p for p in urlparse(path).path.split("/") if p]

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        try:
            parts = self._parts(self.path)
            if not parts or parts == ["index.html"] or parts == ["sectors.html"]:
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["health"]:
                self._send(200, {"status": "ok", "service": "sar-sector-console"})
                return
            if parts == ["api", "state"]:
                self._send(200, self.store.state())
                return
            if len(parts) == 4 and parts[:2] == ["api", "missions"] and parts[3] == "plan":
                mission, vessels, sea_state = self.store.plan_input(int(parts[2]))
                self._send(200, rules.plan_sectors(mission, vessels, sea_state))
                return
            self._send(404, {"error": "接口不存在"})
        except StoreError as exc:
            self._send(exc.status, {"error": str(exc)})
        except ValueError:
            self._send(400, {"error": "路径参数无效"})

    def do_POST(self) -> None:
        try:
            parts = self._parts(self.path)
            data = self._json()
            actor = self._actor(data)
            if parts == ["api", "missions"]:
                result = self.store.create_mission(actor, **data)
            elif parts == ["api", "vessels"]:
                result = self.store.add_vessel(actor, **data)
            elif parts == ["api", "sea-state"]:
                result = self.store.set_sea_state(actor, data.get("level"), data.get("note", ""))
            elif len(parts) == 4 and parts[:2] == ["api", "missions"] and parts[3] == "publish":
                result = self._publish(int(parts[2]), actor, data)
            elif len(parts) == 4 and parts[:2] == ["api", "missions"] and parts[3] == "close":
                result = self.store.close_mission(actor, int(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "vessels"] and parts[3] == "report":
                result = self.store.report_vessel(actor, int(parts[2]), data.get("latitude"),
                                                  data.get("longitude"), data.get("endurance_km"))
            elif len(parts) == 4 and parts[:2] == ["api", "orchestrations"] and parts[3] == "reassign":
                result = self._reassign(int(parts[2]), actor, data)
            elif len(parts) == 4 and parts[:2] == ["api", "orchestrations"] and parts[3] == "cancel":
                result = self.store.cancel_orchestration(actor, int(parts[2]), data.get("reason", ""))
            elif len(parts) == 4 and parts[:2] == ["api", "sectors"] and parts[3] == "cancel":
                result = self.store.cancel_sector(actor, int(parts[2]), data.get("reason", ""))
            else:
                raise StoreError("接口不存在", 404)
            self._send(201, result)
        except StoreError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    # ---------- 组装规则与存储 ----------
    def _publish(self, mission_id: int, actor: str, data: dict[str, Any]) -> dict[str, Any]:
        if "expected_version" not in data:
            raise StoreError("缺少 expected_version（发布基于的页面版本）")
        mission, vessels, sea_state = self.store.plan_input(mission_id)
        plan = rules.plan_sectors(mission, vessels, sea_state)
        return self.store.publish(actor, mission_id, data["expected_version"], plan)

    def _reassign(self, orchestration_id: int, actor: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            sector_id, vessel_id = int(data["sector_id"]), int(data["vessel_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError("改派需要 sector_id 与 vessel_id") from exc
        orch = self.store.get_orchestration(orchestration_id)
        if not orch:
            raise StoreError("编排不存在", 404)
        sector = next((s for s in orch["sectors"] if s["id"] == sector_id), None)
        if not sector:
            raise StoreError("扇区不存在", 404)
        mission = self.store.get_mission(orch["mission_id"])
        vessel = self.store.get_vessel(vessel_id)
        if not vessel:
            raise StoreError("船只不存在", 404)
        metrics = rules.evaluate_vessel(vessel, mission, self.store.sea_state()["level"],
                                        sector["end_deg"] - sector["start_deg"])
        if not metrics["ok"]:
            raise StoreError("该船不适合此扇区：" + "；".join(metrics["reasons"]), 409)
        return self.store.reassign(actor, orchestration_id, sector_id, vessel_id, metrics,
                                   data.get("reason", ""))

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(store: SectorStore, host: str, port: int) -> None:
    SectorHandler.store = store
    server = ThreadingHTTPServer((host, port), SectorHandler)
    print("搜救扇区编排台 listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="搜救扇区编排台")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--seed", action="store_true", help="写入演示数据（已有数据时跳过）")
    args = parser.parse_args()
    store = SectorStore(args.db)
    if args.seed:
        print(json.dumps(store.seed_demo(), ensure_ascii=False))
    serve(store, args.host, args.port)


if __name__ == "__main__":
    main()

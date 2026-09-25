"""扇区编排台 HTTP 服务：只负责路由与 JSON 编解码。

规则在 sector_rules.py，存储在 sector_store.py，页面在 static/sector_console.html。
写操作需要 X-User 与 X-Role 请求头，编排类操作仅 coordinator 角色可用。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sector_store import DEFAULT_DB, ROOT, SectorStore, StoreError

PAGE = ROOT / "static" / "sector_console.html"


class SectorApiHandler(BaseHTTPRequestHandler):
    store: SectorStore

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

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

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/sectors", "/sector_console.html"}:
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "sector-console"})
                return
            if path == "/api/sector/state":
                self._send(200, self.store.state())
                return
            if path.startswith("/api/sector/incidents/") and path.endswith("/history"):
                incident_id = int(path.split("/")[4])
                self._send(200, {"history": self.store.incident_history(incident_id)})
                return
            self._send(404, {"error": "接口不存在"})
        except (StoreError, ValueError) as exc:
            self._send(getattr(exc, "status", 400), {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            data, (actor, role) = self._json(), self._actor()
            if path == "/api/sector/incidents":
                result = self.store.register_incident(actor, role, data)
            elif path == "/api/sector/incidents/update":
                result = self.store.update_incident(actor, role, **data)
            elif path == "/api/sector/resources":
                result = self.store.register_resource(actor, role, data)
            elif path == "/api/sector/resources/update":
                result = self.store.update_resource(actor, role, **data)
            elif path == "/api/sector/plans/preview":
                result = self.store.preview_plan(actor, role, **data)
            elif path == "/api/sector/plans/publish":
                result = self.store.publish_plan(actor, role, **data)
            elif path == "/api/sector/plans/cancel":
                result = self.store.cancel_plan(actor, role, **data)
            elif path == "/api/sector/sectors/reassign":
                result = self.store.reassign_sector(actor, role, **data)
            elif path == "/api/sector/sectors/cancel":
                result = self.store.cancel_sector(actor, role, **data)
            else:
                raise StoreError("接口不存在", 404)
            self._send(201, result)
        except StoreError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(store: SectorStore, host: str, port: int) -> None:
    SectorApiHandler.store = store
    server = ThreadingHTTPServer((host, port), SectorApiHandler)
    print("扇区编排台 listening on http://%s:%s/sectors" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="搜救扇区编排台")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    store = SectorStore(args.db)
    if args.init:
        print(json.dumps(store.seed_demo() if args.seed else {"initialized": True, "db": args.db},
                         ensure_ascii=False))
        return
    serve(store, args.host, args.port)


if __name__ == "__main__":
    main()

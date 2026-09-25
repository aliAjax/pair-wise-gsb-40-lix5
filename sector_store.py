"""扇区编排存储层。

负责 SQLite 持久化、编排版本控制（先到先得）、资源占用与释放、变更历史。
不做任何排区判断（规则见 sector_rules.py），由 HTTP 层组装两者。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "sector_console.db"


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    center_lat REAL NOT NULL,
    center_lon REAL NOT NULL,
    radius_km REAL NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    deadline TEXT NOT NULL,
    required_capability TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vessels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    capabilities TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    speed_kn REAL NOT NULL,
    endurance_km REAL NOT NULL,
    max_sea_state INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    status TEXT NOT NULL DEFAULT 'active',
    sea_state INTEGER NOT NULL,
    feasible INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS sectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orchestration_id INTEGER NOT NULL REFERENCES orchestrations(id),
    seq INTEGER NOT NULL,
    start_deg REAL NOT NULL,
    end_deg REAL NOT NULL,
    vessel_id INTEGER REFERENCES vessels(id),
    distance_km REAL NOT NULL,
    eta_min REAL NOT NULL,
    search_min REAL NOT NULL,
    completion_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER REFERENCES missions(id),
    orchestration_id INTEGER,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    before TEXT NOT NULL DEFAULT '',
    after TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sectors_orch ON sectors(orchestration_id, status);
CREATE INDEX IF NOT EXISTS idx_history_mission ON history(mission_id, id);
"""


class SectorStore:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---------- 基础 ----------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _tx(self):
        """写事务：BEGIN IMMEDIATE 保证并发写串行化，配合版本检查实现先到先得。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _actor(actor: str) -> str:
        actor = (actor or "").strip()
        if not actor:
            raise StoreError("缺少操作人（X-User）")
        return actor

    @staticmethod
    def _position(lat: Any, lon: Any) -> tuple[float, float]:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise StoreError("经纬度必须是数值") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise StoreError("经纬度超出有效范围")
        return lat, lon

    def _hist(self, conn: sqlite3.Connection, mission_id: int | None, orchestration_id: int | None,
              actor: str, action: str, before: Any, after: Any) -> None:
        conn.execute(
            "INSERT INTO history(mission_id,orchestration_id,actor,action,before,after,created_at) VALUES(?,?,?,?,?,?,?)",
            (mission_id, orchestration_id, actor, action,
             _dump(before) if before is not None else "", _dump(after) if after is not None else "", utcnow()),
        )

    @staticmethod
    def _vessel(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["capabilities"] = json.loads(data["capabilities"])
        return data

    @staticmethod
    def _hist_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("before", "after"):
            try:
                data[key] = json.loads(data[key]) if data[key] else None
            except json.JSONDecodeError:
                pass
        return data

    # ---------- 登记 ----------
    def create_mission(self, actor: str, code: str, name: str, center_lat: Any, center_lon: Any,
                       radius_km: Any, priority: Any, deadline: str,
                       required_capability: str = "", note: str = "") -> dict[str, Any]:
        actor = self._actor(actor)
        code, name = (code or "").strip(), (name or "").strip()
        if not code or not name:
            raise StoreError("事件编号和名称不能为空")
        lat, lon = self._position(center_lat, center_lon)
        try:
            radius_km, priority = float(radius_km), int(priority)
        except (TypeError, ValueError) as exc:
            raise StoreError("半径和优先级必须是数值") from exc
        if not 0 < radius_km <= 500 or not 1 <= priority <= 5:
            raise StoreError("搜索半径应在 0-500 公里，优先级 1-5")
        deadline = str(deadline or "").strip()
        try:
            parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise StoreError("最晚完成时刻格式无效") from exc
        if parsed <= datetime.now(timezone.utc):
            raise StoreError("最晚完成时刻必须晚于当前时间")
        with self._tx() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO missions(code,name,center_lat,center_lon,radius_km,priority,deadline,
                       required_capability,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, name, lat, lon, radius_km, priority, parsed.isoformat(timespec="seconds"),
                     (required_capability or "").strip(), (note or "").strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("事件编号已存在", 409) from exc
            self._hist(conn, cur.lastrowid, None, actor, "mission.create", None,
                       {"code": code, "deadline": parsed.isoformat(timespec="seconds")})
            return dict(conn.execute("SELECT * FROM missions WHERE id=?", (cur.lastrowid,)).fetchone())

    def add_vessel(self, actor: str, name: str, capabilities: list[str], latitude: Any, longitude: Any,
                   speed_kn: Any, endurance_km: Any, max_sea_state: Any) -> dict[str, Any]:
        actor = self._actor(actor)
        name = (name or "").strip()
        caps = sorted({str(c).strip() for c in (capabilities or []) if str(c).strip()})
        if not name or not caps:
            raise StoreError("船名和能力不能为空")
        lat, lon = self._position(latitude, longitude)
        try:
            speed_kn, endurance_km, max_sea_state = float(speed_kn), float(endurance_km), int(max_sea_state)
        except (TypeError, ValueError) as exc:
            raise StoreError("航速、航程和适用海况必须是数值") from exc
        if speed_kn <= 0 or endurance_km <= 0 or not 0 <= max_sea_state <= 9:
            raise StoreError("航速、剩余航程或适用海况无效")
        with self._tx() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO vessels(name,capabilities,latitude,longitude,speed_kn,endurance_km,max_sea_state,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, _dump(caps), lat, lon, speed_kn, endurance_km, max_sea_state, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("船名已存在", 409) from exc
            self._hist(conn, None, None, actor, "vessel.register", None, {"name": name, "capabilities": caps})
            return self._vessel(conn.execute("SELECT * FROM vessels WHERE id=?", (cur.lastrowid,)).fetchone())

    def report_vessel(self, actor: str, vessel_id: int, latitude: Any, longitude: Any, endurance_km: Any) -> dict[str, Any]:
        """值班员电话询问后录入的船位与剩余航程报告。"""
        actor = self._actor(actor)
        lat, lon = self._position(latitude, longitude)
        try:
            endurance_km = float(endurance_km)
        except (TypeError, ValueError) as exc:
            raise StoreError("剩余航程必须是数值") from exc
        if endurance_km < 0:
            raise StoreError("剩余航程不能为负")
        with self._tx() as conn:
            vessel = conn.execute("SELECT * FROM vessels WHERE id=?", (vessel_id,)).fetchone()
            if not vessel:
                raise StoreError("船只不存在", 404)
            before = {"name": vessel["name"], "latitude": vessel["latitude"], "longitude": vessel["longitude"],
                      "endurance_km": vessel["endurance_km"]}
            conn.execute(
                "UPDATE vessels SET latitude=?,longitude=?,endurance_km=?,version=version+1,updated_at=? WHERE id=?",
                (lat, lon, endurance_km, utcnow(), vessel_id),
            )
            self._hist(conn, None, None, actor, "vessel.report", before,
                       {"name": vessel["name"], "latitude": lat, "longitude": lon, "endurance_km": endurance_km})
            return self._vessel(conn.execute("SELECT * FROM vessels WHERE id=?", (vessel_id,)).fetchone())

    def set_sea_state(self, actor: str, level: Any, note: str = "") -> dict[str, Any]:
        actor = self._actor(actor)
        try:
            level = int(level)
        except (TypeError, ValueError) as exc:
            raise StoreError("海况等级必须是数值") from exc
        if not 0 <= level <= 9:
            raise StoreError("海况等级应在 0-9 之间")
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='sea_state'").fetchone()
            before = json.loads(row["value"]) if row else None
            value = {"level": level, "note": (note or "").strip(), "by": actor, "updated_at": utcnow()}
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('sea_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_dump(value),),
            )
            self._hist(conn, None, None, actor, "sea_state.update",
                       {"level": before["level"]} if before else None, {"level": level, "note": value["note"]})
            return value

    # ---------- 编排 ----------
    @staticmethod
    def _orch_snapshot(conn: sqlite3.Connection, orchestration_id: int) -> dict[str, Any] | None:
        orch = conn.execute("SELECT * FROM orchestrations WHERE id=?", (orchestration_id,)).fetchone()
        if not orch:
            return None
        data = dict(orch)
        data["sectors"] = [dict(r) for r in conn.execute(
            "SELECT * FROM sectors WHERE orchestration_id=? ORDER BY seq", (orchestration_id,)).fetchall()]
        return data

    def _sector_brief(self, conn: sqlite3.Connection, sector: sqlite3.Row) -> dict[str, Any]:
        name = None
        if sector["vessel_id"] is not None:
            row = conn.execute("SELECT name FROM vessels WHERE id=?", (sector["vessel_id"],)).fetchone()
            name = row["name"] if row else None
        return {"seq": sector["seq"], "vessel_id": sector["vessel_id"], "vessel_name": name,
                "span": "%.0f°-%.0f°" % (sector["start_deg"], sector["end_deg"])}

    def publish(self, actor: str, mission_id: int, expected_version: Any, plan: dict[str, Any]) -> dict[str, Any]:
        """发布编排：仅当 expected_version 与当前版本一致时生效，先到者胜。"""
        actor = self._actor(actor)
        sectors = plan.get("sectors") or []
        with self._tx() as conn:
            mission = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not mission:
                raise StoreError("事件不存在", 404)
            if mission["status"] == "closed":
                raise StoreError("事件已关闭，不能发布编排", 409)
            if int(expected_version) != mission["version"]:
                raise StoreError("已有其他协调员发布了新编排，本次发布被丢弃，请刷新后重试", 409)
            if not sectors:
                raise StoreError("没有可用的待命船只，无法发布编排", 400)
            for sec in sectors:
                vessel = conn.execute("SELECT * FROM vessels WHERE id=?", (sec["vessel_id"],)).fetchone()
                if not vessel:
                    raise StoreError("编排中的船只不存在", 404)
                if vessel["status"] != "available":
                    raise StoreError("船只「%s」刚被其他任务占用，请重新编排" % vessel["name"], 409)
            now = utcnow()
            before = None
            prev = conn.execute(
                "SELECT * FROM orchestrations WHERE mission_id=? AND status='active'", (mission_id,)
            ).fetchone()
            if prev:
                prev_sectors = conn.execute(
                    "SELECT * FROM sectors WHERE orchestration_id=? AND status='active'", (prev["id"],)
                ).fetchall()
                before = {"orchestration_id": prev["id"],
                          "sectors": [self._sector_brief(conn, s) for s in prev_sectors]}
                for sec in prev_sectors:
                    if sec["vessel_id"] is not None:
                        conn.execute("UPDATE vessels SET status='available',version=version+1,updated_at=? WHERE id=?",
                                     (now, sec["vessel_id"]))
                conn.execute("UPDATE sectors SET status='superseded' WHERE orchestration_id=? AND status='active'",
                             (prev["id"],))
                conn.execute("UPDATE orchestrations SET status='superseded',closed_by=?,closed_at=? WHERE id=?",
                             (actor, now, prev["id"]))
            cur = conn.execute(
                """INSERT INTO orchestrations(mission_id,status,sea_state,feasible,note,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (mission_id, "active", int(plan.get("sea_state", 0)), 1 if plan.get("feasible") else 0,
                 "；".join(plan.get("notes", []))[:500], actor, now),
            )
            orch_id = int(cur.lastrowid)
            after_sectors = []
            for sec in sectors:
                conn.execute(
                    """INSERT INTO sectors(orchestration_id,seq,start_deg,end_deg,vessel_id,distance_km,eta_min,
                       search_min,completion_at,status) VALUES(?,?,?,?,?,?,?,?,?,'active')""",
                    (orch_id, sec["seq"], sec["start_deg"], sec["end_deg"], sec["vessel_id"],
                     sec["distance_km"], sec["eta_min"], sec["search_min"], sec["completion_at"]),
                )
                conn.execute("UPDATE vessels SET status='assigned',version=version+1,updated_at=? WHERE id=?",
                             (now, sec["vessel_id"]))
                after_sectors.append({"seq": sec["seq"], "vessel_id": sec["vessel_id"],
                                      "vessel_name": sec.get("vessel_name"),
                                      "span": "%.0f°-%.0f°" % (sec["start_deg"], sec["end_deg"])})
            conn.execute("UPDATE missions SET status='covered',version=version+1 WHERE id=?", (mission_id,))
            self._hist(conn, mission_id, orch_id, actor, "republish" if prev else "publish",
                       before, {"orchestration_id": orch_id, "sectors": after_sectors})
            return self._orch_snapshot(conn, orch_id)

    def reassign(self, actor: str, orchestration_id: int, sector_id: int, new_vessel_id: int,
                 metrics: dict[str, Any], reason: str = "") -> dict[str, Any]:
        """改派扇区：释放原船，指派新船，历史留下前后指派。metrics 由规则层算出。"""
        actor = self._actor(actor)
        with self._tx() as conn:
            orch = conn.execute("SELECT * FROM orchestrations WHERE id=?", (orchestration_id,)).fetchone()
            if not orch or orch["status"] != "active":
                raise StoreError("编排已失效，不能改派", 409)
            sector = conn.execute(
                "SELECT * FROM sectors WHERE id=? AND orchestration_id=?", (sector_id, orchestration_id)
            ).fetchone()
            if not sector:
                raise StoreError("扇区不存在", 404)
            if sector["status"] != "active":
                raise StoreError("扇区已取消，不能改派", 409)
            new_vessel = conn.execute("SELECT * FROM vessels WHERE id=?", (new_vessel_id,)).fetchone()
            if not new_vessel:
                raise StoreError("船只不存在", 404)
            if new_vessel["status"] != "available":
                raise StoreError("船只「%s」当前不可用" % new_vessel["name"], 409)
            old_vessel = conn.execute("SELECT * FROM vessels WHERE id=?", (sector["vessel_id"],)).fetchone()
            now = utcnow()
            if old_vessel:
                conn.execute("UPDATE vessels SET status='available',version=version+1,updated_at=? WHERE id=?",
                             (now, old_vessel["id"]))
            conn.execute("UPDATE vessels SET status='assigned',version=version+1,updated_at=? WHERE id=?",
                         (now, new_vessel_id))
            conn.execute(
                """UPDATE sectors SET vessel_id=?,distance_km=?,eta_min=?,search_min=?,completion_at=? WHERE id=?""",
                (new_vessel_id, metrics["distance_km"], metrics["eta_min"], metrics["search_min"],
                 metrics["completion_at"], sector_id),
            )
            conn.execute("UPDATE missions SET version=version+1 WHERE id=?", (orch["mission_id"],))
            self._hist(conn, orch["mission_id"], orchestration_id, actor, "reassign",
                       {"seq": sector["seq"], "vessel_id": sector["vessel_id"],
                        "vessel_name": old_vessel["name"] if old_vessel else None,
                        "span": "%.0f°-%.0f°" % (sector["start_deg"], sector["end_deg"])},
                       {"seq": sector["seq"], "vessel_id": new_vessel_id, "vessel_name": new_vessel["name"],
                        "span": "%.0f°-%.0f°" % (sector["start_deg"], sector["end_deg"]),
                        "reason": (reason or "").strip()})
            return self._orch_snapshot(conn, orchestration_id)

    def cancel_orchestration(self, actor: str, orchestration_id: int, reason: str = "") -> dict[str, Any]:
        """取消整个编排：释放全部船只，历史留下取消前指派。"""
        actor = self._actor(actor)
        with self._tx() as conn:
            orch = conn.execute("SELECT * FROM orchestrations WHERE id=?", (orchestration_id,)).fetchone()
            if not orch:
                raise StoreError("编排不存在", 404)
            if orch["status"] != "active":
                raise StoreError("编排已取消或被取代", 409)
            sectors = conn.execute(
                "SELECT * FROM sectors WHERE orchestration_id=? AND status='active'", (orchestration_id,)
            ).fetchall()
            before = {"orchestration_id": orchestration_id,
                      "sectors": [self._sector_brief(conn, s) for s in sectors]}
            now = utcnow()
            for sec in sectors:
                if sec["vessel_id"] is not None:
                    conn.execute("UPDATE vessels SET status='available',version=version+1,updated_at=? WHERE id=?",
                                 (now, sec["vessel_id"]))
            conn.execute("UPDATE sectors SET status='cancelled' WHERE orchestration_id=? AND status='active'",
                         (orchestration_id,))
            conn.execute("UPDATE orchestrations SET status='cancelled',closed_by=?,closed_at=? WHERE id=?",
                         (actor, now, orchestration_id))
            conn.execute("UPDATE missions SET status='open',version=version+1 WHERE id=?", (orch["mission_id"],))
            self._hist(conn, orch["mission_id"], orchestration_id, actor, "cancel",
                       before, {"reason": (reason or "").strip()} if reason else None)
            return self._orch_snapshot(conn, orchestration_id)

    def cancel_sector(self, actor: str, sector_id: int, reason: str = "") -> dict[str, Any]:
        """取消单个扇区：释放该船；编排内无活跃扇区时整个编排随之取消。"""
        actor = self._actor(actor)
        with self._tx() as conn:
            sector = conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone()
            if not sector:
                raise StoreError("扇区不存在", 404)
            if sector["status"] != "active":
                raise StoreError("扇区已取消或被取代", 409)
            orch = conn.execute("SELECT * FROM orchestrations WHERE id=?", (sector["orchestration_id"],)).fetchone()
            if not orch or orch["status"] != "active":
                raise StoreError("编排已失效", 409)
            now = utcnow()
            before = self._sector_brief(conn, sector)
            if sector["vessel_id"] is not None:
                conn.execute("UPDATE vessels SET status='available',version=version+1,updated_at=? WHERE id=?",
                             (now, sector["vessel_id"]))
            conn.execute("UPDATE sectors SET status='cancelled' WHERE id=?", (sector_id,))
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM sectors WHERE orchestration_id=? AND status='active'",
                (sector["orchestration_id"],),
            ).fetchone()["c"]
            if not remaining:
                conn.execute("UPDATE orchestrations SET status='cancelled',closed_by=?,closed_at=? WHERE id=?",
                             (actor, now, orch["id"]))
                conn.execute("UPDATE missions SET status='open',version=version+1 WHERE id=?", (orch["mission_id"],))
            else:
                conn.execute("UPDATE missions SET version=version+1 WHERE id=?", (orch["mission_id"],))
            self._hist(conn, orch["mission_id"], orch["id"], actor, "sector.cancel",
                       before, {"reason": (reason or "").strip()} if reason else None)
            return self._orch_snapshot(conn, orch["id"])

    def close_mission(self, actor: str, mission_id: int) -> dict[str, Any]:
        actor = self._actor(actor)
        with self._tx() as conn:
            mission = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if not mission:
                raise StoreError("事件不存在", 404)
            if mission["status"] == "closed":
                raise StoreError("事件已关闭", 409)
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM orchestrations WHERE mission_id=? AND status='active'", (mission_id,)
            ).fetchone()["c"]
            if active:
                raise StoreError("仍有生效中的编排，请先取消", 409)
            conn.execute("UPDATE missions SET status='closed',version=version+1 WHERE id=?", (mission_id,))
            self._hist(conn, mission_id, None, actor, "mission.close",
                       {"code": mission["code"], "status": mission["status"]}, {"status": "closed"})
            return dict(conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    # ---------- 查询 ----------
    def sea_state(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='sea_state'").fetchone()
        if row:
            return json.loads(row["value"])
        return {"level": 3, "note": "初始值", "by": None, "updated_at": None}

    def get_mission(self, mission_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        return dict(row) if row else None

    def get_vessel(self, vessel_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM vessels WHERE id=?", (vessel_id,)).fetchone()
        return self._vessel(row)

    def get_orchestration(self, orchestration_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            orch = conn.execute("SELECT * FROM orchestrations WHERE id=?", (orchestration_id,)).fetchone()
            if not orch:
                return None
            data = dict(orch)
            data["sectors"] = [dict(r) for r in conn.execute(
                "SELECT * FROM sectors WHERE orchestration_id=? ORDER BY seq", (orchestration_id,)).fetchall()]
        return data

    def plan_input(self, mission_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        mission = self.get_mission(mission_id)
        if not mission:
            raise StoreError("事件不存在", 404)
        with self.connect() as conn:
            vessels = [self._vessel(r) for r in conn.execute("SELECT * FROM vessels ORDER BY id").fetchall()]
        return mission, vessels, int(self.sea_state()["level"])

    def state(self) -> dict[str, Any]:
        with self.connect() as conn:
            missions = [dict(r) for r in conn.execute("SELECT * FROM missions ORDER BY id DESC").fetchall()]
            vessels = [self._vessel(r) for r in conn.execute("SELECT * FROM vessels ORDER BY id").fetchall()]
            orchestrations = [dict(r) for r in conn.execute("SELECT * FROM orchestrations ORDER BY id DESC").fetchall()]
            sectors = [dict(r) for r in conn.execute("SELECT * FROM sectors ORDER BY id").fetchall()]
            history = [self._hist_row(r) for r in conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT 200").fetchall()]
        return {"sea_state": self.sea_state(), "missions": missions, "vessels": vessels,
                "orchestrations": orchestrations, "sectors": sectors, "history": history}

    # ---------- 演示数据 ----------
    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM missions").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        actor = "system"
        self.set_sea_state(actor, 3, "实况：东南风 5 级，轻浪")
        self.add_vessel(actor, "海救101", ["医疗", "潜水"], 31.05, 122.10, 22, 600, 6)
        self.add_vessel(actor, "海救102", ["医疗"], 31.60, 122.40, 18, 450, 5)
        self.add_vessel(actor, "东海救115", ["潜水", "拖带"], 30.80, 122.60, 20, 800, 7)
        self.add_vessel(actor, "华英388", ["医疗"], 31.30, 121.95, 15, 120, 4)
        deadline = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
        mission = self.create_mission(actor, "SAR-2026-0925", "浙岱渔运88 失联", 31.30, 122.30,
                                      10.0, 5, deadline, "", "渔船失联，船上 6 人")
        return {"seeded": True, "mission_id": mission["id"]}

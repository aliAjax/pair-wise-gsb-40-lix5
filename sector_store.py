"""扇区编排存储层：SQLite 持久化、并发控制与审计留痕。

规则计算在 sector_rules，页面在 static/sector_console.html，本模块只负责
事务、唯一性约束和前后指派记录。两名协调员同时发布同一事件的编排时，
由 BEGIN IMMEDIATE 与 active 部分唯一索引保证只保留先到的一次。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import sector_rules
from sector_rules import RuleError

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "sector_console.db"


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return sector_rules.iso_now()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise StoreError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise StoreError("角色无权执行：%s" % action, 403)


def _checked(callable_, *args: Any) -> Any:
    """把规则层的 RuleError 转成带 400 状态的 StoreError。"""
    try:
        return callable_(*args)
    except RuleError as exc:
        raise StoreError(str(exc), 400) from exc


class SectorStore:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sar_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    priority INTEGER NOT NULL,
                    deadline TEXT NOT NULL,
                    sea_state INTEGER NOT NULL,
                    required_capability TEXT NOT NULL DEFAULT 'surface',
                    status TEXT NOT NULL DEFAULT 'open',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sar_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    speed_kn REAL NOT NULL,
                    range_km REAL NOT NULL,
                    max_sea_state INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sector_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES sar_incidents(id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan
                    ON sector_plans(incident_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS sectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES sector_plans(id),
                    incident_id INTEGER NOT NULL REFERENCES sar_incidents(id),
                    code TEXT NOT NULL,
                    bearing_start REAL NOT NULL,
                    bearing_end REAL NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    area_km2 REAL NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL,
                    deadline TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    resource_id INTEGER REFERENCES sar_resources(id),
                    eta_min REAL,
                    search_min REAL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS assignment_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_id INTEGER NOT NULL REFERENCES sectors(id),
                    incident_id INTEGER NOT NULL REFERENCES sar_incidents(id),
                    plan_id INTEGER NOT NULL REFERENCES sector_plans(id),
                    action TEXT NOT NULL,
                    prev_resource_id INTEGER REFERENCES sar_resources(id),
                    new_resource_id INTEGER REFERENCES sar_resources(id),
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER REFERENCES sar_incidents(id),
                    plan_id INTEGER REFERENCES sector_plans(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sectors_plan ON sectors(plan_id, code);
                CREATE INDEX IF NOT EXISTS idx_assignment_incident ON assignment_log(incident_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_incident ON plan_events(incident_id, id);
                """
            )

    # ---- 内部工具 -----------------------------------------------------

    def _event(self, conn: sqlite3.Connection, incident_id: int | None, plan_id: int | None,
               actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO plan_events(incident_id,plan_id,actor,action,details,created_at) VALUES(?,?,?,?,?,?)",
            (incident_id, plan_id, actor, action, json_dump(details), utcnow()),
        )

    def _log_assignment(self, conn: sqlite3.Connection, sector_id: int, incident_id: int,
                        plan_id: int, action: str, prev_resource_id: int | None,
                        new_resource_id: int | None, actor: str, reason: str = "") -> None:
        conn.execute(
            """INSERT INTO assignment_log(sector_id,incident_id,plan_id,action,prev_resource_id,
               new_resource_id,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (sector_id, incident_id, plan_id, action, prev_resource_id, new_resource_id,
             actor, reason, utcnow()),
        )

    def _incident_row(self, conn: sqlite3.Connection, incident_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sar_incidents WHERE id=?", (incident_id,)).fetchone()
        if not row:
            raise StoreError("事件不存在", 404)
        return row

    def _resource_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["capabilities"] = json.loads(data["capabilities"])
        return data

    def _available_resources(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM sar_resources WHERE status='available' ORDER BY id").fetchall()
        return [self._resource_dict(row) for row in rows]

    def _release_resource(self, conn: sqlite3.Connection, resource_id: int, now: str) -> None:
        conn.execute(
            "UPDATE sar_resources SET status='available',version=version+1,updated_at=? WHERE id=?",
            (now, resource_id),
        )

    # ---- 登记 ---------------------------------------------------------

    def register_incident(self, actor: str, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记事件")
        data = _checked(sector_rules.validate_incident, payload)
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """INSERT INTO sar_incidents(code,title,center_lat,center_lon,radius_km,priority,
                       deadline,sea_state,required_capability,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["code"], data["title"], data["center_lat"], data["center_lon"],
                     data["radius_km"], data["priority"], data["deadline"], data["sea_state"],
                     data["required_capability"], actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("事件编号已存在", 409) from exc
            incident_id = int(cur.lastrowid)
            self._event(conn, incident_id, None, actor, "incident.registered",
                        {"code": data["code"], "title": data["title"]})
            return dict(conn.execute("SELECT * FROM sar_incidents WHERE id=?", (incident_id,)).fetchone())

    def update_incident(self, actor: str, role: str, incident_id: int, expected_version: int,
                        sea_state: int | None = None, deadline: str | None = None,
                        priority: int | None = None) -> dict[str, Any]:
        """海况或时限变化时更新事件，携带版本号防止覆盖他人修改。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "更新事件")
        changes: dict[str, Any] = {}
        if sea_state is not None:
            changes["sea_state"] = _checked(sector_rules._sea_state, sea_state)
        if deadline is not None:
            moment = _checked(sector_rules.parse_moment, deadline)
            changes["deadline"] = moment.isoformat(timespec="seconds")
        if priority is not None:
            try:
                priority = int(priority)
            except (TypeError, ValueError) as exc:
                raise StoreError("优先级必须是 1 到 5 的整数") from exc
            if not 1 <= priority <= 5:
                raise StoreError("优先级必须是 1 到 5 的整数")
            changes["priority"] = priority
        if not changes:
            raise StoreError("没有需要更新的字段")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = self._incident_row(conn, incident_id)
            if incident["version"] != int(expected_version):
                raise StoreError("事件已变化，请刷新后重试", 409)
            sets = ",".join("%s=?" % key for key in changes)
            conn.execute(
                "UPDATE sar_incidents SET %s,version=version+1 WHERE id=?" % sets,
                (*changes.values(), incident_id),
            )
            self._event(conn, incident_id, None, actor, "incident.updated",
                        {"code": incident["code"], "changes": changes})
            return dict(conn.execute("SELECT * FROM sar_incidents WHERE id=?", (incident_id,)).fetchone())

    def register_resource(self, actor: str, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记资源")
        data = _checked(sector_rules.validate_resource, payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """INSERT INTO sar_resources(name,kind,capabilities,latitude,longitude,speed_kn,
                       range_km,max_sea_state,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (data["name"], data["kind"], json_dump(data["capabilities"]), data["latitude"],
                     data["longitude"], data["speed_kn"], data["range_km"], data["max_sea_state"], utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("资源名称已存在", 409) from exc
            self._event(conn, None, None, actor, "resource.registered",
                        {"resource_id": cur.lastrowid, "name": data["name"]})
            return self._resource_dict(
                conn.execute("SELECT * FROM sar_resources WHERE id=?", (cur.lastrowid,)).fetchone()
            )

    def update_resource(self, actor: str, role: str, resource_id: int, expected_version: int,
                        latitude: float | None = None, longitude: float | None = None,
                        range_km: float | None = None, speed_kn: float | None = None,
                        max_sea_state: int | None = None, status: str | None = None) -> dict[str, Any]:
        """更新船位、剩余航程等动态信息，替代电话重问船位。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "更新资源")
        changes: dict[str, Any] = {}
        if latitude is not None or longitude is not None:
            lat, lon = _checked(sector_rules.validate_position, latitude, longitude)
            changes["latitude"], changes["longitude"] = lat, lon
        if range_km is not None:
            range_km = _checked(sector_rules._number, range_km, "剩余航程")
            if range_km <= 0:
                raise StoreError("剩余航程必须大于 0")
            changes["range_km"] = range_km
        if speed_kn is not None:
            speed_kn = _checked(sector_rules._number, speed_kn, "航速")
            if speed_kn <= 0:
                raise StoreError("航速必须大于 0")
            changes["speed_kn"] = speed_kn
        if max_sea_state is not None:
            changes["max_sea_state"] = _checked(sector_rules._sea_state, max_sea_state, "适用海况")
        if status is not None:
            if status not in {"available", "offline"}:
                raise StoreError("资源状态只能是 available 或 offline")
            changes["status"] = status
        if not changes:
            raise StoreError("没有需要更新的字段")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            resource = conn.execute("SELECT * FROM sar_resources WHERE id=?", (resource_id,)).fetchone()
            if not resource:
                raise StoreError("资源不存在", 404)
            if resource["version"] != int(expected_version):
                raise StoreError("资源状态已变化，请刷新后重试", 409)
            if resource["status"] == "assigned" and changes.get("status"):
                raise StoreError("资源正在执行任务，请先改派或取消扇区", 409)
            sets = ",".join("%s=?" % key for key in changes)
            conn.execute(
                "UPDATE sar_resources SET %s,version=version+1,updated_at=? WHERE id=?" % sets,
                (*changes.values(), utcnow(), resource_id),
            )
            self._event(conn, None, None, actor, "resource.updated",
                        {"resource_id": resource_id, "name": resource["name"], "changes": changes})
            return self._resource_dict(
                conn.execute("SELECT * FROM sar_resources WHERE id=?", (resource_id,)).fetchone()
            )

    # ---- 编排 ---------------------------------------------------------

    def preview_plan(self, actor: str, role: str, incident_id: int) -> dict[str, Any]:
        clean_actor(actor)
        require_role(role, {"coordinator", "viewer"}, "预览编排")
        with self.connect() as conn:
            incident = self._incident_row(conn, incident_id)
            if incident["status"] != "open":
                raise StoreError("事件已关闭，不能编排", 409)
            resources = self._available_resources(conn)
        return _checked(sector_rules.plan_sectors, dict(incident), resources)

    def publish_plan(self, actor: str, role: str, incident_id: int,
                     expected_revision: int | None = None, note: str = "") -> dict[str, Any]:
        """发布扇区编排。

        同一事件同一时刻只允许一个生效编排：不带 expected_revision 的发布
        在已有生效编排时被拒（先到者保留）；带 expected_revision 表示重排，
        版本不符同样被拒。整个发布在单个 IMMEDIATE 事务内完成。
        """
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "发布编排")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = self._incident_row(conn, incident_id)
            if incident["status"] != "open":
                raise StoreError("事件已关闭，不能编排", 409)
            active = conn.execute(
                "SELECT * FROM sector_plans WHERE incident_id=? AND status='active'", (incident_id,)
            ).fetchone()
            if active and expected_revision is None:
                raise StoreError(
                    "已有生效编排（第 %d 版，%s 发布）；如需重排请携带 expected_revision"
                    % (active["revision"], active["published_by"]), 409)
            if active and int(expected_revision or 0) != active["revision"]:
                raise StoreError("编排版本已变化，请刷新后重试", 409)
            if active:
                old_sectors = conn.execute(
                    "SELECT * FROM sectors WHERE plan_id=? AND resource_id IS NOT NULL", (active["id"],)
                ).fetchall()
                for sector in old_sectors:
                    self._release_resource(conn, sector["resource_id"], now)
                    self._log_assignment(conn, sector["id"], incident_id, active["id"], "release",
                                         sector["resource_id"], None, actor,
                                         "被新编排取代")
                conn.execute("UPDATE sectors SET status='released',resource_id=NULL,version=version+1 WHERE plan_id=?",
                             (active["id"],))
                conn.execute("UPDATE sector_plans SET status='superseded' WHERE id=?", (active["id"],))
                self._event(conn, incident_id, active["id"], actor, "plan.superseded",
                            {"revision": active["revision"]})
            resources = self._available_resources(conn)
            plan = _checked(sector_rules.plan_sectors, dict(incident), resources)
            revision = conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 AS r FROM sector_plans WHERE incident_id=?",
                (incident_id,),
            ).fetchone()["r"]
            cur = conn.execute(
                "INSERT INTO sector_plans(incident_id,revision,status,published_by,published_at,note) VALUES(?,?,?,?,?,?)",
                (incident_id, revision, "active", actor, now, note.strip()),
            )
            plan_id = int(cur.lastrowid)
            sector_rows = []
            for sector in plan["sectors"]:
                assignment = sector["assignment"] or {}
                resource_id = assignment.get("resource_id")
                cur_s = conn.execute(
                    """INSERT INTO sectors(plan_id,incident_id,code,bearing_start,bearing_end,center_lat,
                       center_lon,radius_km,area_km2,priority,deadline,status,resource_id,eta_min,search_min)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan_id, incident_id, sector["code"], sector["bearing_start"], sector["bearing_end"],
                     sector["center_lat"], sector["center_lon"], sector["radius_km"], sector["area_km2"],
                     sector["priority"], sector["deadline"], "assigned" if resource_id else "planned",
                     resource_id, assignment.get("eta_min"), assignment.get("search_min")),
                )
                sector_id = int(cur_s.lastrowid)
                if resource_id:
                    changed = conn.execute(
                        "UPDATE sar_resources SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available'",
                        (now, resource_id),
                    )
                    if changed.rowcount != 1:
                        raise StoreError("资源 %s 已被占用" % assignment["resource_name"], 409)
                    self._log_assignment(conn, sector_id, incident_id, plan_id, "assign",
                                         None, resource_id, actor)
                sector_rows.append(dict(conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone()))
            self._event(conn, incident_id, plan_id, actor, "plan.published",
                        {"revision": revision, "sectors": len(sector_rows), "note": note.strip()})
            return {"plan": dict(conn.execute("SELECT * FROM sector_plans WHERE id=?", (plan_id,)).fetchone()),
                    "sectors": sector_rows}

    def cancel_plan(self, actor: str, role: str, plan_id: int, reason: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "取消编排")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plan = conn.execute("SELECT * FROM sector_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise StoreError("编排不存在", 404)
            if plan["status"] != "active":
                raise StoreError("编排已失效，不能取消", 409)
            sectors = conn.execute(
                "SELECT * FROM sectors WHERE plan_id=? AND resource_id IS NOT NULL", (plan_id,)
            ).fetchall()
            for sector in sectors:
                self._release_resource(conn, sector["resource_id"], now)
                self._log_assignment(conn, sector["id"], plan["incident_id"], plan_id, "release",
                                     sector["resource_id"], None, actor, reason.strip() or "编排取消")
            conn.execute("UPDATE sectors SET status='released',resource_id=NULL,version=version+1 WHERE plan_id=?",
                         (plan_id,))
            conn.execute("UPDATE sector_plans SET status='cancelled' WHERE id=?", (plan_id,))
            self._event(conn, plan["incident_id"], plan_id, actor, "plan.cancelled",
                        {"revision": plan["revision"], "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM sector_plans WHERE id=?", (plan_id,)).fetchone())

    # ---- 扇区改派与取消 -----------------------------------------------

    def reassign_sector(self, actor: str, role: str, sector_id: int, new_resource_id: int,
                        reason: str = "") -> dict[str, Any]:
        """改派（或补派）扇区：释放原资源、占用新资源，并留下前后指派记录。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "改派扇区")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sector = conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone()
            if not sector:
                raise StoreError("扇区不存在", 404)
            plan = conn.execute("SELECT * FROM sector_plans WHERE id=?", (sector["plan_id"],)).fetchone()
            if not plan or plan["status"] != "active":
                raise StoreError("扇区所属编排已失效", 409)
            if sector["status"] not in {"planned", "assigned"}:
                raise StoreError("扇区已释放，不能改派", 409)
            new_resource = conn.execute("SELECT * FROM sar_resources WHERE id=?", (new_resource_id,)).fetchone()
            if not new_resource:
                raise StoreError("资源不存在", 404)
            if sector["resource_id"] == new_resource["id"]:
                raise StoreError("改派资源与原资源相同")
            if new_resource["status"] != "available":
                raise StoreError("资源当前不可用", 409)
            incident = self._incident_row(conn, sector["incident_id"])
            evaluation = sector_rules.evaluate_resource(
                self._resource_dict(new_resource), dict(incident), dict(sector))
            if not evaluation["eligible"]:
                raise StoreError("资源不满足扇区条件：%s" % "、".join(evaluation["reasons"]), 409)
            prev_id = sector["resource_id"]
            if prev_id:
                self._release_resource(conn, prev_id, now)
            changed = conn.execute(
                "UPDATE sar_resources SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available'",
                (now, new_resource_id),
            )
            if changed.rowcount != 1:
                raise StoreError("资源已被其他扇区占用", 409)
            conn.execute(
                "UPDATE sectors SET resource_id=?,status='assigned',eta_min=?,search_min=?,version=version+1 WHERE id=?",
                (new_resource_id, evaluation["eta_min"], evaluation["search_min"], sector_id),
            )
            self._log_assignment(conn, sector_id, sector["incident_id"], sector["plan_id"],
                                 "reassign" if prev_id else "assign", prev_id, new_resource_id,
                                 actor, reason.strip())
            return dict(conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone())

    def cancel_sector(self, actor: str, role: str, sector_id: int, reason: str) -> dict[str, Any]:
        """取消扇区：释放原资源并留痕。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "取消扇区")
        if not reason.strip():
            raise StoreError("取消原因不能为空")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sector = conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone()
            if not sector:
                raise StoreError("扇区不存在", 404)
            plan = conn.execute("SELECT * FROM sector_plans WHERE id=?", (sector["plan_id"],)).fetchone()
            if not plan or plan["status"] != "active":
                raise StoreError("扇区所属编排已失效", 409)
            if sector["status"] not in {"planned", "assigned"}:
                raise StoreError("扇区已释放", 409)
            prev_id = sector["resource_id"]
            if prev_id:
                self._release_resource(conn, prev_id, now)
            conn.execute(
                "UPDATE sectors SET status='released',resource_id=NULL,version=version+1 WHERE id=?",
                (sector_id,),
            )
            self._log_assignment(conn, sector_id, sector["incident_id"], sector["plan_id"],
                                 "cancel", prev_id, None, actor, reason.strip())
            return dict(conn.execute("SELECT * FROM sectors WHERE id=?", (sector_id,)).fetchone())

    # ---- 查询 ---------------------------------------------------------

    def _history(self, conn: sqlite3.Connection, incident_id: int | None = None,
                 limit: int = 200) -> list[dict[str, Any]]:
        where, params = "", ()
        if incident_id is not None:
            where, params = "WHERE incident_id=?", (incident_id,)
        events = conn.execute(
            "SELECT * FROM plan_events %s ORDER BY id DESC LIMIT ?" % where, (*params, limit)
        ).fetchall()
        logs = conn.execute(
            """SELECT al.*, s.code AS sector_code, rp.name AS prev_name, rn.name AS new_name
               FROM assignment_log al
               JOIN sectors s ON s.id=al.sector_id
               LEFT JOIN sar_resources rp ON rp.id=al.prev_resource_id
               LEFT JOIN sar_resources rn ON rn.id=al.new_resource_id
               %s ORDER BY al.id DESC LIMIT ?""" % where.replace("incident_id", "al.incident_id"),
            (*params, limit),
        ).fetchall()
        items = []
        for event in events:
            details = json.loads(event["details"])
            items.append({
                "id": "e%d" % event["id"], "at": event["created_at"], "actor": event["actor"],
                "action": event["action"], "incident_id": event["incident_id"],
                "summary": self._summarize_event(event["action"], details), "details": details,
            })
        for log in logs:
            items.append({
                "id": "a%d" % log["id"], "at": log["created_at"], "actor": log["actor"],
                "action": "sector." + log["action"], "incident_id": log["incident_id"],
                "summary": self._summarize_assignment(log), "details": {
                    "sector": log["sector_code"], "prev_resource": log["prev_name"],
                    "new_resource": log["new_name"], "reason": log["reason"],
                },
            })
        items.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
        return items[:limit]

    @staticmethod
    def _summarize_event(action: str, details: dict[str, Any]) -> str:
        if action == "incident.registered":
            return "登记事件 %s（%s）" % (details.get("code"), details.get("title"))
        if action == "incident.updated":
            return "更新事件 %s：%s" % (details.get("code"), json_dump(details.get("changes", {})))
        if action == "resource.registered":
            return "登记资源 %s" % details.get("name")
        if action == "resource.updated":
            return "更新资源 %s：%s" % (details.get("name"), json_dump(details.get("changes", {})))
        if action == "plan.published":
            return "发布第 %d 版编排（%d 个扇区）" % (details.get("revision", 0), details.get("sectors", 0))
        if action == "plan.superseded":
            return "第 %d 版编排被取代" % details.get("revision", 0)
        if action == "plan.cancelled":
            return "取消第 %d 版编排（%s）" % (details.get("revision", 0), details.get("reason") or "未填原因")
        return action

    @staticmethod
    def _summarize_assignment(log: sqlite3.Row) -> str:
        action = log["action"]
        if action == "assign":
            return "%s 指派 %s" % (log["sector_code"], log["new_name"])
        if action == "reassign":
            return "%s 改派 %s → %s" % (log["sector_code"], log["prev_name"], log["new_name"])
        if action == "release":
            return "%s 释放 %s（%s）" % (log["sector_code"], log["prev_name"], log["reason"] or "无说明")
        if action == "cancel":
            prev = "，原资源 %s 已释放" % log["prev_name"] if log["prev_name"] else ""
            return "%s 取消%s（%s）" % (log["sector_code"], prev, log["reason"])
        return "%s %s" % (log["sector_code"], action)

    def state(self) -> dict[str, Any]:
        with self.connect() as conn:
            incidents = [dict(r) for r in conn.execute(
                "SELECT * FROM sar_incidents ORDER BY id DESC").fetchall()]
            resources = [self._resource_dict(r) for r in conn.execute(
                "SELECT * FROM sar_resources ORDER BY id").fetchall()]
            plans = [dict(r) for r in conn.execute(
                "SELECT * FROM sector_plans ORDER BY id DESC LIMIT 50").fetchall()]
            active_ids = [p["id"] for p in plans if p["status"] == "active"]
            sectors = []
            if active_ids:
                marks = ",".join("?" * len(active_ids))
                sectors = [dict(r) for r in conn.execute(
                    """SELECT s.*, p.revision AS plan_revision, r.name AS resource_name
                       FROM sectors s
                       JOIN sector_plans p ON p.id=s.plan_id
                       LEFT JOIN sar_resources r ON r.id=s.resource_id
                       WHERE s.plan_id IN (%s) ORDER BY p.id, s.code""" % marks,
                    active_ids).fetchall()]
            history = self._history(conn)
        return {"incidents": incidents, "resources": resources, "plans": plans,
                "sectors": sectors, "history": history}

    def incident_history(self, incident_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            self._incident_row(conn, incident_id)
            return self._history(conn, incident_id)

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM sar_incidents").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        from datetime import timedelta
        deadline = (sector_rules.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
        incident = self.register_incident("coord-demo", "coordinator", {
            "code": "SAR-2026-041", "title": "远星号失联", "center_lat": 31.2, "center_lon": 122.5,
            "radius_km": 20, "priority": 1, "deadline": deadline, "sea_state": 4,
            "required_capability": "surface",
        })
        self.register_resource("coord-demo", "coordinator", {
            "name": "海巡01", "kind": "vessel", "capabilities": ["surface", "night"],
            "latitude": 31.0, "longitude": 122.0, "speed_kn": 22, "range_km": 400, "max_sea_state": 6})
        self.register_resource("coord-demo", "coordinator", {
            "name": "海巡02", "kind": "vessel", "capabilities": ["surface"],
            "latitude": 31.5, "longitude": 122.9, "speed_kn": 20, "range_km": 350, "max_sea_state": 5})
        self.register_resource("coord-demo", "coordinator", {
            "name": "救助直升机", "kind": "aircraft", "capabilities": ["air", "surface"],
            "latitude": 30.9, "longitude": 122.2, "speed_kn": 150, "range_km": 600, "max_sea_state": 5})
        plan = self.publish_plan("coord-demo", "coordinator", incident["id"], note="首次编排")
        return {"seeded": True, "incident_id": incident["id"], "plan_id": plan["plan"]["id"]}

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sector_rules as rules  # noqa: E402
import sector_server  # noqa: E402
from sector_store import SectorStore, StoreError  # noqa: E402

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def make_mission(**kw):
    mission = {"id": 1, "code": "M-1", "name": "测试事件", "center_lat": 31.3, "center_lon": 122.3,
               "radius_km": 15.0, "priority": 3, "deadline": (NOW + timedelta(hours=6)).isoformat(),
               "required_capability": "", "status": "open", "version": 1}
    mission.update(kw)
    return mission


def make_vessel(vid, name, **kw):
    vessel = {"id": vid, "name": name, "capabilities": ["医疗"], "latitude": 31.3, "longitude": 122.3,
              "speed_kn": 20.0, "endurance_km": 800.0, "max_sea_state": 6, "status": "available"}
    vessel.update(kw)
    return vessel


class RulesTest(unittest.TestCase):
    def test_sea_state_degrades_speed_and_sweep(self):
        self.assertAlmostEqual(1.0, rules.speed_factor(1))
        self.assertLess(rules.speed_factor(6), rules.speed_factor(3))
        self.assertGreaterEqual(rules.speed_factor(9), 0.5)
        self.assertLess(rules.sweep_width_km(7), rules.sweep_width_km(2))
        self.assertGreaterEqual(rules.sweep_width_km(9), 0.5)

    def test_capability_and_sea_state_rejection(self):
        mission = make_mission(required_capability="潜水")
        no_cap = rules.evaluate_vessel(make_vessel(1, "甲"), mission, 3, 120.0, NOW)
        self.assertFalse(no_cap["ok"])
        self.assertTrue(any("能力" in r for r in no_cap["reasons"]))
        weak = make_vessel(2, "乙", capabilities=["潜水"], max_sea_state=2)
        sea = rules.evaluate_vessel(weak, mission, 5, 120.0, NOW)
        self.assertFalse(sea["ok"])
        self.assertTrue(any("海况" in r for r in sea["reasons"]))

    def test_endurance_rejection(self):
        mission = make_mission()
        tired = make_vessel(1, "短腿", endurance_km=10.0)
        ev = rules.evaluate_vessel(tired, mission, 3, 180.0, NOW)
        self.assertFalse(ev["ok"])
        self.assertTrue(any("剩余航程" in r for r in ev["reasons"]))

    def test_plan_splits_360_and_picks_earliest_eta(self):
        mission = make_mission(radius_km=8.0)
        vessels = [
            make_vessel(1, "远船", latitude=31.9, longitude=122.3),
            make_vessel(2, "近船", latitude=31.32, longitude=122.32),
            make_vessel(3, "中船", latitude=31.5, longitude=122.5),
        ]
        plan = rules.plan_sectors(mission, vessels, 3, NOW)
        self.assertTrue(plan["sectors"])
        total = sum(s["end_deg"] - s["start_deg"] for s in plan["sectors"])
        self.assertAlmostEqual(360.0, total)
        self.assertEqual("近船", plan["sectors"][0]["vessel_name"] if len(plan["sectors"]) == 1 else plan["sectors"][0]["vessel_name"])
        # 最近船 ETA 应最小
        etas = {s["vessel_name"]: s["eta_min"] for s in plan["sectors"]}
        self.assertLessEqual(etas.get("近船", 0), etas.get("远船", 10**9))

    def test_plan_uses_minimal_vessels_for_deadline(self):
        mission = make_mission(radius_km=5.0, deadline=(NOW + timedelta(hours=12)).isoformat())
        vessels = [make_vessel(i, "船%d" % i, longitude=122.3 + i * 0.05) for i in range(1, 5)]
        plan = rules.plan_sectors(mission, vessels, 2, NOW)
        self.assertTrue(plan["feasible"])
        self.assertEqual(1, len(plan["sectors"]))
        self.assertAlmostEqual(360.0, plan["sector_deg"])

    def test_deadline_risk_when_impossible(self):
        mission = make_mission(radius_km=30.0, deadline=(NOW + timedelta(minutes=5)).isoformat())
        vessels = [make_vessel(1, "甲"), make_vessel(2, "乙", latitude=31.5)]
        plan = rules.plan_sectors(mission, vessels, 5, NOW)
        self.assertFalse(plan["feasible"])
        self.assertTrue(plan["deadline_risk"])
        self.assertEqual(2, len(plan["sectors"]))

    def test_high_priority_adds_vessel(self):
        mission = make_mission(radius_km=5.0, priority=5,
                               deadline=(NOW + timedelta(hours=12)).isoformat())
        vessels = [make_vessel(i, "船%d" % i, longitude=122.3 + i * 0.05) for i in range(1, 4)]
        plan = rules.plan_sectors(mission, vessels, 2, NOW)
        self.assertTrue(plan["feasible"])
        self.assertEqual(2, len(plan["sectors"]))
        self.assertAlmostEqual(180.0, plan["sector_deg"])

    def test_occupied_vessel_excluded(self):
        mission = make_mission()
        vessels = [make_vessel(1, "忙船", status="assigned"), make_vessel(2, "闲船")]
        plan = rules.plan_sectors(mission, vessels, 3, NOW)
        names = [s["vessel_name"] for s in plan["sectors"]]
        self.assertNotIn("忙船", names)
        self.assertTrue(any(r["vessel_name"] == "忙船" for r in plan["rejected"]))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SectorStore(Path(self.tmp.name) / "t.db")
        self.store.set_sea_state("t", 3)
        self.mission = self.store.create_mission(
            "coord", "SAR-T1", "测试失联船", 31.3, 122.3, 8.0, 4,
            (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat())
        self.v1 = self.store.add_vessel("coord", "海救01", ["医疗"], 31.32, 122.32, 20, 600, 6)
        self.v2 = self.store.add_vessel("coord", "海救02", ["医疗"], 31.5, 122.5, 18, 600, 5)
        self.v3 = self.store.add_vessel("coord", "海救03", ["潜水"], 31.1, 122.1, 22, 600, 6)

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        mission, vessels, sea = self.store.plan_input(self.mission["id"])
        return rules.plan_sectors(mission, vessels, sea)

    def test_publish_first_come_first_served(self):
        plan = self._plan()
        orch = self.store.publish("协调员甲", self.mission["id"], self.mission["version"], plan)
        self.assertEqual("active", orch["status"])
        with self.assertRaises(StoreError) as ctx:
            self.store.publish("协调员乙", self.mission["id"], self.mission["version"], plan)
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("丢弃", str(ctx.exception))
        active = [o for o in self.store.state()["orchestrations"] if o["status"] == "active"]
        self.assertEqual(1, len(active))

    def test_publish_marks_vessels_assigned(self):
        self.store.publish("coord", self.mission["id"], 1, self._plan())
        vessels = {v["name"]: v for v in self.store.state()["vessels"]}
        assigned = [v["name"] for v in vessels.values() if v["status"] == "assigned"]
        self.assertTrue(assigned)
        self.assertEqual("covered", self.store.get_mission(self.mission["id"])["status"])

    def test_republish_supersedes_and_releases(self):
        self.store.publish("coord", self.mission["id"], 1, self._plan())
        mid = self.mission["id"]
        v_before = self.store.get_mission(mid)["version"]
        plan2 = self._plan()  # 此时只剩未占用船只
        self.store.publish("coord2", mid, v_before, plan2)
        state = self.store.state()
        active = [o for o in state["orchestrations"] if o["status"] == "active"]
        superseded = [o for o in state["orchestrations"] if o["status"] == "superseded"]
        self.assertEqual(1, len(active))
        self.assertEqual(1, len(superseded))
        repub = [h for h in state["history"] if h["action"] == "republish"]
        self.assertTrue(repub)
        self.assertTrue(repub[0]["before"]["sectors"])
        self.assertTrue(repub[0]["after"]["sectors"])

    def test_reassign_releases_old_and_records_history(self):
        orch = self.store.publish("coord", self.mission["id"], 1, self._plan())
        sector = orch["sectors"][0]
        old_vessel = sector["vessel_id"]
        free = next(v for v in self.store.state()["vessels"]
                    if v["status"] == "available" and v["id"] != old_vessel)
        mission = self.store.get_mission(self.mission["id"])
        metrics = rules.evaluate_vessel(free, mission, 3, sector["end_deg"] - sector["start_deg"])
        self.store.reassign("coord2", orch["id"], sector["id"], free["id"], metrics, "原船机器故障")
        vessels = {v["id"]: v for v in self.store.state()["vessels"]}
        self.assertEqual("available", vessels[old_vessel]["status"])
        self.assertEqual("assigned", vessels[free["id"]]["status"])
        hist = [h for h in self.store.state()["history"] if h["action"] == "reassign"]
        self.assertEqual(1, len(hist))
        self.assertEqual(old_vessel, hist[0]["before"]["vessel_id"])
        self.assertEqual(free["id"], hist[0]["after"]["vessel_id"])
        self.assertEqual("原船机器故障", hist[0]["after"]["reason"])

    def test_reassign_rejects_occupied_vessel(self):
        orch = self.store.publish("coord", self.mission["id"], 1, self._plan())
        sector = orch["sectors"][0]
        busy = next(s["vessel_id"] for s in orch["sectors"] if s["id"] != sector["id"]) \
            if len(orch["sectors"]) > 1 else sector["vessel_id"]
        with self.assertRaises(StoreError) as ctx:
            self.store.reassign("coord", orch["id"], sector["id"], busy,
                                {"distance_km": 1, "eta_min": 1, "search_min": 1,
                                 "completion_at": "2026-09-25T10:00:00+00:00"})
        self.assertEqual(409, ctx.exception.status)

    def test_cancel_orchestration_releases_all(self):
        orch = self.store.publish("coord", self.mission["id"], 1, self._plan())
        self.store.cancel_orchestration("coord", orch["id"], "海况转好，改空中搜索")
        state = self.store.state()
        self.assertTrue(all(v["status"] == "available" for v in state["vessels"]))
        self.assertEqual("cancelled", [o for o in state["orchestrations"] if o["id"] == orch["id"]][0]["status"])
        self.assertEqual("open", self.store.get_mission(self.mission["id"])["status"])
        hist = [h for h in state["history"] if h["action"] == "cancel"]
        self.assertTrue(hist[0]["before"]["sectors"])

    def test_cancel_sector_releases_and_closes_when_empty(self):
        orch = self.store.publish("coord", self.mission["id"], 1, self._plan())
        for sec in list(self.store.get_orchestration(orch["id"])["sectors"]):
            self.store.cancel_sector("coord", sec["id"])
        state = self.store.state()
        self.assertEqual("cancelled", [o for o in state["orchestrations"] if o["id"] == orch["id"]][0]["status"])
        self.assertTrue(all(v["status"] == "available" for v in state["vessels"]))

    def test_persistence_across_reopen(self):
        self.store.publish("coord", self.mission["id"], 1, self._plan())
        self.store.report_vessel("coord", self.v1["id"], 31.4, 122.4, 500)
        reopened = SectorStore(Path(self.tmp.name) / "t.db")
        state = reopened.state()
        self.assertEqual(1, len([o for o in state["orchestrations"] if o["status"] == "active"]))
        self.assertTrue(state["history"])
        v1 = [v for v in state["vessels"] if v["id"] == self.v1["id"]][0]
        self.assertAlmostEqual(31.4, v1["latitude"])

    def test_close_mission_blocked_while_active(self):
        self.store.publish("coord", self.mission["id"], 1, self._plan())
        with self.assertRaises(StoreError) as ctx:
            self.store.close_mission("coord", self.mission["id"])
        self.assertEqual(409, ctx.exception.status)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = SectorStore(Path(cls.tmp.name) / "srv.db")
        cls.store.set_sea_state("t", 3)
        cls.mission = cls.store.create_mission(
            "coord", "SAR-S1", "并发测试船", 31.3, 122.3, 12.0, 4,
            (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat())
        cls.store.add_vessel("coord", "并发01", ["医疗"], 31.32, 122.32, 20, 600, 6)
        cls.store.add_vessel("coord", "并发02", ["医疗"], 31.5, 122.5, 18, 600, 5)
        sector_server.SectorHandler.store = cls.store
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sector_server.SectorHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _req(self, method, path, body=None, user="coord"):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if body is not None:
            body = dict(body, by=user)  # 中文操作人走 UTF-8 请求体
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_state_endpoint(self):
        status, data = self._req("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertTrue(data["missions"])
        self.assertEqual(3, data["sea_state"]["level"])

    def test_plan_endpoint(self):
        mission = self.store.create_mission(
            "coord", "SAR-S2", "预演测试船", 31.3, 122.3, 10.0, 3,
            (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat())
        self.store.add_vessel("coord", "预演01", ["医疗"], 31.32, 122.32, 20, 600, 6)
        status, plan = self._req("GET", "/api/missions/%d/plan" % mission["id"])
        self.assertEqual(200, status)
        self.assertTrue(plan["sectors"])
        self.assertAlmostEqual(360.0, sum(s["end_deg"] - s["start_deg"] for s in plan["sectors"]))

    def test_concurrent_publish_only_first_wins(self):
        mission = self.store.get_mission(self.mission["id"])
        results = []

        def publish(name):
            results.append(self._req("POST", "/api/missions/%d/publish" % mission["id"],
                                     {"expected_version": mission["version"]}, user=name)[0])

        threads = [threading.Thread(target=publish, args=("协调员%s" % n,)) for n in "甲乙"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted([201, 409]), sorted(results))
        active = [o for o in self.store.state()["orchestrations"]
                  if o["mission_id"] == mission["id"] and o["status"] == "active"]
        self.assertEqual(1, len(active))

    def test_publish_requires_user(self):
        status, data = self._req("POST", "/api/missions/%d/publish" % self.mission["id"],
                                 {"expected_version": 99}, user="")
        self.assertEqual(400, status)
        self.assertIn("操作人", data["error"])


if __name__ == "__main__":
    unittest.main()

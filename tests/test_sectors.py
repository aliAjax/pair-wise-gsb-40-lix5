import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sector_rules  # noqa: E402
from sector_store import SectorStore, StoreError  # noqa: E402


def future(hours=6):
    return (sector_rules.utcnow() + timedelta(hours=hours)).isoformat(timespec="seconds")


INCIDENT = {
    "code": "SAR-T-001", "title": "测试失联", "center_lat": 31.0, "center_lon": 122.0,
    "radius_km": 15, "priority": 1, "deadline": future(8), "sea_state": 3,
    "required_capability": "surface",
}


def resource(name, lat, lon, speed=20, rng=400, sea=6, caps=("surface",)):
    return {"name": name, "kind": "vessel", "capabilities": list(caps), "latitude": lat,
            "longitude": lon, "speed_kn": speed, "range_km": rng, "max_sea_state": sea}


class SectorStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sector.db"
        self.store = SectorStore(self.db)
        self.incident = self.store.register_incident("coord1", "coordinator", dict(INCIDENT))
        self.r1 = self.store.register_resource("coord1", "coordinator", resource("海巡01", 31.0, 122.1))
        self.r2 = self.store.register_resource("coord1", "coordinator", resource("海巡02", 31.1, 121.9))

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, actor="coord1", **kw):
        return self.store.publish_plan(actor, "coordinator", self.incident["id"], **kw)

    def test_incident_registration_validation(self):
        bad_radius = dict(INCIDENT, code="SAR-T-002", radius_km=0)
        with self.assertRaises(StoreError) as ctx:
            self.store.register_incident("coord1", "coordinator", bad_radius)
        self.assertEqual(400, ctx.exception.status)
        past = dict(INCIDENT, code="SAR-T-003", deadline=future(-1))
        with self.assertRaises(StoreError):
            self.store.register_incident("coord1", "coordinator", past)
        with self.assertRaises(StoreError):
            self.store.register_incident("coord1", "coordinator", dict(INCIDENT, code="SAR-T-004", priority=9))
        with self.assertRaises(StoreError) as dup:
            self.store.register_incident("coord1", "coordinator", dict(INCIDENT))
        self.assertEqual(409, dup.exception.status)

    def test_rules_reject_unfit_resources(self):
        unfit = self.store.register_resource("coord1", "coordinator",
                                             resource("小艇", 31.0, 122.0, sea=2))
        preview = self.store.preview_plan("coord1", "coordinator", self.incident["id"])
        assigned = {s["assignment"]["resource_name"] for s in preview["sectors"] if s["assignment"]}
        self.assertNotIn("小艇", assigned)
        rejects = [s["rejects"] for s in preview["sectors"] if s["rejects"]]
        if rejects:
            self.assertIn("海况超限", "、".join(rejects[0]["小艇"]))
        self.assertEqual("available", unfit["status"])

    def test_publish_assigns_sectors_and_marks_resources(self):
        result = self.publish()
        self.assertEqual(1, result["plan"]["revision"])
        self.assertEqual(2, len(result["sectors"]))
        self.assertTrue(all(s["status"] == "assigned" for s in result["sectors"]))
        state = self.store.state()
        self.assertEqual({"assigned"}, {r["status"] for r in state["resources"]})
        self.assertEqual(2, len(state["sectors"]))

    def test_concurrent_publish_keeps_first_only(self):
        barrier = threading.Barrier(2)
        results = []

        def publish(actor):
            barrier.wait()
            try:
                self.publish(actor)
                results.append((actor, "ok"))
            except StoreError as exc:
                results.append((actor, exc.status))

        threads = [threading.Thread(target=publish, args=("coord-a",)),
                   threading.Thread(target=publish, args=("coord-b",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if r[1] == "ok"]
        conflicts = [r for r in results if r[1] == 409]
        self.assertEqual(1, len(oks), results)
        self.assertEqual(1, len(conflicts), results)
        active = [p for p in self.store.state()["plans"] if p["status"] == "active"]
        self.assertEqual(1, len(active))

    def test_replan_requires_current_revision_and_releases_resources(self):
        first = self.publish("coord1")
        with self.assertRaises(StoreError) as stale:
            self.publish("coord2", expected_revision=99)
        self.assertEqual(409, stale.exception.status)
        second = self.publish("coord2", expected_revision=first["plan"]["revision"], note="海况变化重排")
        self.assertEqual(2, second["plan"]["revision"])
        plans = self.store.state()["plans"]
        by_status = {p["status"]: p for p in plans}
        self.assertIn("superseded", by_status)
        self.assertIn("active", by_status)
        history = self.store.incident_history(self.incident["id"])
        self.assertTrue(any(h["action"] == "sector.release" for h in history))
        self.assertTrue(any("被新编排取代" in h["summary"] for h in history))

    def test_reassign_releases_previous_and_records_before_after(self):
        self.publish()
        sector = self.store.state()["sectors"][0]
        old_resource_id = sector["resource_id"]
        self.store.register_resource("coord1", "coordinator", resource("海巡03", 31.05, 122.05))
        new_id = [r for r in self.store.state()["resources"] if r["name"] == "海巡03"][0]["id"]
        updated = self.store.reassign_sector("coord2", "coordinator", sector["id"], new_id, "原船机械故障")
        self.assertEqual(new_id, updated["resource_id"])
        resources = {r["id"]: r for r in self.store.state()["resources"]}
        self.assertEqual("available", resources[old_resource_id]["status"])
        self.assertEqual("assigned", resources[new_id]["status"])
        logs = [h for h in self.store.incident_history(self.incident["id"]) if h["action"] == "sector.reassign"]
        self.assertEqual(1, len(logs))
        self.assertEqual(resources[old_resource_id]["name"], logs[0]["details"]["prev_resource"])
        self.assertEqual("海巡03", logs[0]["details"]["new_resource"])
        with self.assertRaises(StoreError) as same:
            self.store.reassign_sector("coord2", "coordinator", sector["id"], new_id)
        self.assertEqual(400, same.exception.status)

    def test_cancel_sector_releases_resource_and_requires_reason(self):
        self.publish()
        sector = self.store.state()["sectors"][0]
        with self.assertRaises(StoreError):
            self.store.cancel_sector("coord1", "coordinator", sector["id"], "  ")
        updated = self.store.cancel_sector("coord1", "coordinator", sector["id"], "目标已找到")
        self.assertEqual("released", updated["status"])
        resources = {r["id"]: r for r in self.store.state()["resources"]}
        self.assertEqual("available", resources[sector["resource_id"]]["status"])
        logs = [h for h in self.store.incident_history(self.incident["id"]) if h["action"] == "sector.cancel"]
        self.assertEqual(1, len(logs))
        self.assertIn("目标已找到", logs[0]["summary"])

    def test_cancel_plan_releases_everything(self):
        plan = self.publish()["plan"]
        cancelled = self.store.cancel_plan("coord1", "coordinator", plan["id"], "误报")
        self.assertEqual("cancelled", cancelled["status"])
        state = self.store.state()
        self.assertEqual({"available"}, {r["status"] for r in state["resources"]})
        self.assertEqual([], state["sectors"])

    def test_state_survives_reopen(self):
        self.publish()
        self.store.cancel_sector("coord1", "coordinator", self.store.state()["sectors"][0]["id"], "测试留痕")
        reopened = SectorStore(self.db)
        state = reopened.state()
        self.assertEqual(1, len([p for p in state["plans"] if p["status"] == "active"]))
        self.assertEqual(2, len(state["sectors"]))
        actions = {h["action"] for h in state["history"]}
        self.assertIn("sector.assign", actions)
        self.assertIn("sector.cancel", actions)
        self.assertIn("plan.published", actions)

    def test_permission_and_version_guards(self):
        with self.assertRaises(StoreError) as denied:
            self.store.register_incident("viewer1", "viewer", dict(INCIDENT, code="SAR-T-009"))
        self.assertEqual(403, denied.exception.status)
        with self.assertRaises(StoreError) as denied2:
            self.store.publish_plan("viewer1", "viewer", self.incident["id"])
        self.assertEqual(403, denied2.exception.status)
        with self.assertRaises(StoreError) as conflict:
            self.store.update_resource("coord1", "coordinator", self.r1["id"], 99, range_km=300)
        self.assertEqual(409, conflict.exception.status)
        updated = self.store.update_resource("coord1", "coordinator", self.r1["id"],
                                             self.r1["version"], latitude=31.2, longitude=122.2,
                                             range_km=300)
        self.assertEqual(300, updated["range_km"])
        self.assertEqual(self.r1["version"] + 1, updated["version"])

    def test_no_eligible_resource_fails_with_reasons(self):
        self.store.update_incident("coord1", "coordinator", self.incident["id"],
                                   self.incident["version"], sea_state=8)
        with self.assertRaises(StoreError) as ctx:
            self.publish()
        self.assertIn("海况超限", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

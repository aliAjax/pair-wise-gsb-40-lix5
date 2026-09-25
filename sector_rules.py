"""扇区编排规则层。

纯函数模块：不读写数据库、不依赖 HTTP。输入任务参数（中心、半径、优先级、
最晚完成时刻）、船只状态（能力、船位、航速、剩余航程、适用海况）与当前海况，
输出扇区划分方案和逐船评估。调整排区规则只需修改本文件。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

KM_PER_NM = 1.852
MIN_SPEED_FACTOR = 0.5        # 海况降速下限：至少保留 50% 航速
SPEED_LOSS_PER_LEVEL = 0.07   # 1 级海况起，每升一级损失 7% 航速
BASE_SWEEP_WIDTH_KM = 2.0     # 1 级海况下的有效扫宽（公里）
SWEEP_LOSS_PER_LEVEL = 0.25   # 海况每升一级扫宽缩减量
MIN_SWEEP_WIDTH_KM = 0.5
SAFETY_MARGIN = 1.15          # 航程安全余量系数
EXTRA_VESSEL_PRIORITY = 4     # 优先级达到该值时，在满足时限基础上加派一艘


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """从点 1 指向点 2 的方位角（0=正北，顺时针）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def parse_time(value) -> datetime:
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def speed_factor(sea_state: int) -> float:
    return max(MIN_SPEED_FACTOR, 1.0 - SPEED_LOSS_PER_LEVEL * max(0, int(sea_state) - 1))


def sweep_width_km(sea_state: int) -> float:
    return max(MIN_SWEEP_WIDTH_KM, BASE_SWEEP_WIDTH_KM - SWEEP_LOSS_PER_LEVEL * max(0, int(sea_state) - 1))


def effective_speed_kmh(vessel: dict, sea_state: int) -> float:
    return float(vessel["speed_kn"]) * KM_PER_NM * speed_factor(sea_state)


def evaluate_vessel(vessel: dict, mission: dict, sea_state: int, sector_deg: float, now: datetime | None = None) -> dict:
    """评估单船执行一个 sector_deg 扇区的可行性。

    返回 ok、reasons（不合格原因）、distance_km、eta_min（预计到达用时）、
    search_min（扇区搜索耗时）、required_endurance_km（所需航程）、completion_at。
    """
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    required_cap = (mission.get("required_capability") or "").strip()
    capabilities = vessel.get("capabilities") or []
    if required_cap and required_cap not in capabilities:
        reasons.append("缺少能力「%s」" % required_cap)
    if int(sea_state) > int(vessel["max_sea_state"]):
        reasons.append("海况 %d 级超出适用上限 %d 级" % (int(sea_state), int(vessel["max_sea_state"])))
    distance = haversine_km(vessel["latitude"], vessel["longitude"], mission["center_lat"], mission["center_lon"])
    speed = effective_speed_kmh(vessel, sea_state)
    eta_min = distance / speed * 60.0
    radius = float(mission["radius_km"])
    area = math.pi * radius * radius * (float(sector_deg) / 360.0)
    track_km = area / sweep_width_km(sea_state)
    search_min = track_km / speed * 60.0
    required_endurance = (2.0 * distance + track_km) * SAFETY_MARGIN
    if required_endurance > float(vessel["endurance_km"]):
        reasons.append("剩余航程不足：约需 %.0f km，仅剩 %.0f km" % (required_endurance, float(vessel["endurance_km"])))
    completion = now + timedelta(minutes=eta_min + search_min)
    return {
        "ok": not reasons,
        "reasons": reasons,
        "distance_km": round(distance, 1),
        "eta_min": round(eta_min, 1),
        "search_min": round(search_min, 1),
        "required_endurance_km": round(required_endurance, 1),
        "completion_at": iso(completion),
    }


def _build_sectors(chosen: list[dict], mission: dict, sea_state: int, now: datetime) -> tuple[float, list[dict], bool]:
    """把选中的船按相对方位顺次放入等分扇区，返回（扇区角, 扇区列表, 是否全部合格）。"""
    deg = 360.0 / len(chosen)
    lat, lon = mission["center_lat"], mission["center_lon"]
    ordered = sorted(chosen, key=lambda item: bearing_deg(lat, lon, item["vessel"]["latitude"], item["vessel"]["longitude"]))
    sectors: list[dict] = []
    all_ok = True
    for idx, item in enumerate(ordered):
        vessel = item["vessel"]
        ev = evaluate_vessel(vessel, mission, sea_state, deg, now)
        if not ev["ok"]:
            all_ok = False
        sectors.append({
            "seq": idx + 1,
            "vessel_id": vessel["id"],
            "vessel_name": vessel["name"],
            "start_deg": round(idx * deg, 1),
            "end_deg": round((idx + 1) * deg, 1),
            "distance_km": ev["distance_km"],
            "eta_min": ev["eta_min"],
            "search_min": ev["search_min"],
            "completion_at": ev["completion_at"],
            "warnings": ev["reasons"],
        })
    return deg, sectors, all_ok


def plan_sectors(mission: dict, vessels: list[dict], sea_state: int, now: datetime | None = None) -> dict:
    """按资源能力、海况、预计到达时间与剩余航程排出扇区。

    规则：
    1. 过滤：仅待命船只；能力、海况必须满足，剩余航程按扇区大小核算；
    2. 排序：预计到达时间早者优先，并列时剩余航程大者优先；
    3. 选船：取能在最晚完成时刻前完成的最少船数；优先级达到 EXTRA_VESSEL_PRIORITY
       时加派一艘压缩时间；全部出动仍赶不上时限则全员出动并标记 deadline_risk；
    4. 切区：360° 等分，按各船相对方位顺时针顺次分配，减少交叉航行。
    """
    now = now or datetime.now(timezone.utc)
    sea_state = int(sea_state)
    deadline = parse_time(mission["deadline"])
    notes = ["海况 %d 级：航速保留 %.0f%%，有效扫宽 %.2f km" % (sea_state, speed_factor(sea_state) * 100, sweep_width_km(sea_state))]
    pool: list[dict] = []
    rejected: list[dict] = []
    for vessel in vessels:
        if vessel.get("status") != "available":
            rejected.append({"vessel_id": vessel.get("id"), "vessel_name": vessel.get("name"), "reasons": ["当前已被指派"]})
            continue
        probe = evaluate_vessel(vessel, mission, sea_state, 360.0, now)
        hard = [r for r in probe["reasons"] if not r.startswith("剩余航程不足")]
        if hard:
            rejected.append({"vessel_id": vessel.get("id"), "vessel_name": vessel.get("name"), "reasons": hard})
            continue
        pool.append({"vessel": vessel, "eta_min": probe["eta_min"]})
    pool.sort(key=lambda item: (item["eta_min"], -float(item["vessel"]["endurance_km"])))

    def on_time(sectors: list[dict]) -> bool:
        return all(parse_time(s["completion_at"]) <= deadline for s in sectors)

    chosen: tuple[float, list[dict]] | None = None
    for k in range(1, len(pool) + 1):
        deg, sectors, all_ok = _build_sectors(pool[:k], mission, sea_state, now)
        if all_ok and on_time(sectors):
            if int(mission.get("priority", 3)) >= EXTRA_VESSEL_PRIORITY and k < len(pool):
                deg2, sectors2, ok2 = _build_sectors(pool[: k + 1], mission, sea_state, now)
                if ok2:
                    deg, sectors = deg2, sectors2
                    notes.append("优先级 %d 级，加派一艘压缩完成时间" % int(mission["priority"]))
            chosen = (deg, sectors)
            break
    feasible = chosen is not None
    if chosen is None:
        if not pool:
            notes.append("没有满足能力与海况要求的待命船只")
            return {"mission_id": mission.get("id"), "sea_state": sea_state, "sector_deg": 0,
                    "feasible": False, "deadline_risk": True, "sectors": [],
                    "rejected": rejected, "notes": notes, "planned_at": iso(now)}
        deg, sectors, _ = _build_sectors(pool, mission, sea_state, now)
        chosen = (deg, sectors)
        notes.append("全部待命船只出动仍无法保证在最晚完成时刻前完成，请考虑增援或调整时限")
    deg, sectors = chosen
    notes.append("选用 %d 艘待命船，扇区各 %.0f°" % (len(sectors), deg))
    if sectors:
        latest = max(s["completion_at"] for s in sectors)
        notes.append("预计最晚 %s 完成（最晚完成时刻 %s）" % (latest[11:16] + "Z", iso(deadline)[11:16] + "Z"))
    return {"mission_id": mission.get("id"), "sea_state": sea_state, "sector_deg": round(deg, 1),
            "feasible": feasible, "deadline_risk": not feasible, "sectors": sectors,
            "rejected": rejected, "notes": notes, "planned_at": iso(now)}

"""扇区编排规则：纯函数模块，不依赖存储与网络。

值班员登记事件（中心、半径、优先级、最晚完成时刻）后，由本模块根据
资源能力、海况、预计到达时间与剩余航程计算扇区划分和资源匹配。
结果交给 sector_store 持久化，页面只负责展示。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

KMH_PER_KNOT = 1.852
SWEEP_WIDTH_KM = 3.704          # 有效扫宽（2 海里），用于估算搜索耗时与搜索航程
RESERVE_FACTOR = 1.15           # 航程安全余量系数
MAX_SECTORS = 8                 # 单次编排最多划分的扇区数
EARTH_RADIUS_KM = 6371.0088
SECTOR_CENTER_FRACTION = 0.6    # 扇区代表点取半径的 60% 处


class RuleError(Exception):
    """编排输入或资源条件不满足规则（对应 HTTP 400）。"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def parse_moment(value: Any, field: str = "最晚完成时刻") -> datetime:
    text = str(value or "").strip()
    if not text:
        raise RuleError("%s不能为空" % field)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuleError("%s须为 ISO 时间，如 2026-09-25T18:00:00Z" % field) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def validate_position(latitude: Any, longitude: Any) -> tuple[float, float]:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise RuleError("经纬度必须是数值") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RuleError("经纬度超出有效范围")
    return lat, lon


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuleError("%s不能为空" % field)
    return text


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuleError("%s必须是数值" % field) from exc


def _sea_state(value: Any, field: str = "海况") -> int:
    try:
        sea_state = int(value)
    except (TypeError, ValueError) as exc:
        raise RuleError("%s必须是 0 到 9 的整数" % field) from exc
    if not 0 <= sea_state <= 9:
        raise RuleError("%s必须是 0 到 9 的整数" % field)
    return sea_state


def validate_incident(payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """校验事件登记：中心、半径、优先级、最晚完成时刻。"""
    now = now or utcnow()
    code = _text(payload.get("code"), "事件编号")
    title = _text(payload.get("title"), "事件名称")
    lat, lon = validate_position(payload.get("center_lat"), payload.get("center_lon"))
    radius_km = _number(payload.get("radius_km"), "搜索半径")
    if not 0 < radius_km <= 500:
        raise RuleError("搜索半径应在 0 到 500 公里之间")
    try:
        priority = int(payload.get("priority"))
    except (TypeError, ValueError) as exc:
        raise RuleError("优先级必须是 1 到 5 的整数") from exc
    if not 1 <= priority <= 5:
        raise RuleError("优先级必须是 1 到 5 的整数")
    deadline = parse_moment(payload.get("deadline"))
    if deadline <= now:
        raise RuleError("最晚完成时刻必须晚于当前时间")
    sea_state = _sea_state(payload.get("sea_state"))
    capability = str(payload.get("required_capability") or "surface").strip() or "surface"
    return {
        "code": code,
        "title": title,
        "center_lat": lat,
        "center_lon": lon,
        "radius_km": radius_km,
        "priority": priority,
        "deadline": deadline.isoformat(timespec="seconds"),
        "sea_state": sea_state,
        "required_capability": capability,
    }


def validate_resource(payload: dict[str, Any]) -> dict[str, Any]:
    """校验资源登记：能力、船位、航速、剩余航程、适用海况。"""
    name = _text(payload.get("name"), "资源名称")
    kind = _text(payload.get("kind"), "资源类型")
    raw_caps = payload.get("capabilities") or []
    if isinstance(raw_caps, str):
        raw_caps = raw_caps.split(",")
    caps = sorted({str(item).strip() for item in raw_caps if str(item).strip()})
    if not caps:
        raise RuleError("资源能力不能为空")
    lat, lon = validate_position(payload.get("latitude"), payload.get("longitude"))
    speed_kn = _number(payload.get("speed_kn"), "航速")
    range_km = _number(payload.get("range_km"), "剩余航程")
    if speed_kn <= 0 or range_km <= 0:
        raise RuleError("航速和剩余航程必须大于 0")
    max_sea_state = _sea_state(payload.get("max_sea_state"), "适用海况")
    return {
        "name": name,
        "kind": kind,
        "capabilities": caps,
        "latitude": lat,
        "longitude": lon,
        "speed_kn": speed_kn,
        "range_km": range_km,
        "max_sea_state": max_sea_state,
    }


def destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    delta = distance_km / EARTH_RADIUS_KM
    theta = math.radians(bearing_deg)
    p1 = math.radians(lat)
    lat2 = math.asin(math.sin(p1) * math.cos(delta) + math.cos(p1) * math.sin(delta) * math.cos(theta))
    lon2 = math.radians(lon) + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(p1),
        math.cos(delta) - math.sin(p1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


def sector_geometry(incident: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """把事件圆形搜索区按方位角等分为 count 个扇区。"""
    span = 360.0 / count
    area = math.pi * incident["radius_km"] ** 2 / count
    sectors = []
    for index in range(count):
        start = index * span
        mid = start + span / 2
        clat, clon = destination_point(
            incident["center_lat"], incident["center_lon"], mid,
            incident["radius_km"] * SECTOR_CENTER_FRACTION,
        )
        sectors.append({
            "index": index,
            "bearing_start": round(start, 1),
            "bearing_end": round(start + span, 1),
            "center_lat": round(clat, 5),
            "center_lon": round(clon, 5),
            "area_km2": round(area, 2),
        })
    return sectors


def search_minutes(area_km2: float, speed_kn: float) -> float:
    """按扫宽与航速估算搜完该面积所需分钟数。"""
    return area_km2 / (SWEEP_WIDTH_KM * speed_kn * KMH_PER_KNOT) * 60.0


def evaluate_resource(resource: dict[str, Any], incident: dict[str, Any],
                      sector: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """评估单个资源对单个扇区的适用性：能力、海况、预计到达时间、剩余航程。"""
    now = now or utcnow()
    deadline = parse_moment(incident["deadline"])
    remain_min = (deadline - now).total_seconds() / 60.0
    distance_km = haversine_km(
        resource["latitude"], resource["longitude"], sector["center_lat"], sector["center_lon"]
    )
    speed_kmh = resource["speed_kn"] * KMH_PER_KNOT
    eta_min = distance_km / speed_kmh * 60.0
    sweep_min = search_minutes(sector["area_km2"], resource["speed_kn"])
    search_km = sector["area_km2"] / SWEEP_WIDTH_KM
    need_km = (distance_km + search_km + distance_km) * RESERVE_FACTOR
    margin_km = resource["range_km"] - need_km
    reasons = []
    if incident["required_capability"] not in resource.get("capabilities", []):
        reasons.append("缺少能力:%s" % incident["required_capability"])
    if resource["max_sea_state"] < incident["sea_state"]:
        reasons.append("海况超限（现场 %d 级 > 可承受 %d 级）" % (incident["sea_state"], resource["max_sea_state"]))
    if eta_min + sweep_min > remain_min:
        reasons.append("预计完成晚于最晚完成时刻")
    if margin_km < 0:
        reasons.append("剩余航程不足（缺口 %.1f km）" % (-margin_km))
    return {
        "resource_id": resource["id"],
        "resource_name": resource["name"],
        "eligible": not reasons,
        "reasons": reasons,
        "distance_km": round(distance_km, 2),
        "eta_min": round(eta_min, 1),
        "search_min": round(sweep_min, 1),
        "finish_min": round(eta_min + sweep_min, 1),
        "need_km": round(need_km, 1),
        "range_margin_km": round(margin_km, 1),
        "sea_state_margin": resource["max_sea_state"] - incident["sea_state"],
    }


def plan_sectors(incident: dict[str, Any], resources: list[dict[str, Any]],
                 now: datetime | None = None) -> dict[str, Any]:
    """生成扇区编排：划分扇区并按评估结果贪心指派待命资源。

    每个扇区最多占一艘资源；资源不足时多余扇区留空（planned），
    并记录每个资源被排除的原因，供值班员决策。
    """
    now = now or utcnow()
    candidates = [r for r in resources if r.get("status", "available") == "available"]
    if not candidates:
        raise RuleError("没有待命资源，无法编排扇区")
    count = max(1, min(len(candidates), MAX_SECTORS))
    sectors = sector_geometry(incident, count)
    for sector in sectors:
        sector.update({
            "code": "%s-S%d" % (incident["code"], sector["index"] + 1),
            "radius_km": incident["radius_km"],
            "priority": incident["priority"],
            "deadline": incident["deadline"],
            "assignment": None,
            "rejects": {},
        })
    used: set[int] = set()
    for sector in sorted(sectors, key=lambda s: (s["priority"], s["index"])):
        evals = [evaluate_resource(r, incident, sector, now) for r in candidates if r["id"] not in used]
        ok = [e for e in evals if e["eligible"]]
        if ok:
            best = sorted(ok, key=lambda e: (e["finish_min"], -e["range_margin_km"], e["resource_name"]))[0]
            sector["assignment"] = best
            used.add(best["resource_id"])
        else:
            sector["rejects"] = {e["resource_name"]: e["reasons"] for e in evals}
    if all(sector["assignment"] is None for sector in sectors):
        detail = "；".join(
            "%s（%s）" % (name, "、".join(reasons))
            for name, reasons in sorted(sectors[0]["rejects"].items())
        ) or "无资源可评估"
        raise RuleError("没有资源满足编排条件：%s" % detail)
    return {
        "incident_id": incident.get("id"),
        "generated_at": iso_now(),
        "sector_count": count,
        "sectors": sectors,
    }

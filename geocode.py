"""地理编码：中文/英文地点名 → 经纬度；并支持直接解析经纬度文本。

策略（应对国内网络环境）：
- 依次尝试 3 个免费无 Key 服务：Nominatim(OSM) → Photon(komoot) → ArcGIS World Geocoder
- 自动读取 Windows 系统代理 / 环境变量代理（requests 默认不读系统代理）
- 任一服务成功即返回；全部失败给出简短中文提示
"""

import re
import urllib.request

import requests

TIMEOUT = (5, 10)  # (连接超时, 读取超时)
UA = "MSFS-Teleport/1.0 (local flight tool)"


def _proxies():
    """合并环境变量代理与 Windows 系统代理（注册表）。"""
    try:
        p = urllib.request.getproxies()  # Windows 上会读注册表 ProxyEnable/ProxyServer
    except Exception:  # noqa: BLE001
        p = {}
    return p or None


def _get(url, params):
    return requests.get(
        url, params=params, timeout=TIMEOUT,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,en"},
        proxies=_proxies(),
    )


def _nominatim(query, limit):
    data = _get(
        "https://nominatim.openstreetmap.org/search",
        {"q": query, "format": "json", "limit": limit},
    ).json()
    return [
        {
            "name": it.get("display_name", ""),
            "lat": float(it["lat"]),
            "lon": float(it["lon"]),
            "type": it.get("type", ""),
        }
        for it in data
    ]


def _photon(query, limit):
    # 注意：Photon 封禁 python-requests 默认 UA（403），且 lang 只支持 en/de/fr/it 等（zh 会 400）
    # 因此必须带自定义 UA，且不带 lang 参数
    data = _get(
        "https://photon.komoot.io/api/",
        {"q": query, "limit": limit},
    ).json()
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None])
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        name = ", ".join(
            str(p[k]) for k in ("name", "city", "state", "country") if p.get(k)
        ) or p.get("osm_value", "")
        out.append({"name": name, "lat": lat, "lon": lon, "type": p.get("osm_value", "")})
    return out


def _arcgis(query, limit):
    data = _get(
        "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
        {"singleLine": query, "f": "json", "maxLocations": limit, "outFields": "*"},
    ).json()
    out = []
    for c in data.get("candidates", []):
        loc = c.get("location", {})
        if "x" not in loc or "y" not in loc:
            continue
        out.append(
            {
                "name": c.get("address", ""),
                "lat": float(loc["y"]),
                "lon": float(loc["x"]),
                "type": (c.get("attributes") or {}).get("Type", ""),
            }
        )
    return out


_BACKENDS = (_photon, _nominatim, _arcgis)  # Photon 国内可达性最好，放首位


def search(query, limit=6):
    """依次尝试多个地理编码服务。全部失败抛 RuntimeError（简短中文提示）。"""
    for fn in _BACKENDS:
        try:
            results = fn(query, limit)
            if results:
                return results
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("解析服务连不上（已试 3 个源），请检查网络或代理设置")


_LATLON_RE = re.compile(
    r"""^\s*
        (?P<lat>[-+]?\d{1,2}(?:\.\d+)?)\s*([°度]?)\s*([NnSs])?\s*[,，\s]+\s*
        (?P<lon>[-+]?\d{1,3}(?:\.\d+)?)\s*([°度]?)\s*([EeWw])?\s*$""",
    re.VERBOSE,
)


def parse_latlon(text):
    """解析 '45.75, 126.63' / '45.75 126.63' / '45.75N, 126.63E' 等。失败返回 None。"""
    if not text:
        return None
    m = _LATLON_RE.match(text.strip())
    if not m:
        return None
    a = float(m.group("lat"))
    b = float(m.group("lon"))
    if (m.group(3) or "").upper() == "S":
        a = -abs(a)
    if (m.group(6) or "").upper() == "W":
        b = -abs(b)
    if -90 <= a <= 90 and -180 <= b <= 180:
        return (a, b)
    if -90 <= b <= 90 and -180 <= a <= 180:
        return (b, a)
    return None

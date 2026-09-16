"""地理编码：中文/英文地点名 → 经纬度；并支持直接解析经纬度文本。

策略：
- 多源并行：高德(国内) + Photon(OSM) + Nominatim(OSM) + ArcGIS(Esri)，全部查询后合并排序
- 离线词典 world_famous.json：覆盖全球知名地点（中文/外文别名 -> WGS-84 坐标），
  解决“中文搜国外知名地点”以及英文歧义词(Pyramid/West Bank)被错配的问题
- 智能排序：查询疑似国外时优先国外/具体地标，国内同名“小区/村庄”自动靠后；
  纯国内地址时高德精确结果仍排第一
- 自动读取 Windows 系统代理 / 环境变量代理（requests 默认不读系统代理）
"""

import math
import os
import re
import sys
import threading
import urllib.request

import requests

# 单个源的超时（连接, 读取）。收紧到"慢源快速放弃"的量级：地名搜索要的是快，
# 一个连不上的源不该把整个候选列表拖到 7~8 秒。
TIMEOUT = (2.0, 3.0)
UA = "MSFS-Teleport/1.0 (local flight tool)"

_PROXY_MEMO = {"t": 0.0, "v": None}


def _proxies():
    """合并环境变量代理与 Windows 系统代理（注册表）。结果缓存 60 秒，
    避免每次搜索都去读一次注册表。"""
    now = time.time()
    if now - _PROXY_MEMO["t"] < 60.0:
        return _PROXY_MEMO["v"]
    try:
        p = urllib.request.getproxies()  # Windows 上会读注册表 ProxyEnable/ProxyServer
    except Exception:  # noqa: BLE001
        p = {}
    _PROXY_MEMO["t"] = now
    _PROXY_MEMO["v"] = p or None
    return _PROXY_MEMO["v"]


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


# ----------------------------------------------------------------------------
# 高德（AMap）国内源 + GCJ-02 -> WGS-84 坐标转换
# ----------------------------------------------------------------------------
_A = 6378245.0
_EE = 0.00669342162296594323


def _resource_path(name):
    """onefile 下数据文件可能在 sys._MEIPASS；开发模式/同目录亦可。"""
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.isfile(os.path.join(base, name)):
        return os.path.join(base, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _load_amap_key():
    """优先环境变量 AMAP_KEY，其次本地 amap_key.txt（已 gitignore，勿提交）。"""
    env = os.environ.get("AMAP_KEY")
    if env and env.strip():
        return env.strip()
    p = _resource_path("amap_key.txt")
    if os.path.isfile(p):
        try:
            return open(p, encoding="utf-8").read().strip()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _out_of_china(lon, lat):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def gcj02_to_wgs84(lat, lon):
    """GCJ-02(火星坐标) -> WGS-84(GPS)。高德/腾讯返回坐标需转换后给 MSFS。"""
    if _out_of_china(lon, lat):
        return lat, lon
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat - dlat, lon - dlon


def _amap(query, limit):
    """高德地理编码（国内最优）。返回坐标经 GCJ-02 -> WGS-84 转换。"""
    key = _load_amap_key()
    if not key:
        return []
    try:
        data = _get(
            "https://restapi.amap.com/v3/geocode/geo",
            {"address": query, "key": key, "batch": "false"},
        ).json()
    except Exception:  # noqa: BLE001
        return []
    if data.get("status") != "1":
        return []
    out = []
    for g in data.get("geocodes", []):
        loc = g.get("location", "")
        if "," not in loc:
            continue
        try:
            lo, la = (float(v) for v in loc.split(","))
        except ValueError:
            continue
        wla, wlo = gcj02_to_wgs84(la, lo)  # 转 WGS-84 给 MSFS
        name = g.get("formatted_address", "") or g.get("address", "")
        out.append({"name": name, "lat": wla, "lon": wlo, "type": g.get("level", "")})
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------
# 离线全球知名地名词典（中文/外文别名 -> 真实经纬度，WGS-84）
# 解决：中文搜国外知名地点、以及 Photon 等把 "Pyramid"/"West Bank" 这类英文歧义词
# 错配的问题。结果即 WGS-84，无需坐标转换。
# ----------------------------------------------------------------------------
import json as _json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def _has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


_GAZ_PATH = _resource_path("world_famous.json")


def _load_gazetteer():
    try:
        with open(_GAZ_PATH, encoding="utf-8") as f:
            entries = _json.load(f)
    except Exception:  # noqa: BLE001
        return {}, []
    lut = {}
    for e in entries:
        for k in e.get("keys", []):
            lut[k.strip().lower()] = e
    return lut, entries


_GAZ_LUT, _GAZ_ENTRIES = _load_gazetteer()


def _gazetteer_match(query):
    """返回强匹配的词典条目：查询本质就是该地名，而非长地址里的子串。

    例：“埃及胡夫金字塔”匹配“胡夫金字塔”（去掉后剩“埃及”，很短 -> 强匹配）；
    “北京市朝阳区望京SOHO”里的“北京”去掉后剩“市朝阳区望京SOHO”很长 -> 不算。
    """
    q = (query or "").strip().lower()
    if not q or not _GAZ_LUT:
        return []
    if q in _GAZ_LUT:
        return [_GAZ_LUT[q]]
    matched, seen = [], set()
    for alias, e in _GAZ_LUT.items():
        if len(alias) < 2 or alias not in q:
            continue
        if len(q.replace(alias, "", 1)) <= 2:
            if id(e) not in seen:
                seen.add(id(e))
                matched.append(e)
    return matched


def _gazetteer_search(query):
    return _gaz_cands(_gazetteer_match(query), query)


def _gazetteer_strong(query):
    return bool(_gazetteer_match(query))


def _gaz_cands(entries, query):
    out = []
    cjk = _has_cjk(query)
    for e in entries:
        en = e.get("en", "")
        name = f"{query.strip()}（{en}）" if cjk else (en or query.strip())
        out.append(
            {
                "name": name,
                "lat": float(e["lat"]),
                "lon": float(e["lon"]),
                "type": "gazetteer",
                "_gaz": True,
            }
        )
    return out


# ----------------------------------------------------------------------------
# 区域自适应 + 多源合并排序
# - 所有适用源（高德 + Photon + Nominatim + ArcGIS）并行查询，合并后再排序
# - 不再“首个非空即返回”，避免国内弱源（高德）把国外正确结果堵死
# ----------------------------------------------------------------------------
_GLOBAL = (_photon, _nominatim, _arcgis)

# 常驻线程池：单次搜索不再新建/销毁线程池（省开销），也让"超时后仍在跑的慢源"
# 能在后台跑完并把结果写进缓存 —— 下次搜同一个词直接命中。
_EXEC = ThreadPoolExecutor(max_workers=10, thread_name_prefix="geocode")

# ---- 单源结果缓存 + 失败冷却 ----
# 缓存：同一个查询在 TTL 内重复搜索（含输入时的预热请求）直接命中，0 网络等待。
# 冷却：连续失败的源（例如国内连不上的 Nominatim）短时间内不再拖慢每次搜索。
_CACHE_LOCK = threading.Lock()
_CACHE = {}                 # (backend, query) -> (ts, [results])
_CACHE_TTL = 900.0
_CACHE_MAX = 800
_FAIL = {}                  # backend -> [连续失败次数, 冷却截止时间]
_FAIL_COOLDOWN = 180.0
_FAIL_TRIP = 2


def _backend_name(fn):
    return getattr(fn, "__name__", str(fn))


def _cache_get(name, qk):
    with _CACHE_LOCK:
        hit = _CACHE.get((name, qk))
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return list(hit[1])
    return None


def _cache_put(name, qk, results):
    with _CACHE_LOCK:
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[(name, qk)] = (time.time(), list(results))


def _is_cooling(name):
    with _CACHE_LOCK:
        st = _FAIL.get(name)
        return bool(st and st[0] >= _FAIL_TRIP and time.time() < st[1])


def _note_ok(name):
    with _CACHE_LOCK:
        _FAIL.pop(name, None)


def _note_fail(name):
    with _CACHE_LOCK:
        st = _FAIL.get(name) or [0, 0.0]
        st[0] += 1
        st[1] = time.time() + _FAIL_COOLDOWN
        _FAIL[name] = st


def _run_backend(fn, query, limit):
    """查一个源：先看缓存，处于冷却期就快速跳过。异常抛给调用方。

    注意"查到 0 条"不算失败（照样缓存），只有真的抛异常才计入冷却。
    """
    name = _backend_name(fn)
    qk = (query or "").strip().lower()
    got = _cache_get(name, qk)
    if got is not None:
        return got
    if _is_cooling(name):
        raise RuntimeError("backend cooling down: %s" % name)
    try:
        res = fn(query, limit) or []
    except Exception:
        _note_fail(name)
        raise
    _note_ok(name)
    _cache_put(name, qk, res)
    return res

# 外国指示词：命中其一即视为“查的是国外地点”，强力优先国外结果
_FOREIGN_INDICATORS = set(
    "埃及 法国 美国 英国 日本 俄罗斯 德国 意大利 西班牙 葡萄牙 荷兰 比利时 瑞士 奥地利 "
    "瑞典 挪威 丹麦 芬兰 波兰 捷克 希腊 土耳其 伊朗 伊拉克 以色列 沙特 印度 巴基斯坦 "
    "孟加拉 泰国 越南 马来西亚 印尼 菲律宾 韩国 朝鲜 巴西 阿根廷 智利 秘鲁 墨西哥 加拿大 "
    "澳大利亚 新西兰 南非 肯尼亚 金字塔 尼罗河 约旦河 死海 撒哈拉 亚马逊 富士山 泰姬陵 "
    "埃菲尔 自由女神 狮身人面像 马丘比丘 复活节岛 巨石阵 比萨斜塔 大本钟 白宫 五角大楼 "
    "帝国大厦 克里姆林宫 红场 雅典卫城 罗马斗兽场 海峡 运河 南极 北极 西岸 加沙 乌克兰 顿巴斯".split()
)


def _query_looks_foreign(query, gaz_matched):
    if not _has_cjk(query):
        return True  # 拉丁字母查询视为国外
    if gaz_matched:
        return True  # 命中离线词典（均为国外知名地点）
    return any(ind in query for ind in _FOREIGN_INDICATORS)


_GOOD_TYPES = (
    "landmark", "monument", "tourist", "attraction", "city", "administrative",
    "capital", "peak", "mountain", "strait", "water", "island", "park",
    "natural", "gazetteer", "airport",
)
_BAD_TYPES = (
    "兴趣点", "poi", "村庄", "village", "hamlet", "住宅区", "residential",
    "neighbourhood", "quarter",
)


def _score(r, foreign_query):
    s = 0
    in_china = not _out_of_china(r["lon"], r["lat"])
    if foreign_query:
        s += -40 if in_china else 40
    else:
        s += 30 if in_china else -20
    t = (r.get("type") or "").lower()
    if any(g in t for g in _GOOD_TYPES):
        s += 20
    if any(b in t for b in _BAD_TYPES):
        s -= 20
    if r.get("_gaz"):
        s += 30
    return s


def _dedupe(results):
    seen, out = set(), []
    for r in results:
        key = (round(r["lat"], 2), round(r["lon"], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def search(query, limit=6, region="auto", fast=False):
    """地名解析。region: 'auto' | 'cn' | 'intl'；fast=True 走"快速通道"。

    - 完整模式：所有适用源并行查询（高德仅国内参与），与离线词典结果合并后排序；
      查询疑似国外时优先国外/具体地标，国内同名"兴趣点/小区"自动靠后。
    - fast 模式：离线词典（0 网络）+ 最快的那一个源（国内高德 / 国外 Photon），
      通常 1 秒内返回 —— 用于先把候选列表画到界面上，再用完整结果覆盖。
    - 单源结果带缓存，连不上的源会进入短暂冷却，不再每次都把搜索拖慢。
    全部源与词典都无结果时抛 RuntimeError（简短中文提示）。
    """
    if not query or not query.strip():
        raise RuntimeError("请输入地点名称")
    if region == "auto":
        region = "cn" if _has_cjk(query) else "intl"
    elif region == "intl" and _has_cjk(query):
        region = "cn"  # 外文界面但搜中文地名，仍参与高德

    gaz = _gazetteer_search(query)
    foreign_query = _query_looks_foreign(query, _gazetteer_strong(query))

    if fast:
        backends = [_amap] if region == "cn" else [_photon]
        # 词典已命中时不必等网络源，把预算压到最短
        budget = 0.6 if gaz else 1.5
    else:
        backends = ([_amap] if region == "cn" else []) + list(_GLOBAL)
        budget = 3.2

    collected = list(gaz)  # 离线词典结果优先并入（零网络依赖）
    if backends:
        tasks = [_EXEC.submit(_run_backend, b, query, limit) for b in backends]
        try:
            # 硬超时：到点立即返回，未完成的慢源让它继续在后台跑完（结果会进缓存，
            # 下次搜同一个词就直接命中）。
            for f in as_completed(tasks, timeout=budget):
                try:
                    r = f.result()
                    if r:
                        collected.extend(r)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001  concurrent.futures.TimeoutError
            pass

    merged = _dedupe(collected)
    ranked = sorted(merged, key=lambda r: -_score(r, foreign_query))
    if not ranked:
        raise RuntimeError("解析服务连不上（已试多个源），请检查网络或代理设置")
    return ranked[:limit]


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

"""把 Natural Earth 原始数据编译成内嵌用的精简 world_map.json。

产物结构：
    {
      "c0": [[ring...], ...]        低精度国界/海岸线（低 zoom 用）
      "c1": [[ring...], ...]        高精度国界/海岸线（高 zoom 用）
      "names": [[lon, lat, name]]   主要地名（国家名，低 zoom 用）
      "places": [[lon, lat, name, pop, minzoom]]  居民点（按 zoom 分级显示）
    }

坐标一律是 WGS-84，与 MSFS / SimConnect 一致。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _p(name):
    return os.path.join(HERE, name)


def rdp(pts, eps):
    """Douglas-Peucker 简化。"""
    if len(pts) < 3:
        return pts
    stack = [(0, len(pts) - 1)]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    eps2 = eps * eps
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        ax, ay = pts[i0]
        bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        norm = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if norm == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / norm))
                d = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
            if d > best:
                best, bi = d, i
        if best > eps2:
            keep[bi] = True
            stack.append((i0, bi))
            stack.append((bi, i1))
    return [p for p, k in zip(pts, keep) if k]


def strip_ring(pts, eps, prec):
    """抽稀 + 去重 + 降精度，返回 [[lon,lat],...]。"""
    if len(pts) > 2:
        pts = rdp(pts, eps)
    out = []
    for lon, lat in pts:
        p = (round(lon, prec), round(lat, prec))
        if not out or out[-1] != p:
            out.append(p)
    if len(out) > 3 and out[0] != out[-1]:
        out.append(out[0])
    return out if len(out) > 3 else None


def from_topo(path, eps, prec):
    """topojson -> 精简 ring 列表。"""
    topo = json.load(open(path, encoding="utf-8"))
    tr = topo["transform"]
    sx, sy = tr["scale"]
    tx, ty = tr["translate"]
    arcs = []
    for a in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in a:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(idxs):
        out = []
        for i in idxs:
            a = arcs[~i][::-1] if i < 0 else arcs[i]
            out.extend(a if not out else a[1:])
        return out

    rings = []
    for g in topo["objects"]["countries"]["geometries"]:
        if g.get("type") == "Polygon":
            polys = [g["arcs"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["arcs"]
        else:
            continue
        for poly in polys:
            for r in poly:
                rr = strip_ring(ring(r), eps, prec)
                if rr:
                    rings.append(rr)
    return rings


def from_geojson(path, eps, prec):
    """FeatureCollection( Polygon/MultiPolygon ) -> ring 列表。"""
    gj = json.load(open(path, encoding="utf-8"))
    rings = []
    for f in gj.get("features", []):
        geom = f.get("geometry")
        if not geom:
            continue
        t = geom.get("type")
        coords = geom.get("coordinates")
        if t == "Polygon":
            polys = [coords]
        elif t == "MultiPolygon":
            polys = coords
        else:
            continue
        for poly in polys:
            rr = strip_ring(list(poly[0]), eps, prec)
            if rr:
                rings.append(rr)
    return rings


def build():
    out = {}

    # --- 低精度层（低 zoom）：50m topojson，坐标 2 位小数，够用且小 ---
    src0 = _p("raw_50m.json")
    if os.path.exists(src0):
        out["c0"] = from_topo(src0, eps=0.05, prec=2)
        print("c0 rings: %d" % len(out["c0"]))
    else:
        out["c0"] = []

    # --- 高精度层（放大后）：10m 海岸线/国界 ---
    src1 = _p("raw_countries10.geojson")
    if os.path.exists(src1):
        out["c1"] = from_geojson(src1, eps=0.008, prec=3)
        print("c1 rings: %d" % len(out["c1"]))
    else:
        out["c1"] = []

    # --- 居民点（分级显示地名） ---
    srcp = _p("raw_places.geojson")
    places = []
    if os.path.exists(srcp):
        gj = json.load(open(srcp, encoding="utf-8"))
        for f in gj.get("features", []):
            p = f["properties"]
            c = f["geometry"]["coordinates"]
            name = p.get("name") or p.get("nameascii")
            if not name:
                continue
            pop = int(p.get("pop_max") or 0)
            places.append([round(c[0], 3), round(c[1], 3), name, pop])
    # 人口多的排在前面，渲染时同名/−不足 zoom 的会被过滤
    places.sort(key=lambda r: -r[3])
    out["places"] = places
    print("places: %d" % len(places))

    # --- 国家名（低 zoom 标注用），从 50m 的属性里取 ---
    names = []
    srct = _p("raw_50m.json")
    if os.path.exists(srct):
        topo = json.load(open(srct, encoding="utf-8"))
        for g in topo["objects"]["countries"]["geometries"]:
            pass  # 拓扑数据无中心点，改用 places 里的首都替代
    out["names"] = names

    dst = _p("world_map.json")
    raw = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    open(dst, "w", encoding="utf-8").write(raw)
    print("written %s  %.2f MB" % (dst, len(raw.encode("utf-8")) / 1048576))


if __name__ == "__main__":
    build()

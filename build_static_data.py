"""Export the citywide analysis to a single static data.js file for the
read-only public site (static_site/index.html). No Flask/Dash at runtime -
GitHub Pages only serves static files, so this is a one-shot data dump, not
a server. Re-run this after any pipeline/algorithm rebuild
(../python/build_region.py --citywide) to refresh the public site's data.

Chevron math (_tangent/chevron_coords) is copied verbatim from
../python/app.py rather than imported, since importing app.py would build
the whole Dash app as a side effect (it constructs app.layout at module
scope) just to reach two small pure functions - this pair is already
production-validated there, low risk of drift given how small and stable
it is.
"""
import json
import re
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "python" / "data" / "prepared"
WARDS_PATH = ROOT / "Boundaries_-_Wards_(2023-)_20260717.geojson"
EDIT_STATE_PATH = ROOT / "edit-state.json"
BIKE_ROUTES_PATH = ROOT / "Bike_Routes_20260801.geojson"
OUT_PATH = Path(__file__).parent / "data.js"

ROUND = 6  # ~11cm precision - plenty, keeps the payload small

# Segments to force to "none" regardless of recommend_action/edit-state.
# Cell 809's "East 116th Street" (osm_55822, osm_55713) is 4 duplicate rows
# in segments.parquet sharing the exact same fnode/tnode pair (a source-data
# artifact, not a real second street) - since the live app's edit-state key
# is fnode_id_tnode_id with no cell_id (a known collision issue, see
# [[project_street_direction]] memory), Dash ends up with several segment
# components sharing one id, so clicking never reliably reaches all of them
# and the user can't reject it in the live app. Excluding it here directly
# until the underlying duplicate-row/collision bug is fixed at the source.
MANUAL_EXCLUDE = {
    ("osm_55822", "osm_55713"),
}

# Whole cells manually reviewed and rejected outright - force every segment
# in these cells to "none" regardless of recommend_action/edit-state, same
# rationale as MANUAL_EXCLUDE above but keyed by cell rather than by a
# specific duplicate-row pair.
MANUAL_EXCLUDE_CELLS = {841, 899, 418}


def rd(coords):
    """Round a list of [lat, lon] pairs to ROUND decimals."""
    return [[round(lat, ROUND), round(lon, ROUND)] for lat, lon in coords]


def line_latlngs(geom):
    x, y = geom.coords.xy
    return [[y[i], x[i]] for i in range(len(x))]


def ring_latlngs(geom):
    x, y = geom.exterior.coords.xy
    return [[y[i], x[i]] for i in range(len(x))]


def polygon_parts(geom):
    if geom.geom_type == "MultiPolygon":
        return [ring_latlngs(g) for g in geom.geoms]
    return [ring_latlngs(geom)]


def is_locked(row):
    return row.dir_travel == "B" or bool(row.cdot_contraflow)


def seg_key(row):
    # Matches app.py's seg_key() exactly - fnode_id_tnode_id, no cell_id -
    # so this export matches whatever the live app actually displays,
    # including that key's known cross-cell collision quirk (see
    # [[project_street_direction_osm_swap]]/memory: not fixed there, so not
    # silently "fixed" here either - staying faithful to the live app is
    # the whole point of this export).
    return f"{row.fnode_id}_{row.tnode_id}"


def effective_action(row, edits):
    # Mirrors app.py's effective_action()/save_changes(): a locked segment
    # always reads "none" regardless of any (even stale) edit-state entry;
    # otherwise an explicit edit-state override wins over the algorithm's
    # own recommend_action - this is the user's actual REVIEWED decision,
    # not just the raw algorithm guess, which is what the public site
    # should show. Segments the user manually flagged with no algorithm
    # recommendation at all (recommend_action == "none") are included too.
    if is_locked(row):
        return "none"
    if (row.fnode_id, row.tnode_id) in MANUAL_EXCLUDE:
        return "none"
    if pd.notna(row.cell_id) and int(row.cell_id) in MANUAL_EXCLUDE_CELLS:
        return "none"
    return edits.get(seg_key(row), {}).get("action", row.recommend_action)


# --- Cross-street self-reference cleanup ---------------------------------
#
# Some cross_street_start/end values are themselves a "/"-joined pair from
# a dogleg intersection (two street names meeting at slightly different
# points treated as one node), and one of the two sometimes just repeats the
# street being edited under a different abbreviation style (e.g. "E 85TH
# ST" for a change on "East 85th Street"). "Between X and the street itself"
# isn't a useful cross street, so strip whichever part matches.
_SUFFIX_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "PLACE": "PL", "ROAD": "RD", "PARKWAY": "PKWY", "COURT": "CT",
    "LANE": "LN", "TERRACE": "TER", "CIRCLE": "CIR", "HIGHWAY": "HWY",
    "EXPRESSWAY": "EXPY", "SQUARE": "SQ",
}
_DIR_MAP = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def _normalize_street(name):
    if not name:
        return ""
    tokens = re.sub(r"[.\-]", " ", name.upper()).split()
    tokens = [_DIR_MAP.get(t, t) for t in tokens]
    if tokens:
        tokens[-1] = _SUFFIX_MAP.get(tokens[-1], tokens[-1])
    return " ".join(tokens)


def strip_self_reference(cross_value, street_names):
    """street_names is every distinct street name in the change being
    described (a merged corridor may cover more than one), so a cross-field
    part is stripped if it matches ANY of them, not just a single joined
    label. Surviving parts are normalized (e.g. "South Essex Avenue" ->
    "S ESSEX AVE") since cross-street fields mix fully-spelled-out and
    already-abbreviated forms depending on which source segment they came
    from, and showing both styles side by side in one "Between X and Y"
    reads as if it's two different kinds of thing rather than two streets."""
    if not cross_value:
        return cross_value
    targets = {_normalize_street(n) for n in street_names}
    parts = [p.strip() for p in cross_value.split("/")]
    kept = [_normalize_street(p) for p in parts if _normalize_street(p) not in targets]
    return " / ".join(kept) if kept else None


# --- Near-miss node unification --------------------------------------------
#
# Two segments that touch at the same physical intersection should merge
# into one change even when they're on different streets (a dogleg/jog), but
# occasionally the shared intersection carries different fnode/tnode ids on
# each side (rounding, or the OSM-swap id-collision issue - see
# [[project_street_direction_osm_swap]]), which used to keep them from being
# recognized as touching at all. Union any two distinct node ids within
# MERGE_DIST_FT of each other so the connectivity graph below treats them as
# one junction.
MERGE_DIST_FT = 660.0  # 1/8 mile


def _haversine_ft(p1, p2):
    lat1, lon1 = p1
    lat2, lon2 = p2
    r = 20925721.0  # earth radius, feet
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * r * asin(sqrt(a))


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def unify_nearby_nodes(rows):
    # Only "loose end" node ids (degree 1 in the raw fnode/tnode graph) are
    # merge candidates: a node already shared by two rows is a confirmed
    # touch and needs no help, and a single segment's own two ends are never
    # a real intersection no matter how short the block is (most Chicago
    # blocks are well under 660ft, so comparing every node pair here without
    # this guard unions a short segment's own endpoints with each other).
    degree, owner_row = {}, {}
    for row in rows:
        for n in (row.fnode_id, row.tnode_id):
            degree[n] = degree.get(n, 0) + 1
            owner_row[n] = row  # only read back for degree-1 (loose) nodes
    loose = [n for n, d in degree.items() if d == 1]
    node_coord = {}
    for row in rows:
        coords = list(row.geometry.coords)
        if row.fnode_id in loose:
            node_coord.setdefault(row.fnode_id, (coords[0][1], coords[0][0]))
        if row.tnode_id in loose:
            node_coord.setdefault(row.tnode_id, (coords[-1][1], coords[-1][0]))

    def other_end(node):
        r = owner_row[node]
        return r.tnode_id if r.fnode_id == node else r.fnode_id

    candidates = []
    for i in range(len(loose)):
        for j in range(i + 1, len(loose)):
            a, b = loose[i], loose[j]
            if owner_row[a] is owner_row[b]:
                continue  # a single segment's own two ends - never a real touch
            dist = _haversine_ft(node_coord[a], node_coord[b])
            if dist <= MERGE_DIST_FT:
                candidates.append((dist, a, b))
    candidates.sort(key=lambda t: t[0])

    # Union nearest pairs first, re-checking before each one: two nodes
    # merging can transitively pull a THIRD node's segment closed into a
    # self-loop even though no single pairwise check above ever compared
    # that segment's two ends directly to each other (e.g. A-B touch at a
    # real intersection, and a short unrelated segment C-D happens to have D
    # within range of B too - unioning B and D would make C-D's own two ends
    # share a root once D=B=A's neighborhood). Skip any union that would
    # cause either endpoint's own segment to close on itself this way.
    dsu = _UnionFind()
    for _, a, b in candidates:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb:
            continue
        if dsu.find(other_end(a)) == rb or dsu.find(other_end(b)) == ra:
            continue
        dsu.union(a, b)
    return dsu


print("Loading edit-state (your reviewed overrides)...")
if EDIT_STATE_PATH.exists():
    with open(EDIT_STATE_PATH, encoding="utf-8") as f:
        EDIT_STATE = json.load(f)
    print(f"  {len(EDIT_STATE)} edit-state entries loaded from {EDIT_STATE_PATH.name}")
else:
    EDIT_STATE = {}
    print(f"  WARNING: {EDIT_STATE_PATH.name} not found - the export will show raw algorithm "
          f"recommendations only, NOT your reviewed decisions. Re-run after exporting it.")

print("Loading prepared data...")
segments = gpd.read_parquet(DATA_DIR / "segments.parquet")
bounding = gpd.read_parquet(DATA_DIR / "bounding.parquet")
node_points_df = pd.read_parquet(DATA_DIR / "node_points.parquet")
green = gpd.read_parquet(DATA_DIR / "green.parquet")
cells = gpd.read_parquet(DATA_DIR / "cells.parquet")

region_extent = cells.union_all().convex_hull.buffer(0.01)
wards = gpd.read_file(WARDS_PATH)
wards = wards[wards.intersects(region_extent)][["ward", "geometry"]]
wards["ward"] = wards["ward"].astype(int)

print("Joining each segment to its ward...")
seg_midpoints_3435 = segments.to_crs(3435).geometry.interpolate(0.5, normalized=True)
seg_midpoints = gpd.GeoDataFrame(
    {"fnode_id": segments["fnode_id"], "tnode_id": segments["tnode_id"]},
    geometry=seg_midpoints_3435, crs=3435,
).to_crs(4326)
seg_ward = gpd.sjoin(seg_midpoints, wards, how="left", predicate="within")[
    ["fnode_id", "tnode_id", "ward"]
].drop_duplicates(subset=["fnode_id", "tnode_id"])
segments = segments.merge(seg_ward, on=["fnode_id", "tnode_id"], how="left")

minx, miny, maxx, maxy = segments.total_bounds
CENTER = [(miny + maxy) / 2, (minx + maxx) / 2]
OVERALL_BOUNDS = [[miny, minx], [maxy, maxx]]

# --- Direction chevrons - copied from python/app.py verbatim.
LAT_SCALE = np.cos(np.radians(CENTER[0]))


def _tangent(geom, frac, forward):
    eps = 0.02
    t0, t1 = max(0.0, frac - eps), min(1.0, frac + eps)
    p0, p1 = geom.interpolate(t0, normalized=True), geom.interpolate(t1, normalized=True)
    dx, dy = (p1.x - p0.x) * LAT_SCALE, (p1.y - p0.y)
    if not forward:
        dx, dy = -dx, -dy
    norm = (dx ** 2 + dy ** 2) ** 0.5
    return (dx / norm, dy / norm) if norm else (1.0, 0.0)


def chevron_coords(geom, frac, forward, size=0.00006, angle_deg=28):
    tip = geom.interpolate(frac, normalized=True)
    ux, uy = _tangent(geom, frac, forward)
    ang = np.radians(angle_deg)
    wings = []
    for sign in (1, -1):
        bx, by = -ux, -uy
        rx = bx * np.cos(sign * ang) - by * np.sin(sign * ang)
        ry = bx * np.sin(sign * ang) + by * np.cos(sign * ang)
        wings.append((tip.x + (rx * size) / LAT_SCALE, tip.y + ry * size))
    return [wings[0], (tip.x, tip.y), wings[1]]


def chevron_latlngs(geom, frac, forward, **kw):
    return [[lat, lon] for lon, lat in chevron_coords(geom, frac, forward, **kw)]


print("Building base direction-chevron layer (current direction, all one-way streets)...")


def base_arrows(gdf):
    out = []
    for row in gdf.itertuples():
        if row.dir_travel not in ("F", "T"):
            continue
        out.append(rd(chevron_latlngs(row.geometry, 0.5, forward=(row.dir_travel == "F"))))
    return out


BASE_ARROWS = base_arrows(segments) + base_arrows(bounding)

print("Applying your edit-state overrides on top of the raw algorithm recommendations...")
segments["final_action"] = segments.apply(lambda r: effective_action(r, EDIT_STATE), axis=1)
n_overridden = (segments["final_action"] != segments["recommend_action"]).sum()
print(f"  {n_overridden} segments differ from the raw algorithm recommendation "
      f"(rejected, manually added, or locked)")

def split_into_chains(rows, unify):
    """Decompose an entire action group (all reversal, or all contraflow,
    rows citywide - not scoped to one cell or one connected component) into
    maximal walkable chains, each a list of (row, forward) pairs. A chain
    ends at any node where segment count != 2 - a dangling end (1) or a real
    branch/junction (3+) - so a fork anywhere breaks into separate chains
    fanning out from that point, rather than one stray junction disqualifying
    every other segment merge in the same (now citywide-scale) connected
    component the way a single all-or-nothing simple-path check would.
    `unify(row)` returns a row's (fnode, tnode) after collapsing any nodes
    within MERGE_DIST_FT of each other, so a shared-intersection touch is
    recognized even when the two sides carry different raw node ids."""
    node_degree, node_segs = {}, {}
    for row in rows:
        for n in unify(row):
            node_degree[n] = node_degree.get(n, 0) + 1
            node_segs.setdefault(n, []).append(row)
    hard_nodes = {n for n, d in node_degree.items() if d != 2}

    def walk_from(start_node, row0):
        fu, tu = unify(row0)
        forward0 = fu == start_node
        chain = [(row0, forward0)]
        visited.add(id(row0))
        current = tu if forward0 else fu
        while current not in hard_nodes:
            candidates = [r for r in node_segs[current] if id(r) not in visited]
            if not candidates:
                break
            nxt = candidates[0]
            visited.add(id(nxt))
            nfu, ntu = unify(nxt)
            forward = nfu == current
            chain.append((nxt, forward))
            current = ntu if forward else nfu
        return chain

    visited = set()
    chains = []
    for n in hard_nodes:
        for row in node_segs[n]:
            if id(row) not in visited:
                chains.append(walk_from(n, row))
    # Anything left is a pure cycle (every node on it has degree 2, so it
    # never touched a hard node) - keep those as individual unmerged
    # segments rather than picking an arbitrary place to cut the loop open.
    for row in rows:
        if id(row) not in visited:
            chains.append([(row, True)])
    return chains


def merge_group(order):
    """Combine a walk-ordered chain of segments into one change: one
    continuous line, cross streets from the two outer ends, and one
    proposed-direction chevron - only when every segment's proposed
    (flipped) direction agrees on which way the merged run points, since a
    real contraflow/reversal corridor should be coherent end to end. The
    chain isn't required to stay on one street - a dogleg through a shared
    intersection onto a different street is a single coherent change too,
    so the label joins every distinct street name encountered along the
    walk."""
    coords = []
    for row, forward in order:
        pts = line_latlngs(row.geometry)
        if not forward:
            pts = list(reversed(pts))
        if coords and coords[-1] == pts[0]:
            pts = pts[1:]
        coords.extend(pts)

    # Compare consecutive names in their normalized form, not raw string
    # equality - full_name is occasionally inconsistent for the same real
    # street (e.g. "South Parnell Avenue" on one segment, "S PARNELL AVE" on
    # the next), which would otherwise show up as a bogus "Street A / Street
    # A" join.
    names_in_order, names_norm = [], []
    for row, _ in order:
        nm = row.full_name if pd.notna(row.full_name) else None
        if not nm:
            continue
        norm = _normalize_street(nm)
        if names_norm and names_norm[-1] == norm:
            continue
        names_in_order.append(nm)
        names_norm.append(norm)
    street_label = " / ".join(names_in_order) if names_in_order else "(unnamed)"

    first_row, first_fwd = order[0]
    last_row, last_fwd = order[-1]
    cross_start = first_row.cross_street_start if first_fwd else first_row.cross_street_end
    cross_end = last_row.cross_street_end if last_fwd else last_row.cross_street_start

    directions = set()
    for row, forward in order:
        if row.dir_travel in ("F", "T"):
            current_matches_walk = (row.dir_travel == "F") == forward
            directions.add(not current_matches_walk)
    if len(directions) != 1:
        return None  # inconsistent flip direction across the chain - don't merge
    walk_forward = directions.pop()

    merged_geom = LineString([(lon, lat) for lat, lon in coords])
    proposed_chevron = rd(chevron_latlngs(merged_geom, 0.5, forward=walk_forward))
    lats = [p[0] for p in coords]
    lons = [p[1] for p in coords]
    return {
        "street": street_label,
        "crossStart": strip_self_reference(cross_start, names_in_order) if pd.notna(cross_start) else None,
        "crossEnd": strip_self_reference(cross_end, names_in_order) if pd.notna(cross_end) else None,
        "ward": int(first_row.ward) if pd.notna(first_row.ward) else None,
        "cellId": int(first_row.cell_id) if pd.notna(first_row.cell_id) else None,
        "line": rd(coords),
        "proposedChevron": proposed_chevron,
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]],
        "segments": len(order),
    }


def single_entry(row):
    line = rd(line_latlngs(row.geometry))
    lats = [p[0] for p in line]
    lons = [p[1] for p in line]
    street_name = row.full_name if pd.notna(row.full_name) else "(unnamed)"
    return {
        "street": street_name,
        "crossStart": strip_self_reference(row.cross_street_start, [street_name]) if pd.notna(row.cross_street_start) else None,
        "crossEnd": strip_self_reference(row.cross_street_end, [street_name]) if pd.notna(row.cross_street_end) else None,
        "ward": int(row.ward) if pd.notna(row.ward) else None,
        "cellId": int(row.cell_id) if pd.notna(row.cell_id) else None,
        "line": line,
        "proposedChevron": rd(chevron_latlngs(row.geometry, 0.5, forward=(row.dir_travel == "T"))),
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]],
        "segments": 1,
    }


print("Building the changes list (merging touching segments, any street name or "
      "cell, into one change - cell is a review-workflow grouping, not a real "
      "boundary a rider or reviewer of this public site should see reflected "
      "in where one change stops and the next starts)...")
changes_src = segments[segments["final_action"].isin(["reversal", "contraflow"])].copy()

CHANGES = []
for action, grp in changes_src.groupby("final_action", dropna=False):
    rows = list(grp.itertuples())
    dsu = unify_nearby_nodes(rows)
    unify = lambda row, dsu=dsu: (dsu.find(row.fnode_id), dsu.find(row.tnode_id))
    for order in split_into_chains(rows, unify):
        merged = merge_group(order)
        if merged:
            merged["action"] = action
            CHANGES.append(merged)
        else:
            for row, _ in order:
                entry = single_entry(row)
                entry["action"] = action
                CHANGES.append(entry)

CHANGES.sort(key=lambda c: (c["ward"] if c["ward"] is not None else 999, c["street"], c["line"][0]))

n_merged = sum(1 for c in CHANGES if c["segments"] > 1)
print(f"  {len(CHANGES)} changes ({sum(1 for c in CHANGES if c['action']=='reversal')} reversal, "
      f"{sum(1 for c in CHANGES if c['action']=='contraflow')} contraflow), "
      f"{n_merged} of which are merged multi-block runs")

print("Building context layers (arterial/collector, bike routes, parks, control points, wards)...")
# Simplified for payload size - these are background reference lines, not
# the focus content (that's `changes`, kept at full precision) - a ~5.5m
# tolerance is invisible at any zoom level a viewer would actually use.
SIMPLIFY_TOL = 0.00005
BOUNDING_ART = [
    {"line": rd(line_latlngs(row.geometry.simplify(SIMPLIFY_TOL))),
     "name": row.full_name if pd.notna(row.full_name) else None}
    for row in bounding[bounding.class_code == "ART"].itertuples()
]
BOUNDING_COL = [
    {"line": rd(line_latlngs(row.geometry.simplify(SIMPLIFY_TOL))),
     "name": row.full_name if pd.notna(row.full_name) else None}
    for row in bounding[bounding.class_code == "COL"].itertuples()
]
print(f"Loading CDOT bike routes directly from {BIKE_ROUTES_PATH.name} (facility type + "
      f"contraflow flag, independent of the has_bike segment join so facilities on "
      f"arterials/collectors - which never get a has_bike flag - aren't dropped)...")
BIKE_TYPE_CODE = {
    "Protected Bike Lane": "protected",
    "Buffered Bike Lane": "buffered",
    "Bike Lane": "lane",
    "Marked Shared Lane": "shared",
    "Neighborhood Greenway": "greenway",
}
bike_gdf = gpd.read_file(BIKE_ROUTES_PATH)
bike_gdf = bike_gdf[bike_gdf.intersects(region_extent)]
BIKE_ROUTES = []
for row in bike_gdf.itertuples():
    geom = row.geometry
    parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
    type_code = BIKE_TYPE_CODE.get(row.displayrou, "lane")
    for part in parts:
        BIKE_ROUTES.append({
            "line": rd(line_latlngs(part)),
            "type": type_code,
            "contraflow": row.contraflow == "Y",
        })
print(f"  {len(BIKE_ROUTES)} bike route line segments in region")
PARKS = [
    rd(part) for row in green[green.geometry.geom_type == "Polygon"].itertuples()
    for part in polygon_parts(row.geometry)
]

SIGNALS = [[round(r.y, ROUND), round(r.x, ROUND)] for r in node_points_df[node_points_df.control == "signal"].itertuples()]
STOP_ALL = [[round(r.y, ROUND), round(r.x, ROUND)] for r in node_points_df[node_points_df.control == "stop_all"].itertuples()]
STOP_OTHER = [[round(r.y, ROUND), round(r.x, ROUND)] for r in node_points_df[node_points_df.control == "stop_other"].itertuples()]

WARDS = {}
WARD_SIMPLIFY_TOL = 0.0002  # ~22m - a background reference outline, not a precise legal boundary
for row in wards.itertuples():
    b = row.geometry.bounds
    simplified = row.geometry.simplify(WARD_SIMPLIFY_TOL, preserve_topology=True)
    WARDS[int(row.ward)] = {
        "boundary": [rd(part) for part in polygon_parts(simplified)],
        "bounds": [[b[1], b[0]], [b[3], b[2]]],
        "label": [round(row.geometry.centroid.y, ROUND), round(row.geometry.centroid.x, ROUND)],
    }

META = {
    "center": [round(CENTER[0], ROUND), round(CENTER[1], ROUND)],
    "bounds": OVERALL_BOUNDS,
    "totalChanges": len(CHANGES),
    "totalReversal": sum(1 for c in CHANGES if c["action"] == "reversal"),
    "totalContraflow": sum(1 for c in CHANGES if c["action"] == "contraflow"),
}

print("Writing data.js...")
payload = {
    "meta": META,
    "changes": CHANGES,
    "baseArrows": BASE_ARROWS,
    "boundingArt": BOUNDING_ART,
    "boundingCol": BOUNDING_COL,
    "bikeRoutes": BIKE_ROUTES,
    "parks": PARKS,
    "signals": SIGNALS,
    "stopAll": STOP_ALL,
    "stopOther": STOP_OTHER,
    "wards": WARDS,
}
with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("const DATA = ")
    json.dump(payload, f, separators=(",", ":"))
    f.write(";\n")

size_mb = OUT_PATH.stat().st_size / 1_000_000
print(f"Done. {OUT_PATH} ({size_mb:.2f} MB)")

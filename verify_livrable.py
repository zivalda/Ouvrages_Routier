"""
Verification du livrable operationnel, ligne par ligne.

Pour chaque section : profils MNT tous les 10 m, etiquetes avec le PR+abs
du livrable (pas la distance le long de la ligne, qui decale les anciens PNG).

On separe volontairement :
  - morphologie des talus (ce que l'oeil voit : pente qui descend / monte)
  - classe centre vs TN (regression qui relie les deux bouts, souvent fausse
    des que les deux cotes n'ont pas la meme altitude)

Usage :
  python verify_livrable.py --line 13
  python verify_livrable.py --all
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import time
from collections import Counter
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote, unquote

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import LineString, Point

STEP_M = 10
HALF_WIDTH = 90  # assez large pour un talus au-dela d'une route laterale
PLAT_MAX_HALF = 22  # plateforme A31 bornee a +/- 22 m du centre
FOSSE_MAX_M = 18  # terre-plein / petit escalier entre deux chaussees
DZ_ROAD = 0.70
PLATFORM_HOLE_M = 3  # bruit / accotement, pas une fin de plateforme
CENTRE_STAIR_MAX_DZ = 4.5  # denivelle max entre les deux chaussees (PR259 ~3 m)
CENTRE_STAIR_MAX_M = 20  # largeur du talus interne / terre-plein
SMOOTH_M = 5
SLOPE_WIN = 3
SLOPE_TALUS = 0.10  # 10 % : un talus doux compte encore ; 15 % s'arretait trop tot
TALUS_MAX_M = 42
TALUS_GENTLE_M = 10
TALUS_FLAT_N = 8
TALUS_LOOKAHEAD = 14  # oscillation / banc : la pente reprend juste apres
TALUS_FOOT_M = 6  # metres externes = TN pour la hauteur lue sur l'axe
VERSANT_MIN_SLOPE = 0.05  # 5 % : en dessous, le lointain est du TN plat, pas un coteau
VERSANT_RUPTURE = 0.08  # le talus doit etre nettement plus raide que le versant
SLOPE_CONTINUE = 0.04  # 4 % : on prolonge le talus tant que ca pente
SLOPE_OET_H = 1.0  # H17 : sur un TN deja en pente, un OET leger compte des 1 m
SIDE_ROAD_MIN_M = 10  # largeur min d'une route laterale (pas A31)
SIDE_ROAD_FLAT = 0.05
SIDE_CONNECTOR_MAX_M = 16
SIDE_ROAD_MAX_M = 40
OUTER_FIT_START = 14
OUTER_FIT_END = 38
CENTRE_RASANT = 2.0
TALUS_REMBLAI = 2.0
MIXTE_THRESHOLD = 2.0
TN_ECART_DOUTEUX = 4.0

COLORS = {
    "remblai": "#C6EFCE",
    "deblai": "#BDD7EE",
    "rasant": "#FFF2CC",
    "mixte": "#FFC7CE",
    "mixte_faible": "#F8CBAD",
    "unilateral_remblai": "#E2EFDA",
    "unilateral_deblai": "#DDEBF7",
    "plat": "#FFF2CC",
    "pont": "#E2D5F1",
    "mur de soutènement": "#D9D9D9",
    "indetermine": "#F2F2F2",
}


def _col(df, *names):
    for n in names:
        if n in df.columns:
            return n
    # colonnes parfois mojibake selon l'encodage Excel
    lower = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    raise KeyError(names)


def load_inventory(path):
    df = pd.read_excel(path)
    c_nom = _col(df, "Tronçon", "Troncon")
    c_prd = _col(df, "PR début", "PR debut")
    c_absd = _col(df, "Abs début", "Abs debut")
    c_prf = _col(df, "PR fin")
    c_absf = _col(df, "Abs fin")
    c_len = _col(df, "Longueur (m)")
    c_cl = _col(df, "Classification")
    out = pd.DataFrame({
        "nom": df[c_nom].astype(str),
        "PR_start": pd.to_numeric(df[c_prd], errors="coerce").astype(int),
        "abcisse_start": pd.to_numeric(df[c_absd], errors="coerce").astype(int),
        "PR_end": pd.to_numeric(df[c_prf], errors="coerce").astype(int),
        "abcisse_end": pd.to_numeric(df[c_absf], errors="coerce").astype(int),
        "length": pd.to_numeric(df[c_len], errors="coerce").astype(int),
        "classification": df[c_cl].astype(str).str.strip().str.lower(),
    })
    if "Mur soutènement" in df.columns:
        out["mur"] = df["Mur soutènement"].astype(str)
    elif "Mur soutenement" in df.columns:
        out["mur"] = df["Mur soutenement"].astype(str)
    else:
        out["mur"] = "-"
    out["mur"] = (
        out["mur"].astype(str)
        .str.replace("\u2014", "-", regex=False)
        .str.replace("\u2013", "-", regex=False)
        .str.replace("nan", "-", regex=False)
    )
    out["classification"] = out["classification"].replace({"mur de soutènement": "mur de soutènement"})
    return out


class PrLocator:
    def __init__(self, pr_gdf):
        d = pr_gdf[pr_gdf["cote"].astype(str).str.upper().str.startswith("D")].copy()
        d["numero"] = d["numero"].astype(int)
        self.rows = d.sort_values("numero")
        self.by_num = {int(r.numero): float(r.distance_route) for r in self.rows.itertuples()}
        self.nums = sorted(self.by_num)

    def cl_distance(self, pr, ab):
        pr, ab = int(pr), int(ab)
        if pr in self.by_num:
            return self.by_num[pr] + ab
        below = [n for n in self.nums if n <= pr]
        if not below:
            return float(ab)
        n0 = below[-1]
        return self.by_num[n0] + (pr - n0) * 1000 + ab

    def label(self, cl_dist):
        below = [n for n in self.nums if self.by_num[n] <= cl_dist]
        if not below:
            n0 = self.nums[0]
            return n0, max(0, int(round(cl_dist - self.by_num[n0])))
        n0 = below[-1]
        return n0, int(round(cl_dist - self.by_num[n0]))


class CompoundAxis:
    """Un lot = une ligne centrale + un MNT. Evite de recoudre des trous de 100 m."""

    def __init__(self, gdf):
        self.lots = []
        gdf = gdf.copy()
        if "lot" in gdf.columns:
            gdf = gdf.sort_values("lot")
        for _, r in gdf.iterrows():
            geom = r.geometry
            if geom.geom_type == "MultiLineString":
                geom = max(geom.geoms, key=lambda g: g.length)
            self.lots.append({
                "n": int(r["lot"]),
                "cl": geom,
                "offset": float(r.get("offset", 0) or 0),
                "d_pr_start": float(r["d_pr_start"]),
                "pr_anchor": int(r["pr_anchor"]),
            })

    def locate(self, pr, ab):
        pr, ab = int(pr), int(ab)
        interior = []
        near = []
        for lot in self.lots:
            d_local = lot["d_pr_start"] + (pr - lot["pr_anchor"]) * 1000 + ab
            length = lot["cl"].length
            if 0 <= d_local <= length:
                interior.append((lot["cl"], d_local, lot))
            elif -80 <= d_local <= length + 80:
                clamped = max(0.0, min(d_local, length))
                near.append((abs(d_local - clamped), lot["cl"], clamped, lot))
        if interior:
            return interior[0][0], interior[0][1]
        if near:
            near.sort()
            return near[0][1], near[0][2]
        lot = self.lots[-1]
        d_local = lot["d_pr_start"] + (pr - lot["pr_anchor"]) * 1000 + ab
        return lot["cl"], max(0.0, min(d_local, lot["cl"].length))

    def substring_pr(self, pr_s, ab_s, pr_e, ab_e, length=1):
        pos_s = int(pr_s) * 1000 + int(ab_s)
        pos_e = int(pr_e) * 1000 + int(ab_e)
        if pos_e <= pos_s:
            pos_e = pos_s + max(int(length or 1), 1)
        pts = []
        for pos in range(pos_s, pos_e + 1, 10):
            cl, d = self.locate(pos // 1000, pos % 1000)
            p = cl.interpolate(min(max(d, 0.0), cl.length))
            pts.append((p.x, p.y))
        if len(pts) < 2:
            cl, d = self.locate(pr_s, ab_s)
            p = cl.interpolate(min(max(d, 0.0), cl.length))
            return LineString([(p.x, p.y), (p.x + 0.1, p.y)])
        return LineString(pts)


def _expand_mnt_paths(mnt_path):
    p = Path(mnt_path)
    if p.is_dir():
        return [str(x) for x in sorted(p.glob("mnt*.tif")) if not x.name.endswith(".xml")]
    if p.suffix.lower() == ".txt":
        return [
            ln.strip()
            for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    return [str(p)]


class MntSampler:
    """Un GeoTIFF, un VRT, un .txt (liste de tuiles) ou un dossier de mnt*.tif."""

    _MAX_BAND_BYTES = 400_000_000

    def __init__(self, mnt_path):
        paths = _expand_mnt_paths(mnt_path)
        if not paths:
            raise FileNotFoundError(mnt_path)
        self.srcs = [rasterio.open(p) for p in paths]
        self.src = self.srcs[0]
        self.nodata = self.src.nodata
        self.transform = self.src.transform
        self.band = None
        if len(self.srcs) == 1:
            src = self.src
            nbytes = int(src.width) * int(src.height) * np.dtype(src.dtypes[0]).itemsize
            if nbytes <= self._MAX_BAND_BYTES:
                self.band = src.read(1)

    def _src_at(self, x, y):
        for src in self.srcs:
            b = src.bounds
            if b.left <= x <= b.right and b.bottom <= y <= b.top:
                return src
        return None

    def _z_band(self, xs, ys):
        rows, cols = rasterio.transform.rowcol(self.transform, xs, ys)
        rows = np.asarray(rows, dtype=int)
        cols = np.asarray(cols, dtype=int)
        zs = np.full(xs.shape, np.nan, dtype=float)
        valid = (rows >= 0) & (cols >= 0) & (rows < self.band.shape[0]) & (cols < self.band.shape[1])
        zs[valid] = self.band[rows[valid], cols[valid]]
        if self.nodata is not None:
            zs[zs == self.nodata] = np.nan
        zs[zs < -1000] = np.nan
        return zs

    def _z_window(self, src, xs, ys):
        zs = np.full(xs.shape, np.nan, dtype=float)
        if xs.size == 0:
            return zs
        rows, cols = rasterio.transform.rowcol(src.transform, xs, ys)
        rows = np.asarray(rows, dtype=int)
        cols = np.asarray(cols, dtype=int)
        valid = (rows >= 0) & (cols >= 0) & (rows < src.height) & (cols < src.width)
        if not valid.any():
            return zs
        r0, r1 = int(rows[valid].min()), int(rows[valid].max()) + 1
        c0, c1 = int(cols[valid].min()), int(cols[valid].max()) + 1
        band = src.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0))
        vals = band[rows[valid] - r0, cols[valid] - c0].astype(float)
        if src.nodata is not None:
            vals[vals == src.nodata] = np.nan
        vals[vals < -1000] = np.nan
        zs[valid] = vals
        return zs

    def z_many(self, xs, ys):
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        if self.band is not None:
            return self._z_band(xs, ys)
        zs = np.full(xs.shape, np.nan, dtype=float)
        if xs.size == 0:
            return zs
        filled = np.zeros(xs.shape, dtype=bool)
        srcs = list(self.srcs)
        mid = int(xs.size // 2)
        hit = self._src_at(float(xs.ravel()[mid]), float(ys.ravel()[mid]))
        if hit is not None:
            srcs = [hit] + [s for s in srcs if s is not hit]
        for src in srcs:
            if filled.all():
                break
            todo = ~filled
            part = self._z_window(src, xs[todo], ys[todo])
            zs[todo] = np.where(np.isfinite(part), part, zs[todo])
            filled = np.isfinite(zs)
        return zs

    def close(self):
        for src in self.srcs:
            src.close()


def perpendicular(centerline, cl_dist):
    p = centerline.interpolate(cl_dist)
    if cl_dist <= 15:
        q = centerline.interpolate(min(cl_dist + 10, centerline.length))
        angle = math.atan2(q.y - p.y, q.x - p.x)
    else:
        q = centerline.interpolate(max(cl_dist - 10, 0))
        angle = math.atan2(p.y - q.y, p.x - q.x)
    dx = HALF_WIDTH * math.cos(angle + math.pi / 2)
    dy = HALF_WIDTH * math.sin(angle + math.pi / 2)
    return p, LineString([(p.x - dx, p.y - dy), (p.x + dx, p.y + dy)])


def sample_profile(mnt, perp, center_pt):
    (x0, y0), (x1, y1) = list(perp.coords)
    n = int(round(perp.length)) + 1
    dists = np.linspace(0, perp.length, n)
    xs = np.linspace(x0, x1, n)
    ys = np.linspace(y0, y1, n)
    zs = mnt.z_many(xs, ys)
    d_center = float(perp.project(center_pt))
    return dists, zs, d_center


def _med(zs):
    v = zs[np.isfinite(zs)]
    if len(v) == 0:
        return np.nan
    return float(np.median(v))


def _band(dists, zs, a, b):
    lo, hi = (a, b) if a <= b else (b, a)
    return zs[(dists >= lo) & (dists <= hi)]


def _meter_grid(dists, zs):
    n = int(round(float(dists[-1]))) + 1
    xs = np.arange(n, dtype=float)
    ez = np.interp(xs, dists, zs)
    return xs, ez


def _smooth_ez(ez, win=SMOOTH_M):
    """Medianne glissante : un pic MNT de 1 m ne coupe plus la plateforme / le talus."""
    if win < 3 or len(ez) < 3:
        return np.asarray(ez, dtype=float)
    return (
        pd.Series(ez, dtype=float)
        .rolling(win, center=True, min_periods=1)
        .median()
        .to_numpy()
    )


def _close_holes(mask, max_hole):
    n = len(mask)
    out = mask.copy()
    i = 0
    while i < n:
        if out[i]:
            i += 1
            continue
        j = i
        while j < n and not out[j]:
            j += 1
        if i > 0 and j < n and out[i - 1] and out[j] and (j - i) <= max_hole:
            out[i:j] = True
        i = j
    return out


def _dz_leave(ez, j, direction, win=SLOPE_WIN):
    """Pente en s'eloignant de la route, m/m. Positif = on monte."""
    prev = j - direction * win
    n = len(ez)
    if j < 0 or j >= n or prev < 0 or prev >= n:
        return np.nan
    if not (np.isfinite(ez[j]) and np.isfinite(ez[prev])):
        return np.nan
    return float(ez[j] - ez[prev]) / float(win)


def _slope_leaving(ez, edge, direction, d0, d1):
    """Pente m/m en s'eloignant de la route, entre d0 et d1 metres du bord."""
    n = len(ez)
    j0 = int(edge + direction * d0)
    j1 = int(edge + direction * d1)
    if j0 < 0 or j0 >= n or j1 < 0 or j1 >= n:
        return np.nan
    if not (np.isfinite(ez[j0]) and np.isfinite(ez[j1])) or d1 == d0:
        return np.nan
    return float(ez[j1] - ez[j0]) / float(d1 - d0)


def _outer_line(ez, edge, direction, d0=OUTER_FIT_START, d1=OUTER_FIT_END):
    """Droite du terrain loin de la route (versant ou TN), z au bord = extrapolation."""
    n = len(ez)
    xs, zs = [], []
    for d in range(int(d0), int(d1) + 1):
        j = int(edge + direction * d)
        if j < 0 or j >= n:
            break
        if np.isfinite(ez[j]):
            xs.append(float(d))
            zs.append(float(ez[j]))
    if len(xs) < 8:
        return np.nan, np.nan, np.nan
    x = np.asarray(xs)
    y = np.asarray(zs)
    coef = np.polyfit(x, y, 1)
    pred = np.polyval(coef, x)
    ss_res = np.sum((y - pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 1e-6 else np.nan
    z_at_edge = float(np.polyval(coef, 0.0))
    return float(coef[0]), z_at_edge, r2


def _slope_resumes(ez, j, direction, sign_ref, start, max_m=TALUS_MAX_M):
    """La pente du talus reprend-elle dans les TALUS_LOOKAHEAD m (bruit ou banc) ?"""
    n = len(ez)
    for la in range(1, TALUS_LOOKAHEAD + 1):
        k = j + direction * la
        if k < 0 or k >= n or abs(k - start) > max_m:
            return False
        dz = _dz_leave(ez, k, direction)
        if not np.isfinite(dz) or abs(dz) <= SLOPE_TALUS:
            continue
        if sign_ref == 0 or np.sign(dz) == sign_ref:
            return True
    return False


def detect_platform(ez, d_center):
    """Plateforme autour du centre (~60 m), bornee vers 40-80 m.

    Cas frequent (PR259) : deux chaussees de part et d'autre de 60, avec un
    petit talus interne / escalier vers le centre. Ce talus n'est PAS un OET.
    La plateforme englobe les deux voies ; les talus comptes sont les bords
    externes. Chaque voie a son altitude (souvent 1 a 3 m d'ecart).
    """
    n = len(ez)
    c = int(round(np.clip(d_center, 0, n - 1)))
    plat_lo = max(0, c - PLAT_MAX_HALF)
    plat_hi = min(n - 1, c + PLAT_MAX_HALF)

    def _med_rng(a, b):
        a, b = max(0, a), min(n, b)
        sl = ez[a:b]
        sl = sl[np.isfinite(sl)]
        return float(np.median(sl)) if len(sl) else np.nan

    z_near = _med_rng(c - 6, c + 7)
    z_l_side = _med_rng(c - 12, c - 2)
    z_r_side = _med_rng(c + 3, c + 13)
    z_c = float(ez[c]) if np.isfinite(ez[c]) else z_near
    if (
        np.isfinite(z_c) and np.isfinite(z_l_side) and np.isfinite(z_r_side)
        and z_c < min(z_l_side, z_r_side) - 0.35
    ):
        z_road = float(0.5 * (z_l_side + z_r_side))
    else:
        z_road = z_near if np.isfinite(z_near) else z_c

    in_band = np.zeros(n, dtype=bool)
    if np.isfinite(z_road):
        for i in range(plat_lo, plat_hi + 1):
            if np.isfinite(ez[i]) and abs(float(ez[i]) - z_road) <= DZ_ROAD:
                in_band[i] = True
    in_band = _close_holes(in_band, PLATFORM_HOLE_M)

    s_band = e_band = c
    i = c
    while i - 1 >= plat_lo and in_band[i - 1]:
        i -= 1
        s_band = i
    i = c
    while i + 1 <= plat_hi and in_band[i + 1]:
        i += 1
        e_band = i
    if not in_band[c]:
        s_band = e_band = c
        i = c
        while i - 1 >= plat_lo:
            i -= 1
            if in_band[i]:
                s_band = i
            else:
                break
        i = c
        while i + 1 <= plat_hi:
            i += 1
            if in_band[i]:
                e_band = i
            else:
                break

    flat = np.zeros(n, dtype=bool)
    for i in range(max(1, plat_lo), min(n - 1, plat_hi) + 1):
        if not (np.isfinite(ez[i - 1]) and np.isfinite(ez[i]) and np.isfinite(ez[i + 1])):
            continue
        if abs(ez[i] - ez[i - 1]) <= SLOPE_TALUS and abs(ez[i + 1] - ez[i]) <= SLOPE_TALUS:
            flat[i] = True

    clusters = []
    i = plat_lo
    while i <= plat_hi:
        if not flat[i]:
            i += 1
            continue
        j = i
        while j <= plat_hi and flat[j]:
            j += 1
        if j - i >= 6:
            zmed = float(np.nanmedian(ez[i:j]))
            clusters.append((i, j - 1, zmed))
        i = j

    plat_s, plat_e = s_band, e_band
    z_left_plat = z_right_plat = z_road if np.isfinite(z_road) else z_c
    dual = False

    lefts = [cl for cl in clusters if 0.5 * (cl[0] + cl[1]) < c]
    rights = [cl for cl in clusters if 0.5 * (cl[0] + cl[1]) > c]
    if lefts and rights:
        left_cl = max(lefts, key=lambda cl: cl[1])
        right_cl = min(rights, key=lambda cl: cl[0])
        gap_a, gap_b = left_cl[1] + 1, right_cl[0] - 1
        gap_len = 0 if gap_b < gap_a else gap_b - gap_a + 1
        gap_mid = 0.5 * (left_cl[1] + right_cl[0])
        dz_pair = abs(left_cl[2] - right_cl[2])
        if gap_len <= CENTRE_STAIR_MAX_M and abs(gap_mid - c) <= 12 and dz_pair <= CENTRE_STAIR_MAX_DZ:
            if gap_len <= 0:
                zgap, zbump = z_c, z_c
            else:
                sl = ez[gap_a:gap_b + 1]
                zgap = float(np.nanmin(sl)) if np.isfinite(sl).any() else z_c
                zbump = float(np.nanmax(sl)) if np.isfinite(sl).any() else z_c
            z_hi = max(left_cl[2], right_cl[2])
            if zbump <= z_hi + 0.8:
                dual = True
                plat_s, plat_e = left_cl[0], right_cl[1]
                z_left_plat, z_right_plat = left_cl[2], right_cl[2]
                i = plat_s
                while i - 1 >= plat_lo and np.isfinite(ez[i - 1]) and abs(ez[i - 1] - z_left_plat) <= DZ_ROAD:
                    i -= 1
                    plat_s = i
                i = plat_e
                while i + 1 <= plat_hi and np.isfinite(ez[i + 1]) and abs(ez[i + 1] - z_right_plat) <= DZ_ROAD:
                    i += 1
                    plat_e = i

    if clusters and not dual:
        def dist_to_c(cl):
            a, b, _ = cl
            if a <= c <= b:
                return 0
            return min(abs(a - c), abs(b - c))

        seed = min(range(len(clusters)), key=lambda k: dist_to_c(clusters[k]))
        used = {seed}

        def span():
            return min(clusters[k][0] for k in used), max(clusters[k][1] for k in used)

        changed = True
        while changed:
            changed = False
            s0, e0 = span()
            for k, (a, b, z) in enumerate(clusters):
                if k in used:
                    continue
                if b < s0:
                    gap_a, gap_b = b + 1, s0 - 1
                elif a > e0:
                    gap_a, gap_b = e0 + 1, a - 1
                else:
                    used.add(k)
                    changed = True
                    continue
                if gap_b < gap_a or gap_b - gap_a + 1 > FOSSE_MAX_M:
                    continue
                if abs(0.5 * (gap_a + gap_b) - c) > 12:
                    continue
                zgap = float(np.nanmin(ez[gap_a:gap_b + 1]))
                zhigh = max(clusters[seed][2], z)
                if not np.isfinite(zgap) or zgap > zhigh - DZ_ROAD:
                    continue
                seed_mid = 0.5 * (clusters[seed][0] + clusters[seed][1])
                k_mid = 0.5 * (a + b)
                same_side = (seed_mid - c) * (k_mid - c) > 0
                if same_side and abs(z - clusters[seed][2]) > 1.5:
                    continue
                used.add(k)
                changed = True
        s_cl, e_cl = span()
        plat_s = min(plat_s, s_cl)
        plat_e = max(plat_e, e_cl)
        lefts2 = [clusters[k] for k in used if 0.5 * (clusters[k][0] + clusters[k][1]) < c]
        rights2 = [clusters[k] for k in used if 0.5 * (clusters[k][0] + clusters[k][1]) > c]
        if lefts2:
            z_left_plat = lefts2[-1][2] if len(lefts2) == 1 else max(lefts2, key=lambda cl: cl[1])[2]
        if rights2:
            z_right_plat = rights2[0][2] if len(rights2) == 1 else min(rights2, key=lambda cl: cl[0])[2]

    plat_s = max(plat_s, plat_lo)
    plat_e = min(plat_e, plat_hi)
    return plat_s, plat_e, c, z_road, z_left_plat, z_right_plat, dual


def talus_glued(ez, edge, direction, z_plat, s_far=np.nan):
    """Talus colle au bord de plateforme, puis TN plat ou saut de montagne.

    - On ignore les premiers metres encore a l'altitude plateforme.
    - Pente sur 3 m, pas 1 m (bruit).
    - Une pente douce (>= 4 %) compte des le depart (deblai leger sur versant).
    - On ne s'arrete pas a la premiere oscillation : s'il reste une pente
      du meme signe dans les 14 m, on continue (banc / route en escalier).
    - Pied = aplatissement durable, ou retour a la pente du versant, ou fin.
    - Hauteur = denivele plateforme vs TN au pied (lus sur l'axe Y).
    """
    n = len(ez)
    edge = int(np.clip(edge, 0, n - 1))
    start = edge
    for step in range(0, 6):
        j = edge + direction * step
        if j < 0 or j >= n:
            break
        if np.isfinite(ez[j]) and np.isfinite(z_plat) and abs(float(ez[j]) - z_plat) <= DZ_ROAD:
            start = j
        elif step > 0:
            break

    foot = start
    saw_slope = False
    had_steep = False
    sign_ref = 0
    flat_run = 0
    for step in range(1, TALUS_MAX_M + 1):
        j = start + direction * step
        if j < 0 or j >= n:
            break
        if abs(j - edge) > TALUS_MAX_M:
            break
        dz = _dz_leave(ez, j, direction)
        steep = np.isfinite(dz) and abs(dz) > SLOPE_TALUS
        same = sign_ref == 0 or (np.isfinite(dz) and np.sign(dz) == sign_ref)
        gentle = np.isfinite(dz) and abs(dz) > SLOPE_CONTINUE and same
        if steep and (sign_ref == 0 or same or not saw_slope):
            if sign_ref == 0:
                sign_ref = int(np.sign(dz))
            saw_slope = True
            had_steep = True
            foot = j
            flat_run = 0
            continue
        if not saw_slope and gentle:
            if sign_ref == 0:
                sign_ref = int(np.sign(dz))
            saw_slope = True
            foot = j
            flat_run = 0
            continue
        if saw_slope and gentle:
            if (
                had_steep
                and np.isfinite(s_far)
                and abs(s_far) >= VERSANT_MIN_SLOPE
                and np.sign(dz) == np.sign(s_far)
                and abs(dz) <= abs(s_far) + 0.03
            ):
                break
            foot = j
            flat_run = 0
            continue
        if saw_slope:
            if _slope_resumes(ez, j, direction, sign_ref, start):
                foot = j
                flat_run = 0
                continue
            flat_run += 1
            if flat_run >= TALUS_FLAT_N:
                break
            continue
        foot = j
        if step >= TALUS_GENTLE_M:
            break

    lo, hi = (foot, start) if direction < 0 else (start, foot)
    if hi < lo:
        lo, hi = hi, lo
    lo_i, hi_i = max(0, int(lo)), min(n - 1, int(hi))
    outer_a = int(foot)
    outer_b = int(foot - direction * (TALUS_FOOT_M - 1))
    a, b = min(outer_a, outer_b), max(outer_a, outer_b)
    a, b = max(0, a), min(n - 1, b)
    outer = ez[a:b + 1]
    outer = outer[np.isfinite(outer)]
    if len(outer):
        z_foot = float(np.nanmedian(outer))
    else:
        aa = max(0, int(foot) - 1)
        bb = min(n - 1, int(foot) + 1)
        z_foot = _med(ez[min(aa, bb):max(aa, bb) + 1])
    dh = float(z_plat - z_foot) if np.isfinite(z_foot) and np.isfinite(z_plat) else np.nan
    return dh, float(lo_i), float(hi_i), int(sign_ref)


def parallel_road_span(ez, edge, direction):
    """Route laterale juste hors A31 : on ne la prend pas pour le talus.

    Schema type PR 272+985 : plateforme A31, petite pente, autre route
    (10-40 m, presque plate), puis le vrai talus d'OET (~ x=100).
    Si le plat au-dela de l'A31 est le TN (pas de pente ensuite), on ignore.
    """
    n = len(ez)
    edge = int(np.clip(edge, 0, n - 1))
    connector_end = None
    for step in range(1, SIDE_CONNECTOR_MAX_M + 1):
        j = edge + direction * step
        if j < 0 or j >= n:
            return None
        dz = _dz_leave(ez, j, direction)
        if np.isfinite(dz) and abs(dz) > SIDE_ROAD_FLAT:
            connector_end = j
            continue
        if connector_end is not None:
            break
    if connector_end is None:
        return None
    inner = connector_end + direction
    if inner < 0 or inner >= n:
        return None
    outer = inner
    for step in range(0, SIDE_ROAD_MAX_M):
        j = inner + direction * step
        if j < 0 or j >= n:
            break
        dz = _dz_leave(ez, j, direction)
        if not np.isfinite(dz) or abs(dz) <= SIDE_ROAD_FLAT:
            outer = j
            continue
        break
    if abs(outer - inner) + 1 < SIDE_ROAD_MIN_M:
        return None
    s_after = _slope_leaving(ez, outer, direction, 1, 8)
    if not np.isfinite(s_after) or abs(s_after) < SLOPE_CONTINUE:
        return None
    lo, hi = (outer, inner) if direction < 0 else (inner, outer)
    return int(min(lo, hi)), int(max(lo, hi))


def _clip_span(edge, direction, n, width=12):
    j_edge = int(np.clip(edge, 0, n - 1))
    j_foot = int(np.clip(edge + direction * width, 0, n - 1))
    lo, hi = (j_foot, j_edge) if direction < 0 else (j_edge, j_foot)
    return float(min(lo, hi)), float(max(lo, hi))


def talus_or_versant(ez, edge, direction, z_plat):
    """Talus = rupture colle au bord. Versant = pente du pays sans ouvrage.

    Hauteur = denivele plateforme vs TN au pied (ce qu'on lit sur l'axe Y).
    H17 : si le TN lateral est deja en pente sans rupture, un leger remblai
    ou deblai (des 1 m vs le versant) reste un OET.
    """
    s_near = _slope_leaving(ez, edge, direction, 2, 8)
    s_far = _slope_leaving(ez, edge, direction, 16, 32)
    dh, lo, hi, sign_ref = talus_glued(ez, edge, direction, z_plat, s_far)
    slope_o, z_tn_edge, r2 = _outer_line(ez, edge, direction)
    far_slope = s_far if np.isfinite(s_far) else slope_o
    far_is_versant = np.isfinite(far_slope) and abs(far_slope) >= VERSANT_MIN_SLOPE
    same_sign = (
        np.isfinite(s_near) and np.isfinite(far_slope)
        and s_near != 0 and far_slope != 0
        and np.sign(s_near) == np.sign(far_slope)
    )
    no_rupture = same_sign and abs(s_near) <= abs(far_slope) + VERSANT_RUPTURE
    fit_ok = np.isfinite(r2) and r2 >= 0.65 and np.isfinite(z_tn_edge) and np.isfinite(z_plat)
    residual = float(z_plat - z_tn_edge) if fit_ok else np.nan
    visual = dh if np.isfinite(dh) else np.nan

    if far_is_versant and no_rupture:
        # Pas de vrai talus : la pente du coteau continue. Hauteur = ecart
        # plateforme vs ce versant (pas le denivele jusqu'au bout du profil).
        n = len(ez)
        if np.isfinite(residual) and abs(residual) >= SLOPE_OET_H:
            lo2, hi2 = _clip_span(edge, direction, n, 12)
            return residual, lo2, hi2, False, s_far
        if np.isfinite(visual) and abs(visual) >= SLOPE_OET_H and abs(visual) < 3.0:
            return visual, lo, hi, False, s_far
        j = int(np.clip(edge, 0, n - 1))
        return 0.0, float(j), float(j), True, s_far

    h_use = visual if np.isfinite(visual) else residual
    if not np.isfinite(h_use):
        h_use = 0.0
    return h_use, lo, hi, False, s_far


def analyze_profile(dists, zs, d_center):
    """Centre = milieu de plateforme. Fosse interne inclus, talus = bords externes."""
    xs, ez = _meter_grid(dists, zs)
    ez = _smooth_ez(ez)
    c = int(round(np.clip(d_center, 0, len(ez) - 1)))
    plat_s, plat_e, _, z_road, z_left_plat, z_right_plat, dual = detect_platform(ez, c)
    inner = ez[plat_s:plat_e + 1]
    if dual and np.isfinite(z_left_plat) and np.isfinite(z_right_plat):
        z_plat = float(0.5 * (z_left_plat + z_right_plat))
    elif np.isfinite(z_road) and len(inner):
        band = inner[np.isfinite(inner) & (np.abs(inner - z_road) <= DZ_ROAD)]
        z_plat = _med(band) if len(band) else (_med(inner) if plat_e >= plat_s else np.nan)
    else:
        z_plat = _med(inner) if plat_e >= plat_s else (ez[c] if np.isfinite(ez[c]) else np.nan)
    if not np.isfinite(z_plat):
        z_plat = z_road if np.isfinite(z_road) else (ez[c] if np.isfinite(ez[c]) else np.nan)
    if not np.isfinite(z_left_plat):
        z_left_plat = z_plat
    if not np.isfinite(z_right_plat):
        z_right_plat = z_plat

    side_l = parallel_road_span(ez, plat_s, -1)
    side_r = parallel_road_span(ez, plat_e, +1)
    left_edge = side_l[0] if side_l else plat_s
    right_edge = side_r[1] if side_r else plat_e

    dh_left, left_lo, left_hi, versant_left, s_far_l = talus_or_versant(
        ez, left_edge, -1, z_left_plat,
    )
    dh_right, right_lo, right_hi, versant_right, s_far_r = talus_or_versant(
        ez, right_edge, +1, z_right_plat,
    )

    far_l = _med(_band(dists, zs, 0, min(12, float(dists[-1]))))
    far_r = _med(_band(dists, zs, max(0.0, float(dists[-1]) - 12), float(dists[-1])))
    far_mask = (dists <= 12) | (dists >= dists[-1] - 12)
    xd, yd = dists[far_mask], zs[far_mask]
    ok = np.isfinite(xd) & np.isfinite(yd)
    tn_slope = tn_z_c = r2 = np.nan
    if ok.sum() >= 4:
        x, y = xd[ok], yd[ok]
        coef = np.polyfit(x, y, 1)
        tn_slope = float(coef[0])
        pred = np.polyval(coef, x)
        ss_res = np.sum((y - pred) ** 2)
        ss_tot = np.sum((y - y.mean()) ** 2)
        r2 = float(1 - ss_res / ss_tot) if ss_tot > 1e-6 else np.nan
        tn_z_c = float(np.polyval(coef, d_center))
    h_centre = z_plat - tn_z_c if np.isfinite(tn_z_c) else np.nan
    tn_ecart = abs(far_l - far_r) if np.isfinite(far_l) and np.isfinite(far_r) else np.nan
    tn_douteux = bool(np.isfinite(tn_ecart) and tn_ecart >= TN_ECART_DOUTEUX)

    thr_l = SLOPE_OET_H if (np.isfinite(s_far_l) and abs(s_far_l) >= VERSANT_MIN_SLOPE) else MIXTE_THRESHOLD
    thr_r = SLOPE_OET_H if (np.isfinite(s_far_r) and abs(s_far_r) >= VERSANT_MIN_SLOPE) else MIXTE_THRESHOLD
    morph = morphology(dh_left, dh_right, (thr_l, thr_r))

    return {
        "z_plat": z_plat,
        "z_left_plat": float(z_left_plat) if np.isfinite(z_left_plat) else z_plat,
        "z_right_plat": float(z_right_plat) if np.isfinite(z_right_plat) else z_plat,
        "z_left": (z_left_plat - dh_left) if np.isfinite(dh_left) and np.isfinite(z_left_plat) else np.nan,
        "z_right": (z_right_plat - dh_right) if np.isfinite(dh_right) and np.isfinite(z_right_plat) else np.nan,
        "dh_left": dh_left,
        "dh_right": dh_right,
        "h_centre_tn": h_centre,
        "tn_slope": tn_slope,
        "tn_r2": r2,
        "tn_ecart": tn_ecart,
        "tn_douteux": tn_douteux,
        "second_left": False,
        "second_right": False,
        "n_chaussees": 2 if dual else 1,
        "morph": morph,
        "d_center": d_center,
        "plat_s": float(plat_s),
        "plat_e": float(plat_e),
        "side_left": side_l,
        "side_right": side_r,
        "left_lo": float(left_lo),
        "left_hi": float(left_hi),
        "right_lo": float(right_lo),
        "right_hi": float(right_hi),
        "versant_left": bool(versant_left),
        "versant_right": bool(versant_right),
        "s_far_left": float(s_far_l) if np.isfinite(s_far_l) else np.nan,
        "s_far_right": float(s_far_r) if np.isfinite(s_far_r) else np.nan,
        "tn_coef": (tn_slope, tn_z_c - tn_slope * d_center) if np.isfinite(tn_slope) and np.isfinite(tn_z_c) else None,
    }


def _side(h, thr):
    if not np.isfinite(h):
        return "plat"
    if h >= thr:
        return "remblai"
    if h <= -thr:
        return "deblai"
    return "plat"


def morphology(dh_left, dh_right, thr=None):
    """Meme seuils que le centre: +/- 2 m, ou +/- 1 m si le TN lateral pente (H17)."""
    if isinstance(thr, (tuple, list)) and len(thr) == 2:
        thr_l, thr_r = float(thr[0]), float(thr[1])
    elif thr is None:
        thr_l = thr_r = MIXTE_THRESHOLD
    else:
        thr_l = thr_r = float(thr)
    s_l = _side(dh_left, thr_l)
    s_r = _side(dh_right, thr_r)
    if {s_l, s_r} == {"remblai", "deblai"}:
        return "mixte"
    if s_l == s_r and s_l in ("remblai", "deblai"):
        return s_l
    if s_l in ("remblai", "deblai") and s_r == "plat":
        return "unilateral_" + s_l
    if s_r in ("remblai", "deblai") and s_l == "plat":
        return "unilateral_" + s_r
    return "plat"


def _draw_h_arrow(ax, x, z0, z1, dh, color):
    if not (np.isfinite(x) and np.isfinite(z0) and np.isfinite(z1) and np.isfinite(dh)):
        return
    if abs(dh) < 0.35:
        return
    ax.annotate(
        "", xy=(x, z1), xytext=(x, z0),
        arrowprops=dict(arrowstyle="<->", color=color, lw=1.15),
    )
    ax.text(
        x + 1.2, 0.5 * (z0 + z1), f"H={dh:+.1f} m",
        color=color, fontsize=8, va="center",
    )


def plot_profile(dists, zs, info, title, path):
    fig, ax = plt.subplots(figsize=(8.2, 3.6), dpi=90)
    xs, ez = _meter_grid(dists, zs)
    ez_s = _smooth_ez(ez)
    ax.plot(xs, ez_s, color="#333333", lw=1.6, label="MNT (lecture H)")
    ax.plot(dists, zs, color="#777777", lw=0.5, alpha=0.35, marker="o", ms=1.4)
    if info.get("tn_coef") is not None:
        a, b = info["tn_coef"]
        ax.plot(
            dists, a * dists + b, ls="--", color="#C00000", lw=1.2,
            label="Droite des extremites (versant)",
        )
    dc = info["d_center"]
    ax.axvspan(info["plat_s"], info["plat_e"], color="#9DC3E6", alpha=0.30, label="Plateforme A31")
    side_l = info.get("side_left")
    side_r = info.get("side_right")
    side_lbl = "Route laterale (pas A31)"
    if side_l:
        ax.axvspan(side_l[0], side_l[1], color="#7F8C8D", alpha=0.22, label=side_lbl)
        side_lbl = None
    if side_r:
        ax.axvspan(side_r[0], side_r[1], color="#7F8C8D", alpha=0.22, label=side_lbl)
    if not info.get("versant_left") and info["left_hi"] > info["left_lo"] + 0.5:
        ax.axvspan(info["left_lo"], info["left_hi"], color="#ED7D31", alpha=0.18, label="Talus gauche")
    elif info.get("versant_left"):
        ax.axvspan(info["left_lo"], info["left_hi"], color="#AAAAAA", alpha=0.12, label="Versant gauche (pas un talus)")
    if not info.get("versant_right") and info["right_hi"] > info["right_lo"] + 0.5:
        ax.axvspan(info["right_lo"], info["right_hi"], color="#7030A0", alpha=0.18, label="Talus droit")
    elif info.get("versant_right"):
        ax.axvspan(info["right_lo"], info["right_hi"], color="#AAAAAA", alpha=0.12, label="Versant droit (pas un talus)")
    ax.axvline(dc, color="#5B9BD5", lw=0.8, ls=":")
    zlp = info.get("z_left_plat", info["z_plat"])
    zrp = info.get("z_right_plat", info["z_plat"])
    _draw_h_arrow(
        ax, 0.5 * (info["left_lo"] + info["left_hi"]),
        zlp, zlp - info["dh_left"] if np.isfinite(info["dh_left"]) else np.nan,
        info["dh_left"], "#C65911",
    )
    _draw_h_arrow(
        ax, 0.5 * (info["right_lo"] + info["right_hi"]),
        zrp, zrp - info["dh_right"] if np.isfinite(info["dh_right"]) else np.nan,
        info["dh_right"], "#5B2C6F",
    )
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("Distance sur le profil en travers (m), 0 = gauche")
    ax.set_ylabel("Altitude (m)")
    ax.legend(loc="best", fontsize=7)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _savefig(fig, path)
    plt.close(fig)


def _savefig(fig, path):
    """Windows: un PNG deja ouvert peut renvoyer Errno 22, on reessaie."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp.png"
    last = None
    for _ in range(6):
        try:
            fig.savefig(tmp)
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.25)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    raise last


def plot_strip(samples, title, path):
    if not samples:
        return
    xs = [s["cl"] for s in samples]
    fig, ax = plt.subplots(figsize=(10, 2.4), dpi=90)
    morphs = [s["morph"] for s in samples]
    ymap = {m: i for i, m in enumerate(sorted(set(morphs)))}
    ax.scatter(xs, [ymap[m] for m in morphs], c=[COLORS.get(m, "#ccc") for m in morphs], s=28)
    ax.set_yticks(list(ymap.values()), list(ymap.keys()))
    ax.set_ylabel("Morphologie")
    ax.set_xlabel("Distance sur la ligne centrale (m)")
    ax.set_title(title, fontsize=10)
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    _savefig(fig, path)
    plt.close(fig)


def verdict(livrable, vote, mixte_faible_pct, tn_douteux_pct, unilateral_pct, h_centre_med=None):
    liv = livrable
    hnote = ""
    if h_centre_med is not None and np.isfinite(h_centre_med) and abs(h_centre_med) < CENTRE_RASANT:
        hnote = (
            f" H centre vs versant = {h_centre_med:+.2f} m (sous 2 m): "
            f"ce n'est pas la hauteur d'ouvrage, c'est la pente du terrain."
        )

    if liv == "pont":
        return "N/A", "Pont: pas de classification de talus."
    if liv.startswith("mur"):
        return "N/A", "Mur de soutenement: hors morphologie de talus."

    if liv == "mixte":
        if vote in ("mixte", "mixte_faible"):
            return "COHERENT", "Talus immediat gauche et droit de signes opposes."
        if vote in ("remblai", "unilateral_remblai"):
            return "DOUTEUX", "Allure de remblai sur les talus immediats, pas un mixte." + hnote
        if vote in ("deblai", "unilateral_deblai"):
            return "DOUTEUX", "Allure de deblai sur les talus immediats, pas un mixte."
        return "DOUTEUX", "Morphologie majoritaire: " + str(vote)

    core = vote.replace("unilateral_", "")
    if liv == "remblai" and core == "remblai":
        if vote == "unilateral_remblai":
            return "COHERENT", (
                "Remblai d'un seul cote (souvent le cote vallee). "
                "L'autre cote est le versant ou la deuxieme chaussee, pas un deblai."
                + hnote
            )
        return "COHERENT", "Pente descendante au bord de plateforme des deux cotes." + hnote
    if liv == "deblai" and core == "deblai":
        return "COHERENT", "Pente montante au bord de plateforme (deblai)."
    if liv == "rasant" and vote in ("plat", "rasant"):
        return "COHERENT", "Peu de denivele au bord de chaussee; centre dans +/- 2 m."
    if liv == "rasant" and vote in ("mixte", "mixte_faible"):
        return "DOUTEUX", (
            "Livrable rasant mais talus gauche et droit opposes a +/- 2 m (vote mixte)."
        )
    if liv == "rasant" and core in ("remblai", "deblai"):
        return "DOUTEUX", (
            f"Livrable rasant mais talus immediat {vote}. "
            "Verifier s'il s'agit d'un petit ouvrage ou d'une deuxieme chaussee."
        )
    if liv in ("remblai", "deblai") and vote in ("mixte", "mixte_faible"):
        return "DOUTEUX", (
            "Les talus externes s'opposent encore (au moins 2 m de chaque cote). "
            "Relire le profil au bord des chaussees."
        )
    if liv == "remblai" and vote == "plat":
        if h_centre_med is not None and h_centre_med >= CENTRE_RASANT:
            return "COHERENT", "Centre au-dessus du versant; talus externe peu marque sur 2-12 m." + hnote
        return "DOUTEUX", "Talus externes peu marques: possible rasant."
    if liv == "deblai" and vote == "plat":
        if h_centre_med is not None and h_centre_med <= -CENTRE_RASANT:
            return "COHERENT", "Centre sous le versant; talus externe peu marque sur 2-12 m."
        return "DOUTEUX", "Talus externes peu marques: possible rasant."
    return "DOUTEUX", f"Livrable {liv}, vote {vote}."


def overlapping_source(src, pr_s, ab_s, pr_e, ab_e):
    def pr_int(v):
        m = re.search(r"(\d+)", str(v))
        return int(m.group(1)) if m else None

    s0 = pr_s * 1000 + ab_s
    e0 = pr_e * 1000 + ab_e
    rows = []
    for _, r in src.iterrows():
        ps = pr_int(r["PR_start"])
        pe = pr_int(r["PR_end"])
        if ps is None:
            continue
        a0 = int(r["abcisse_start"])
        a1 = int(r["abcisse_end"])
        s1, e1 = ps * 1000 + a0, pe * 1000 + a1
        if e1 > s0 and s1 < e0:
            rows.append(r)
    return pd.DataFrame(rows) if rows else src.iloc[0:0]


def overlapping_abs(log_df, cl_s, cl_e, locator):
    if log_df is None or len(log_df) == 0:
        return None
    out = []
    for _, r in log_df.iterrows():
        txt = str(r.get("position_debut", ""))
        nums = re.findall(r"\d+", txt)
        if len(nums) >= 2:
            cl = locator.cl_distance(int(nums[0]), int(nums[1]))
            if cl_s - 80 <= cl <= cl_e + 80:
                out.append(r)
    return pd.DataFrame(out) if out else None


def href_url(path: str) -> str:
    """Encode + and spaces so a colleague opening via HTTP / Teams / SharePoint
    does not get a 404 (plus is otherwise read as a space)."""
    return quote(unquote(str(path or "")), safe="/._-")


def write_section_html(path, meta, samples, src_rows, abs_rows, rel_strip, rel_imgs):
    vcolor = {"COHERENT": "#C6EFCE", "DOUTEUX": "#FFE699", "INCOHERENT": "#FFC7CE", "N/A": "#D9D9D9"}
    bg = vcolor.get(meta["verdict"], "#eee")
    parts = [f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>{escape(meta['nom'])}</title>
<style>
body {{ font-family: Calibri, Arial, sans-serif; margin: 24px; color: #222; }}
.badge {{ display:inline-block; padding:6px 12px; font-weight:700; }}
table {{ border-collapse: collapse; font-size: 13px; }}
td, th {{ border: 1px solid #bbb; padding: 4px 8px; }}
.grid {{ display:flex; flex-wrap:wrap; gap:12px; }}
.card {{ width: 420px; }}
.card img {{ width: 100%; border: 1px solid #ccc; }}
.muted {{ color: #555; font-size: 13px; }}
</style></head><body>
<p><a href="../index.html">Retour a toutes les sections</a></p>
<h1>Ligne {meta['n']} - {escape(meta['nom'])}</h1>
<p class="badge" style="background:{bg}">{escape(meta['verdict'])}</p>
<p>{escape(meta['commentaire'])}</p>
<ul>
<li>Livrable: <b>{escape(meta['classification'])}</b>, PR {meta['pr_s']}+{meta['ab_s']:03d} a PR {meta['pr_e']}+{meta['ab_e']:03d} ({meta['length']} m)</li>
<li>Mur: {escape(str(meta['mur']))}</li>
<li>Vote talus (depuis le bord de plateforme): <b>{escape(meta['vote'])}</b></li>
<li>Profils tous les 10 m: {meta['n_prof']}</li>
</ul>
<h2>Lecture des profils</h2>
<p class="muted">Le trait bleu vertical est le centre du profil (milieu de plateforme).
Bande bleue: plateforme A31 (~+/- 22 m du centre). Gris: une autre route
collee a l'A31 (on ne la prend pas pour le talus). Orange et violet: talus
d'OET, au-dela de cette route s'il y en a une. La fleche H est le denivele
plateforme A31 vs pied du talus, celui qu'on lit sur l'axe Y. Si le TN
lateral pente deja (>= 5 %), un leger remblai/deblai (des 1 m) reste un OET.</p>
<p><img src="{escape(href_url(rel_strip))}" style="max-width:100%;border:1px solid #ccc"></p>
<h2>Profils tous les {STEP_M} m</h2>
<div class="grid">
"""]
    for s, img in zip(samples, rel_imgs):
        pr, ab = s["pr"], s["ab"]
        parts.append(
            f'<div class="card"><img src="{escape(href_url(img))}">'
            f'<p>PR {pr}+{ab:03d} &nbsp; gauche {s["dh_left"]:+.1f} m &nbsp; droit {s["dh_right"]:+.1f} m'
            f'<br>morph. {escape(s["morph"])} &nbsp; H vs versant {s["h_centre_tn"]:+.2f} m</p></div>'
        )
    parts.append("</div>")
    if src_rows is not None and len(src_rows):
        parts.append("<h2>Lignes source sur ce PR</h2><table><tr>")
        cols = [c for c in ["nom", "chaussee", "classification", "length", "hauteur_centre_max", "hauteur_talus_max"] if c in src_rows.columns]
        for c in cols:
            parts.append(f"<th>{escape(c)}</th>")
        parts.append("</tr>")
        for _, r in src_rows.iterrows():
            parts.append("<tr>")
            for c in cols:
                parts.append(f"<td>{escape(str(r[c]))}</td>")
            parts.append("</tr>")
        parts.append("</table>")
    if abs_rows is not None and len(abs_rows):
        parts.append("<h2>Absorptions sur ce lineaire</h2><table><tr>")
        for c in abs_rows.columns:
            parts.append(f"<th>{escape(str(c))}</th>")
        parts.append("</tr>")
        for _, r in abs_rows.iterrows():
            parts.append("<tr>")
            for c in abs_rows.columns:
                parts.append(f"<td>{escape(str(r[c]))}</td>")
            parts.append("</tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


MANUAL_CLASS_OPTIONS = (
    ("", "-"),
    ("conforme", "conforme"),
    ("remblai", "remblai"),
    ("deblai", "deblai"),
    ("rasant", "rasant"),
    ("mixte", "mixte"),
    ("pont", "pont"),
    ("mur", "mur"),
)

INDEX_PAGE_JS = r"""
<script>
(function () {
  var KEY = "verdicts_manuels:" + location.pathname;
  var statusEl = document.getElementById("save-status");

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg || "";
  }

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("tr[data-nom]"));
  }

  function collect() {
    var out = {};
    rows().forEach(function (tr) {
      var nom = tr.getAttribute("data-nom");
      var classe = tr.querySelector(".v-classe").value;
      var note = tr.querySelector(".v-note").value.trim();
      if (classe || note) {
        out[nom] = {
          n: tr.getAttribute("data-n"),
          classe_livrable: tr.getAttribute("data-livrable"),
          classe: classe,
          note: note
        };
      }
    });
    return out;
  }

  function paint(tr) {
    var classe = tr.querySelector(".v-classe").value;
    var liv = tr.getAttribute("data-livrable") || "";
    var cell = tr.querySelector(".cell-manuel");
    if (!classe) {
      cell.style.background = "";
      return;
    }
    var same = (classe === "conforme") || (classe === liv) ||
      (classe === "mur" && liv.indexOf("mur") === 0);
    cell.style.background = same ? "#C6EFCE" : "#FFE699";
  }

  function apply(data) {
    if (!data) return;
    rows().forEach(function (tr) {
      var rec = data[tr.getAttribute("data-nom")];
      if (!rec) return;
      if (typeof rec === "string") rec = {classe: rec, note: ""};
      var sel = tr.querySelector(".v-classe");
      var note = tr.querySelector(".v-note");
      var cl = rec.classe || rec.classe_manuelle || "";
      if (cl) sel.value = cl;
      if (rec.note) note.value = rec.note;
      paint(tr);
    });
  }

  function persistLocal() {
    try {
      localStorage.setItem(KEY, JSON.stringify(collect()));
    } catch (e) {}
  }

  function csvCell(s) {
    s = String(s == null ? "" : s);
    if (/[",\n;]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function toCsv() {
    var lines = ["nom;n;classe_livrable;classe_manuelle;note"];
    rows().forEach(function (tr) {
      var classe = tr.querySelector(".v-classe").value;
      var note = tr.querySelector(".v-note").value.trim();
      lines.push([
        csvCell(tr.getAttribute("data-nom")),
        csvCell(tr.getAttribute("data-n")),
        csvCell(tr.getAttribute("data-livrable")),
        csvCell(classe),
        csvCell(note)
      ].join(";"));
    });
    return "\ufeff" + lines.join("\r\n");
  }

  function downloadBlob(blob, name) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1500);
  }

  function parseCsv(text) {
    text = text.replace(/^\ufeff/, "");
    var lines = text.split(/\r?\n/).filter(function (l) { return l.trim(); });
    if (!lines.length) return {};
    var sep = lines[0].indexOf(";") >= 0 ? ";" : ",";
    var header = lines[0].split(sep).map(function (h) {
      return h.trim().replace(/^"|"$/g, "");
    });
    var out = {};
    for (var i = 1; i < lines.length; i++) {
      var cells = [];
      var cur = "";
      var q = false;
      var line = lines[i];
      for (var j = 0; j < line.length; j++) {
        var ch = line[j];
        if (q) {
          if (ch === '"' && line[j + 1] === '"') { cur += '"'; j++; }
          else if (ch === '"') q = false;
          else cur += ch;
        } else if (ch === '"') q = true;
        else if (ch === sep) { cells.push(cur); cur = ""; }
        else cur += ch;
      }
      cells.push(cur);
      var rec = {};
      header.forEach(function (h, k) { rec[h] = (cells[k] || "").trim(); });
      var nom = rec.nom;
      if (!nom) continue;
      out[nom] = {
        classe: rec.classe_manuelle || rec.classe || "",
        note: rec.note || ""
      };
    }
    return out;
  }

  async function saveFile() {
    persistLocal();
    var blob = new Blob([toCsv()], {type: "text/csv;charset=utf-8"});
    downloadBlob(blob, "verdicts_manuels.csv");
    setStatus("CSV telecharge. Renvoyez verdicts_manuels.csv (le navigateur ne l'ecrit pas dans ce dossier tout seul).");
  }

  function bind() {
    rows().forEach(function (tr) {
      paint(tr);
      tr.querySelector(".v-classe").addEventListener("change", function () {
        paint(tr);
        persistLocal();
        setStatus("Choix conserve dans le navigateur. Pensez a Telecharger le CSV.");
      });
      tr.querySelector(".v-note").addEventListener("input", function () {
        persistLocal();
      });
    });
    var btn = document.getElementById("btn-save");
    if (btn) btn.addEventListener("click", function () { saveFile(); });
    var inp = document.getElementById("btn-import");
    if (inp) inp.addEventListener("change", function () {
      var f = inp.files && inp.files[0];
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        var text = String(reader.result || "");
        var data;
        try { data = JSON.parse(text); }
        catch (e) { data = parseCsv(text); }
        apply(data);
        persistLocal();
        setStatus("Verdicts importes depuis " + f.name + ".");
      };
      reader.readAsText(f, "utf-8");
      inp.value = "";
    });
  }

  function load() {
    try {
      apply(JSON.parse(localStorage.getItem(KEY) || "{}"));
    } catch (e) {}
    if (location.protocol === "file:") return;
    fetch("verdicts_manuels.csv").then(function (r) {
      if (!r.ok) throw new Error("no csv");
      return r.text();
    }).then(function (t) {
      apply(parseCsv(t));
      persistLocal();
    }).catch(function () {
      return fetch("verdicts_manuels.json").then(function (r) {
        if (!r.ok) throw new Error("no json");
        return r.json();
      }).then(function (d) {
        apply(d);
        persistLocal();
      });
    }).catch(function () {});
  }

  bind();
  load();
})();
</script>
"""


def _manual_select_html(selected):
    opts = []
    for value, label in MANUAL_CLASS_OPTIONS:
        sel = " selected" if (selected or "") == value else ""
        opts.append(f'<option value="{escape(value)}"{sel}>{escape(label)}</option>')
    return '<select class="v-classe">' + "".join(opts) + "</select>"


def load_manual_verdicts(index_path):
    folder = os.path.dirname(index_path) or "."
    out = {}
    json_path = os.path.join(folder, "verdicts_manuels.json")
    csv_path = os.path.join(folder, "verdicts_manuels.csv")
    if not os.path.exists(csv_path):
        csv_path = os.path.join(folder, "index_apres_correction_manuelle.csv")
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for nom, rec in data.items():
                if isinstance(rec, str):
                    out[str(nom)] = {"classe": rec, "note": ""}
                elif isinstance(rec, dict):
                    out[str(nom)] = rec
        elif isinstance(data, list):
            for rec in data:
                nom = str(rec.get("nom", "") or "")
                if nom:
                    out[nom] = rec
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            except csv.Error:
                class _Semi(csv.excel):
                    delimiter = ";"
                dialect = _Semi if ";" in sample.split("\n", 1)[0] else csv.excel
            for rec in csv.DictReader(f, dialect=dialect):
                nom = str(rec.get("nom", "") or "").strip()
                if not nom:
                    continue
                out[nom] = {
                    "classe": str(rec.get("classe_manuelle") or rec.get("classe") or "").strip(),
                    "note": str(rec.get("note") or "").strip(),
                }
    return out


def _dept_from_index(path):
    p = os.path.normpath(path).replace("\\", "/").lower()
    if "output_a31_57" in p:
        return "57"
    return "54"


def _apply_cmd_from_index(path):
    parts = os.path.normpath(path).split(os.sep)
    out_dir = "."
    ver = "v10"
    if "sections_apres_conditions" in parts:
        i = parts.index("sections_apres_conditions")
        if i > 0:
            out_dir = parts[i - 1]
        if i + 1 < len(parts):
            ver = parts[i + 1]
    return f"python apply_manual_corrections.py --output-dir {out_dir} --version {ver}"


def write_index(path, rows, manual=None):
    if manual is None:
        manual = load_manual_verdicts(path)
    apply_cmd = _apply_cmd_from_index(path)
    dept = _dept_from_index(path)
    parts = [f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Verification livrable A31 ({dept})</title>
<style>
body {{ font-family: Calibri, Arial, sans-serif; margin: 24px; color: #222; }}
.toolbar {{ position: sticky; top: 0; background: #fff; padding: 8px 0 12px;
  border-bottom: 1px solid #ccc; margin-bottom: 12px; z-index: 2; }}
.toolbar button, .toolbar label {{ margin-right: 8px; font-size: 14px; }}
#save-status {{ color: #0a5; font-size: 13px; margin-left: 8px; }}
.muted {{ color: #555; font-size: 13px; }}
table {{ border-collapse: collapse; font-size: 13px; }}
th {{ position: sticky; top: 58px; background: #f3f3f3; z-index: 1; }}
td, th {{ border: 1px solid #bbb; padding: 5px 8px; }}
select.v-classe {{ min-width: 110px; }}
input.v-note {{ width: 140px; }}
.cell-manuel {{ white-space: nowrap; }}
</style></head><body>
<h1>Verification ligne a ligne, A31 ({dept})</h1>
<p>Cliquez un troncon pour voir les profils MNT tous les 10 m. Le point a 60 m est le milieu de la plateforme
(bande vers 40-80 m). Un fosse interne qui remonte fait encore partie de la plateforme.</p>
<div class="toolbar">
<button type="button" id="btn-save">Telecharger le CSV</button>
<label>Importer <input type="file" id="btn-import" accept=".csv,.json,text/csv,application/json"></label>
<span id="save-status"></span>
</div>
<p class="muted">Ouvrez ce fichier par double-clic apres avoir dezippe le dossier (pas depuis Teams ni SharePoint).
Colonne Verdict manuel : votre classification. Conforme = d'accord avec le livrable.
Les choix restent dans le navigateur. Telecharger le CSV, puis renvoyez verdicts_manuels.csv.
Pour recaler l'Excel : <code>{escape(apply_cmd)}</code></p>
<table><tr>
<th>#</th><th>Troncon</th><th>Classe livrable</th><th>Vote talus</th>
<th>Verdict auto</th><th>Commentaire</th>
<th>Verdict manuel</th><th>Note</th>
</tr>
"""]
    vcolor = {"COHERENT": "#C6EFCE", "DOUTEUX": "#FFE699", "INCOHERENT": "#FFC7CE", "N/A": "#D9D9D9"}
    for r in rows:
        bg = vcolor.get(r["verdict"], "#fff")
        href = escape(href_url(r["href"]))
        nom = r["nom"]
        rec = manual.get(nom) or {}
        if isinstance(rec, str):
            rec = {"classe": rec, "note": ""}
        classe_m = str(rec.get("classe") or rec.get("classe_manuelle") or "")
        note_m = str(rec.get("note") or "")
        parts.append(
            f'<tr style="background:{bg}" data-nom="{escape(nom)}" '
            f'data-n="{r["n"]}" data-livrable="{escape(r["classification"])}">'
            f'<td>{r["n"]}</td>'
            f'<td><a href="{href}">{escape(nom)}</a></td>'
            f'<td>{escape(r["classification"])}</td>'
            f'<td>{escape(str(r["vote"]))}</td>'
            f'<td><b>{escape(r["verdict"])}</b></td>'
            f'<td>{escape(r["commentaire"])}</td>'
            f'<td class="cell-manuel">{_manual_select_html(classe_m)}</td>'
            f'<td><input class="v-note" type="text" value="{escape(note_m, quote=True)}"></td>'
            f'</tr>'
        )
    parts.append("</table>")
    parts.append(INDEX_PAGE_JS)
    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


def patch_section_image_urls(dest: str) -> int:
    """Encode + in existing section.html image paths (same 404 as the index links)."""
    n = 0
    root = os.path.join(dest, "sections")
    if not os.path.isdir(root):
        return 0
    src_re = re.compile(r'(src=")([^"]+)(")')
    for dirpath, _, files in os.walk(root):
        if "section.html" not in files:
            continue
        html_path = os.path.join(dirpath, "section.html")
        html = Path(html_path).read_text(encoding="utf-8")
        new = src_re.sub(lambda m: m.group(1) + href_url(m.group(2)) + m.group(3), html)
        if new != html:
            Path(html_path).write_text(new, encoding="utf-8")
            n += 1
    return n


def rebuild_index_from_existing(path):
    """Reecrit un index.html deja genere pour y ajouter la colonne manuelle."""
    with open(path, encoding="utf-8") as f:
        html = f.read()
    rows = []
    pat_old = re.compile(
        r'<tr style="background:[^"]*"><td>(\d+)</td>'
        r'<td><a href="([^"]+)">([^<]*)</a></td>'
        r'<td>([^<]*)</td><td>([^<]*)</td>'
        r'<td><b>([^<]*)</b></td><td>(.*?)</td></tr>',
        re.DOTALL,
    )
    pat_new = re.compile(
        r'<tr style="background:[^"]*" data-nom="([^"]+)" data-n="(\d+)" '
        r'data-livrable="([^"]+)">'
        r'<td>\d+</td>'
        r'<td><a href="([^"]+)">[^<]*</a></td>'
        r'<td>[^<]*</td><td>([^<]*)</td>'
        r'<td><b>([^<]*)</b></td><td>(.*?)</td>'
        r'<td class="cell-manuel">',
        re.DOTALL,
    )
    if 'data-nom="' in html:
        for m in pat_new.finditer(html):
            rows.append({
                "n": int(m.group(2)),
                "nom": unescape(m.group(1)),
                "classification": unescape(m.group(3)),
                "href": unescape(m.group(4)),
                "vote": unescape(m.group(5)),
                "verdict": unescape(m.group(6)),
                "commentaire": unescape(m.group(7)),
            })
    else:
        for m in pat_old.finditer(html):
            rows.append({
                "n": int(m.group(1)),
                "href": unescape(m.group(2)),
                "nom": unescape(m.group(3)),
                "classification": unescape(m.group(4)),
                "vote": unescape(m.group(5)),
                "verdict": unescape(m.group(6)),
                "commentaire": unescape(m.group(7)),
            })
    if not rows:
        raise ValueError(f"Aucune ligne reconnue dans {path}")
    write_index(path, rows)
    patch_section_image_urls(os.path.dirname(path))
    return True


def process_section(i, row, centerline, locator, mnt, src, log_df, dest_root, axis=None):
    n = i + 1
    slug = f"{n:02d}_{row['nom']}_{row['classification']}".replace(" ", "_")
    slug = re.sub(r"[^A-Za-z0-9_+\-]", "", slug)
    sdir = os.path.join(dest_root, "sections", slug)
    os.makedirs(sdir, exist_ok=True)

    cl_s = locator.cl_distance(row["PR_start"], row["abcisse_start"]) if locator is not None else float(row["PR_start"]) * 1000 + float(row["abcisse_start"])
    cl_e = locator.cl_distance(row["PR_end"], row["abcisse_end"]) if locator is not None else float(row["PR_end"]) * 1000 + float(row["abcisse_end"])
    if axis is None:
        if cl_e <= cl_s:
            cl_e = min(centerline.length, cl_s + max(row["length"], STEP_M))
        cl_e = min(cl_e, centerline.length)
        cl_s = max(0.0, min(cl_s, centerline.length))

    meta_base = {
        "n": n,
        "nom": row["nom"],
        "classification": row["classification"],
        "mur": row["mur"],
        "pr_s": int(row["PR_start"]),
        "ab_s": int(row["abcisse_start"]),
        "pr_e": int(row["PR_end"]),
        "ab_e": int(row["abcisse_end"]),
        "length": int(row["length"]),
        "cl_s": cl_s,
        "cl_e": cl_e,
    }

    if row["classification"] in ("pont",) or str(row["classification"]).startswith("mur"):
        samples = []
        vote = "N/A"
        tn_pct = 0
        uni_pct = 0
        mf_pct = 0
        verd, com = verdict(row["classification"], vote, 0, 0, 0)
        fig, ax = plt.subplots(figsize=(6, 1.5), dpi=80)
        ax.axis("off")
        ax.set_title("Pas de profil talus (pont / mur)")
        strip = os.path.join(sdir, "strip.png")
        fig.savefig(strip)
        plt.close(fig)
        write_section_html(
            os.path.join(sdir, "section.html"),
            {**meta_base, "verdict": verd, "commentaire": com, "vote": vote,
             "n_prof": 0, "tn_pct": 0},
            [], None, None, "strip.png", [],
        )
        return {
            "n": n, "nom": row["nom"], "classification": row["classification"],
            "vote": vote, "verdict": verd, "commentaire": com, "tn_pct": 0,
            "href": f"sections/{slug}/section.html",
        }

    if axis is not None:
        pos_s = int(row["PR_start"]) * 1000 + int(row["abcisse_start"])
        pos_e = int(row["PR_end"]) * 1000 + int(row["abcisse_end"])
        if pos_e <= pos_s:
            pos_e = pos_s + max(int(row["length"]), STEP_M)
        positions = list(range(pos_s, pos_e, STEP_M))
        if not positions or positions[-1] < pos_e - 1:
            positions.append(pos_e)
    else:
        positions = list(np.arange(cl_s, cl_e, STEP_M))
        if not positions or positions[-1] < cl_e - 1:
            positions.append(cl_e)
    samples = []
    rel_imgs = []
    for k, cl in enumerate(positions):
        if axis is not None:
            pr, ab = int(cl) // 1000, int(cl) % 1000
            line, d = axis.locate(pr, ab)
            center, perp = perpendicular(line, d)
        else:
            pr, ab = locator.label(cl)
            center, perp = perpendicular(centerline, cl)
        dists, zs, d_c = sample_profile(mnt, perp, center)
        info = analyze_profile(dists, zs, d_c)
        rec = {"cl": cl, "pr": pr, "ab": ab, **info}
        samples.append(rec)
        png_name = f"p_{k:03d}_PR{pr}+{ab:03d}.png"
        title = (
            f"{row['nom']}  |  PR {pr}+{ab:03d}  |  livrable {row['classification']}\n"
            f"gauche {info['dh_left']:+.1f} m   droit {info['dh_right']:+.1f} m   "
            f"morph. {info['morph']}   H vs versant {info['h_centre_tn']:+.2f} m"
        )
        plot_profile(dists, zs, info, title, os.path.join(sdir, png_name))
        rel_imgs.append(png_name)

    morphs = [s["morph"] for s in samples]
    vote = Counter(morphs).most_common(1)[0][0]
    tn_pct = 100 * sum(1 for s in samples if s["tn_douteux"]) / max(len(samples), 1)
    uni_pct = 100 * sum(1 for m in morphs if str(m).startswith("unilateral")) / max(len(samples), 1)
    mf_pct = 100 * sum(1 for m in morphs if m == "mixte_faible") / max(len(samples), 1)
    hs = [s["h_centre_tn"] for s in samples if np.isfinite(s["h_centre_tn"])]
    h_med = float(np.median(hs)) if hs else None
    verd, com = verdict(row["classification"], vote, mf_pct, tn_pct, uni_pct, h_centre_med=h_med)

    plot_strip(samples, f"Ligne {n} {row['nom']} - {row['classification']} (vote {vote})", os.path.join(sdir, "strip.png"))
    src_rows = overlapping_source(src, row["PR_start"], row["abcisse_start"], row["PR_end"], row["abcisse_end"])
    abs_rows = overlapping_abs(log_df, cl_s, cl_e, locator) if locator is not None else None

    write_section_html(
        os.path.join(sdir, "section.html"),
        {**meta_base, "verdict": verd, "commentaire": com, "vote": vote,
         "n_prof": len(samples), "tn_pct": tn_pct, "mur": row["mur"]},
        samples, src_rows, abs_rows, "strip.png", rel_imgs,
    )
    print(f"  [{n:02d}] {row['nom']:18s} livrable={row['classification']:10s} vote={vote:20s} {verd}")
    return {
        "n": n, "nom": row["nom"], "classification": row["classification"],
        "vote": vote, "verdict": verd, "commentaire": com, "tn_pct": tn_pct,
        "href": f"sections/{slug}/section.html",
    }


def _summary_from_section_html(i, row, dest_root):
    n = i + 1
    slug = f"{n:02d}_{row['nom']}_{row['classification']}".replace(" ", "_")
    slug = re.sub(r"[^A-Za-z0-9_+\-]", "", slug)
    path = os.path.join(dest_root, "sections", slug, "section.html")
    if not os.path.exists(path):
        return None
    html = Path(path).read_text(encoding="utf-8")
    m_v = re.search(r'class="badge"[^>]*>([^<]+)</p>', html)
    m_vote = re.search(r"Vote talus[^:]*: <b>([^<]*)</b>", html)
    m_com = re.search(r'</p>\s*<p>([^<]*)</p>\s*<ul>', html)
    return {
        "n": n, "nom": row["nom"], "classification": row["classification"],
        "vote": unescape(m_vote.group(1)) if m_vote else "?",
        "verdict": unescape(m_v.group(1)) if m_v else "?",
        "commentaire": unescape(m_com.group(1)) if m_com else "",
        "tn_pct": 0,
        "href": f"sections/{slug}/section.html",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output_A31_v3")
    parser.add_argument("--version", default="v9")
    parser.add_argument("--inventory", default=None)
    parser.add_argument("--mnt", default="data/mnt.tif")
    parser.add_argument("--dest", default=None)
    parser.add_argument("--line", type=int, default=None, help="Numéro 1-based de la ligne du livrable")
    parser.add_argument("--start", type=int, default=None, help="Reprend --all a partir de cette ligne 1-based")
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Reecrit seulement index.html (colonne verdict manuel) sans recalculer les profils",
    )
    args = parser.parse_args()

    out_dir = args.output_dir
    ver = args.version
    if args.rebuild_index:
        dest = args.dest or os.path.join(out_dir, "sections_apres_conditions", ver, "verification")
        index_path = os.path.join(dest, "index.html")
        if not os.path.exists(index_path):
            raise SystemExit(f"Index introuvable : {index_path}")
        changed = rebuild_index_from_existing(index_path)
        print(("Index mis a jour : " if changed else "Index deja a jour : ") + index_path)
        return

    inv_path = args.inventory or os.path.join(
        out_dir, "sections_apres_conditions", ver, "inventaire_operationnel_ponts.xlsx"
    )
    if not os.path.exists(inv_path):
        inv_path = os.path.join(out_dir, "sections_apres_conditions", ver, "inventaire_operationnel.xlsx")
    dest = args.dest or os.path.join(out_dir, "sections_apres_conditions", ver, "verification")
    os.makedirs(os.path.join(dest, "sections"), exist_ok=True)

    inv = load_inventory(inv_path)
    axis = None
    centerline = None
    locator = None
    axis_path = os.path.join(out_dir, "axis_lots.gpkg")
    if os.path.exists(axis_path):
        axis = CompoundAxis(gpd.read_file(axis_path))
    else:
        pr_gdf = gpd.read_file(os.path.join(out_dir, "projected_PR.gpkg"))
        cl_gdf = gpd.read_file(os.path.join(out_dir, "central_line_profil.gpkg"))
        centerline = cl_gdf.geometry.iloc[0]
        if centerline.geom_type == "MultiLineString":
            centerline = max(centerline.geoms, key=lambda g: g.length)
        locator = PrLocator(pr_gdf)
    src = gpd.read_file(os.path.join(out_dir, "selected_ouvrages.gpkg"))
    log_path = os.path.join(out_dir, "sections_apres_conditions", ver, "absorptions_ilots.csv")
    log_df = pd.read_csv(log_path) if os.path.exists(log_path) else None

    mnt = MntSampler(args.mnt)
    try:
        if args.line is not None:
            indices = [args.line - 1]
        elif args.all:
            start = max((args.start or 1) - 1, 0)
            indices = list(range(start, len(inv)))
        else:
            indices = [12]  # 13e ligne par defaut (mixte PR260+990)

        summaries = []
        for i in indices:
            if i < 0 or i >= len(inv):
                raise SystemExit(f"Ligne {i+1} hors livrable ({len(inv)} lignes)")
            summaries.append(
                process_section(
                    i, inv.iloc[i], centerline, locator, mnt, src, log_df, dest,
                    axis=axis,
                )
            )

        if args.all and args.start and args.start > 1:
            by_n = {s["n"]: s for s in summaries}
            summaries = []
            for i in range(len(inv)):
                if i + 1 in by_n:
                    summaries.append(by_n[i + 1])
                else:
                    filled = _summary_from_section_html(i, inv.iloc[i], dest)
                    if filled:
                        summaries.append(filled)

        # si --all, index global ; si une ligne, index partiel + lien
        index_path = os.path.join(dest, "index.html")
        if args.all or (args.line is None and len(indices) > 1):
            write_index(index_path, summaries)
        else:
            # index minimal pointant la section
            write_index(index_path, summaries)
        patch_section_image_urls(dest)
        synt = pd.DataFrame(summaries)
        synt.to_excel(os.path.join(dest, "synthese_verification.xlsx"), index=False)
        print(f"\nRapport : {index_path}")
        if len(summaries) == 1:
            print(f"Section : {os.path.join(dest, summaries[0]['href'])}")
    finally:
        mnt.close()


if __name__ == "__main__":
    main()

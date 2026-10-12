"""
Livrable operationnel v10 a partir de selected_ouvrages.xlsx.

Une ligne = une section physique (D et G fusionnes). Objectif : un inventaire
lisible ET fidele - nombre raisonnable d'OET, sans damier 200 m, sans rasant
a 8 m de talus, sans avaler un vrai deblai de 400 m dans 8 km de rasant.

v9 : ne pas prendre une tranche de versant pour un talus (rupture au bord
vs pente du pays qui continue).

v10 : hauteur = denivele lu sur l'axe (plateforme vs TN au pied du talus).
Sur un TN deja en pente, un leger remblai/deblai reste un OET. Si les talus
sont ambigus, on s'appuie sur l'OET voisin (y compris contre un pont).
Si la queue d'un OET est deja du meme type que l'OET suivant
(tous les derniers profils consecutifs, pas seulement 4), la coupure avance.

Les hypotheses (H1-H20) sont la source de verite ; elles sont recopiees
telles quelles en tete du Word, avant le tableau.
"""

import argparse
import os
import re
import warnings

import geopandas as gpd
import numpy as np
import pandas as pd
from openpyxl.styles import Font, Alignment, PatternFill

from recompute_classification import side_class, combine_side_and_center

# --- seuils (voir H6, H7, H8, H10, H12, H13) ---
SANDWICH_MAX_M = 250
SANDWICH_RASANT_M = 400
FRONTIERE_MIN_M = 80
RASANT_FRONTIERE_M = 200
MIN_OET_WEAK_M = 150
OET_WEAK_H = 3.5
PONT_CRUMB_M = 40
MAX_SOFT_GAP_M = 100
PONT_MARGIN = 15
HEIGHT_CAP = 30.0
CENTRE_RASANT_ABS = 2.0
DH_SMOOTH_RADIUS = 4  # profils 10 m : medianne sur +/- 40 m (fenetre 90 m)
FRONTIER_TAIL_M = 200  # H20 : toute la queue consecutive du type OET 2 (plafonnee)
FRONTIER_MIN_KEEP_M = 40
FRONTIER_MIN_MOVE_M = 20

SIGNIFICANT = {"remblai", "deblai", "mixte", "mur de soutènement"}
OET_CLASSES = {"remblai", "deblai", "mixte"}
NON_ABSORBABLE = {None, "PONT", "mur de soutènement"}
MUR = "mur de soutènement"

HYPOTHESES = [
    "H1. Source : inventaire de base selected_ouvrages.xlsx (meme contenu que le GPKG pour classification, PR, longueurs et hauteurs). Les geometries de talus gauche/droite, absentes de l'Excel, sont lues dans selected_ouvrages.gpkg uniquement pour detecter un mixte de profil.",
    "H2. Une ligne du livrable = une section physique unique. D et G sont deux projections du meme profil en travers : un desaccord a une meme abscisse est un decalage de chainage, jamais un mixte D vs G.",
    "H3. Mixte = talus gauche remblai ET talus droit deblai (hauteur signee d'au moins +2 m d'un cote et d'au plus -2 m de l'autre), meme seuil que le centre. Les talus sont les bords externes de la plateforme (~40-80 m). Un petit talus interne / escalier vers le centre (pos. 60) entre deux chaussees n'est pas un OET : la plateforme prend les deux voies. Si un cote externe est rasant et l'autre est un OET, la classe est cet OET.",
    "H4. Fusion D/G a une abscisse : meme classe -> cette classe ; mur d'un seul cote -> classe de l'autre cote (le mur va dans la colonne Mur) ; rasant contre un OET -> l'OET ; remblai contre deblai -> le cote dont |hauteur au centre| est la plus grande (le plus certain vis-a-vis du TN). On ne fabrique pas de mixte a partir de D vs G.",
    "H5. Un pont est l'emprise BD TOPO (construction surfacique et lineaire, nature Pont/Viaduc) projetee sur la ligne centrale, plus +/- 15 m autour des morceaux d'OET entierement supprimes par un pont. Il apparait dans le tableau (classe pont, sans hauteur). Aucune fusion ni absorption ne traverse un pont. On ne classe pas le MNT sur le tablier : un viaduc n'est pas une suite rasant / deblai / pont.",
    "H6. Un trou mou (aucune donnee ni en D ni en G) de 100 m ou moins entre deux sections de meme classe est fusionne, longueur du trou incluse (filtre source longueur < 20 m + decalage de chainage). Un trou plus long reste une coupure.",
    "H7. Sandwich (erreur de classification detectable) : un ilot de 250 m ou moins, encadre par deux voisins de la meme classe, chacun au moins aussi long que l'ilot, est absorbe dans cette classe. Exemple : remblai / 80 m rasant / remblai -> un seul remblai. Un pont, et une section mur de soutenement des deux cotes, ne sont jamais absorbes.",
    "H8. Frontiere (decalage de contact, pas un ouvrage) : (a) tout ilot de 80 m ou moins entre deux classes differentes est sous la resolution operationnelle -> voisin le plus long ; (b) un ilot rasant de 200 m ou moins entre deux classes differentes est un decalage de frontiere -> voisin le plus long. Un OET long et haut entre deux classes differentes est conserve.",
    "H9. Un mur de soutenement ne concerne en general qu'un cote. La classification affichee reste celle du terrain ; la colonne Mur soutenement indique D, G, ou D et G.",
    "H10. Hauteurs : |h| > 30 m = artefact MNT, remplace par la moyenne du segment. Pour un rasant, H max = |hauteur du centre par rapport au TN| (par construction entre 0 et 2 m). Pour remblai, deblai et mixte, H max = hauteur de talus. Pour un mixte, H remblai et H deblai sont les hauteurs signees des deux cotes du profil (positif = remblai, negatif = deblai).",
    "H11. Apres absorption, H max est mesuree sur les metres dont la classe d'origine est celle affichee. Un rasant qui a avale un petit remblai ne reprend pas la hauteur de talus de ce remblai, sinon on afficherait 8 m sur un rasant.",
    "H12. Plateforme et talus (v8) : profil lisse (medianne 5 m). La plateforme ignore un trou de 1-3 m. Deux chaussees de part et d'autre du centre (escalier / terre-plein vers la pos. 60) forment UNE plateforme ; le talus interne n'est pas classe. Chaque voie garde son altitude pour le talus externe. Le talus externe est colle au bord, pente sur 3 m, et ne s'arrete pas a la premiere oscillation.",
    "H13. Coherence longitudinale : les hauteurs de talus sont medianes sur ~90 m avant de classer. Un ilot rasant de 200 m ou moins, ou un OET faible (< 150 m et |H| < 3,5 m) entre deux classes, est un bruit de detection, pas un ouvrage. Un vrai deblai long et haut n'est pas absorbe.",
    "H14. Une miette de moins de 40 m collee a un pont (reste de masque) est recollée au voisin terre. Un trou court entre deux ponts reste un pont.",
    "H15. Versant vs talus (v9) : un talus est une rupture collee au bord de plateforme qui s'arrete (TN plat ou rupture de pente). Un versant est une pente qui continue loin de la route, sans rupture. On ne prend pas une tranche de coteau pour un OET. S'il y a un vrai talus pose sur un versant, la hauteur est l'ecart plateforme vs ce versant (cote par cote), pas vs une droite qui relie les deux bouts du profil.",
    "H16. Hauteur affichee (v10) = denivele plateforme vs terrain naturel au pied du talus, celui qu'on lit sur l'axe Y du profil. On ne prend plus un percentile du talus entier, ni l'ecart a une droite qui relie les deux bouts du profil (cette droite mélange vallee et coteau et sous-estime la coupe).",
    "H17. OET leger sur versant (v10) : si le terrain naturel lateral a deja une pente (>= 5 %), un leger remblai ou deblai (seuil 1 m au lieu de 2 m) reste un OET. Le versant rend l'ouvrage plus sensible ; on ne le classe pas rasant sous pretexte que la hauteur vis-a-vis du coteau est petite.",
    "H18. Talus ambigus (v10) : si la detection de talus est incertaine (rasant court) et qu'un voisin est un OET, on prend la classe de cet OET. Un rasant de 200 m ou moins entre un OET et un pont est rattache a l'OET (le pont n'empeche plus cette fusion).",
    "H19. Route laterale (v10) : une autre route collee a l'A31 (bretelle, voie parallele) n'est pas le talus d'OET. Le talus commence au-dela de cette route. La hauteur reste plateforme A31 vs pied du talus.",
    "H20. Frontiere (v10) : pour ne pas scintiller, on n'ouvre un nouvel OET qu'apres plusieurs profils coherents. Ensuite, toute la queue de l'OET 1 dont les profils (bruts, sans medianne) sont deja du meme type que l'OET 2 est rattachee a l'OET 2 : 3, 4, 5 ou 6 profils, tant qu'ils se suivent.",
]

FILLS = {
    "remblai": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "deblai": PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid"),
    "rasant": PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),
    "mixte": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "mur de soutènement": PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"),
    "pont": PatternFill(start_color="E2D5F1", end_color="E2D5F1", fill_type="solid"),
}

DOCX_HEX = {
    "remblai": "C6EFCE",
    "deblai": "BDD7EE",
    "rasant": "FFF2CC",
    "mixte": "FFC7CE",
    "mur de soutènement": "D9D9D9",
    "pont": "E2D5F1",
}

NOM_RE = re.compile(r"PR(\d+)\+(\d+)_([DG])$")


def parse_nom(nom):
    m = NOM_RE.search(str(nom))
    if not m:
        return None, None
    pr, ab, cote = m.groups()
    return int(pr) * 1000 + int(ab), cote


def signed_height_left(dist_min, dist_max, alt_min, alt_max):
    if pd.isna(dist_min) or pd.isna(dist_max) or pd.isna(alt_min) or pd.isna(alt_max):
        return np.nan
    if dist_max > dist_min:
        return alt_max - alt_min
    return -(alt_max - alt_min)


def signed_height_right(dist_min, dist_max, alt_min, alt_max):
    if pd.isna(dist_min) or pd.isna(dist_max) or pd.isna(alt_min) or pd.isna(alt_max):
        return np.nan
    if dist_max < dist_min:
        return alt_max - alt_min
    return -(alt_max - alt_min)


def sanitize_height(h_max, h_moy, cap=HEIGHT_CAP):
    if pd.notna(h_max) and abs(float(h_max)) > cap:
        if pd.notna(h_moy) and 0 < abs(float(h_moy)) <= cap:
            return float(h_moy)
        return float(np.sign(float(h_max)) * cap)
    if pd.notna(h_max) and pd.notna(h_moy) and float(h_max) > float(h_moy) + 3 and float(h_moy) > 0:
        return float(h_moy)
    if pd.notna(h_max):
        return float(h_max)
    if pd.notna(h_moy) and 0 < abs(float(h_moy)) <= cap:
        return float(h_moy)
    return np.nan


def sanitize_centre(h_max, h_moy, cap=HEIGHT_CAP):
    if pd.notna(h_max) and abs(float(h_max)) > cap:
        if pd.notna(h_moy) and abs(float(h_moy)) <= cap:
            return float(h_moy)
        return np.nan
    if pd.notna(h_max):
        return float(h_max)
    if pd.notna(h_moy) and abs(float(h_moy)) <= cap:
        return float(h_moy)
    return np.nan


def _pr_int(series):
    return series.astype(str).str.extract(r"(\d+)", expand=False).astype(int)


def load_selected_ouvrages(xlsx_path, gpkg_path=None):
    """Inventaire de base = xlsx. Le GPKG n'apporte que les colonnes de profil."""
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Inventaire de base introuvable : {xlsx_path}")

    df = pd.read_excel(xlsx_path)
    if gpkg_path and os.path.exists(gpkg_path):
        gdf = gpd.read_file(gpkg_path)
        extra = gdf.drop(columns="geometry", errors="ignore")
        add_cols = [c for c in extra.columns if c not in df.columns and c != "nom"]
        if add_cols:
            df = df.merge(extra[["nom"] + add_cols], on="nom", how="left")

    if "cote" not in df.columns and "chaussee" in df.columns:
        df = df.rename(columns={"chaussee": "cote"})
    if "cote" not in df.columns:
        df["cote"] = df["nom"].map(lambda n: parse_nom(n)[1])
    if df["cote"].isna().any():
        df["cote"] = df["cote"].fillna(df["nom"].map(lambda n: parse_nom(n)[1]))

    assert df["cote"].isin(["D", "G"]).all(), "Cote (D/G) non reconnu dans une des lignes"

    df["PR_start"] = _pr_int(df["PR_start"])
    df["PR_end"] = _pr_int(df["PR_end"])
    df["pos_start"] = df["PR_start"].astype(int) * 1000 + df["abcisse_start"].astype(int)
    df["pos_end"] = df["PR_end"].astype(int) * 1000 + df["abcisse_end"].astype(int)

    bad = df[df["pos_end"] <= df["pos_start"]]
    if len(bad):
        print(f"ATTENTION : {len(bad)} ligne(s) avec pos_end <= pos_start, ignorees :")
        print(bad[["nom", "PR_start", "abcisse_start", "PR_end", "abcisse_end"]])
        df = df[df["pos_end"] > df["pos_start"]].copy()

    for col in [
        "left_dist_min", "left_dist_max", "left_alt_min", "left_alt_max",
        "right_dist_min", "right_dist_max", "right_alt_min", "right_alt_max",
    ]:
        if col not in df.columns:
            df[col] = np.nan

    df["sh_left"] = df.apply(
        lambda r: signed_height_left(r["left_dist_min"], r["left_dist_max"], r["left_alt_min"], r["left_alt_max"]),
        axis=1,
    )
    df["sh_right"] = df.apply(
        lambda r: signed_height_right(r["right_dist_min"], r["right_dist_max"], r["right_alt_min"], r["right_alt_max"]),
        axis=1,
    )

    n_before = df["classification"].copy()
    df["classification"] = df.apply(_reclassify_from_sides, axis=1)
    n_changed = int((df["classification"] != n_before).sum())
    if n_changed:
        print(f"Recalcul talus (H3, seuils +/- 2 m) : {n_changed} ouvrage(s) reclassifies")
        print(
            pd.DataFrame({"avant": n_before, "apres": df["classification"]})
            .loc[n_before != df["classification"]]
            .groupby(["avant", "apres"]).size().to_string()
        )

    df["hauteur_talus_max"] = [
        sanitize_height(hmax, hmoy)
        for hmax, hmoy in zip(df.get("hauteur_talus_max", np.nan), df.get("hauteur_talus_moyenne", np.nan))
    ]
    df["hauteur_centre_max"] = [
        sanitize_centre(hmax, hmoy)
        for hmax, hmoy in zip(df.get("hauteur_centre_max", np.nan), df.get("hauteur_centre_moyenne", np.nan))
    ]
    return df


def _reclassify_from_sides(row):
    """H3 : mixte si les deux talus s'opposent a +/- 2 m ; un OET unilateral l'emporte."""
    if row["classification"] == MUR:
        return MUR
    left_c = side_class(row["sh_left"] if pd.notna(row["sh_left"]) else None)
    right_c = side_class(row["sh_right"] if pd.notna(row["sh_right"]) else None)
    return combine_side_and_center(row["classification"], left_c, right_c)


def morph_to_oet(morph):
    if morph == "mixte":
        return "mixte"
    if morph in ("remblai", "unilateral_remblai"):
        return "remblai"
    if morph in ("deblai", "unilateral_deblai"):
        return "deblai"
    return "rasant"


def overlay_verify_talus(harmonized, sh_left, sh_right, pont_mask, pos_min, output_dir, mnt_path):
    """Remesure les talus au bord de plateforme (meme logique que le HTML).

    Les hauteurs sont ensuite medianes le long de la route (~90 m) pour
    tuer le scintillement d'un profil 10 m sur deux.
    """
    import verify_livrable as v

    axis_path = os.path.join(output_dir, "axis_lots.gpkg")
    pr_path = os.path.join(output_dir, "projected_PR.gpkg")
    cl_path = os.path.join(output_dir, "central_line_profil.gpkg")
    if not os.path.exists(mnt_path):
        print("Pas de MNT / ligne centrale : talus source (GPKG) conserves.")
        return harmonized, sh_left, sh_right, np.array(harmonized, dtype=object)

    axis = None
    locator = None
    centerline = None
    if os.path.exists(axis_path):
        axis = v.CompoundAxis(gpd.read_file(axis_path))
    elif os.path.exists(pr_path) and os.path.exists(cl_path):
        pr_gdf = gpd.read_file(pr_path)
        centerline = gpd.read_file(cl_path).geometry.iloc[0]
        if centerline.geom_type == "MultiLineString":
            centerline = max(centerline.geoms, key=lambda g: g.length)
        locator = v.PrLocator(pr_gdf)
    else:
        print("Pas de MNT / ligne centrale : talus source (GPKG) conserves.")
        return harmonized, sh_left, sh_right, np.array(harmonized, dtype=object)

    mnt = v.MntSampler(mnt_path)
    n = len(harmonized)
    pos_max = pos_min + n - 1
    samples = []
    print("Remesure des talus au bord de plateforme, tous les 10 m...")
    try:
        for pos in range(int(pos_min), int(pos_max) + 1, 10):
            i0 = pos - pos_min
            i1 = min(n, i0 + 10)
            pr, ab = pos_to_pr_abcisse(pos)
            try:
                if axis is not None:
                    cl, cl_d = axis.locate(pr, ab)
                else:
                    cl, cl_d = centerline, locator.cl_distance(pr, ab)
            except Exception:
                continue
            if cl_d < 0 or cl_d > cl.length:
                continue
            center, perp = v.perpendicular(cl, min(cl_d, cl.length))
            dists, zs, d_c = v.sample_profile(mnt, perp, center)
            info = v.analyze_profile(dists, zs, d_c)
            sl = info.get("s_far_left", np.nan)
            sr = info.get("s_far_right", np.nan)
            raw_thr_l = 1.0 if (np.isfinite(sl) and abs(sl) >= 0.05) else 2.0
            raw_thr_r = 1.0 if (np.isfinite(sr) and abs(sr) >= 0.05) else 2.0
            samples.append({
                "i0": i0, "i1": i1, "pos": pos,
                "dh_l": info["dh_left"], "dh_r": info["dh_right"],
                "s_l": sl, "s_r": sr,
                "raw_class": morph_to_oet(
                    v.morphology(info["dh_left"], info["dh_right"], (raw_thr_l, raw_thr_r))
                ),
                "pont": bool(np.any(pont_mask[i0:i1])),
            })
            if len(samples) % 200 == 0:
                print(f"  ... {len(samples)} profils (PR {pr}+{ab:03d})")
    finally:
        mnt.close()
    print(f"  {len(samples)} profils mesures")

    n_s = len(samples)
    dh_l = np.array([s["dh_l"] for s in samples], dtype=float)
    dh_r = np.array([s["dh_r"] for s in samples], dtype=float)
    s_l = np.array([s.get("s_l", np.nan) for s in samples], dtype=float)
    s_r = np.array([s.get("s_r", np.nan) for s in samples], dtype=float)
    is_pont = np.array([s["pont"] for s in samples], dtype=bool)
    raw_cls = np.array(harmonized, dtype=object)
    r = DH_SMOOTH_RADIUS
    for k, s in enumerate(samples):
        if s["pont"]:
            continue
        for i in range(s["i0"], s["i1"]):
            if pont_mask[i] or harmonized[i] in (MUR, "PONT", "pont"):
                continue
            raw_cls[i] = s.get("raw_class", harmonized[i])
        lo, hi = max(0, k - r), min(n_s, k + r + 1)
        keep = ~is_pont[lo:hi]
        med_l = np.nanmedian(dh_l[lo:hi][keep]) if keep.any() else dh_l[k]
        med_r = np.nanmedian(dh_r[lo:hi][keep]) if keep.any() else dh_r[k]
        med_sl = np.nanmedian(s_l[lo:hi][keep]) if keep.any() else s_l[k]
        med_sr = np.nanmedian(s_r[lo:hi][keep]) if keep.any() else s_r[k]
        thr_l = 1.0 if (np.isfinite(med_sl) and abs(med_sl) >= 0.05) else 2.0
        thr_r = 1.0 if (np.isfinite(med_sr) and abs(med_sr) >= 0.05) else 2.0
        classe = morph_to_oet(v.morphology(med_l, med_r, (thr_l, thr_r)))
        for i in range(s["i0"], s["i1"]):
            if pont_mask[i] or harmonized[i] in (MUR, "PONT", "pont"):
                continue
            harmonized[i] = classe
            if np.isfinite(med_l):
                sh_left[i] = float(med_l)
            if np.isfinite(med_r):
                sh_right[i] = float(med_r)
    return harmonized, sh_left, sh_right, raw_cls


def load_pont_positions(rejected_csv_path):
    if not os.path.exists(rejected_csv_path):
        print("Pas de rejected_ouvrages.csv trouve : aucune zone de pont ne sera bloquee.")
        return []

    rej = pd.read_csv(rejected_csv_path, sep=";")
    pont_rows = rej[rej["raison_rejet"] == "supprime_par_pont"]

    positions = []
    for nom in pont_rows["nom"]:
        pos, _ = parse_nom(nom)
        if pos is not None:
            positions.append(pos)
        else:
            print(f"  (nom de pont non parse : {nom!r})")
    return positions


def apply_gpkg_pont_mask(pont_mask, pos_min, output_dir):
    """Marque l'emprise reelle des ponts BD TOPO, pas seulement +/- 15 m autour d'un nom rejete."""
    surf_path = os.path.join(output_dir, "ponts_surface.gpkg")
    lin_path = os.path.join(output_dir, "ponts_lineaire.gpkg")
    pr_path = os.path.join(output_dir, "projected_PR.gpkg")
    cl_path = os.path.join(output_dir, "central_line_profil.gpkg")
    pont_paths = [p for p in (surf_path, lin_path) if os.path.exists(p)]
    axis_path = os.path.join(output_dir, "axis_lots.gpkg")
    has_axis = os.path.exists(axis_path) or (os.path.exists(pr_path) and os.path.exists(cl_path))
    if not pont_paths or not has_axis:
        print("Pas de GPKG ponts / ligne centrale : masque pont = rejected +/- 15 m seulement.")
        return pont_mask

    import verify_livrable as v
    from shapely.ops import unary_union
    from shapely.prepared import prep

    geoms = []
    for path in pont_paths:
        gdf = gpd.read_file(path)
        geoms.extend([g for g in gdf.geometry if g is not None and not g.is_empty])
    if not geoms:
        return pont_mask

    union = unary_union([g.buffer(10) for g in geoms])
    prepared_union = prep(union)

    axis_path = os.path.join(output_dir, "axis_lots.gpkg")
    axis = None
    locator = None
    centerline = None
    if os.path.exists(axis_path):
        axis = v.CompoundAxis(gpd.read_file(axis_path))
    else:
        pr_gdf = gpd.read_file(pr_path)
        centerline = gpd.read_file(cl_path).geometry.iloc[0]
        if centerline.geom_type == "MultiLineString":
            centerline = max(centerline.geoms, key=lambda g: g.length)
        locator = v.PrLocator(pr_gdf)

    n = len(pont_mask)
    n_before = int(pont_mask.sum())
    for i in range(n):
        pos = pos_min + i
        pr, ab = pos_to_pr_abcisse(pos)
        if axis is not None:
            cl, cl_d = axis.locate(pr, ab)
        else:
            cl, cl_d = centerline, locator.cl_distance(pr, ab)
        if cl_d < 0 or cl_d > cl.length:
            continue
        if prepared_union.intersects(cl.interpolate(cl_d)):
            pont_mask[i] = True
    n_after = int(pont_mask.sum())
    print(
        f"Ponts BD TOPO projetes sur la ligne centrale : {n_after} m "
        f"(dont {n_after - n_before} m hors fenetres rejected +/- {PONT_MARGIN} m)"
    )
    return pont_mask


def build_grid(df, pont_positions, output_dir=None):
    pos_min = int(df["pos_start"].min())
    pos_max = int(df["pos_end"].max())
    n = pos_max - pos_min + 1

    D_class = np.full(n, None, dtype=object)
    G_class = np.full(n, None, dtype=object)
    D_talus = np.full(n, np.nan)
    G_talus = np.full(n, np.nan)
    D_centre = np.full(n, np.nan)
    G_centre = np.full(n, np.nan)
    sh_left = np.full(n, np.nan)
    sh_right = np.full(n, np.nan)

    for _, row in df.iterrows():
        s = int(row["pos_start"]) - pos_min
        e = int(row["pos_end"]) - pos_min
        arr_class = D_class if row["cote"] == "D" else G_class
        arr_talus = D_talus if row["cote"] == "D" else G_talus
        arr_centre = D_centre if row["cote"] == "D" else G_centre
        arr_class[s:e] = row["classification"]
        h_t = row.get("hauteur_talus_max", np.nan)
        h_c = row.get("hauteur_centre_max", np.nan)
        if pd.notna(h_t):
            arr_talus[s:e] = float(h_t)
        if pd.notna(h_c):
            arr_centre[s:e] = float(h_c)

        if pd.notna(row["sh_left"]):
            sh = float(row["sh_left"])
            if abs(sh) > HEIGHT_CAP:
                sh = np.sign(sh) * HEIGHT_CAP
            seg = sh_left[s:e]
            seg[np.isnan(seg)] = sh
        if pd.notna(row["sh_right"]):
            sh = float(row["sh_right"])
            if abs(sh) > HEIGHT_CAP:
                sh = np.sign(sh) * HEIGHT_CAP
            seg = sh_right[s:e]
            seg[np.isnan(seg)] = sh

    pont_mask = np.zeros(n, dtype=bool)
    for pos in pont_positions:
        s = max(0, pos - pos_min - PONT_MARGIN)
        e = min(n, pos - pos_min + PONT_MARGIN + 1)
        if s < e:
            pont_mask[s:e] = True
    if output_dir:
        pont_mask = apply_gpkg_pont_mask(pont_mask, pos_min, output_dir)

    return D_class, D_talus, G_class, G_talus, D_centre, G_centre, sh_left, sh_right, pont_mask, pos_min


def _abs_or_nan(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    try:
        if pd.isna(v):
            return np.nan
    except (TypeError, ValueError):
        pass
    return abs(float(v))


def harmonize_cell(d, g, d_centre, g_centre):
    """H4."""
    if d is None and g is None:
        return None
    if d is None:
        return g
    if g is None:
        return d
    if d == g:
        return d
    if d == MUR:
        return g
    if g == MUR:
        return d
    if d == "rasant" and g in SIGNIFICANT:
        return g
    if g == "rasant" and d in SIGNIFICANT:
        return d
    dc, gc = _abs_or_nan(d_centre), _abs_or_nan(g_centre)
    if pd.isna(gc) or (not pd.isna(dc) and dc >= gc):
        return d
    return g


def harmonize_grid(D_class, G_class, D_centre, G_centre, pont_mask):
    n = len(D_class)
    harmonized = np.empty(n, dtype=object)
    for i in range(n):
        if pont_mask[i]:
            harmonized[i] = "PONT"
        else:
            harmonized[i] = harmonize_cell(D_class[i], G_class[i], D_centre[i], G_centre[i])
    return harmonized


def run_length_encode(arr):
    runs = []
    n = len(arr)
    i = 0
    while i < n:
        j = i
        v = arr[i]
        while j < n and arr[j] == v:
            j += 1
        runs.append((i, j, v))
        i = j
    return runs


def drop_soft_gaps(runs, max_gap=MAX_SOFT_GAP_M):
    """H6."""
    result = []
    pending_gap = None
    for s, e, v in runs:
        if v is None:
            pending_gap = (s, e)
            continue
        if pending_gap is not None:
            gs, ge = pending_gap
            gap_len = ge - gs
            if result and result[-1][2] == v and v != "PONT" and gap_len <= max_gap:
                ps, _, pv = result[-1]
                result[-1] = (ps, e, pv)
            else:
                result.append((s, e, v))
            pending_gap = None
        else:
            if result and result[-1][2] == v:
                ps, _, pv = result[-1]
                result[-1] = (ps, e, pv)
            else:
                result.append((s, e, v))
    return result


def merge_adjacent(runs):
    out = []
    for s, e, v in runs:
        if out and out[-1][2] == v:
            out[-1] = (out[-1][0], e, v)
        else:
            out.append((s, e, v))
    return out


def _run_strength(s, e, sh_left, sh_right):
    if sh_left is None or sh_right is None:
        return 99.0
    vals = np.concatenate([np.asarray(sh_left[s:e], dtype=float), np.asarray(sh_right[s:e], dtype=float)])
    vals = vals[np.isfinite(vals)]
    if len(vals) == 0:
        return 0.0
    return float(np.nanmax(np.abs(vals)))


def absorb_islands(runs, sh_left=None, sh_right=None):
    """H7 sandwich + H8/H13 frontiere + H18 rasant vs pont / bout de chaine."""
    runs = list(runs)
    log = []
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(runs) - 1:
            l_s, l_e, l_v = runs[i - 1]
            m_s, m_e, m_v = runs[i]
            r_s, r_e, r_v = runs[i + 1]
            mid_len = m_e - m_s
            left_len = l_e - l_s
            right_len = r_e - r_s

            if m_v in NON_ABSORBABLE:
                i += 1
                continue

            pont_left = l_v == "PONT"
            pont_right = r_v == "PONT"
            oet_left = l_v in OET_CLASSES
            oet_right = r_v in OET_CLASSES
            if (
                m_v == "rasant"
                and mid_len <= RASANT_FRONTIERE_M
                and (pont_left ^ pont_right)
                and (oet_left or oet_right)
            ):
                target = l_v if oet_left else r_v
                log.append({
                    "regle": "rasant_voisin_pont",
                    "classification_absorbee": m_v,
                    "longueur_absorbee_m": mid_len,
                    "reclasse_en": target,
                    "position_debut_idx": m_s,
                    "position_fin_idx": m_e,
                })
                if oet_left:
                    runs[i - 1:i + 1] = [(l_s, m_e, l_v)]
                else:
                    runs[i:i + 2] = [(m_s, r_e, r_v)]
                changed = True
                break

            if l_v in NON_ABSORBABLE or r_v in NON_ABSORBABLE:
                i += 1
                continue

            if l_v == r_v:
                limit = SANDWICH_RASANT_M if m_v == "rasant" else SANDWICH_MAX_M
                if mid_len <= limit and left_len >= mid_len and right_len >= mid_len:
                    log.append({
                        "regle": "sandwich_voisins_identiques",
                        "classification_absorbee": m_v,
                        "longueur_absorbee_m": mid_len,
                        "reclasse_en": l_v,
                        "position_debut_idx": m_s,
                        "position_fin_idx": m_e,
                    })
                    runs[i - 1:i + 2] = [(l_s, r_e, l_v)]
                    changed = True
                    break
                i += 1
                continue

            weak_oet = (
                m_v in OET_CLASSES
                and mid_len <= MIN_OET_WEAK_M
                and _run_strength(m_s, m_e, sh_left, sh_right) < OET_WEAK_H
            )
            take_frontier = (
                mid_len <= FRONTIERE_MIN_M
                or (m_v == "rasant" and mid_len <= RASANT_FRONTIERE_M)
                or weak_oet
            )
            if take_frontier:
                merge_left = left_len >= right_len
                target = l_v if merge_left else r_v
                log.append({
                    "regle": "frontiere_voisin_plus_long" if not weak_oet else "ilot_oet_faible",
                    "classification_absorbee": m_v,
                    "longueur_absorbee_m": mid_len,
                    "reclasse_en": target,
                    "position_debut_idx": m_s,
                    "position_fin_idx": m_e,
                })
                if merge_left:
                    runs[i - 1:i + 1] = [(l_s, m_e, l_v)]
                else:
                    runs[i:i + 2] = [(m_s, r_e, r_v)]
                changed = True
                break
            i += 1

    # H18 : rasant court en bout de chaine, colle a un OET (talus ambigus)
    if len(runs) >= 2:
        s0, e0, v0 = runs[0]
        if v0 == "rasant" and (e0 - s0) <= RASANT_FRONTIERE_M and runs[1][2] in OET_CLASSES:
            log.append({
                "regle": "rasant_voisin_oet",
                "classification_absorbee": v0,
                "longueur_absorbee_m": e0 - s0,
                "reclasse_en": runs[1][2],
                "position_debut_idx": s0,
                "position_fin_idx": e0,
            })
            runs[0:2] = [(s0, runs[1][1], runs[1][2])]
        if len(runs) >= 2:
            s1, e1, v1 = runs[-1]
            if v1 == "rasant" and (e1 - s1) <= RASANT_FRONTIERE_M and runs[-2][2] in OET_CLASSES:
                log.append({
                    "regle": "rasant_voisin_oet",
                    "classification_absorbee": v1,
                    "longueur_absorbee_m": e1 - s1,
                    "reclasse_en": runs[-2][2],
                    "position_debut_idx": s1,
                    "position_fin_idx": e1,
                })
                runs[-2:] = [(runs[-2][0], e1, runs[-2][2])]
    return merge_adjacent(runs), log


def realign_frontiers(runs, raw_cls, max_tail=FRONTIER_TAIL_M, orig=None):
    """H20 : toute la queue de l'OET 1 deja du type OET 2 passe a l'OET 2.

    On recule depuis la frontiere, profil par profil (10 m), tant que la
    morphologie brute est celle de l'OET suivant. Pas une fenetre fixe de 4.
    """
    if raw_cls is None or len(runs) < 2:
        return list(runs), []
    runs = list(runs)
    log = []
    i = 0
    while i < len(runs) - 1:
        s1, e1, v1 = runs[i]
        s2, e2, v2 = runs[i + 1]
        if v1 is None or v2 is None or v1 in NON_ABSORBABLE or v2 in NON_ABSORBABLE:
            i += 1
            continue
        if v1 == v2:
            i += 1
            continue
        max_move = min(max_tail, e1 - s1 - FRONTIER_MIN_KEEP_M)
        if max_move < FRONTIER_MIN_MOVE_M:
            i += 1
            continue
        new_e1 = e1
        while new_e1 - 10 >= e1 - max_move and new_e1 - 10 >= s1:
            a, b2 = new_e1 - 10, new_e1
            sl = [x for x in raw_cls[a:b2] if x is not None]
            if not sl:
                break
            vals, counts = np.unique(np.asarray(sl, dtype=object), return_counts=True)
            if str(vals[int(np.argmax(counts))]) != str(v2):
                break
            new_e1 = a
        moved = e1 - new_e1
        if moved < FRONTIER_MIN_MOVE_M or new_e1 - s1 < FRONTIER_MIN_KEEP_M:
            i += 1
            continue
        log.append({
            "regle": "frontiere_queue_vers_suivant",
            "classification_absorbee": v1,
            "longueur_absorbee_m": moved,
            "reclasse_en": v2,
            "position_debut_idx": new_e1,
            "position_fin_idx": e1,
        })
        runs[i] = (s1, new_e1, v1)
        runs[i + 1] = (new_e1, e2, v2)
        if orig is not None:
            orig[new_e1:e1] = v2
        i += 1
    return merge_adjacent(runs), log


def absorb_pont_crumbs(runs, max_len=PONT_CRUMB_M):
    """H14 : miettes collees a un pont, ou trou court entre deux ponts."""
    runs = list(runs)
    log = []
    changed = True
    while changed:
        changed = False
        for i, (s, e, v) in enumerate(runs):
            if v in ("PONT", MUR) or v is None:
                continue
            if v is None:
                continue
            if e - s > max_len:
                continue
            left = runs[i - 1] if i > 0 else None
            right = runs[i + 1] if i + 1 < len(runs) else None
            left_pont = left is not None and left[2] == "PONT"
            right_pont = right is not None and right[2] == "PONT"
            if not left_pont and not right_pont:
                continue
            if left_pont and right_pont:
                log.append({
                    "regle": "trou_entre_ponts",
                    "classification_absorbee": v,
                    "longueur_absorbee_m": e - s,
                    "reclasse_en": "PONT",
                    "position_debut_idx": s,
                    "position_fin_idx": e,
                })
                runs[i - 1:i + 2] = [(left[0], right[1], "PONT")]
                changed = True
                break
            other = right if left_pont else left
            if other is None or other[2] in NON_ABSORBABLE:
                continue
            log.append({
                "regle": "miette_collée_pont",
                "classification_absorbee": v,
                "longueur_absorbee_m": e - s,
                "reclasse_en": other[2],
                "position_debut_idx": s,
                "position_fin_idx": e,
            })
            if left_pont:
                runs[i:i + 2] = [(s, other[1], other[2])]
            else:
                runs[i - 1:i + 1] = [(other[0], e, other[2])]
            changed = True
            break
    return merge_adjacent(runs), log


def _nanmax_pair(a, b):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmax(np.vstack([a, b]), axis=0)


def _nanmax_abs_pair(a, b):
    return _nanmax_pair(np.abs(a), np.abs(b))


def stats_for_run(s, e, classe, orig, D_talus, G_talus, D_centre, G_centre, sh_left, sh_right):
    """H10 + H11."""
    orig_seg = orig[s:e]
    mask = orig_seg == classe

    overlay_abs = _nanmax_abs_pair(
        np.asarray(sh_left[s:e], dtype=float),
        np.asarray(sh_right[s:e], dtype=float),
    )
    source_abs = _nanmax_abs_pair(
        np.asarray(D_talus[s:e], dtype=float),
        np.asarray(G_talus[s:e], dtype=float),
    )
    talus_abs = np.where(np.isfinite(overlay_abs), overlay_abs, source_abs)
    talus_abs = np.where(talus_abs > HEIGHT_CAP, HEIGHT_CAP, talus_abs)
    centre = _nanmax_abs_pair(D_centre[s:e], G_centre[s:e])

    if classe in (MUR, "PONT", "pont"):
        h_max = np.nan
    elif classe == "rasant":
        vals = centre[mask] if mask.any() else centre
        vals = vals[~np.isnan(vals)]
        vals = vals[vals <= CENTRE_RASANT_ABS + 0.05]
        h_max = float(np.nanmax(vals)) if len(vals) else np.nan
    else:
        # H16 : |H| lu sur l'axe Y (overlay), pas le max algebrique source
        # (qui ecrasait les deblais negatifs).
        vals = talus_abs[mask] if mask.any() else talus_abs
        vals = vals[~np.isnan(vals)]
        vals = vals[vals <= HEIGHT_CAP]
        if len(vals):
            h_max = float(np.nanpercentile(vals, 90))
        else:
            h_max = np.nan

    all_vals = np.concatenate([sh_left[s:e], sh_right[s:e]])
    all_vals = all_vals[~np.isnan(all_vals)]
    all_vals = all_vals[np.abs(all_vals) <= HEIGHT_CAP]
    if len(all_vals):
        remblai_vals = all_vals[all_vals > 0]
        deblai_vals = all_vals[all_vals < 0]
        h_remblai = float(np.max(remblai_vals)) if len(remblai_vals) else np.nan
        h_deblai = float(np.min(deblai_vals)) if len(deblai_vals) else np.nan
    else:
        h_remblai, h_deblai = np.nan, np.nan

    return {
        "hauteur_affichee": round(h_max, 2) if not np.isnan(h_max) else np.nan,
        "hauteur_remblai_m": round(h_remblai, 2) if not np.isnan(h_remblai) else np.nan,
        "hauteur_deblai_m": round(h_deblai, 2) if not np.isnan(h_deblai) else np.nan,
    }


def pos_to_pr_abcisse(pos):
    return int(pos // 1000), int(pos % 1000)


def _pos_m(pr, ab):
    return int(pr) * 1000 + int(ab)


def export_livrable_gpkg(out_df, output_dir, dest_path):
    """Couche QGIS : une ligne du livrable = un troncon sur la ligne centrale."""
    from shapely.ops import substring, unary_union
    import verify_livrable as v

    axis_path = os.path.join(output_dir, "axis_lots.gpkg")
    pr_path = os.path.join(output_dir, "projected_PR.gpkg")
    cl_path = os.path.join(output_dir, "central_line_profil.gpkg")
    src_path = os.path.join(output_dir, "selected_ouvrages.gpkg")
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    if os.path.exists(axis_path):
        axis = v.CompoundAxis(gpd.read_file(axis_path))
        geoms = []
        for _, r in out_df.iterrows():
            geoms.append(axis.substring_pr(
                r["PR_start"], r["abcisse_start"], r["PR_end"], r["abcisse_end"],
                r.get("length", 1),
            ))
        gdf = gpd.GeoDataFrame(out_df.copy(), geometry=geoms, crs="EPSG:2154")
    elif os.path.exists(pr_path) and os.path.exists(cl_path):
        pr_gdf = gpd.read_file(pr_path)
        cl_gdf = gpd.read_file(cl_path)
        centerline = cl_gdf.geometry.iloc[0]
        if centerline.geom_type == "MultiLineString":
            centerline = max(centerline.geoms, key=lambda g: g.length)
        locator = v.PrLocator(pr_gdf)
        geoms = []
        for _, r in out_df.iterrows():
            cl_s = locator.cl_distance(r["PR_start"], r["abcisse_start"])
            cl_e = locator.cl_distance(r["PR_end"], r["abcisse_end"])
            cl_s = max(0.0, min(cl_s, centerline.length))
            cl_e = max(0.0, min(cl_e, centerline.length))
            if cl_e <= cl_s:
                cl_e = min(centerline.length, cl_s + max(float(r.get("length", 1)), 1.0))
            geoms.append(substring(centerline, cl_s, cl_e))
        gdf = gpd.GeoDataFrame(out_df.copy(), geometry=geoms, crs=cl_gdf.crs)
    elif os.path.exists(src_path):
        src = gpd.read_file(src_path)
        xlsx_path = os.path.join(output_dir, "selected_ouvrages.xlsx")
        if os.path.exists(xlsx_path):
            xlsx = pd.read_excel(xlsx_path)
            keep = [c for c in ("nom", "PR_start", "abcisse_start", "PR_end", "abcisse_end") if c in xlsx.columns]
            if "nom" in keep and len(keep) > 1:
                src = src.drop(columns=[c for c in keep if c != "nom" and c in src.columns], errors="ignore")
                src = src.merge(xlsx[keep], on="nom", how="left")
        src_s = pd.to_numeric(src["PR_start"], errors="coerce") * 1000 + pd.to_numeric(src["abcisse_start"], errors="coerce")
        src_e = pd.to_numeric(src["PR_end"], errors="coerce") * 1000 + pd.to_numeric(src["abcisse_end"], errors="coerce")
        geoms = []
        for _, r in out_df.iterrows():
            s = _pos_m(r["PR_start"], r["abcisse_start"])
            e = _pos_m(r["PR_end"], r["abcisse_end"])
            subset = src.loc[(src_s < e) & (src_e > s)]
            if len(subset):
                geoms.append(unary_union(list(subset.geometry)))
            else:
                geoms.append(None)
        gdf = gpd.GeoDataFrame(out_df.copy(), geometry=geoms, crs=src.crs)
        print("Pas de ligne centrale : GPKG construit depuis selected_ouvrages.gpkg.")
    else:
        print("Pas de projected_PR ni selected_ouvrages.gpkg : GPKG livrable ignore.")
        return None

    gdf.to_file(dest_path, driver="GPKG", layer="inventaire")
    print(f"Ecrit : {dest_path} ({len(gdf)} troncons, {gdf.crs})")
    return gdf


def _mur_label(s, e, D_class, G_class):
    has_d = D_class is not None and np.any(D_class[s:e] == MUR)
    has_g = G_class is not None and np.any(G_class[s:e] == MUR)
    if has_d and has_g:
        return "D et G"
    if has_d:
        return "D"
    if has_g:
        return "G"
    return "-"


def build_output(
    route, runs, orig, D_talus, G_talus, D_centre, G_centre,
    sh_left, sh_right, pos_min, D_class=None, G_class=None,
):
    rows = []
    for s, e, v in runs:
        if v is None:
            continue
        pos_start = s + pos_min
        pos_end = e + pos_min
        pr_s, ab_s = pos_to_pr_abcisse(pos_start)
        pr_e, ab_e = pos_to_pr_abcisse(pos_end)
        classe = "pont" if v == "PONT" else v
        if classe == "pont":
            stats = {
                "hauteur_affichee": np.nan,
                "hauteur_remblai_m": np.nan,
                "hauteur_deblai_m": np.nan,
            }
            mur = "-"
        else:
            stats = stats_for_run(
                s, e, v, orig, D_talus, G_talus, D_centre, G_centre, sh_left, sh_right,
            )
            mur = _mur_label(s, e, D_class, G_class)
        rows.append({
            "nom": f"{route}_PR{pr_s}+{ab_s}",
            "classification": classe,
            "mur_soutenement": mur,
            "PR_start": pr_s,
            "abcisse_start": ab_s,
            "PR_end": pr_e,
            "abcisse_end": ab_e,
            "length": pos_end - pos_start,
            **stats,
        })
    out = pd.DataFrame(rows)
    if len(out):
        for col in ["hauteur_remblai_m", "hauteur_deblai_m"]:
            out.loc[out["classification"] != "mixte", col] = np.nan
    return out


def _livrable_frame(df):
    dash = "-"

    def _h(val):
        if pd.isna(val):
            return dash
        return f"{float(val):.2f}".replace(".", ",")

    rows = []
    for _, r in df.iterrows():
        mixte = r["classification"] == "mixte"
        rows.append({
            "Tronçon": r["nom"],
            "PR début": int(r["PR_start"]),
            "Abs début": f"{int(r['abcisse_start']):03d}",
            "PR fin": int(r["PR_end"]),
            "Abs fin": f"{int(r['abcisse_end']):03d}",
            "Longueur (m)": int(r["length"]),
            "Classification": r["classification"],
            "Mur soutènement": r["mur_soutenement"] if r["mur_soutenement"] else dash,
            "H max (m)": _h(r["hauteur_affichee"]),
            "H remblai (m)": _h(r["hauteur_remblai_m"]) if mixte else dash,
            "H déblai (m)": _h(r["hauteur_deblai_m"]) if mixte else dash,
        })
    return pd.DataFrame(rows)


def save_styled_excel(df, path):
    liv = _livrable_frame(df)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        liv.to_excel(writer, sheet_name="Inventaire", index=False)
        ws = writer.sheets["Inventaire"]

        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")

        classification_col = None
        for cell in ws[1]:
            if cell.value == "Classification":
                classification_col = cell.column
                break

        if classification_col is not None:
            for row in ws.iter_rows(min_row=2):
                classe = row[classification_col - 1].value
                fill = FILLS.get(str(classe).lower())
                if fill:
                    for cell in row:
                        cell.fill = fill

        for column_cells in ws.columns:
            max_length = 0
            column = column_cells[0].column_letter
            for cell in column_cells:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except Exception:
                    pass
            ws.column_dimensions[column].width = max(10, min(max_length + 2, 22))

        ws.freeze_panes = "A2"
        for col_name in ("Abs début", "Abs fin"):
            letter = None
            for cell in ws[1]:
                if cell.value == col_name:
                    letter = cell.column_letter
                    break
            if letter:
                for cell in ws[letter][1:]:
                    cell.number_format = "@"
                    if cell.value is not None:
                        cell.value = f"{int(cell.value):03d}"


def save_docx(df, path, route, n_source=None, n_abs=None, log_df=None, departement="54"):
    from docx import Document
    from docx.oxml.ns import nsdecls
    from docx.oxml import parse_xml
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Cm, Emu, Pt
    from docx.enum.section import WD_ORIENT

    liv = _livrable_frame(df)
    n_sec = len(liv)

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Cm(29.7)
    section.page_height = Cm(21.0)
    section.left_margin = Emu(317500)
    section.right_margin = Emu(317500)
    section.top_margin = Emu(444500)
    section.bottom_margin = Emu(444500)

    doc.add_heading(
        f"Inventaire des ouvrages en terre - {route} (livrable operationnel)",
        level=1,
    )
    doc.add_heading(
        f"Une ligne = une section physique, classification harmonisée D/G, département {departement}",
        level=2,
    )

    src = n_source if n_source is not None else "?"
    nab = n_abs if n_abs is not None else "?"
    doc.add_paragraph(
        f"Source : selected_ouvrages.xlsx ({src} lignes D+G), "
        f"harmonisees en {n_sec} sections ({nab} absorption(s) dans absorptions_ilots.csv)."
    )

    doc.add_heading("Hypothèses (avant le tableau)", level=2)
    for hyp in HYPOTHESES:
        doc.add_paragraph(hyp)

    if log_df is not None and len(log_df):
        n_s = int((log_df["regle"] == "sandwich_voisins_identiques").sum()) if "regle" in log_df.columns else 0
        n_f = int(log_df["regle"].isin(["frontiere_voisin_plus_long", "ilot_oet_faible"]).sum()) if "regle" in log_df.columns else 0
        n_p = int(log_df["regle"].isin(["miette_collée_pont", "trou_entre_ponts"]).sum()) if "regle" in log_df.columns else 0
        n_n = int(log_df["regle"].isin(["rasant_voisin_pont", "rasant_voisin_oet"]).sum()) if "regle" in log_df.columns else 0
        n_q = int((log_df["regle"] == "frontiere_queue_vers_suivant").sum()) if "regle" in log_df.columns else 0
        doc.add_paragraph(
            f"Application sur ce linéaire : {n_s} sandwich(s) H7, {n_f} frontière(s)/ilot(s) H8-H13, {n_p} miette(s) pont H14, {n_n} rasant(s) rattache(s) a l'OET voisin H18, {n_q} queue(s) recalee(s) sur l'OET suivant H20."
        )

    doc.add_heading("Tableau des sections", level=2)

    cols = list(liv.columns)
    table = doc.add_table(rows=1, cols=len(cols))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    def _set_run(cell, text, bold=False, size=15, align="center"):
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = (
            WD_ALIGN_PARAGRAPH.LEFT if align == "left" else WD_ALIGN_PARAGRAPH.CENTER
        )
        run = p.add_run(str(text))
        run.bold = bold
        run.font.size = Pt(size / 2)
        run.font.name = "Calibri"
        tcPr = cell._tc.get_or_add_tcPr()
        tcPr.append(parse_xml(f'<w:vAlign {nsdecls("w")} w:val="center"/>'))
        tcPr.append(parse_xml(
            f'<w:tcMar {nsdecls("w")}>'
            f'<w:top w:w="40" w:type="dxa"/>'
            f'<w:left w:w="80" w:type="dxa"/>'
            f'<w:bottom w:w="40" w:type="dxa"/>'
            f'<w:right w:w="80" w:type="dxa"/>'
            f"</w:tcMar>"
        ))

    def _shade(cell, hex_color):
        cell._tc.get_or_add_tcPr().append(
            parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}" w:val="clear"/>')
        )

    hdr = table.rows[0].cells
    for i, c in enumerate(cols):
        _set_run(hdr[i], c, bold=True, size=15)
        _shade(hdr[i], "D9D9D9")

    for _, row in liv.iterrows():
        cells = table.add_row().cells
        hex_color = DOCX_HEX.get(str(row["Classification"]).lower(), "FFFFFF")
        for i, c in enumerate(cols):
            align = "left" if c == "Tronçon" else "center"
            _set_run(cells[i], row[c], bold=False, size=15, align=align)
            _shade(cells[i], hex_color)

    doc.save(path)


def run_pipeline(df, pont_positions, route="A31", output_dir=None, mnt_path="data/mnt.tif"):
    (
        D_class, D_talus, G_class, G_talus, D_centre, G_centre,
        sh_left, sh_right, pont_mask, pos_min,
    ) = build_grid(df, list(pont_positions), output_dir=output_dir)

    harmonized = harmonize_grid(D_class, G_class, D_centre, G_centre, pont_mask)
    raw_cls = np.array(harmonized, dtype=object)
    if output_dir:
        harmonized, sh_left, sh_right, raw_cls = overlay_verify_talus(
            harmonized, sh_left, sh_right, pont_mask, pos_min, output_dir, mnt_path,
        )
    orig = np.array(harmonized, dtype=object)

    runs = run_length_encode(harmonized)
    runs = drop_soft_gaps(runs, max_gap=MAX_SOFT_GAP_M)
    runs, absorption_log = absorb_islands(runs, sh_left=sh_left, sh_right=sh_right)
    runs, crumb_log = absorb_pont_crumbs(runs)
    runs, frontier_log = realign_frontiers(runs, raw_cls, orig=orig)
    absorption_log = list(absorption_log) + list(crumb_log) + list(frontier_log)

    out = build_output(
        route, runs, orig, D_talus, G_talus, D_centre, G_centre,
        sh_left, sh_right, pos_min, D_class=D_class, G_class=G_class,
    )

    log_df = pd.DataFrame([
        {
            **entry,
            "position_debut": pos_to_pr_abcisse(entry["position_debut_idx"] + pos_min),
            "position_fin": pos_to_pr_abcisse(entry["position_fin_idx"] + pos_min),
        }
        for entry in absorption_log
    ])
    return out, log_df


def main():
    parser = argparse.ArgumentParser(description="Genere l'inventaire operationnel harmonise D/G (v10)")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dest-dir", default=None)
    parser.add_argument("--version", default="v9")
    parser.add_argument("--route", default="A31")
    parser.add_argument("--mnt", default="data/mnt.tif")
    parser.add_argument("--departement", default="54")
    args = parser.parse_args()

    dest = args.dest_dir or os.path.join(args.output_dir, "sections_apres_conditions", args.version)
    os.makedirs(dest, exist_ok=True)

    xlsx_path = os.path.join(args.output_dir, "selected_ouvrages.xlsx")
    gpkg_path = os.path.join(args.output_dir, "selected_ouvrages.gpkg")
    rejected_path = os.path.join(args.output_dir, "rejected_ouvrages.csv")

    df = load_selected_ouvrages(xlsx_path, gpkg_path)
    n_source = len(df)
    pont_positions = load_pont_positions(rejected_path)

    out, log_df = run_pipeline(
        df, pont_positions, route=args.route,
        output_dir=args.output_dir, mnt_path=args.mnt,
    )

    def _save_xlsx(path):
        try:
            save_styled_excel(out, path)
            print(f"Ecrit : {path} ({len(out)} sections)")
            return path
        except PermissionError:
            alt = path.replace(".xlsx", "_nouveau.xlsx")
            save_styled_excel(out, alt)
            print(f"Fichier ouvert ailleurs. Ecrit : {alt} ({len(out)} sections)")
            return alt

    out_path = _save_xlsx(os.path.join(dest, "inventaire_operationnel.xlsx"))
    print(out["classification"].value_counts())
    _save_xlsx(os.path.join(dest, "inventaire_operationnel_ponts.xlsx"))
    try:
        export_livrable_gpkg(
            out, args.output_dir, os.path.join(dest, "inventaire_operationnel.gpkg"),
        )
    except Exception as exc:
        print(f"GPKG livrable non genere : {exc}")

    log_path = os.path.join(dest, "absorptions_ilots.csv")
    log_df.to_csv(log_path, index=False)
    print(f"Ecrit : {log_path} ({len(log_df)} absorptions)")

    n_abs = len(log_df)
    for name in (
        f"inventaire_{args.route}_operationnel.docx",
        f"inventaire_{args.route}_operationnel_{args.version}.docx",
    ):
        docx_path = os.path.join(dest, name)
        try:
            save_docx(
                out, docx_path, args.route,
                n_source=n_source, n_abs=n_abs, log_df=log_df,
                departement=args.departement,
            )
            print(f"Ecrit : {docx_path}")
        except ImportError:
            print("python-docx absent : Word non genere. pip install python-docx")
            break
        except PermissionError:
            print(f"Fichier ouvert ailleurs, Word non ecrase : {docx_path}")

    return out, log_df


if __name__ == "__main__":
    main()

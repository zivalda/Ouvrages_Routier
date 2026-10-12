"""
Recalcule la colonne 'classification' de classified_profiles.gpkg (sortie de
ProfileAnalyzer.analyze_profile) avec la regle mixte, sans jamais retoucher
au MNT.

  - 1ere passe (inchangee) : classify_point() a partir de
    height_difference_nat_terrain, avec les seuils reels du run
    (classification_threshold_remblai=2, classification_threshold_deblai=-2).

  - 2e passe, sur TOUS les profils (plus seulement si le centre est rasant),
    a partir des talus gauche/droite (left_dist/alt, right_dist/alt) :
      * cote "remblai" si hauteur signee >= 2, "deblai" si <= -2, sinon rasant
      * les deux cotes opposes (remblai vs deblai) -> "mixte"
      * un seul cote significatif, l'autre rasant -> cet OET
      * les deux cotes rasant -> on garde la 1ere passe (centre)

Ceci est la source de verite partagee avec profile_analyzer_viz.py.
"""

import sys
import geopandas as gpd
import pandas as pd
import numpy as np

CLASSIFICATION_THRESHOLD_REMBLAI = 2
CLASSIFICATION_THRESHOLD_DEBLAI = -2

MIXTE_THRESHOLD = 2  # meme seuil que le centre (remblai >= 2, deblai <= -2)


def first_pass_classification(height_difference):
    if pd.isna(height_difference):
        return "unknown"
    if height_difference >= CLASSIFICATION_THRESHOLD_REMBLAI:
        return "remblai"
    elif height_difference <= CLASSIFICATION_THRESHOLD_DEBLAI:
        return "deblai"
    else:
        return "rasant"


def signed_height_left(dist_min, dist_max, alt_min, alt_max):
    if pd.isna(dist_min) or pd.isna(dist_max) or pd.isna(alt_min) or pd.isna(alt_max):
        return None
    # cote gauche : la plateforme est du cote des grandes distances (proche
    # de platform_start) ; si le point "max" (le plus haut) est celui qui a
    # la plus grande distance (donc le plus proche de la route), le terrain
    # descend en s'eloignant de la route -> profil remblai (positif).
    if dist_max > dist_min:
        return alt_max - alt_min
    else:
        return -(alt_max - alt_min)


def signed_height_right(dist_min, dist_max, alt_min, alt_max):
    if pd.isna(dist_min) or pd.isna(dist_max) or pd.isna(alt_min) or pd.isna(alt_max):
        return None
    # cote droit : la plateforme est du cote des petites distances ; si le
    # point "max" a la plus PETITE distance (donc le plus proche de la
    # route), le terrain descend en s'eloignant -> remblai (positif).
    if dist_max < dist_min:
        return alt_max - alt_min
    else:
        return -(alt_max - alt_min)


def side_class(signed_height):
    if signed_height is None:
        return "rasant"
    try:
        if np.isnan(signed_height):
            return "rasant"
    except TypeError:
        pass
    if signed_height >= MIXTE_THRESHOLD:
        return "remblai"
    elif signed_height <= -MIXTE_THRESHOLD:
        return "deblai"
    else:
        return "rasant"


def combine_side_and_center(center_class, left_class, right_class):
    """Fusionne le classement du centre et des deux talus.

    L'opposition gauche/droite (mixte) et un talus significatif d'un seul
    cote l'emportent sur un centre rasant *et* sur un centre remblai/deblai :
    un vrai mixte ne doit plus disparaitre parce que la plateforme est 2 m
    au-dessus du terrain naturel.
    """
    if {left_class, right_class} == {"remblai", "deblai"}:
        return "mixte"
    if left_class == right_class and left_class in ("remblai", "deblai"):
        return left_class
    if left_class in ("remblai", "deblai") and right_class == "rasant":
        return left_class
    if right_class in ("remblai", "deblai") and left_class == "rasant":
        return right_class
    if center_class in ("remblai", "deblai", "rasant", "mixte"):
        return center_class
    return "rasant"


def recompute_row(row):
    fp = first_pass_classification(row["height_difference_nat_terrain"])

    lh = signed_height_left(row["left_dist_min"], row["left_dist_max"], row["left_alt_min"], row["left_alt_max"])
    rh = signed_height_right(row["right_dist_min"], row["right_dist_max"], row["right_alt_min"], row["right_alt_max"])

    return combine_side_and_center(fp, side_class(lh), side_class(rh))


def main(in_path, out_path):
    gdf = gpd.read_file(in_path, layer="points")
    print(f"Charge {len(gdf)} points depuis {in_path}")

    old_counts = gdf["classification"].value_counts()
    print("\nAvant (ancienne regle) :")
    print(old_counts)

    gdf["classification_old"] = gdf["classification"]
    gdf["classification"] = gdf.apply(recompute_row, axis=1)

    new_counts = gdf["classification"].value_counts()
    print("\nApres (nouvelle regle) :")
    print(new_counts)

    changed = gdf[gdf["classification"] != gdf["classification_old"]]
    print(f"\nPoints (metres) dont la classification change : {len(changed)} / {len(gdf)}")
    print(changed.groupby(["classification_old", "classification"]).size())

    gdf.to_file(out_path, layer="points", driver="GPKG")
    print(f"\nEcrit : {out_path}")


if __name__ == "__main__":
    in_path = sys.argv[1] if len(sys.argv) > 1 else "classified_profiles.gpkg"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "classified_profiles_v2.gpkg"
    main(in_path, out_path)

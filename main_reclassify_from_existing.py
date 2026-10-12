"""
Regenere l'inventaire (segments + selected_ouvrages.xlsx) d'une route DEJA
traitee par ProfileAnalyzer, avec la nouvelle regle "mixte", SANS refaire
l'analyse par metre sur le MNT.

Reutilise :
  - <existing_output_folder>/classified_profiles.gpkg  (sortie de
    ProfileAnalyzer.analyze_profile, un point par metre le long de la ligne
    centrale)
  - <existing_output_folder>/lines_selected.gpkg  (troncons de route
    BDTOPO recuperes lors du run original, evite un nouvel appel WFS)

Refait, comme le script original (main_ouvrages_detector.py /
main_profiles_detector_long_road.py) :
  - SegmentConstructor.construct_segments()  (a besoin d'un appel WFS pour
    les points de repere -> necessite un reseau qui atteint
    https://data.geopf.fr, donc a lancer depuis le poste ou le run original
    a deja fonctionne, pas depuis un environnement sans acces internet)
  - OuvragesSelector.select_ouvrages()  (idem, appel WFS pour les ponts)

Usage :
    python main_reclassify_from_existing.py --route A31 \
        --existing-output-dir output_A31 \
        --output-dir output_A31_v2_mixte
"""

import argparse
import os

import geopandas as gpd

from recompute_classification import recompute_row
from segments_constructor import SegmentConstructor
from select_ouvrages import OuvragesSelector


def main():
    parser = argparse.ArgumentParser(
        description="Regenere l'inventaire avec la nouvelle regle mixte, a partir d'un run existant"
    )
    parser.add_argument('--route', type=str, required=True, help='Code de la route (ex. A31)')
    parser.add_argument('--existing-output-dir', type=str, required=True,
                         help='Dossier de sortie du run original (contient classified_profiles.gpkg et lines_selected.gpkg)')
    parser.add_argument('--output-dir', type=str, required=True,
                         help='Nouveau dossier de sortie pour le run recalcule')
    args = parser.parse_args()

    route = args.route
    existing_folder = args.existing_output_dir
    output_folder = args.output_dir
    os.makedirs(output_folder, exist_ok=True)

    classified_path = os.path.join(existing_folder, "classified_profiles.gpkg")
    lines_path = os.path.join(existing_folder, "lines_selected.gpkg")

    print(f"[1/4] Chargement de {classified_path}")
    points_gdf = gpd.read_file(classified_path, layer="points")
    print(f"    {len(points_gdf)} points charges")

    print("[2/4] Recalcul de la classification (nouvelle regle mixte, sans MNT)")
    old_counts = points_gdf["classification"].value_counts()
    points_gdf["classification"] = points_gdf.apply(recompute_row, axis=1)
    new_counts = points_gdf["classification"].value_counts()
    print("    Avant :", dict(old_counts))
    print("    Apres :", dict(new_counts))

    print(f"[3/4] Chargement de {lines_path}")
    lines_selected = gpd.read_file(lines_path)

    print("[4/4] Construction des segments + selection des ouvrages (appels WFS IGN)")
    constructor = SegmentConstructor(
        classified_profiles=points_gdf,
        output_folder=output_folder,
        route_number=route,
        lines_selected=lines_selected,
    )
    ouvrages_gdf = constructor.construct_segments()
    constructor.save_output(ouvrages_gdf)

    selector = OuvragesSelector(
        ouvrages_gdf=ouvrages_gdf,
        output_folder=output_folder,
        route_number=route,
        lines_selected=lines_selected,
    )
    selected_ouvrages = selector.select_ouvrages()
    selector.save_output(selected_ouvrages)

    print(f"\nTermine. Nouvel inventaire : {os.path.join(output_folder, 'selected_ouvrages.xlsx')}")


if __name__ == "__main__":
    main()

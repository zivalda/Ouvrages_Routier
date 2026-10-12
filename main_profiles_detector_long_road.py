"""
Variante de main_ouvrages_detector.py pour les longues routes.

Sur une route longue, les requêtes WFS (tronçons, points de repère, ponts)
peuvent renvoyer beaucoup d'entités et durer plusieurs minutes ; sans retry,
une simple erreur transitoire (ex. 504 côté IGN) fait planter tout le run.

get_data_functions.py gère désormais la pagination et les retries pour tout
le monde. Ce script ajoute, en plus, un découpage explicite en étapes avec
suivi du temps écoulé et messages d'erreur clairs par étape, pour pouvoir
diagnostiquer immédiatement sur quelle étape un run de plusieurs dizaines de
minutes a échoué.
"""

import argparse
import sys
import time
import traceback
from contextlib import contextmanager

from profile_analyzer_viz import ProfileAnalyzer
from segments_constructor import SegmentConstructor
from select_ouvrages import OuvragesSelector
from draw_selected_profiles import DrawSelectedProfiles


@contextmanager
def step(name):
    print(f"\n{'='*60}\n[ETAPE] {name}\n{'='*60}")
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"[ECHEC] {name} après {elapsed:.1f}s : {exc}")
        raise
    else:
        elapsed = time.monotonic() - start
        print(f"[OK] {name} terminée en {elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(
        description="Détecteur d'ouvrages en terre — variante robuste pour les longues routes"
    )
    parser.add_argument('--route', type=str, default=None, help='Code de la route (ex. A33)')
    parser.add_argument('--output-dir', type=str, default=None, help='Dossier de sortie (défaut : output_<route>)')
    args = parser.parse_args()

    if args.route:
        route = args.route
    else:
        route = input("Saisir le code de la route (ex. A33): ")

    output_folder = args.output_dir if args.output_dir else f"output_{route}"

    classification_threshold_remblai = 2
    classification_threshold_deblai = -2

    run_start = time.monotonic()

    try:
        with step("Lecture du MNT + récupération des tronçons de route"):
            analyzer = ProfileAnalyzer(
                mnt_path="data/mnt.tif",
                output_folder=output_folder,
                classification_threshold_remblai=classification_threshold_remblai,
                classification_threshold_deblai=classification_threshold_deblai,
                route_number=route
            )

        with step("Analyse des profils en travers"):
            segments_gdf, calculation_points_gdf = analyzer.analyze_profile()
            analyzer.save_output(segments_gdf, calculation_points_gdf)

        with step("Construction des segments (remblai/déblai/rasant)"):
            constructor = SegmentConstructor(
                classified_profiles=segments_gdf,
                output_folder=output_folder,
                route_number=route,
                lines_selected=analyzer.lines_selected
            )
            ouvrages_gdf = constructor.construct_segments()
            constructor.save_output(ouvrages_gdf)

        with step("Sélection des ouvrages (filtrage ponts/CS/FS/DS)"):
            selector = OuvragesSelector(
                ouvrages_gdf=ouvrages_gdf,
                output_folder=output_folder,
                route_number=route,
                lines_selected=analyzer.lines_selected
            )
            selected_ouvrages = selector.select_ouvrages()
            selector.save_output(selected_ouvrages)

        with step("Génération des profils sélectionnés"):
            drawer = DrawSelectedProfiles(
                route_number=route,
                output_folder=output_folder,
                mnt_path="data/mnt.tif"
            )
            drawer.generate()

    except Exception:
        total_elapsed = time.monotonic() - run_start
        print(f"\n[ARRET] Traitement de la route {route} interrompu après {total_elapsed/60:.1f} min.")
        print(traceback.format_exc())
        sys.exit(1)

    total_elapsed = time.monotonic() - run_start
    print(f"\n[TERMINE] Route {route} traitée avec succès en {total_elapsed/60:.1f} min.")
    print(f"Sorties : {output_folder}")


if __name__ == "__main__":
    main()

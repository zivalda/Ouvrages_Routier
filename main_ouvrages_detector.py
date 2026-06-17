import argparse

from profile_analyzer_viz import ProfileAnalyzer
from segments_constructor import SegmentConstructor
from select_ouvrages import OuvragesSelector
from draw_selected_profiles import DrawSelectedProfiles

def main():
    parser = argparse.ArgumentParser(description="Détecteur d'ouvrages en terre")
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

    analyzer = ProfileAnalyzer(
        mnt_path = "data/mnt.tif",
        output_folder = output_folder,
        classification_threshold_remblai = classification_threshold_remblai,
        classification_threshold_deblai = classification_threshold_deblai,
        route_number = route
    )
    segments_gdf, calculation_points_gdf = analyzer.analyze_profile()
    analyzer.save_output(segments_gdf, calculation_points_gdf)
     
    constructor = SegmentConstructor(
        classified_profiles = segments_gdf,
        output_folder = output_folder,
        route_number = route,
        lines_selected = analyzer.lines_selected
    )
    ouvrages_gdf = constructor.construct_segments()
    constructor.save_output(ouvrages_gdf)
    """ 

    constructor = SegmentConstructor(
        classified_profiles = segments_gdf,
        output_folder = output_folder,
        route_number = route,
        lines_selected = analyzer.lines_selected
    )

    constructor.construct()
""" 
    selector = OuvragesSelector(
        ouvrages_gdf = ouvrages_gdf,
        output_folder = output_folder,
        route_number = route,
        lines_selected = analyzer.lines_selected
    )
    selected_ouvrages = selector.select_ouvrages()
    selector.save_output(selected_ouvrages)


    drawer = DrawSelectedProfiles(
        route_number=route,
        output_folder=output_folder,
        mnt_path="data/mnt.tif"
    )

    drawer.generate()

if __name__ == "__main__":
    main()
 
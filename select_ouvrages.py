import geopandas as gpd
import os
import pandas as pd
import numpy as np
from get_data_functions import get_ponts_from_geom
from openpyxl.styles import Font,Alignment,PatternFill



class OuvragesSelector:

    # Natures BD TOPO (couche construction_lineaire) traitées comme de vrais
    # ponts : leur emprise coupe réellement le terrain support, donc on les
    # exclut du calcul de classification (comme les ponts de
    # construction_surfacique).
    NATURES_PONT_LINEAIRE = {"Pont", "Viaduc"}

    # Un mur de soutènement est aussi exclu du calcul (une pente/hauteur de
    # talus naturel n'a pas de sens sur un parement de mur), mais
    # contrairement à un pont il correspond à un vrai ouvrage en terre :
    # voir select_ouvrages() pour comment il est réintégré à l'inventaire.
    NATURES_MUR_SOUTENEMENT = {"Mur de soutènement"}

    def __init__(self, ouvrages_gdf, output_folder, route_number, lines_selected):
        self.ouvrages_gdf = ouvrages_gdf
        self.route = lines_selected
        self.output_folder = output_folder
        self.filter_route = f"numero='{route_number}'"
                # géométrie globale de la route
        route_geom = self.ouvrages_gdf.unary_union
        self.ponts_gdf = get_ponts_from_geom(route_geom, "BDTOPO_V3:construction_surfacique")
        construction_lineaire_gdf = get_ponts_from_geom(route_geom, "BDTOPO_V3:construction_lineaire")
        self.rejected_ouvrages=[]


        #  buffer zone autour de la route
        buffer_distance = 10

        #  filtre spatial
        self.ponts_gdf = self.ponts_gdf[self.ponts_gdf.intersects(route_geom.buffer(buffer_distance))]
        construction_lineaire_gdf = construction_lineaire_gdf[
            construction_lineaire_gdf.intersects(route_geom.buffer(buffer_distance))
        ]

        # construction_lineaire mélange ponts, murs anti-bruit et murs de
        # soutènement (champ "nature") : on ne garde dans ponts2_gdf (utilisé
        # pour supprimer les zones invalides comme un pont) que les vrais
        # ponts. Les murs anti-bruit ne coupent pas le terrassement
        # sous-jacent et ne sont plus exclus du tout. Les murs de
        # soutènement sont gérés à part (cf. select_ouvrages).
        self.ponts2_gdf = self._filter_by_nature(construction_lineaire_gdf, self.NATURES_PONT_LINEAIRE)
        self.murs_soutenement_gdf = self._filter_by_nature(construction_lineaire_gdf, self.NATURES_MUR_SOUTENEMENT)
        self.murs_antibruit_gdf = self._filter_by_nature(construction_lineaire_gdf, {"Mur anti-bruit"})

    @staticmethod
    def _filter_by_nature(gdf, natures):
        if "nature" not in gdf.columns:
            return gdf.iloc[0:0]
        return gdf[gdf["nature"].isin(natures)]

    @staticmethod
    def _hauteur_centre_max_signee(series):
        """Valeur signée de plus grande magnitude (garde le signe remblai/déblai
        au lieu d'un simple max) ; NaN si le groupe n'a aucune valeur connue
        (cas d'un mur de soutènement, dont la hauteur/pente n'est pas calculée)."""
        if series.notna().any():
            return series.loc[series.abs().idxmax()]
        return np.nan

    def merge_close_segments(self, gdf):
            if len(gdf) <= 1:
                return gdf
                
            # Create buffer around segments
            buffered = gdf.geometry.buffer(5)  # 5m buffer to detect 10m gaps
            
            # Dissolve overlapping buffers
            merged = buffered.unary_union
            
            # Convert merged result back to segments
            if merged.geom_type == 'MultiPolygon':
                merged_segments = []
                for polygon in merged.geoms:
                    # Get original segments that intersect with this merged area
                    intersecting = gdf[gdf.geometry.intersects(polygon)]
                    if not intersecting.empty:
                        # Create a new merged segment
                        merged_geom = intersecting.geometry.unary_union
                        merged_segment = {
                            'geometry': merged_geom,
                            'nom': intersecting.iloc[0]['nom'],
                            'classification': intersecting.iloc[0]['classification'],
                            'chaussee':intersecting.iloc[0]['chaussee'],
                            'PR_start': intersecting.iloc[0]['PR_start'],
                            'abcisse_start': intersecting.iloc[0]['abcisse_start'],
                            'PR_end': intersecting.iloc[-1]['PR_end'],
                            'abcisse_end': intersecting.iloc[-1]['abcisse_end'],
                            'length': intersecting.geometry.length.sum(),
                            #'hauteur_max': intersecting['hauteur_max'].max(),
                            #'pente_max': intersecting['pente_max'].max(),
                            #'hauteur_moyenne': intersecting['hauteur_moyenne'].mean(),
                            #'pente_moyenne': intersecting['pente_moyenne'].mean(),
                            'hauteur_talus_max':intersecting['hauteur_talus_max'].max(),
                            'hauteur_talus_moyenne':intersecting['hauteur_talus_moyenne'].mean(),
                            #'hauteur_centre_max':intersecting['hauteur_centre_max'].max(),
                            'hauteur_centre_max':self._hauteur_centre_max_signee(intersecting['hauteur_centre_max']),
                            'hauteur_centre_moyenne':intersecting['hauteur_centre_moyenne'].mean(),
                            'pente_max':intersecting['pente_max'].max(),
                            'pente_moyenne':intersecting['pente_moyenne'].mean(),
                            'route': intersecting.iloc[0]['route'],
                            'profil_talus_route':intersecting.iloc[0]['profil_talus_route'],

                            'profil_talus_ouvrage':
                            intersecting.iloc[0]['profil_talus_ouvrage'],
                            'profil_talus_route':
                            intersecting.iloc[0]['profil_talus_route'],

                            'profil_talus_ouvrage':
                            intersecting.iloc[0]['profil_talus_ouvrage'],

                            'profil_talus_PR':
                            intersecting.iloc[0]['profil_talus_PR'],

                            'profil_talus_abcisse':
                            intersecting.iloc[0]['profil_talus_abcisse'],

                            'profil_centre_route':
                            intersecting.iloc[0]['profil_centre_route'],

                            'profil_centre_ouvrage':
                            intersecting.iloc[0]['profil_centre_ouvrage'],

                            'profil_pente_route':
                            intersecting.iloc[0]['profil_pente_route'],

                            'profil_pente_ouvrage':
                            intersecting.iloc[0]['profil_pente_ouvrage'],

                            'profil_talus_id':
                            intersecting.iloc[0]['profil_talus_id'],

                            'profil_centre_id':
                            intersecting.iloc[0]['profil_centre_id'],

                            'profil_pente_id':
                            intersecting.iloc[0]['profil_pente_id'],
                            'talus_dist_min':
                            intersecting.iloc[0]['talus_dist_min'],

                            'talus_alt_min':
                            intersecting.iloc[0]['talus_alt_min'],

                            'talus_dist_max':
                            intersecting.iloc[0]['talus_dist_max'],

                            'talus_alt_max':
                            intersecting.iloc[0]['talus_alt_max'],

                            "left_dist_min":
                            intersecting.iloc[0]["left_dist_min"],

                            "left_alt_min":
                            intersecting.iloc[0]["left_alt_min"],

                            "left_dist_max":
                            intersecting.iloc[0]["left_dist_max"],

                            "left_alt_max":
                            intersecting.iloc[0]["left_alt_max"],

                            "right_dist_min":
                            intersecting.iloc[0]["right_dist_min"],

                            "right_alt_min":
                            intersecting.iloc[0]["right_alt_min"],

                            "right_dist_max":
                            intersecting.iloc[0]["right_dist_max"],

                            "right_alt_max":
                            intersecting.iloc[0]["right_alt_max"],
                                                    }
                        merged_segments.append(merged_segment)
                
                if merged_segments:
                    result = gpd.GeoDataFrame(merged_segments, crs=gdf.crs)
                    for col in result.columns:
                        if col in gdf.columns:
                            result[col] = result[col].astype(gdf[col].dtype)
                    return result
                
            return gdf

    def remove_overlapping_zones(self, linestring, zones_a_filtrer, buffer_distance=10):
        for element in zones_a_filtrer.geometry:
            if element.geom_type == 'MultiPolygon':
                element = element.buffer(buffer_distance)
                linestring = linestring.difference(element)
            elif element.geom_type == 'MultiLineString':
                element = element.buffer(buffer_distance)
                linestring = linestring.difference(element)
            elif element.geom_type == "LineString":
                element = element.buffer(buffer_distance)
                linestring = linestring.difference(element)
            else:
                raise ValueError(f"Unsupported geometry type: {element.geom_type}")

        return linestring

    def select_ouvrages(self):
    
        #selected_ouvrages=self.ouvrages_gdf[self.ouvrages_gdf['classification'].isin(['remblai','deblai'])]
        selected_ouvrages=self.ouvrages_gdf.copy()

        # 1) Retrait des zones qui recoupent un vrai pont (construction_surfacique
        #    + construction_lineaire filtrée sur nature in {"Pont","Viaduc"}) :
        #    il n'y a pas de terrain support à cet endroit, l'ouvrage est
        #    invalide -> on le supprime définitivement.
        selected_ouvrages.loc[:,'geometry']=selected_ouvrages['geometry'].apply(lambda x:self.remove_overlapping_zones(x,self.ponts_gdf))

        selected_ouvrages.loc[:,'geometry']=selected_ouvrages['geometry'].apply(lambda x:self.remove_overlapping_zones(x,self.ponts2_gdf))
        for _,row in selected_ouvrages.iterrows():

            if row.geometry.is_empty:

                self.rejected_ouvrages.append({

                    "nom":row["nom"],

                    "PR_start":row["PR_start"],

                    "PR_end":row["PR_end"],

                    "classification":row["classification"],

                    "chaussee":row["chaussee"],

                    "raison_rejet":"supprime_par_pont"

                })

        # 2) Retrait des zones qui recoupent un mur de soutènement. Le calcul
        #    de pente/hauteur (talus naturel supposé régulier) n'a pas de
        #    sens sur un parement de mur, donc on l'exclut aussi du calcul de
        #    classification. Mais contrairement à un pont, il y a bien un
        #    ouvrage en terre à cet endroit : on garde la ligne d'origine
        #    (avant ce retrait) pour la faire réapparaître plus bas dans
        #    l'inventaire comme classification "mur de soutènement", au lieu
        #    de la perdre silencieusement comme un pont.
        avant_mur = selected_ouvrages.copy()
        selected_ouvrages.loc[:,'geometry']=selected_ouvrages['geometry'].apply(lambda x:self.remove_overlapping_zones(x,self.murs_soutenement_gdf))

        mur_rows=[]
        for idx,row in selected_ouvrages.iterrows():

            if row.geometry.is_empty and not avant_mur.loc[idx,'geometry'].is_empty:

                self.rejected_ouvrages.append({

                    "nom":row["nom"],

                    "PR_start":row["PR_start"],

                    "PR_end":row["PR_end"],

                    "classification":row["classification"],

                    "chaussee":row["chaussee"],

                    "raison_rejet":"supprime_par_mur_soutenement"

                })

                mur_row = avant_mur.loc[idx].to_dict()
                mur_row["classification"] = "mur de soutènement"
                for col in ["hauteur_talus_max","hauteur_talus_moyenne","hauteur_centre_max",
                            "hauteur_centre_moyenne","pente_max","pente_moyenne"]:
                    if col in mur_row:
                        mur_row[col] = np.nan
                mur_rows.append(mur_row)

        selected_ouvrages=selected_ouvrages[~selected_ouvrages.is_empty]

        # Create separate GeoDataFrames
        remblai=selected_ouvrages[selected_ouvrages['classification']=="remblai"]
        deblai=selected_ouvrages[selected_ouvrages['classification']=="deblai"]
        rasant=selected_ouvrages[selected_ouvrages['classification']=="rasant"]
        mixte = selected_ouvrages[selected_ouvrages['classification'] == "mixte"]

        if mur_rows:
            mur_soutenement = gpd.GeoDataFrame(pd.DataFrame(mur_rows), geometry='geometry', crs=selected_ouvrages.crs)
        else:
            mur_soutenement = selected_ouvrages.iloc[0:0].copy()

        merged_results=[]

        for classe_gdf in [remblai,deblai,rasant,mixte,mur_soutenement]:

            for chaussee in ["D","G"]:

                subset=classe_gdf[
                    classe_gdf["chaussee"]==chaussee
                ]

                if not subset.empty:

                    merged_results.append(
                        self.merge_close_segments(subset)
                    )

        if not merged_results:

            return gpd.GeoDataFrame(
                crs=selected_ouvrages.crs
            )

        merged_ouvrages=pd.concat(
            merged_results,
            ignore_index=True
        )

        
        merged_ouvrages = merged_ouvrages.drop_duplicates(
            subset=[
                "classification",
                "chaussee",
                "PR_start",
                "abcisse_start",
                "PR_end",
                "abcisse_end"
            ]
        ).reset_index(drop=True)
        

        # Un mur de soutènement reste dans l'inventaire quelle que soit sa
        # longueur : ce n'est pas un artefact de segmentation à filtrer,
        # mais un ouvrage identifié comme tel sur le terrain.
        rejected_length=merged_ouvrages[
            (merged_ouvrages.geometry.length<=20)
            & (merged_ouvrages['classification']!="mur de soutènement")
        ]

        for _,row in rejected_length.iterrows():

            self.rejected_ouvrages.append({

                "nom":row["nom"],

                "PR_start":row["PR_start"],

                "PR_end":row["PR_end"],

                "classification":row["classification"],

                "chaussee":row["chaussee"],

                "raison_rejet":"longueur<20m"

            })

        selected_ouvrages=merged_ouvrages[
            (merged_ouvrages.geometry.length>20)
            | (merged_ouvrages['classification']=="mur de soutènement")
        ].copy()

        selected_ouvrages["PR_num"]=selected_ouvrages["PR_start"].astype(str).str.extract(r'PR(\d+)')[0].astype(int)

        selected_ouvrages["ordre_chaussee"]=selected_ouvrages["PR_start"].astype(str).str[-1].map({
            "D":0,
            "G":1
        })

        selected_ouvrages=selected_ouvrages.sort_values(
            [
                "ordre_chaussee",
                "PR_num",
                "abcisse_start",
                "abcisse_end"
            ]
        ).drop(
            columns=[
                "ordre_chaussee",
                "PR_num"
            ]
        ).reset_index(drop=True)

        pd.DataFrame(
            self.rejected_ouvrages
        ).to_csv(

            os.path.join(
                self.output_folder,
                "rejected_ouvrages.csv"
            ),

            index=False,
            sep=";"
        )

        with open(
            os.path.join(
                self.output_folder,
                "debug_selected_ouvrages.txt"
            ),
            "w",
            encoding="utf-8"
        ) as f:

            for _, row in selected_ouvrages.iterrows():

                f.write(
                    f"\n=====================================\n"
                    f"OUVRAGE={row['nom']}\n"
                    f"classification={row['classification']}\n"
                    f"PR_start={row['PR_start']}\n"
                    f"PR_end={row['PR_end']}\n"
                    f"hauteur_talus_max={row['hauteur_talus_max']}\n"
                    f"hauteur_centre_max={row['hauteur_centre_max']}\n"
                    f"pente_max={row['pente_max']}\n"
                    f"profil_talus_id={row['profil_talus_id']}\n"
                    f"talus_dist_min={row['talus_dist_min']}\n"
                    f"talus_dist_max={row['talus_dist_max']}\n"
                    f"left_dist_min={row['left_dist_min']}\n"
                    f"left_dist_max={row['left_dist_max']}\n"
                    f"right_dist_min={row['right_dist_min']}\n"
                    f"right_dist_max={row['right_dist_max']}\n"
                    f"=====================================\n"
                )

        return selected_ouvrages
    

    def save_output(self, selected_gdf):

        # Créer le dossier si nécessaire
        os.makedirs(self.output_folder, exist_ok=True)

        selected_gdf = selected_gdf.copy()

        # Colonnes numériques à arrondir
        cols_to_round = [
            "length",
            "hauteur_talus_max",
            "talus_dist_min",
            "talus_alt_min",
            "talus_dist_max",
            "talus_alt_max",
            "hauteur_talus_moyenne",
            "hauteur_centre_max",
            "hauteur_centre_moyenne",
            "pente_max",
            "pente_moyenne",
            "abcisse_start",
            "abcisse_end"
        ]

        for col in cols_to_round:
            if col in selected_gdf.columns:
                selected_gdf[col] = (
                    selected_gdf[col]
                    .astype(float)
                    .round(2)
                )

        # Sauvegarde GPKG
        output_file = os.path.join(
            self.output_folder,
            "selected_ouvrages.gpkg"
        )

        selected_gdf.to_file(
            output_file,
            driver='GPKG',
            layer='ouvrages'
        )

        # EXPORT EXCEL
        excel_df = selected_gdf.drop(
            columns=["geometry"],
            errors="ignore"
        )



        excel_df.insert(0, "id", range(1, len(excel_df) + 1))
        

        colonnes = [
            "id",
            "route",
            "nom",
            "classification",
            "PR_start",
            "abcisse_start",
            "PR_end",
            "abcisse_end",
            "length",
            "hauteur_talus_max",
            "hauteur_talus_moyenne",
            "hauteur_centre_max",
            "hauteur_centre_moyenne",
            "pente_max",
            "pente_moyenne",
        ]

        excel_df["PR_start"] = (
            excel_df["PR_start"]
            .astype(str)
            .str.extract(r"PR(\d+)")[0]
        )

        excel_df["PR_end"] = (
            excel_df["PR_end"]
            .astype(str)
            .str.extract(r"PR(\d+)")[0]
        )

        colonnes_existantes = [
            c for c in colonnes
            if c in excel_df.columns
        ]

        excel_df = excel_df[
            colonnes_existantes
        ]

        excel_path = os.path.join(
            self.output_folder,
            "selected_ouvrages.xlsx"
        )

        with pd.ExcelWriter(
            excel_path,
            engine='openpyxl'
        ) as writer:

            excel_df.to_excel(
                writer,
                sheet_name='Ouvrages',
                index=False
            )

            worksheet = writer.sheets[
                'Ouvrages'
            ]
            # Style titres
            for cell in worksheet[1]:

                cell.font = Font(
                    bold=True
                )

                cell.alignment = Alignment(
                    horizontal='center',
                    vertical='center'
                )

            fills = {
                "remblai": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
                "deblai": PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid"),
                "rasant": PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),
                "mixte":PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
                "mur de soutènement": PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"),
            }

            classification_col = None

            for cell in worksheet[1]:
                if cell.value == "classification":
                    classification_col = cell.column
                    break

            if classification_col is not None:
                for row in worksheet.iter_rows(min_row=2):
                    classe = row[classification_col - 1].value
                    fill = fills.get(str(classe).lower())

                    if fill:
                        for cell in row:
                            cell.fill = fill

            # Ajuster largeur automatique
            for column_cells in worksheet.columns:

                max_length = 0

                column = (
                    column_cells[0]
                    .column_letter
                )

                for cell in column_cells:

                    try:
                        if cell.value:

                            max_length = max(
                                max_length,
                                len(str(cell.value))
                            )

                    except:
                        pass


                adjusted_width = max(10, min(max_length + 2, 22))

                worksheet.column_dimensions[
                    column
                ].width = adjusted_width

            # Format nombres
            for col in worksheet.columns:

                for cell in col:

                    if isinstance(
                        cell.value,
                        (int, float)
                    ):

                        cell.number_format = (
                            '0.00'
                        )

                        cell.alignment = Alignment(
                            horizontal='center'
                        )

            # Figer première ligne
            worksheet.freeze_panes = 'A2'

        print(
            f"Excel sauvegardé : {excel_path}"
        )
        # Sauvegarde ponts
        self.ponts_gdf.to_file(
            os.path.join(
                self.output_folder,
                "ponts_surface.gpkg"
            ),
            driver='GPKG'
        )

        self.ponts2_gdf.to_file(
            os.path.join(
                self.output_folder,
                "ponts_lineaire.gpkg"
            ),
            driver='GPKG'
        )

        # Sauvegarde des murs (traçabilité / audit), s'il y en a sur la route
        if not self.murs_soutenement_gdf.empty:
            self.murs_soutenement_gdf.to_file(
                os.path.join(
                    self.output_folder,
                    "murs_soutenement.gpkg"
                ),
                driver='GPKG'
            )

        if not self.murs_antibruit_gdf.empty:
            self.murs_antibruit_gdf.to_file(
                os.path.join(
                    self.output_folder,
                    "murs_antibruit.gpkg"
                ),
                driver='GPKG'
            )
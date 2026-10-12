import geopandas as gpd
import rasterio
from shapely.geometry import MultiLineString, LineString, Point, box
import math
import os
from sklearn.linear_model import LinearRegression
import numpy as np
import logging
import matplotlib.pyplot as plt
from get_data_functions import get_data, get_mnt
import pandas as pd
from shapely.ops import unary_union, linemerge, snap
from recompute_classification import side_class, combine_side_and_center


class ProfileAnalyzer:
    """
    Class to analyze profiles along a route and classify them as remblai, deblai or rasant
    """
    def __init__(self, mnt_path, output_folder, classification_threshold_remblai, classification_threshold_deblai, route_number):
        self.mnt_path = mnt_path
        self.dem, self.transform, self.boundingbox = self._read_dem()
        self.output_folder = output_folder
        os.makedirs(self.output_folder,exist_ok=True)
        self.classification_threshold_remblai = classification_threshold_remblai
        self.classification_threshold_deblai = classification_threshold_deblai
        self.route_number = route_number
        self.filter_route = f"cpx_numero='{route_number}'"
        self.lines_selected = get_data(self.filter_route, "BDTOPO_V3:troncon_de_route", self.boundingbox)
        self.central_line = self.build_central_line()
        #self.lines_selected = self.lines_selected[self.lines_selected['nature'] == 'Type autoroutier']
        
        # Save lines_selected to check its contents
        os.makedirs(self.output_folder, exist_ok=True)
        output_file = os.path.join(self.output_folder, "lines_selected.gpkg")
        self.lines_selected.to_file(output_file, driver='GPKG')
        print(f"Saved lines_selected to: {output_file}")
        
        # Setup logging
        os.makedirs(self.output_folder, exist_ok=True)
        log_file = os.path.join(self.output_folder, "profile_analysis.log")
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger(__name__)
        
        self.r2_scores = []  # Add this line to store R² scores

    def _read_dem(self):
        """Read the DEM file and return the elevation data and transform"""
        with rasterio.open(self.mnt_path) as src:
            print(f"DEM bounds: {src.bounds}")
            print(f"DEM shape: {src.shape}")
            print(f"DEM resolution: {src.res}")
            return src.read(1), src.transform, src.bounds

    def get_raster_value(self, point):
        """Get the elevation value from the raster at a given point"""
        try:
            row, col = rasterio.transform.rowcol(self.transform, point.x, point.y)
            if 0 <= row < self.dem.shape[0] and 0 <= col < self.dem.shape[1]:
                return self.dem[row, col]
            else:
                print(f"Point outside raster bounds: row={row}, col={col}")
        except IndexError as e:
            print(f"IndexError: {e}")
        except Exception as e:
            print(f"Other error: {e}")
        return None
    
    def calculate_angle(self, point1, point2):
        """Calculate the angle between two points"""
        dx = point2[0] - point1[0]
        dy = point2[1] - point1[1]
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)
        return angle_deg

    def calculate_distance(self, point1, point2):
        """Calculate the distance between two points"""
        return math.sqrt((point2.x - point1.x)**2 + (point2.y - point1.y)**2)
    
    def calculate_slope(self, point1, point2):
        Z1 = self.get_raster_value(point1)
        Z2 = self.get_raster_value(point2)
        if Z1 is None or Z2 is None:
            return None
        deltaZ = abs(Z2 - Z1)

        dist = self.calculate_distance(point1, point2)
        if dist is None:
            return None
        slope = deltaZ/dist

        return slope
    
    def calculate_perpendicular_line(self, current_distance, line):
        """Calculate the perpendicular line at a given distance along the route"""
        current_point = line.interpolate(current_distance)

        # Calculate angle
        if current_distance <= 15:
            next_point = line.interpolate(current_distance + 10)
            angle = self.calculate_angle((current_point.x, current_point.y), (next_point.x, next_point.y))
        else:
            prev_point = line.interpolate(current_distance - 10)
            angle = self.calculate_angle((prev_point.x, prev_point.y), (current_point.x, current_point.y))
        
        # Calculate perpendicular line endpoints
        dx = 60 * math.cos(math.radians(angle + 90))
        dy = 60 * math.sin(math.radians(angle + 90))
        start_point = (current_point.x - dx, current_point.y - dy)
        end_point = (current_point.x + dx, current_point.y + dy)

        # Calculate perpendicular line of length 160 m
        perpendicular_line = LineString([start_point, end_point])
        perpendicular_line = self.orient_perpendicular(
            perpendicular_line,
            current_point,
            current_distance
        )

        return perpendicular_line

    def calculate_average_height(self, perpendicular_line, startpoint, endpoint):
        """Calculate the average height between 2 points on the perpendicular line"""
        # Create intermediate points
        intermediate_points = []
        i = startpoint
        
        print(f"\nCalculating average height:")
        print(f"Perpendicular line length: {perpendicular_line.length}")
        
        while i <= endpoint:
            intermediate_point = perpendicular_line.interpolate(i)
            intermediate_points.append(intermediate_point)
            i += 1
        
        sum_elevations = 0
        valid_points = 0

        for point in intermediate_points:
            elevation = self.get_raster_value(point)
            print(f"Point coordinates: ({point.x}, {point.y}), Elevation: {elevation}")
            if elevation is not None:
                sum_elevations += elevation
                valid_points += 1

        print(f"Valid points found: {valid_points}")
        
        if valid_points == 0:
            print("No valid points found!")
            return None

        average_height = sum_elevations / valid_points
        return average_height
    
    def calculate_minmax_height(self, perpendicular_line, startpoint, endpoint):
        """Calculate the minimum and maximum height along the perpendicular line"""
        intermediate_points = []
        i = startpoint
        while i <= endpoint:
            intermediate_point = perpendicular_line.interpolate(i)
            intermediate_points.append(intermediate_point)
            i += 1

        max_height = 0
        min_height = 1000
        valid_points = 0

        for point in intermediate_points:
            elevation = self.get_raster_value(point)
            print(f"Point coordinates: ({point.x}, {point.y}), Elevation: {elevation}")
            if elevation is not None:
                if elevation > max_height:
                    max_height = elevation
                if elevation < min_height:
                    min_height = elevation
                valid_points += 1

        print(f"Valid points found: {valid_points}")
        
        if valid_points == 0:
            print("No valid points found!")
            return None, None

        return max_height, min_height

    
    def detect_real_talus(self, perpendicular_line, platform_start, platform_end):

        elevations = []

        for d in range(121):
            z = self.get_raster_value(perpendicular_line.interpolate(d))
            elevations.append(z)

        slope_threshold = 0.15
        consecutive_needed = 2
        flatten_ratio = 0.15

        left_talus_start = None
        left_talus_end = None

        count = 0

        for i in range(platform_start, 2, -1):

            if elevations[i] is None or elevations[i - 1] is None:
                continue

            slope = abs(elevations[i] - elevations[i - 1])

            if slope > slope_threshold:

                count += 1

                if count >= consecutive_needed:
                    left_talus_start = i + consecutive_needed - 1
                    break

            else:

                count = 0

        if left_talus_start is not None:

            sign_change_found = False
            max_slope_seen = 0

            for i in range(left_talus_start - 1, 2, -1):

                if elevations[i - 1] is None or elevations[i] is None or elevations[i + 1] is None:
                    continue

                delta1 = elevations[i] - elevations[i - 1]
                delta2 = elevations[i + 1] - elevations[i]

                slope = abs(delta1)

                max_slope_seen = max(max_slope_seen, slope)

                if delta1 * delta2 < 0:

                    left_talus_end = i
                    sign_change_found = True
                    break

                if max_slope_seen > 0 and slope < max_slope_seen * flatten_ratio:

                    left_talus_end = i
                    sign_change_found = True
                    break

            if not sign_change_found:

                best_break = None
                best_variation = 0

                for i in range(left_talus_start - 5, 5, -1):

                    slopes_before = []
                    slopes_after = []

                    for j in range(max(i - 3, 1), i):

                        if elevations[j] is None or elevations[j - 1] is None:
                            continue

                        slopes_before.append(abs(elevations[j] - elevations[j - 1]))

                    for j in range(i, min(i + 3, len(elevations) - 1)):

                        if elevations[j] is None or elevations[j + 1] is None:
                            continue

                        slopes_after.append(abs(elevations[j + 1] - elevations[j]))

                    if len(slopes_before) < 2 or len(slopes_after) < 2:
                        continue

                    mean_before = np.mean(slopes_before)
                    mean_after = np.mean(slopes_after)

                    variation = abs(mean_before - mean_after)

                    if variation > best_variation:

                        best_variation = variation
                        best_break = i

                left_talus_end = best_break

        right_talus_start = None
        right_talus_end = None

        count = 0

        for i in range(platform_end, len(elevations) - 1):

            if elevations[i] is None or elevations[i + 1] is None:
                continue

            slope = abs(elevations[i + 1] - elevations[i])

            if slope > slope_threshold:

                count += 1

                if count >= consecutive_needed:
                    right_talus_start = i - consecutive_needed + 1
                    break

            else:

                count = 0

        if right_talus_start is not None:

            sign_change_found = False
            max_slope_seen = 0

            for i in range(right_talus_start + 1, len(elevations) - 2):

                if elevations[i - 1] is None or elevations[i] is None or elevations[i + 1] is None:
                    continue

                delta1 = elevations[i] - elevations[i - 1]
                delta2 = elevations[i + 1] - elevations[i]

                slope = abs(delta2)

                max_slope_seen = max(max_slope_seen, slope)

                if delta1 * delta2 < 0:

                    right_talus_end = i
                    sign_change_found = True
                    break

                if max_slope_seen > 0 and slope < max_slope_seen * flatten_ratio:

                    right_talus_end = i
                    sign_change_found = True
                    break

            if not sign_change_found:

                best_break = None
                best_variation = 0

                for i in range(right_talus_start + 5, len(elevations) - 6):

                    slopes_before = []
                    slopes_after = []

                    for j in range(max(i - 3, 1), i):

                        if elevations[j] is None or elevations[j + 1] is None:
                            continue

                        slopes_before.append(abs(elevations[j + 1] - elevations[j]))

                    for j in range(i, min(i + 3, len(elevations) - 1)):

                        if elevations[j] is None or elevations[j + 1] is None:
                            continue

                        slopes_after.append(abs(elevations[j + 1] - elevations[j]))

                    if len(slopes_before) < 2 or len(slopes_after) < 2:
                        continue

                    mean_before = np.mean(slopes_before)
                    mean_after = np.mean(slopes_after)

                    variation = abs(mean_before - mean_after)

                    if variation > best_variation:

                        best_variation = variation
                        best_break = i

                right_talus_end = best_break

        return left_talus_start, left_talus_end, right_talus_start, right_talus_end
    
    def detect_platform(self, perpendicular_line, center_point):

        elevations = []

        for d in range(121):
            point = perpendicular_line.interpolate(d)
            z = self.get_raster_value(point)

            if z is None:
                elevations.append(np.nan)
            else:
                elevations.append(z)

        center = int(round(
            perpendicular_line.project(center_point)
        ))

        center = max(
            0,
            min(center, len(elevations) - 1)
        )

        slope_threshold = 0.20
        consecutive_needed = 3

        platform_start = center

        consecutive = 0

        for i in range(center, 1, -1):

            if np.isnan(elevations[i]) or np.isnan(elevations[i - 1]):
                continue

            slope = abs(elevations[i] - elevations[i - 1])

            if slope > slope_threshold:
                consecutive += 1
            else:
                consecutive = 0

            if consecutive >= consecutive_needed:
                platform_start = i + consecutive_needed - 1
                break

        platform_end = center

        consecutive = 0

        for i in range(center, len(elevations) - 1):

            if np.isnan(elevations[i]) or np.isnan(elevations[i + 1]):
                continue

            slope = abs(elevations[i + 1] - elevations[i])

            if slope > slope_threshold:
                consecutive += 1
            else:
                consecutive = 0

            if consecutive >= consecutive_needed:
                platform_end = i - consecutive_needed + 1
                break

        print(
            f"centre={center} "
            f"start={platform_start} "
            f"end={platform_end}"
        )

        return platform_start, platform_end

    def calculate_height_difference(self, height1, height2):
        """Calculates the height difference between the level of the route and the terrain on the right"""
        if height1 is None or height2 is None:
            return None
        height_difference = height1 - height2
        return height_difference
    
    def calculate_natural_slope(self, perpendicular_line, startpoint1, endpoint1, startpoint2, endpoint2):
        """Determines a linear regression fonction describing the altitude and slope of the natural terrain"""
        intermediate_points = []
        distance = []
        altitude = []

        i = startpoint1
        j = startpoint2
        startpoint_line = perpendicular_line.interpolate(0)

        while i <= endpoint1:
            intermediate_point = perpendicular_line.interpolate(i)
            intermediate_points.append(intermediate_point)
            i += 1
        while j <= endpoint2:
            intermediate_point = perpendicular_line.interpolate(j)
            intermediate_points.append(intermediate_point)
            j += 1

        for point in intermediate_points:
            dist = self.calculate_distance(startpoint_line, point)
            alt = self.get_raster_value(point)
            if alt is not None:
                distance.append(dist)
                altitude.append(alt)

        if not distance or not altitude:
            print("No valid elevation data found for natural slope calculation")
            return None

        dist_arr = np.array(distance).reshape(-1, 1)
        alt_arr = np.array(altitude).reshape(-1, 1)

        try:
            reg = LinearRegression().fit(dist_arr, alt_arr)
            r2_score = reg.score(dist_arr, alt_arr)
            self.logger.info(f"R² score: {r2_score}")
            
            # Store R² score with distance information
            current_distance = perpendicular_line.interpolate(0).distance(self.lines_selected.iloc[0].geometry)
            self.r2_scores.append({
                'distance': current_distance,
                'r2_score': r2_score,
                'coefficients': reg.coef_[0][0],
                'intercept': reg.intercept_[0]
            })
            
            return reg, reg.coef_[0][0]
        except Exception as e:
            print(f"Error in linear regression: {e}")
            return None

    def calculate_interpolated_altitude(self, distance, reg):
        """Calculate the interpolated altitude using the regression model"""
        if reg is None:
            return None

        distance_reshaped = np.array([distance]).reshape(-1,1)    
        altitude = reg.predict(distance_reshaped)
        return altitude[0][0]


    def calculate_attributes_deblai(self, perpendicular_line, reg, coef,platform_start):
        """Calculate attributes for deblai profile"""

        calculation_points = []

        self.logger.info("Starting calculate_attributes_deblai V2")

        #  Trouver l'intersection TN
        prev_difference = None

        dist_max = None
        alt_max = None
        prev_j = None
        j = platform_start
        while j > 0:

            point = perpendicular_line.interpolate(j)

            current_altitude = self.get_raster_value(point)

            if current_altitude is None:
                j -= 0.5
                continue

            interpolated_altitude = self.calculate_interpolated_altitude(
                j,
                reg
            )

            if interpolated_altitude is None:
                j -= 0.5
                continue

            current_difference = (
                current_altitude
                - interpolated_altitude
            )

            point1 = perpendicular_line.interpolate(j + 0.5)
            point2 = perpendicular_line.interpolate(j - 0.5)

            current_slope = self.calculate_slope(
                point1,
                point2
            )

            calculation_points.append({
                "point": point,
                "elevation": current_altitude,
                "slope": current_slope,
                "distance": j
            })

            if (
                prev_difference is not None
                and prev_difference * current_difference <= 0
            ):

                denom = abs(prev_difference) + abs(current_difference)

                if denom != 0:
                    ratio = abs(prev_difference) / denom
                    dist_max = prev_j + ratio * (j - prev_j)
                else:
                    dist_max = j

                alt_max = self.calculate_interpolated_altitude(
                    dist_max,
                    reg
                )

                self.logger.info(
                    f"Intersection TN trouvée : "
                    f"dist_max={dist_max}, alt_max={alt_max}"
                )

                break 

            prev_j = j
            prev_difference = current_difference

            j -= 0.5

        if dist_max is None:

            self.logger.warning(
                "Intersection TN non trouvée -> fallback"
            )

            dist_max = j

            alt_max = self.get_raster_value(
                perpendicular_line.interpolate(j)
            )


        # 2) Chercher le fond du déblai
        seuil_descente = 0.05
        seuil_plateau = 0.03
        plateau_min_points = 12

        plateau_count = 0

        last_slope_dist = dist_max
        last_slope_alt = alt_max

        dist_min = None
        alt_min = None

        k = dist_max

        #while k < 60:
        while k < platform_start:
            

            z1 = self.get_raster_value(
                perpendicular_line.interpolate(k)
            )

            z2 = self.get_raster_value(
                perpendicular_line.interpolate(
                    min(k + 3, 120)
                )
            )

            if z1 is None or z2 is None:
                k += 0.5
                continue

            delta = (z2 - z1) / 3

            # vraie descente
            if delta < -seuil_descente:

                last_slope_dist = k + 0.5
                last_slope_alt = z2

                plateau_count = 0

            # zone plate
            elif abs(delta) < seuil_plateau:

                plateau_count += 1

            else:

                plateau_count = 0

            if plateau_count >= plateau_min_points:

                dist_min = last_slope_dist
                alt_min = last_slope_alt

                self.logger.info(
                    f"Fond déblai détecté : "
                    f"dist_min={dist_min}, alt_min={alt_min}"
                )

                break

            k += 0.5

        if dist_min is None:

            dist_min = last_slope_dist
            alt_min = last_slope_alt

        #  Calculs finaux
        distance = abs(dist_max - dist_min)

        if distance == 0:

            return (
                None,
                None,
                None,
                None,
                calculation_points,
                None,
                None,
                None,
                None
            )

        height_difference = abs(
            alt_max - alt_min
        )

        slope_ouvrage_total = (
            height_difference / distance
        )

        slope_ouvrage_section = None

        safety_margin = 1.5

        if distance > (safety_margin * 2):

            point_min = perpendicular_line.interpolate(
                dist_min + 2
            )

            point_max = perpendicular_line.interpolate(
                dist_max - 2
            )

            slope_ouvrage_section = self.calculate_slope(
                point_min,
                point_max
            )

        slope_ouvrage_middle = None

        section_length = 3

        if distance > section_length:

            point_middle_min = perpendicular_line.interpolate(
                dist_min
                + (distance / 2)
                - (section_length / 2)
            )

            point_middle_max = perpendicular_line.interpolate(
                dist_min
                + (distance / 2)
                + (section_length / 2)
            )

            slope_ouvrage_middle = self.calculate_slope(
                point_middle_min,
                point_middle_max
            )

        return (
            slope_ouvrage_total,
            slope_ouvrage_section,
            slope_ouvrage_middle,
            height_difference,
            calculation_points,
            dist_min,
            alt_min,
            dist_max,
            alt_max
        )

    def calculate_attributes_remblai(self, perpendicular_line, reg, coef,platform_start):
        """Calculate attributes for remblai profile"""

        calculation_points = []
        self.logger.info("Starting calculate_attributes_remblai V2")

        # Chercher l'intersection avec le terrain naturel

        j = platform_start
        prev_difference = None
        prev_j = None
        prev_altitude = None

        dist_min = None
        alt_min = None

        max_iterations = 80
        iteration_count = 0

        while j > 0 and iteration_count < max_iterations:

            iteration_count += 1

            point = perpendicular_line.interpolate(j)
            current_altitude = self.get_raster_value(point)

            if current_altitude is None:
                j -= 0.5
                continue

            interpolated_altitude = self.calculate_interpolated_altitude(
                j,
                reg
            )

            if interpolated_altitude is None:
                j -= 0.5
                continue

            current_difference = current_altitude - interpolated_altitude

            point1 = perpendicular_line.interpolate(j + 0.5)
            point2 = perpendicular_line.interpolate(j - 0.5)
            current_slope = self.calculate_slope(point1, point2)

            calculation_points.append({
                "point": point,
                "elevation": current_altitude,
                "slope": current_slope,
                "distance": j
            })

            if prev_difference is not None:

                if prev_difference * current_difference <= 0:

                    # interpolation linéaire pour tomber plus proche
                    # de la vraie intersection
                    denom = abs(prev_difference) + abs(current_difference)

                    if denom != 0:
                        ratio = abs(prev_difference) / denom
                        dist_min = prev_j + ratio * (j - prev_j)
                    else:
                        dist_min = j

                    alt_min = self.calculate_interpolated_altitude(
                        dist_min,
                        reg
                    )

                    self.logger.info(
                        f"Intersection TN trouvée : "
                        f"dist_min={dist_min}, alt_min={alt_min}"
                    )

                    break

            prev_difference = current_difference
            prev_j = j
            prev_altitude = current_altitude

            j -= 0.5
        
        if dist_min is None or alt_min is None:

            self.logger.warning(
                "Intersection TN non trouvée -> fallback"
            )

            dist_min = j

            alt_min = self.get_raster_value(
                perpendicular_line.interpolate(j)
            )

        #  Depuis l'intersection, chercher le sommet du talus
        #    = dernier point de vraie montée avant plateau


        seuil_montee = 0.05
        seuil_plateau = 0.03
        plateau_min_points = 12  

        plateau_count = 0

        last_slope_dist = dist_min
        last_slope_alt = alt_min

        dist_max = None
        alt_max = None

        k = dist_min

        #while k < 60:
        while k < platform_start:
           
            z1 = self.get_raster_value(
                perpendicular_line.interpolate(k)
            )

            z2 = self.get_raster_value(
                perpendicular_line.interpolate(
                    min(k + 3, 120)
                )
            )

            if z1 is None or z2 is None:
                k += 0.5
                continue

            delta = (z2 - z1) / 3

            # vraie montée du talus
            if delta > seuil_montee:

                last_slope_dist = k + 0.5
                last_slope_alt = z2
                plateau_count = 0

            # zone quasi plate
            elif abs(delta) < seuil_plateau:

                plateau_count += 1

            # petit changement non stable
            else:

                plateau_count = 0

            # plateau confirmé
            if plateau_count >= plateau_min_points:

                dist_max = last_slope_dist
                alt_max = last_slope_alt

                self.logger.info(
                    f"Plateau détecté : "
                    f"dist_max={dist_max}, alt_max={alt_max}"
                )

                break

            k += 0.5

        # Si aucun plateau trouvé, on garde le dernier point de montée
        if dist_max is None or alt_max is None:

            dist_max = last_slope_dist
            alt_max = last_slope_alt

            self.logger.info(
                f"Aucun plateau confirmé, dernier point de montée utilisé : "
                f"dist_max={dist_max}, alt_max={alt_max}"
            )

        distance = abs(dist_max - dist_min)

        if distance == 0:
            self.logger.warning("Zero distance found, cannot calculate slope")
            return None, None, None, None, calculation_points, None, None, None, None

        height_difference = None

        if alt_max is not None and alt_min is not None:
            height_difference = abs(alt_max - alt_min)

        if height_difference is not None and height_difference > 50:
            height_difference = None

        slope_ouvrage_total = None

        if height_difference is not None and distance is not None:
            slope_ouvrage_total = height_difference / distance

        # ==================================================
        # 3) Pentes comme avant
        # ==================================================

        slope_ouvrage_section = None
        safety_margin = 1.5

        if distance > (safety_margin * 2):

            point_min = perpendicular_line.interpolate(
                dist_min + 2
            )

            point_max = perpendicular_line.interpolate(
                dist_max - 2
            )

            slope_ouvrage_section = self.calculate_slope(
                point_min,
                point_max
            )

        slope_ouvrage_middle = None
        section_length = 3

        if distance > section_length:

            point_middle_min = perpendicular_line.interpolate(
                dist_min
                + (distance / 2)
                - (section_length / 2)
            )

            point_middle_max = perpendicular_line.interpolate(
                dist_min
                + (distance / 2)
                + (section_length / 2)
            )

            slope_ouvrage_middle = self.calculate_slope(
                point_middle_min,
                point_middle_max
            )

        if height_difference is not None and slope_ouvrage_total is not None:
            self.logger.info(
                f"Final remblai V2: "
                f"height_diff={height_difference:.2f}, "
                f"slope={slope_ouvrage_total:.2f}"
            )

        return (
            slope_ouvrage_total,
            slope_ouvrage_section,
            slope_ouvrage_middle,
            height_difference,
            calculation_points,
            dist_min,
            alt_min,
            dist_max,
            alt_max
        )
    

    def calculate_attributes_remblai_right(
        self,
        perpendicular_line,
        reg,
        coef,
        platform_end
    ):

        calculation_points = []

        j = platform_end

        prev_difference = None
        prev_j = None

        dist_min = None
        alt_min = None

        while j < 120:

            point = perpendicular_line.interpolate(j)

            current_altitude = self.get_raster_value(point)

            if current_altitude is None:
                j += 0.5
                continue

            interpolated_altitude = self.calculate_interpolated_altitude(
                j,
                reg
            )

            if interpolated_altitude is None:
                j += 0.5
                continue

            current_difference = (
                current_altitude
                - interpolated_altitude
            )

            point1 = perpendicular_line.interpolate(j - 0.5)
            point2 = perpendicular_line.interpolate(j + 0.5)

            current_slope = self.calculate_slope(
                point1,
                point2
            )

            calculation_points.append({
                "point": point,
                "elevation": current_altitude,
                "slope": current_slope,
                "distance": j
            })

            if (
                prev_difference is not None
                and prev_difference * current_difference <= 0
            ):

                denom = abs(prev_difference) + abs(current_difference)

                if denom != 0:
                    ratio = abs(prev_difference) / denom
                    dist_min = prev_j + ratio * (j - prev_j)
                else:
                    dist_min = j

                alt_min = self.calculate_interpolated_altitude(
                    dist_min,
                    reg
                )

                break

            prev_difference = current_difference
            prev_j = j

            j += 0.5

        if dist_min is None:

            dist_min = j

            alt_min = self.get_raster_value(
                perpendicular_line.interpolate(j)
            )

        seuil_montee = 0.05
        seuil_plateau = 0.03
        plateau_min_points = 12

        plateau_count = 0

        last_slope_dist = dist_min
        last_slope_alt = alt_min

        dist_max = None
        alt_max = None

        k = dist_min

        #while k > 60:
        while k > platform_end:
            
            z1 = self.get_raster_value(
                perpendicular_line.interpolate(k)
            )

            z2 = self.get_raster_value(
                perpendicular_line.interpolate(
                    max(k - 3, 0)
                )
            )

            if z1 is None or z2 is None:
                k -= 0.5
                continue

            delta = (z2 - z1) / 3

            if delta > seuil_montee:

                last_slope_dist = k - 0.5
                last_slope_alt = z2

                plateau_count = 0

            elif abs(delta) < seuil_plateau:

                plateau_count += 1

            else:

                plateau_count = 0

            if plateau_count >= plateau_min_points:

                dist_max = last_slope_dist
                alt_max = last_slope_alt

                break

            k -= 0.5

        if dist_max is None:

            dist_max = last_slope_dist
            alt_max = last_slope_alt

        distance = abs(dist_max - dist_min)

        if distance == 0:
            return None, None, None, None, calculation_points, None, None, None, None

        height_difference = abs(
            alt_max - alt_min
        )

        slope_ouvrage_total = (
            height_difference / distance
        )

        return (
            slope_ouvrage_total,
            None,
            None,
            height_difference,
            calculation_points,
            dist_min,
            alt_min,
            dist_max,
            alt_max
        )
    
    def calculate_attributes_deblai_right(
        self,
        perpendicular_line,
        reg,
        coef,
        platform_end
    ):

        calculation_points = []

        prev_difference = None

        dist_max = None
        alt_max = None
        prev_j = None

        j = platform_end

        while j < 120:

            point = perpendicular_line.interpolate(j)

            current_altitude = self.get_raster_value(point)

            if current_altitude is None:
                j += 0.5
                continue

            interpolated_altitude = self.calculate_interpolated_altitude(
                j,
                reg
            )

            if interpolated_altitude is None:
                j += 0.5
                continue

            current_difference = (
                current_altitude
                - interpolated_altitude
            )

            point1 = perpendicular_line.interpolate(j - 0.5)
            point2 = perpendicular_line.interpolate(j + 0.5)

            current_slope = self.calculate_slope(
                point1,
                point2
            )

            calculation_points.append({
                "point": point,
                "elevation": current_altitude,
                "slope": current_slope,
                "distance": j
            })

            if (
                prev_difference is not None
                and prev_difference * current_difference <= 0
            ):

                denom = abs(prev_difference) + abs(current_difference)

                if denom != 0:
                    ratio = abs(prev_difference) / denom
                    dist_max = prev_j + ratio * (j - prev_j)
                else:
                    dist_max = j

                alt_max = self.calculate_interpolated_altitude(
                    dist_max,
                    reg
                )

                break

            prev_j = j
            prev_difference = current_difference

            j += 0.5

        if dist_max is None:

            dist_max = j

            alt_max = self.get_raster_value(
                perpendicular_line.interpolate(j)
            )

        seuil_descente = 0.05
        seuil_plateau = 0.03
        plateau_min_points = 12

        plateau_count = 0

        last_slope_dist = dist_max
        last_slope_alt = alt_max

        dist_min = None
        alt_min = None

        k = dist_max

        #while k > 60:
        while k > platform_end:
            
            z1 = self.get_raster_value(
                perpendicular_line.interpolate(k)
            )

            z2 = self.get_raster_value(
                perpendicular_line.interpolate(
                    max(k - 3, 0)
                )
            )

            if z1 is None or z2 is None:
                k -= 0.5
                continue

            delta = (z2 - z1) / 3

            if delta < -seuil_descente:

                last_slope_dist = k - 0.5
                last_slope_alt = z2
                plateau_count = 0

            elif abs(delta) < seuil_plateau:

                plateau_count += 1

            else:

                plateau_count = 0

            if plateau_count >= plateau_min_points:

                dist_min = last_slope_dist
                alt_min = last_slope_alt

                break

            k -= 0.5

        if dist_min is None:

            dist_min = last_slope_dist
            alt_min = last_slope_alt

        distance = abs(dist_max - dist_min)

        if distance == 0:

            return (
                None,
                None,
                None,
                None,
                calculation_points,
                None,
                None,
                None,
                None
            )

        height_difference = abs(
            alt_max - alt_min
        )

        slope_ouvrage_total = (
            height_difference / distance
        )

        return (
            slope_ouvrage_total,
            None,
            None,
            height_difference,
            calculation_points,
            dist_min,
            alt_min,
            dist_max,
            alt_max
        )

    def classify_point(self, height_difference):
        """Classify point as zone de remblai, zone de deblai ou en profil rasant"""
        if height_difference is None:
            return "unknown"

        if height_difference >= self.classification_threshold_remblai:
            return "remblai"
        elif height_difference <= self.classification_threshold_deblai:
            return "deblai"
        else:
            return "rasant"
    
    def determine_routewidth(self, row):

        if row['nombre_de_voies'] == 2:

            ref_terrain_start = 20
            ref_terrain_end = 30

            ref_minmax_start = 20
            ref_minmax_end = 40

            ref_slope_start = 50
            ref_slope_end = 30

            ref_terrain_start1 = 0
            ref_terrain_end1 = 30

            ref_terrain_start2 = 90
            ref_terrain_end2 = 120

        else:

            ref_terrain_start = 15
            ref_terrain_end = 25

            ref_minmax_start = 25
            ref_minmax_end = 45

            ref_slope_start = 45
            ref_slope_end = 25

            ref_terrain_start1 = 0
            ref_terrain_end1 = 25

            ref_terrain_start2 = 95
            ref_terrain_end2 = 120

        return (
            ref_terrain_start,
            ref_terrain_end,
            ref_minmax_start,
            ref_minmax_end,
            ref_slope_start,
            ref_slope_end,
            ref_terrain_start1,
            ref_terrain_end1,
            ref_terrain_start2,
            ref_terrain_end2
        )
    def visualize_profile(self, i, perpendicular_line, reg, coef, current_distance, output_folder):
        """Visualize the profile and regression line at a specific distance."""
        intermediate_points = []
        distances = []
        elevations = []

        # Generate intermediate points along the perpendicular line
        for i in range(0, int(perpendicular_line.length) + 1):
            point = perpendicular_line.interpolate(i)
            elevation = self.get_raster_value(point)
            if elevation is not None:
                intermediate_points.append(point)
                distances.append(i)
                elevations.append(elevation)

        # Plot the profile
        plt.figure(figsize=(10, 6))
        plt.plot(distances, elevations, label="Terrain Profile", marker="o", linestyle="-")

        # Add regression line if available
        if reg is not None:
            regression_distances = np.array(distances).reshape(-1, 1)
            regression_elevations = reg.predict(regression_distances)
            plt.plot(distances, regression_elevations, label="Regression Line", color="red", linestyle="--")

        middle_value = (max(elevations) + min(elevations)) / 2

        y_min = middle_value - 10
        y_max = middle_value + 10

        # Add labels and legend
        plt.title(f"Profile Visualization at Distance {current_distance} m")
        plt.xlabel("Distance along Perpendicular Line (m)")
        plt.ylabel("Elevation (m)")
        plt.legend()
        plt.grid(True)
        plt.ylim(y_min, y_max)
        plt.tight_layout()

        # Save the plot as a PNG file
        output_file = os.path.join(output_folder, f"profile{self.route_number}_{i}_{int(current_distance)}m.png")
        plt.savefig(output_file)
        plt.close()

        self.logger.info(f"Profile visualization saved: {output_file}")


    def build_central_line(self, step=5):
        
        lines = []
        for geom in self.lines_selected.geometry:
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(list(geom.geoms))

        print("Morceaux trouvés :", len(lines))

        if len(lines) < 2:
            raise ValueError("Pas assez de lignes pour construire la ligne centrale")

        union = unary_union(lines)

        snapped_lines = [
            snap(line, union, 5)
            for line in lines
        ]

        merged = linemerge(unary_union(snapped_lines))

        if merged.geom_type == "LineString":
            raise ValueError("Une seule chaussée détectée")

        merged_lines = list(merged.geoms)

        merged_lines = sorted(
            merged_lines,
            key=lambda l: l.length,
            reverse=True
        )

        line1 = merged_lines[0]
        line2 = merged_lines[1]

        print(f"Longueur voie 1 : {line1.length:.2f}")
        print(f"Longueur voie 2 : {line2.length:.2f}")

        if Point(line1.coords[0]).distance(Point(line2.coords[0])) > Point(line1.coords[0]).distance(Point(line2.coords[-1])):
            line2 = LineString(list(line2.coords)[::-1])

        points = []

        normal_length = 120

        # n = int(line1.length / step)
        n = int(min(line1.length, line2.length) / step)

        last_offset_x = None
        last_offset_y = None

        for i in range(n + 1):

            t = i / n

            p1 = line1.interpolate(
                t,
                normalized=True
            )

            distance_on_line = t * line1.length

            d_before = max(distance_on_line - 5, 0)
            d_after = min(distance_on_line + 5, line1.length)

            p_before = line1.interpolate(d_before)
            p_after = line1.interpolate(d_after)

            dx = p_after.x - p_before.x
            dy = p_after.y - p_before.y

            norm = math.sqrt(dx * dx + dy * dy)

            if norm == 0:
                continue

            dx = dx / norm
            dy = dy / norm

            nx = -dy
            ny = dx

            normal_line = LineString([
                (
                    p1.x - normal_length * nx,
                    p1.y - normal_length * ny
                ),
                (
                    p1.x + normal_length * nx,
                    p1.y + normal_length * ny
                )
            ])

            candidates = []

            search_offsets = [
                0,
                -5, 5,
                -10, 10,
                -20, 20
            ]

            for offset in search_offsets:

                test_distance = min(max(distance_on_line + offset, 0), line1.length)

                test_point = line1.interpolate(test_distance)

                d_before_test = max(test_distance - 5, 0)
                d_after_test = min(test_distance + 5, line1.length)

                p_before_test = line1.interpolate(d_before_test)
                p_after_test = line1.interpolate(d_after_test)

                dx_test = p_after_test.x - p_before_test.x
                dy_test = p_after_test.y - p_before_test.y

                norm_test = math.sqrt(dx_test * dx_test + dy_test * dy_test)

                if norm_test == 0:
                    continue

                dx_test /= norm_test
                dy_test /= norm_test

                nx_test = -dy_test
                ny_test = dx_test

                test_normal = LineString([
                    (
                        test_point.x - normal_length * nx_test,
                        test_point.y - normal_length * ny_test
                    ),
                    (
                        test_point.x + normal_length * nx_test,
                        test_point.y + normal_length * ny_test
                    )
                ])

                inter = test_normal.intersection(line2)

                if inter.is_empty:
                    continue

                if inter.geom_type == "Point":
                    candidates.append(inter)

                elif inter.geom_type == "MultiPoint":
                    candidates.extend(list(inter.geoms))
                elif inter.geom_type in [
                    "LineString",
                    "MultiLineString"
                ]:

                    d = line2.project(test_point)
                    candidates.append(line2.interpolate(d))

                elif inter.geom_type == "GeometryCollection":
                    for g in inter.geoms:
                        if g.geom_type == "Point":
                            candidates.append(g)

                if candidates:
                    break

            if candidates:
                p2 = min(candidates,key=lambda p: p.distance(p1))
                last_offset_x = p2.x - p1.x
                last_offset_y = p2.y - p1.y

            else:
                if last_offset_x is not None and last_offset_y is not None:
                    p2 = Point(
                        p1.x + last_offset_x,
                        p1.y + last_offset_y
                    )
                else:

                    d = line2.project(p1)
                    p2 = line2.interpolate(d)
                    last_offset_x = p2.x - p1.x
                    last_offset_y = p2.y - p1.y

            middle = Point(
                (p1.x + p2.x) / 2,
                (p1.y + p2.y) / 2
            )
            points.append(middle)

        if len(points) < 2:
            raise ValueError("Impossible de construire la ligne centrale")

        self.central_line = LineString(points)

        start_point = Point(self.central_line.coords[0])
        end_point = Point(self.central_line.coords[-1])

        print("CENTRAL START :", start_point.x, start_point.y)
        print("CENTRAL END   :", end_point.x, end_point.y)

        self.central_line = LineString(
            list(self.central_line.coords)[::-1]
        )
        central_gdf = gpd.GeoDataFrame(
            [{
                "name": "central_line",
                "geometry": self.central_line
            }],
            geometry="geometry",
            crs=self.lines_selected.crs
        )

        central_path = os.path.join(
            self.output_folder,
            "central_line_profil.gpkg"
        )

        central_gdf.to_file(
            central_path,
            layer="central_line",
            driver="GPKG"
        )

        print(f"Ligne centrale sauvegardée : {central_path}")
        print(f"Longueur centrale : {self.central_line.length:.2f}")
        return self.central_line
    
    def get_closest_bdtopo_line(self, point):
        distances = self.lines_selected.distance(point)
        idx = distances.idxmin()
        return self.lines_selected.loc[idx]
    

    # orientation des perpendiculaire
    def orient_perpendicular(self, perpendicular_line, center_point,profile_id):

        p0 = perpendicular_line.interpolate(0)
        p120 = perpendicular_line.interpolate(perpendicular_line.length)
        center_dist = self.central_line.project(center_point)

        before_dist = max(center_dist - 1, 0)
        after_dist = min(center_dist + 1, self.central_line.length)
        p_before = self.central_line.interpolate(before_dist)

        p_after = self.central_line.interpolate(after_dist)
        dx_line = p_after.x - p_before.x
        dy_line = p_after.y - p_before.y

        dx_test = p0.x - center_point.x
        dy_test = p0.y - center_point.y
        cross = dx_line * dy_test - dy_line * dx_test


        reversed_profile=False
        if cross < 0:
            perpendicular_line = LineString(list(perpendicular_line.coords)[::-1])
            reversed_profile=True

        p0_after=perpendicular_line.interpolate(0)
        p120_after=perpendicular_line.interpolate(perpendicular_line.length)

        with open(os.path.join(self.output_folder,"debug_orientation_perpendicular.txt"),"a",encoding="utf-8") as f:
            f.write(f"\n=====================================\n")
            f.write(f"center_dist={center_dist}\n")
            f.write(f"profile_id={profile_id}\n")
            f.write(f"cross={cross}\n")
            f.write(f"reversed_profile={reversed_profile}\n")
            f.write(f"AVANT p0=({p0.x},{p0.y}) p120=({p120.x},{p120.y})\n")
            f.write(f"APRES p0=({p0_after.x},{p0_after.y}) p120=({p120_after.x},{p120_after.y})\n")
            f.write(f"central_before=({p_before.x},{p_before.y}) central_after=({p_after.x},{p_after.y})\n")

        return perpendicular_line
        

    def analyze_profile(self):
        """Analyze the profile and classify it"""
        

        self.logger.info("Starting profile analysis")
        self.logger.info(
            f"Number of selected lines: {len(self.lines_selected)}"
        )

        all_segments = []
        all_calculation_points = []
        all_perpendicular_lines = []
        #debug_points = []
        platform_points = []
        talus_points = []
        real_talus_points = []
        line = self.central_line
        length = line.length
        self.logger.info(
            f"\nProcessing central line : {length:.2f} m"
        )

        current_distance = 0

        points = []

        while current_distance <= length:

            current_point = line.interpolate(
                current_distance
            )

            closest_row = self.get_closest_bdtopo_line(
                current_point
            )

            
            ref_terrain_start, ref_terrain_end, \
            ref_minmax_start, ref_minmax_end, \
            ref_slope_start, ref_slope_end, \
            ref_terrain_start1, ref_terrain_end1, \
            ref_terrain_start2, ref_terrain_end2 = \
                self.determine_routewidth(
                    closest_row
                )

            next_point = line.interpolate(
                current_distance + 1
            )

            perpendicular_line = self.calculate_perpendicular_line(
                current_distance,
                line
            )

            all_perpendicular_lines.append({
                "distance": current_distance,
                "geometry": perpendicular_line
            })

            platform_start, platform_end = self.detect_platform(
                perpendicular_line,
                current_point
            )

            left_talus_start, left_talus_end, right_talus_start, right_talus_end = self.detect_real_talus(perpendicular_line, platform_start, platform_end)

            

            if left_talus_start is not None:
                real_talus_points.append({"point_type":"left_talus_start","geometry":perpendicular_line.interpolate(left_talus_start)})

            if left_talus_end is not None:
                real_talus_points.append({"point_type":"left_talus_end","geometry":perpendicular_line.interpolate(left_talus_end)})

            if right_talus_start is not None:
                real_talus_points.append({"point_type":"right_talus_start","geometry":perpendicular_line.interpolate(right_talus_start)})

            if right_talus_end is not None:
                real_talus_points.append({"point_type":"right_talus_end","geometry":perpendicular_line.interpolate(right_talus_end)})

            platform_points.append({
                "distance_profil": current_distance,
                "point_type": "platform_start",
                "geometry": perpendicular_line.interpolate(platform_start)
            })

            platform_points.append({
                "distance_profil": current_distance,
                "point_type": "platform_end",
                "geometry": perpendicular_line.interpolate(platform_end)
            })

            average_height_route = self.calculate_average_height(
                perpendicular_line,
                platform_start,
                platform_end
            )

            if (
                left_talus_end is not None
                and right_talus_end is not None
                and left_talus_end > 10
                and right_talus_end < 110
            ):

                reg, coef = self.calculate_natural_slope(
                    perpendicular_line,
                    0,
                    left_talus_end,
                    right_talus_end,
                    120
                )

            else:

                reg, coef = self.calculate_natural_slope(
                    perpendicular_line,
                    0,
                    max(0, platform_start - 5),
                    min(120, platform_end + 5),
                    120
                )
            center_distance = perpendicular_line.project(current_point)
            interpolated_height_nat_terrain_route = \
                self.calculate_interpolated_altitude(
                    #60,
                    center_distance,
                    reg
                )

            height_difference_nat_terrain = (
                average_height_route
                - interpolated_height_nat_terrain_route
            )

            hauteur_centre = (
                average_height_route
                - interpolated_height_nat_terrain_route
            )

            profile_type = self.classify_point(height_difference_nat_terrain)

            self.logger.info(f"\nAt distance {current_distance}:")
            self.logger.info(f"Profile type: {profile_type}")
            self.logger.info(f"Height difference: {height_difference_nat_terrain}")

            max_height_difference = None
            slope_ouvrage_total = None
            slope_ouvrage_section = None
            slope_ouvrage_middle = None
            calculation_points = None
            dist_min = None
            alt_min = None
            dist_max = None
            alt_max = None

            left_dist_min = None
            left_alt_min = None
            left_dist_max = None
            left_alt_max = None

            right_points = None
            right_height = None
            right_slope_total = None

            right_dist_min = None
            right_alt_min = None
            right_dist_max = None
            right_alt_max = None

            
            if profile_type == "deblai":

                slope_ouvrage_total, slope_ouvrage_section, \
                slope_ouvrage_middle, max_height_difference, \
                calculation_points, left_dist_min, left_alt_min, \
                left_dist_max, left_alt_max = self.calculate_attributes_deblai(
                    perpendicular_line,
                    reg,
                    coef,
                    platform_start
                )

                right_slope_total, _, _, right_height, \
                right_points, right_dist_min, right_alt_min, \
                right_dist_max, right_alt_max = \
                self.calculate_attributes_deblai_right(
                    perpendicular_line,
                    reg,
                    coef,
                    platform_end
                )

            elif profile_type == "remblai":

                slope_ouvrage_total, slope_ouvrage_section, \
                slope_ouvrage_middle, max_height_difference, \
                calculation_points, left_dist_min, left_alt_min, \
                left_dist_max, left_alt_max,  = self.calculate_attributes_remblai(
                    perpendicular_line,
                    reg,
                    coef,
                    platform_start
                )
                right_slope_total, _, _, right_height, \
                right_points, right_dist_min, right_alt_min, \
                right_dist_max, right_alt_max = \
                self.calculate_attributes_remblai_right(
                    perpendicular_line,
                    reg,
                    coef,
                    platform_end
                )


            if left_talus_start is not None and left_talus_end is not None:

                z1=self.get_raster_value(perpendicular_line.interpolate(left_talus_start))
                z2=self.get_raster_value(perpendicular_line.interpolate(left_talus_end))

                if z1>=z2:

                    left_dist_max=left_talus_start
                    left_alt_max=z1

                    left_dist_min=left_talus_end
                    left_alt_min=z2

                else:

                    left_dist_max=left_talus_end
                    left_alt_max=z2

                    left_dist_min=left_talus_start
                    left_alt_min=z1

            if right_talus_start is not None and right_talus_end is not None:

                z1=self.get_raster_value(perpendicular_line.interpolate(right_talus_start))
                z2=self.get_raster_value(perpendicular_line.interpolate(right_talus_end))

                if z1>=z2:

                    right_dist_max=right_talus_start
                    right_alt_max=z1

                    right_dist_min=right_talus_end
                    right_alt_min=z2

                else:

                    right_dist_max=right_talus_end
                    right_alt_max=z2

                    right_dist_min=right_talus_start
                    right_alt_min=z1

            left_height_real=None
            left_slope_real=None

            if left_talus_start is not None and left_talus_end is not None:

                distance=abs(left_talus_start-left_talus_end)

                if left_alt_max is not None and left_alt_min is not None and distance>0:

                    left_height_real=abs(left_alt_max-left_alt_min)
                    left_slope_real=left_height_real/distance

            right_height_real=None
            right_slope_real=None

            if right_talus_start is not None and right_talus_end is not None:

                distance=abs(right_talus_start-right_talus_end)

                if right_alt_max is not None and right_alt_min is not None and distance>0:

                    right_height_real=abs(right_alt_max-right_alt_min)
                    right_slope_real=right_height_real/distance

            
            if left_height_real is not None:

                max_height_difference=left_height_real
                slope_ouvrage_total=left_slope_real

            if right_height_real is not None:

                right_height=right_height_real
                right_slope_total=right_slope_real

                if max_height_difference is None or right_height_real>max_height_difference:

                    max_height_difference=right_height_real
                    slope_ouvrage_total=right_slope_real

            

            old_profile_type = profile_type

            left_signed_height = None
            right_signed_height = None

            if left_talus_start is not None and left_talus_end is not None:

                z1 = self.get_raster_value(perpendicular_line.interpolate(left_talus_start))
                z2 = self.get_raster_value(perpendicular_line.interpolate(left_talus_end))

                if z1 is not None and z2 is not None:
                    left_signed_height = z1 - z2

            if right_talus_start is not None and right_talus_end is not None:

                z1 = self.get_raster_value(perpendicular_line.interpolate(right_talus_start))
                z2 = self.get_raster_value(perpendicular_line.interpolate(right_talus_end))

                if z1 is not None and z2 is not None:
                    right_signed_height = z1 - z2

            profile_type = combine_side_and_center(
                old_profile_type,
                side_class(left_signed_height),
                side_class(right_signed_height),
            )
                
            if calculation_points:
                all_calculation_points.extend(calculation_points)

            if right_points:
                all_calculation_points.extend(right_points)

            point = Point(current_point)

            if left_dist_min is not None:
                talus_points.append({
                    "distance_profil": current_distance,
                    "point_type": "left_talus_end",
                    "geometry": perpendicular_line.interpolate(left_dist_min)
                })

            if left_dist_max is not None:
                talus_points.append({
                    "distance_profil": current_distance,
                    "point_type": "left_talus_start",
                    "geometry": perpendicular_line.interpolate(left_dist_max)
                })

            if right_dist_min is not None:
                talus_points.append({
                    "distance_profil": current_distance,
                    "point_type": "right_talus_end",
                    "geometry": perpendicular_line.interpolate(right_dist_min)
                })

            if right_dist_max is not None:
                talus_points.append({
                    "distance_profil": current_distance,
                    "point_type": "right_talus_start",
                    "geometry": perpendicular_line.interpolate(right_dist_max)
                })

            points.append({
                'geometry': point,
                'classification': profile_type,
                'height_difference_nat_terrain': height_difference_nat_terrain,
                'average_height_route': average_height_route,
                'interpolated_height_nat_terrain_route': interpolated_height_nat_terrain_route,
                'reg_coef': reg.coef_[0][0],
                'reg_intercept': reg.intercept_[0],
                'num_voies': closest_row['nombre_de_voies'],
                'hauteur_centre': hauteur_centre,
                'distance_profil': current_distance,
                'largeur_route': closest_row['largeur_de_chaussee'],
                'num_route': closest_row['cpx_numero'],
                'max_height_difference': max_height_difference,
                'slope_ouvrage_total': slope_ouvrage_total,
                'slope_ouvrage_section': slope_ouvrage_section,
                'slope_ouvrage_middle': slope_ouvrage_middle,
                'left_dist_min': left_dist_min,
                'left_alt_min': left_alt_min,
                'left_dist_max': left_dist_max,
                'left_alt_max': left_alt_max,
                'talus_dist_min': left_dist_min,
                'talus_alt_min': left_alt_min,
                'talus_dist_max': left_dist_max,
                'talus_alt_max': left_alt_max,

                'right_dist_min': right_dist_min,
                'right_alt_min': right_alt_min,
                'right_dist_max': right_dist_max,
                'right_alt_max': right_alt_max,
                'right_height': right_height,
                'right_slope_total': right_slope_total,
                'platform_start': platform_start,
                'platform_end': platform_end,
                'platform_width': platform_end - platform_start,
                'center_distance': center_distance,
            })

            current_distance += 1

        all_segments.extend(points)

        
        points_gdf = gpd.GeoDataFrame(all_segments, crs=self.lines_selected.crs)

        if all_calculation_points:

            calculation_points_data = []

            for point_data in all_calculation_points:

                calculation_points_data.append({
                    'geometry': point_data['point'],
                    'elevation': point_data['elevation'],
                    'slope': point_data['slope'],
                    'distance': point_data['distance']
                })

            calculation_points_gdf = gpd.GeoDataFrame(
                calculation_points_data,
                crs=self.lines_selected.crs
            )

        else:

            calculation_points_gdf = None

        self.logger.info("\nAnalysis completed successfully")

        perpendicular_gdf = gpd.GeoDataFrame(
            all_perpendicular_lines,
            geometry="geometry",
            crs=self.lines_selected.crs
        )

        perpendicular_path = os.path.join(
            self.output_folder,
            "perpendicular_profil.gpkg"
        )

        perpendicular_gdf.to_file(
            perpendicular_path,
            driver="GPKG"
        )

        print(
            f"Profils perpendiculaires sauvegardés : {perpendicular_path}"
        )

        platform_gdf = gpd.GeoDataFrame(
            platform_points,
            geometry="geometry",
            crs=self.lines_selected.crs
        )

        platform_gdf.to_file(
            os.path.join(
                self.output_folder,
                "platform_points.gpkg"
            ),
            driver="GPKG"
        )
        
        talus_gdf = gpd.GeoDataFrame(
            talus_points,
            geometry="geometry",
            crs=self.lines_selected.crs
        )

        talus_gdf.to_file(
            os.path.join(
                self.output_folder,
                "talus_points.gpkg"
            ),
            driver="GPKG"
        )

        real_talus_gdf = gpd.GeoDataFrame(
            real_talus_points,
            geometry="geometry",
            crs=self.lines_selected.crs
        )

        real_talus_gdf.to_file(
            os.path.join(
                self.output_folder,
                "real_talus_points.gpkg"
            ),
            driver="GPKG"
        )

        return points_gdf, calculation_points_gdf

    def save_output(self, points_gdf, calculation_points_gdf):
        """Save the classified profiles, calculation points, and R² scores"""
        os.makedirs(self.output_folder, exist_ok=True)
        
        # Save R² scores to CSV
        r2_output_file = os.path.join(self.output_folder, f"r2_scores_{self.route_number}.csv")
        
        r2_df = pd.DataFrame(self.r2_scores)
        r2_df.to_csv(r2_output_file, index=False)
        print(f"R² scores saved to: {r2_output_file}")
        
        # Save segments
        output_file = os.path.join(self.output_folder, "classified_profiles.gpkg")
        points_gdf.to_file(output_file, driver='GPKG', layer='points')
        
        # Save calculation points if they exist
        if calculation_points_gdf is not None:
            calculation_points_gdf.to_file(output_file, driver='GPKG', layer='calculation_points')
        
        print(f"Classified profiles saved as: {output_file}")
        print("Layers created: 'points' and 'calculation_points'")
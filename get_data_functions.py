import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import geopandas as gpd
from shapely.geometry import shape, box

WFS_URL = "https://data.geopf.fr/wfs/ows"
WMS_URL = "https://data.geopf.fr/wms-r"

# Une longue route peut renvoyer beaucoup d'entités : on pagine les requêtes WFS
# et on retente automatiquement les erreurs transitoires (timeouts, 502/503/504)
# pour éviter les échecs de type "Request failed with status code: 504".
REQUEST_TIMEOUT = 120  # secondes
PAGE_SIZE = 1000       # entités par page WFS
MAX_PAGES = 200        # garde-fou (200 000 entités max)


def _session_with_retries():
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=2,  # 2s, 4s, 8s, 16s, 32s entre les tentatives
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_data(filter, type_of_data, bbox):
    """
    Fetches data from the WFS service, en paginant (COUNT/STARTINDEX) pour
    éviter les timeouts de passerelle sur les requêtes volumineuses.
    """
    session = _session_with_retries()
    all_features = []
    start_index = 0

    for page in range(MAX_PAGES):
        params = {
            "SERVICE": "WFS",
            "REQUEST": "GetFeature",
            "VERSION": "2.0.0",
            "TYPENAMES": type_of_data,
            "OUTPUTFORMAT": "application/json",
            "CQL_FILTER": filter,
            "SRSNAME": "EPSG:2154",  # Lambert-93
            "COUNT": PAGE_SIZE,
            "STARTINDEX": start_index,
        }

        try:
            response = session.get(WFS_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(
                f"Echec de la requête WFS ({type_of_data}, filtre={filter!r}) "
                f"après plusieurs tentatives : {e}"
            ) from e

        if response.status_code != 200:
            raise RuntimeError(
                f"Requête WFS échouée ({type_of_data}, filtre={filter!r}) : "
                f"code retour {response.status_code}"
            )

        try:
            content = response.json()
        except requests.exceptions.JSONDecodeError as e:
            raise RuntimeError(
                f"Réponse WFS invalide ({type_of_data}, filtre={filter!r}) : {e}"
            ) from e

        features = content.get("features", [])
        all_features.extend(features)
        print(f"  [WFS {type_of_data}] page {page + 1} : {len(features)} entité(s) "
              f"(total {len(all_features)})")

        if len(features) < PAGE_SIZE:
            break
        start_index += PAGE_SIZE
    else:
        print(f"  [WFS {type_of_data}] limite de {MAX_PAGES} pages atteinte, "
              f"des entités peuvent manquer.")

    if not all_features:
        gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:2154")
    else:
        gdf = gpd.GeoDataFrame.from_features(all_features)
        gdf.set_crs(epsg=2154, inplace=True)

    if type_of_data == "BDTOPO_V3:point_de_repere":
        print("\n===== AVANT CLIP =====")
        if "numero" in gdf.columns:
            print(
                gdf[
                    gdf["numero"].astype(str) == "290"
                ][["numero","libelle","cote"]]
            )

    # Use bounding box to filter relevant sections
    print(f"Bounding box: {bbox}")
    if bbox:
        save_bbox_as_geopackage(bbox, "bounding_box1.gpkg")
    print(f"GeoDataFrame bounds: {gdf.total_bounds}")

    if bbox:
        print("\n===== DEBUG BBOX =====")
        print("BBox originale:", bbox)

        minx, miny, maxx, maxy = bbox
        minx += 50
        miny += 50
        maxx -= 50
        maxy -= 50

        print("BBox modifiée:", (minx, miny, maxx, maxy))
        if minx >= maxx or miny >= maxy:
            print(" BBOX invalide après réduction  on annule le clip")
        else:

            print("\n===== DEBUG CLIP =====")
            print("Nombre AVANT clip:", len(gdf))
            print("Bounds AVANT:", gdf.total_bounds)

            bbox_geom = box(minx, miny, maxx, maxy)

            gdf = gpd.clip(gdf, bbox_geom)

            if type_of_data == "BDTOPO_V3:point_de_repere":

                print("\n===== APRES CLIP =====")

                print(
                    gdf[
                        gdf["numero"].astype(str) == "290"
                    ][["numero","libelle","cote"]]
                )

            print("Nombre APRÈS clip:", len(gdf))
            print("Bounds APRÈS:", gdf.total_bounds)
            print("Empty ?", gdf.empty)

            print("\n===== DEBUG VALIDITÉ =====")
            print("Géométries valides ?", gdf.is_valid.all())
            save_bbox_as_geopackage(gdf.total_bounds, "bounding_box2.gpkg")

    return gdf

def get_ponts_from_geom(route_geom, type_of_data):
    """
    Récupère les entités intersectant l'emprise de la route, en paginant
    (COUNT/STARTINDEX) pour éviter les timeouts sur les longues routes.
    """
    session = _session_with_retries()
    buffer = route_geom.buffer(1000)
    minx, miny, maxx, maxy = buffer.bounds

    all_features = []
    start_index = 0

    for page in range(MAX_PAGES):
        params = {
            "SERVICE": "WFS",
            "REQUEST": "GetFeature",
            "VERSION": "2.0.0",
            "TYPENAMES": type_of_data,
            "OUTPUTFORMAT": "application/json",
            "BBOX": f"{minx},{miny},{maxx},{maxy},EPSG:2154",
            "SRSNAME": "EPSG:2154",
            "COUNT": PAGE_SIZE,
            "STARTINDEX": start_index,
        }

        try:
            response = session.get(WFS_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(
                f"Echec de la requête WFS ({type_of_data}) après plusieurs tentatives : {e}"
            ) from e

        print(f"Bridge status ({type_of_data}, page {page + 1}):", response.status_code)

        if response.status_code != 200:
            raise RuntimeError(
                f"Requête WFS échouée ({type_of_data}) : code retour {response.status_code}"
            )

        features = response.json().get("features", [])
        all_features.extend(features)

        if len(features) < PAGE_SIZE:
            break
        start_index += PAGE_SIZE
    else:
        print(f"  [WFS {type_of_data}] limite de {MAX_PAGES} pages atteinte, "
              f"des entités peuvent manquer.")

    if not all_features:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:2154")

    gdf = gpd.GeoDataFrame.from_features(all_features)
    gdf.set_crs(epsg=2154, inplace=True)

    return gdf

def get_mnt(bbox_values, data_mnt):
        session = _session_with_retries()

        # Calculate width and height maintaining aspect ratio
        minx, miny, maxx, maxy = [float(x) for x in bbox_values]
        bbox_width = maxx - minx
        bbox_height = maxy - miny
        if bbox_height / bbox_width < 1:
            target_width = 2048  # pixels
            target_height = int((bbox_height / bbox_width) * target_width)
        else:
            target_height = 2048
            target_width = int((bbox_width / bbox_height) * target_height)

        params_mnt = {
            "SERVICE": "WMS",
            "REQUEST": "GetMap",
            "VERSION": "1.3.0",
            "LAYERS": data_mnt,
            "FORMAT": "image/geotiff",
            "CRS": "EPSG:2154",  # Lambert-93
            "BBOX": f"{minx},{miny},{maxx},{maxy}",
            "WIDTH": target_width,
            "HEIGHT": target_height,
            "STYLES": ""  # Required empty parameter
        }

        try:
            response_mnt = session.get(WMS_URL, params=params_mnt, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Echec de la requête WMS (MNT) après plusieurs tentatives : {e}") from e

        print(f"MNT response status: {response_mnt.status_code}")

        if response_mnt.status_code == 200:
            # Save the GeoTIFF file
            with open("output_mnt.tif", "wb") as f:
                f.write(response_mnt.content)
            print("Saved DEM to output_dem.tif")
        else:
            print(f"MNT request failed with status code: {response_mnt.status_code}")
            print(f"Response content: {response_mnt.text}")

def save_bbox_as_geopackage(bbox, output_path):
    """
    Save the bounding box as a polygon in a GeoPackage.
    bbox: (minx, miny, maxx, maxy)
    output_path: path to the output .gpkg file
    """
    minx, miny, maxx, maxy = bbox
    polygon = box(minx, miny, maxx, maxy)
    gdf = gpd.GeoDataFrame({'geometry': [polygon]}, crs="EPSG:2154")
    gdf.to_file(output_path, driver="GPKG")
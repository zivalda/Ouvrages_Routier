"""
Traitement en chaîne des MNT par département et par tronçon.

Structure attendue :
    MNT/
      51/
        T1_MNT/mnt.tif
        T2_MNT/mnt.tif
      54/
        T1_MNT/mnt.tif
      57/
        T1_MNT/mnt.tif

Sorties générées dans :
    output_<route>/
      51/
        T1_output_<route>/
        T2_output_<route>/
      54/
        T1_output_<route>/
      57/
        T1_output_<route>/
      rapport_erreurs_<timestamp>.txt   (si erreurs)

Si un tronçon est déjà traité (dossier de sortie existant), il est ignoré.

Usage :
    python batch_processing.py
    python batch_processing.py --route N4
    python batch_processing.py --route N4 --mnt-dir D:/MNT
"""

import argparse
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).parent

MNT_DIR_DEFAULT = Path(
    r"C:\Users\salma.chiadmi\Desktop\03_EEVPI\01_Ouvrages_En_Terre\01_Recensement_OET\algo_recensement_final\MNT"
)


def troncon_sort_key(folder: Path) -> int:
    match = re.search(r"T(\d+)", folder.name)
    return int(match.group(1)) if match else 0


def find_tif(folder: Path) -> Path | None:
    for ext in ("*.tif", "*.TIF", "*.tiff", "*.TIFF"):
        files = list(folder.glob(ext))
        if files:
            return files[0]
    return None


def stream_subprocess(cmd: list[str], cwd: Path) -> tuple[int, list[str]]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines = []
    for line in process.stdout:
        print(f"  {line}", end="", flush=True)
        lines.append(line)
    process.wait()
    return process.returncode, lines


def process_troncon(
    dept: str,
    troncon_id: str,
    route: str,
    mnt_file: Path,
    data_dir: Path,
    output_parent_dept: Path,
) -> None:
    """Copie le MNT, lance le traitement, déplace les sorties."""
    dest_mnt = data_dir / "mnt.tif"
    temp_output_name = f"_tmp_{dept}_{troncon_id}_{route}"
    temp_output = ROOT_DIR / temp_output_name
    output_dest = output_parent_dept / f"{troncon_id}_output_{route}"

    # Nettoyage d'un résidu d'un run précédent échoué
    if temp_output.exists():
        shutil.rmtree(temp_output)

    # Étape 1 : copie du MNT
    if dest_mnt.exists():
        dest_mnt.unlink()
    shutil.copy2(mnt_file, dest_mnt)
    print(f"  [1/4] MNT copié : {mnt_file.name} -> data/mnt.tif")

    # Étape 2 : exécution du traitement
    print(f"  [2/4] Lancement du traitement (route={route})...")
    returncode, output_lines = stream_subprocess(
        [sys.executable, "main_ouvrages_detector.py", "--route", route, "--output-dir", temp_output_name],
        cwd=ROOT_DIR,
    )

    if returncode != 0:
        last_output = "".join(output_lines[-80:])
        raise RuntimeError(
            f"main_ouvrages_detector.py a échoué (code retour {returncode}).\n"
            f"--- Dernière sortie ---\n{last_output}"
        )

    # Étape 3 : déplacement vers la destination finale
    if temp_output.exists():
        output_parent_dept.mkdir(parents=True, exist_ok=True)
        if output_dest.exists():
            shutil.rmtree(output_dest)
        shutil.move(str(temp_output), str(output_dest))
        print(f"  [3/4] Sorties déplacées -> {output_dest.relative_to(ROOT_DIR)}")
    else:
        print(f"  [3/4] ATTENTION : dossier temporaire {temp_output_name}/ introuvable.")

    # Étape 4 : suppression du MNT de data/
    if dest_mnt.exists():
        dest_mnt.unlink()
    print(f"  [4/4] MNT supprimé de data/")


def write_error_report(failures: list[dict], output_root: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_root / f"rapport_erreurs_{ts}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Rapport d'erreurs - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")
        for fail in failures:
            f.write(f"Département : {fail['dept']}  |  Tronçon : {fail['troncon']}  |  Route : {fail['route']}\n")
            f.write(f"Erreur      : {fail['error']}\n")
            if fail.get("traceback"):
                f.write(f"Traceback :\n{fail['traceback']}\n")
            f.write("-" * 40 + "\n\n")
    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="Traitement en chaîne des MNT par département et tronçon"
    )
    parser.add_argument("--route",      type=str,  default=None, help="Code de la route (ex. N4)")
    parser.add_argument("--mnt-dir",    type=Path, default=MNT_DIR_DEFAULT, help="Dossier MNT/ racine")
    parser.add_argument("--output-dir", type=Path, default=None, help="Dossier output (défaut : output_<route>/)")
    args = parser.parse_args()

    mnt_base: Path = args.mnt_dir
    data_dir: Path = ROOT_DIR / "data"

    # Code route
    route = args.route or input("Saisir le code de la route (ex. N4) : ").strip()
    if not route:
        print("[ERREUR] Code route vide. Arrêt.")
        sys.exit(1)

    output_root: Path = args.output_dir or ROOT_DIR / f"output_{route}"

    # Vérification du dossier MNT
    if not mnt_base.exists():
        print(f"[ERREUR FATALE] Dossier MNT introuvable : {mnt_base}")
        sys.exit(1)

    # Découverte des dossiers de département (sous-dossiers numériques)
    dept_folders = sorted(
        [d for d in mnt_base.iterdir() if d.is_dir() and d.name.isdigit()],
        key=lambda d: int(d.name),
    )
    if not dept_folders:
        print(f"[ERREUR FATALE] Aucun dossier de département trouvé dans {mnt_base}")
        sys.exit(1)

    print(f"\nRoute        : {route}")
    print(f"Départements : {', '.join(d.name for d in dept_folders)}")
    print(f"Sorties      : {output_root}")

    output_root.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    failures: list[dict] = []
    successes: list[str] = []
    skipped: list[str] = []

    for dept_folder in dept_folders:
        dept = dept_folder.name  # ex. "51"

        troncon_folders = sorted(dept_folder.glob("T*_MNT"), key=troncon_sort_key)
        if not troncon_folders:
            print(f"\n[AVERTISSEMENT] Aucun dossier T*_MNT dans département {dept}, ignoré.")
            continue

        output_parent_dept = output_root / dept
        print(f"\n{'#'*60}")
        print(f"Département {dept} — {len(troncon_folders)} tronçon(s) : {', '.join(f.name for f in troncon_folders)}")
        print(f"{'#'*60}")

        for troncon_folder in troncon_folders:
            troncon_name = troncon_folder.name
            troncon_id = troncon_name.replace("_MNT", "")  # ex. "T1"
            label = f"{dept}/{troncon_id}"

            print(f"\n{'='*60}")
            print(f"Tronçon : {dept}/{troncon_name}  |  Route : {route}")

            # Fichier MNT
            mnt_file = find_tif(troncon_folder)
            if mnt_file is None:
                error_msg = f"Aucun fichier .tif trouvé dans {troncon_folder}"
                print(f"  [SKIP] {error_msg}")
                failures.append({"dept": dept, "troncon": troncon_id, "route": route,
                                  "error": error_msg, "traceback": None})
                continue

            # Déjà traité ?
            output_dest = output_parent_dept / f"{troncon_id}_output_{route}"
            old_location = output_root / f"{troncon_id}_output_{route}"

            if output_dest.exists():
                print(f"  [SKIP] Déjà traité — {output_dest.relative_to(ROOT_DIR)}")
                skipped.append(label)
                successes.append(label)
                continue

            if old_location.exists():
                # Résultat existant à l'ancien emplacement (avant organisation par département)
                output_parent_dept.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(old_location), str(output_dest))
                    print(f"  [MIGRÉ] {old_location.relative_to(ROOT_DIR)} -> {output_dest.relative_to(ROOT_DIR)}")
                except PermissionError as e:
                    print(f"  [ERREUR MIGRATION] Impossible de déplacer {old_location.name} :")
                    print(f"    {e}")
                    print(f"    => Fermez tout fichier ouvert dans ce dossier (Excel, QGIS...) puis relancez.")
                    failures.append({"dept": dept, "troncon": troncon_id, "route": route,
                                     "error": f"Migration bloquée (fichier ouvert) : {e}",
                                     "traceback": None})
                    continue
                skipped.append(label)
                successes.append(label)
                continue

            print(f"  Fichier MNT : {mnt_file.name}")
            print("-" * 60)

            try:
                process_troncon(dept, troncon_id, route, mnt_file, data_dir, output_parent_dept)
                print(f"\n[OK] {dept}/{troncon_name} traité avec succès.")
                successes.append(label)

            except Exception as exc:
                tb = traceback.format_exc()
                print(f"\n[ERREUR] {dept}/{troncon_name} : {exc}")
                failures.append({"dept": dept, "troncon": troncon_id, "route": route,
                                  "error": str(exc), "traceback": tb})
                # Nettoyage même en cas d'erreur
                dest_mnt = data_dir / "mnt.tif"
                if dest_mnt.exists():
                    dest_mnt.unlink()
                tmp = ROOT_DIR / f"_tmp_{dept}_{troncon_id}_{route}"
                if tmp.exists():
                    shutil.rmtree(tmp)
                print("  Passage au tronçon suivant...")

    # Rapport final
    newly_done = [s for s in successes if s not in skipped]
    print(f"\n{'='*60}")
    print("RAPPORT FINAL")
    print("=" * 60)
    print(f"  Traités ce run : {len(newly_done)}  ({', '.join(newly_done) if newly_done else '-'})")
    print(f"  Déjà faits     : {len(skipped)}  ({', '.join(skipped) if skipped else '-'})")
    print(f"  Échecs         : {len(failures)}")

    if failures:
        print(f"\n{'='*60}")
        print("TRONÇONS EN ÉCHEC :")
        print("=" * 60)
        for i, f in enumerate(failures, 1):
            print(f"\n  [{i}] {f['dept']}/{f['troncon']}  (Route : {f['route']})")
            print(f"       Erreur : {f['error']}")
            if f.get("traceback"):
                print("       Traceback :")
                for line in f["traceback"].splitlines():
                    print(f"         {line}")

        report_path = write_error_report(failures, output_root)
        print(f"\n  Rapport d'erreurs : {report_path.relative_to(ROOT_DIR)}")
    else:
        print("\n  Aucun échec.")


if __name__ == "__main__":
    main()

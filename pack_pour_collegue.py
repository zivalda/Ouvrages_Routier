"""Prepare les dashboards de verification pour envoi a un collegue.

Le 404 au premier profil vient du signe + dans les noms de dossiers
(PR284+500). Teams, SharePoint et beaucoup de serveurs HTTP le lisent
comme un espace. On recode les liens, on ajoute un mode d'emploi, puis
on zippe le dossier complet (index.html doit rester a cote de sections/).

Usage :
  python pack_pour_collegue.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

HERE = Path(__file__).resolve().parent

DASHBOARDS = [
    {
        "label": "A31_57",
        "dest": HERE / "output_A31_57" / "verification",
        "zip": HERE / "output_A31_57" / "Dashboard_A31_57_pour_collegue.zip",
        "output_dir": "output_A31_57",
        "version": "v10",
    },
    {
        "label": "A31_57_v10",
        "dest": HERE / "output_A31_57" / "sections_apres_conditions" / "v10" / "verification",
        "zip": None,
        "output_dir": "output_A31_57",
        "version": "v10",
    },
    {
        "label": "A31_v3",
        "dest": HERE / "output_A31_v3" / "sections_apres_conditions" / "v10" / "verification",
        "zip": HERE / "output_A31_v3" / "Dashboard_A31_v3_pour_collegue.zip",
        "output_dir": "output_A31_v3",
        "version": "v10",
    },
]

LIRE_MOI = """Dashboard de verification des ouvrages en terre
==============================================

Le collegue n'a pas besoin de Python ni d'un serveur.

1. Dezippez TOUT le dossier (pas seulement index.html).
2. N'ouvrez pas le fichier depuis Teams, Outlook ou SharePoint :
   ces apercus cassent les liens et donnent une erreur 404.
3. Dans l'Explorateur Windows, double-cliquez OUVRIR_LE_DASHBOARD.bat
   (ou index.html).
4. Cliquez un troncon pour voir les profils tous les 10 m.
5. Remplissez la colonne Verdict manuel (conforme, remblai, deblai, etc.)
   et la note si besoin.
6. Cliquez Telecharger le CSV.
7. Renvoyez le fichier verdicts_manuels.csv (dossier Telechargements).

Les choix restent aussi dans le navigateur, mais seul le CSV permet de
recaler l'Excel ensuite.
"""

OUVRIR_BAT = """@echo off
cd /d "%~dp0"
if not exist "index.html" (
  echo index.html introuvable. Dezippez tout le dossier avant d'ouvrir.
  pause
  exit /b 1
)
if not exist "sections" (
  echo Le dossier sections est absent. Les profils donneront une erreur 404.
  echo Dezippez l'archive complete, pas seulement index.html.
  pause
  exit /b 1
)
start "" "%~dp0index.html"
"""


def href_url(path: str) -> str:
    return quote(unquote(str(path or "")), safe="/._-")


def rebuild_index(dest: Path, output_dir: str, version: str) -> bool:
    cmd = [
        sys.executable,
        str(HERE / "verify_livrable.py"),
        "--rebuild-index",
        "--output-dir",
        output_dir,
        "--version",
        version,
        "--dest",
        str(dest),
    ]
    run = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True)
    if run.returncode != 0:
        print("  rebuild-index ignore :", (run.stderr or run.stdout or "")[-400:])
        return False
    print(" ", (run.stdout or "").strip() or "index reconstruit")
    return True


def patch_index(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    html = re.sub(
        r'href="(sections/[^"]+)"',
        lambda m: f'href="{href_url(m.group(1))}"',
        html,
    )
    html = html.replace(
        ">Enregistrer les verdicts</button>",
        ">Telecharger le CSV</button>",
    )
    if 'if (location.protocol === "file:") return;' not in html:
        html = html.replace(
            '    fetch("verdicts_manuels.csv")',
            '    if (location.protocol === "file:") return;\n    fetch("verdicts_manuels.csv")',
        )
    path.write_text(html, encoding="utf-8")


def patch_sections(dest: Path) -> int:
    n = 0
    src_re = re.compile(r'(src=")([^"]+)(")')
    root = dest / "sections"
    if not root.is_dir():
        return 0
    for html_path in root.glob("*/section.html"):
        html = html_path.read_text(encoding="utf-8")
        new = src_re.sub(lambda m: m.group(1) + href_url(m.group(2)) + m.group(3), html)
        if new != html:
            html_path.write_text(new, encoding="utf-8")
            n += 1
    return n


def write_helpers(dest: Path) -> None:
    (dest / "LIRE_MOI.txt").write_text(LIRE_MOI, encoding="utf-8")
    (dest / "OUVRIR_LE_DASHBOARD.bat").write_text(OUVRIR_BAT, encoding="utf-8")


def zip_dest(dest: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(dest):
            for name in files:
                if name.endswith(".zip") or name.startswith("~$"):
                    continue
                full = Path(root) / name
                arc = Path(dest.name) / full.relative_to(dest)
                zf.write(full, arc.as_posix())


def first_href_ok(dest: Path) -> str:
    html = (dest / "index.html").read_text(encoding="utf-8")
    m = re.search(r'href="(sections/[^"]+)"', html)
    if not m:
        return "aucun lien de profil"
    href = unquote(m.group(1))
    target = dest / href.replace("/", os.sep)
    if target.is_file():
        return f"OK {m.group(1)} -> {target.name}"
    return f"MANQUANT {m.group(1)}"


def main() -> None:
    for spec in DASHBOARDS:
        dest = spec["dest"]
        print(f"=== {spec['label']}")
        if not (dest / "index.html").is_file():
            print(f"  absent : {dest}")
            continue
        rebuild_index(dest, spec["output_dir"], spec["version"])
        patch_index(dest / "index.html")
        n = patch_sections(dest)
        write_helpers(dest)
        print(f"  pages profils recodees : {n}")
        print(f"  premier lien : {first_href_ok(dest)}")
        zip_path = spec["zip"]
        if zip_path is None:
            continue
        print(f"  zip -> {zip_path}")
        zip_dest(dest, zip_path)
        print(f"  taille zip : {zip_path.stat().st_size / 1e6:.1f} Mo")


if __name__ == "__main__":
    main()

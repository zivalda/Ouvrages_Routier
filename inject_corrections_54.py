"""Place les corrections manuelles du 54 sur le dashboard v10 actuel, puis rezippe."""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

import pandas as pd

from pack_pour_collegue import (
    first_href_ok,
    patch_index,
    patch_sections,
    rebuild_index,
    write_helpers,
    zip_dest,
)

HERE = Path(__file__).resolve().parent
DEST = HERE / "output_A31_v3" / "sections_apres_conditions" / "v10"
VERIF = DEST / "verification"
ZIP_PATH = HERE / "output_A31_v3" / "Dashboard_A31_v3_pour_collegue.zip"
DESKTOP_ZIP = Path.home() / "Desktop" / "Dashboard_A31_v3_pour_collegue.zip"
NOM_PR = re.compile(r"PR\s*(\d+)\+(\d+)", re.I)


def _pos(pr, ab) -> int:
    return int(pr) * 1000 + int(ab)


def _pr_of(nom) -> int | None:
    m = NOM_PR.search(str(nom or ""))
    if not m:
        return None
    return _pos(m.group(1), m.group(2))


def _clean(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def flush_excel(path: Path) -> None:
    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("Excel ouvert eventuellement, mais win32com absent : on lit le fichier disque.")
        return
    try:
        excel = win32com.client.GetActiveObject("Excel.Application")
        target = str(path.resolve()).lower()
        for wb in excel.Workbooks:
            if str(Path(wb.FullName).resolve()).lower() == target:
                wb.Save()
                print(f"Classeur Excel enregistre : {wb.FullName}")
                return
        print("Le classeur de corrections n'est pas le classeur Excel actif.")
    except Exception as exc:
        print(f"Impossible d'enregistrer Excel automatiquement ({exc}).")


def load_corrections() -> pd.DataFrame:
    xlsx = VERIF / "index_apres_correction_manuelle.xlsx"
    csv_path = VERIF / "index_apres_correction_manuelle.csv"
    flush_excel(xlsx)
    if xlsx.exists():
        df = pd.read_excel(xlsx)
        print(f"Corrections lues : {xlsx.name}")
    else:
        df = pd.read_csv(csv_path, sep=";")
        print(f"Corrections lues : {csv_path.name}")
    df["classe_manuelle"] = df["classe_manuelle"].map(_clean)
    df["note"] = df["note"].map(_clean)
    return df[(df["classe_manuelle"] != "") | (df["note"] != "")].copy()


def load_inventory() -> pd.DataFrame:
    inv = pd.read_excel(DEST / "inventaire_operationnel_ponts.xlsx")
    cols = {str(c): c for c in inv.columns}

    def pick(*keys):
        for name, col in cols.items():
            low = name.lower()
            if all(k in low for k in keys):
                return col
        raise KeyError(keys)

    nom = pick("ron") if any("ron" in str(c).lower() for c in inv.columns) else pick("nom")
    out = pd.DataFrame({
        "nom": inv[nom].astype(str),
        "classe": inv[pick("class")].astype(str),
        "pr_s": inv[pick("pr", "d")].astype(int),
        "ab_s": inv[pick("abs", "d")].astype(int),
        "pr_e": inv[pick("pr", "fin")].astype(int),
        "ab_e": inv[pick("abs", "fin")].astype(int),
    })
    out["n"] = range(1, len(out) + 1)
    out["s"] = [_pos(r.pr_s, r.ab_s) for r in out.itertuples()]
    out["e"] = [_pos(r.pr_e, r.ab_e) for r in out.itertuples()]
    out.loc[out["e"] <= out["s"], "e"] = out["s"] + 1
    return out


def locate(inv: pd.DataFrame, p: int):
    hits = inv[(inv["s"] <= p) & (p < inv["e"])]
    if hits.empty:
        hits = inv[inv["s"] == p]
    return hits.iloc[0] if len(hits) else None


def map_corrections(src: pd.DataFrame, inv: pd.DataFrame) -> list[dict]:
    by_nom: dict[str, dict] = {}
    print("\nCorrespondance anciens PR -> dashboard v10 actuel")
    for rec in src.to_dict("records"):
        p = _pr_of(rec.get("nom"))
        if p is None:
            print(f"  ignore (PR illisible) : {rec}")
            continue
        row = locate(inv, p)
        if row is None:
            print(f"  introuvable : {rec['nom']}")
            continue
        liv = str(row["classe"]).strip()
        wanted = rec["classe_manuelle"]
        note = rec["note"]
        old = str(rec["nom"])
        if old != row["nom"]:
            extra = f"ex {old}"
            note = f"{note} ({extra})" if note else extra
        if wanted and wanted == liv:
            classe = "conforme"
        else:
            classe = wanted
        prev = by_nom.get(row["nom"], {"classe": "", "note": ""})
        if classe:
            prev["classe"] = classe
        if note:
            prev["note"] = (prev["note"] + " | " + note).strip(" |") if prev["note"] else note
        prev.update({
            "nom": row["nom"],
            "n": int(row["n"]),
            "classe_livrable": liv,
        })
        by_nom[row["nom"]] = prev
        print(
            f"  {old:16s} {wanted or '-':8s} -> {row['nom']:16s} "
            f"livrable={liv:8s} manuel={prev['classe'] or '-'}"
        )
    return list(by_nom.values())


def write_verdicts(rows: list[dict]) -> Path:
    path = VERIF / "verdicts_manuels.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["nom", "n", "classe_livrable", "classe_manuelle", "note"],
            delimiter=";",
        )
        w.writeheader()
        for rec in rows:
            w.writerow({
                "nom": rec["nom"],
                "n": rec["n"],
                "classe_livrable": rec["classe_livrable"],
                "classe_manuelle": rec.get("classe") or "",
                "note": rec.get("note") or "",
            })
    print(f"\nEcrit : {path} ({len(rows)} troncons)")
    return path


def main() -> None:
    src = load_corrections()
    inv = load_inventory()
    mapped = map_corrections(src, inv)
    write_verdicts(mapped)
    rebuild_index(VERIF, "output_A31_v3", "v10")
    patch_index(VERIF / "index.html")
    patch_sections(VERIF)
    write_helpers(VERIF)
    print("premier lien :", first_href_ok(VERIF))
    zip_dest(VERIF, ZIP_PATH)
    print(f"zip : {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1e6:.1f} Mo)")
    try:
        DESKTOP_ZIP.write_bytes(ZIP_PATH.read_bytes())
        print(f"copie Bureau : {DESKTOP_ZIP}")
    except OSError as exc:
        print(f"copie Bureau impossible : {exc}")


if __name__ == "__main__":
    main()

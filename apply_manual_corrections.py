"""Recale l'inventaire Excel a partir des corrections du dashboard.

Le navigateur ne peut pas ecrire le .xlsx. Apres Enregistrer (CSV), lancer :

    python apply_manual_corrections.py --output-dir output_A31_57 --version v10
    python apply_manual_corrections.py --output-dir output_A31_v3 --version v10

Cherche verdicts_manuels.csv (verification) ou corrections_A31_57_operationnel.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil

import numpy as np
import pandas as pd

from harmonize_ouvrages import export_livrable_gpkg, save_styled_excel
from verify_livrable import load_inventory

CLASS_OK = {
    "remblai",
    "deblai",
    "rasant",
    "mixte",
    "pont",
    "mur de soutènement",
}
CLASS_ALIAS = {
    "mur": "mur de soutènement",
    "mur de soutenement": "mur de soutènement",
    "mur de soutènement": "mur de soutènement",
    "conforme": None,
    "-": None,
    "": None,
}
NOM_PR = re.compile(r"PR\s*(\d+)\+(\d+)", re.I)


def _pos(pr, ab) -> int:
    return int(pr) * 1000 + int(ab)


def _parse_h(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return np.nan
    s = str(val).strip().replace(" ", "").replace(",", ".")
    if s in ("-", "", "nan", "None"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def _nanmax(a, b):
    vals = [x for x in (a, b) if x is not None and not (isinstance(x, float) and np.isnan(x))]
    return max(vals) if vals else np.nan


def _norm_class(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in CLASS_ALIAS:
        return CLASS_ALIAS[s]
    if s in CLASS_OK:
        return s
    return s if s else None


def _find_csv(dest, output_dir, explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise SystemExit(f"CSV introuvable : {explicit}")
        return explicit
    candidates = [
        os.path.join(dest, "verdicts_manuels.csv"),
        os.path.join(dest, "verification", "verdicts_manuels.csv"),
        os.path.join(dest, "verification", "verdicts_manuels.json"),
        os.path.join(dest, "corrections_A31_57_operationnel.csv"),
        os.path.join(output_dir, "corrections_A31_57_operationnel.csv"),
        os.path.join(output_dir, "corrections_A31_57.csv"),
        os.path.join(output_dir, "dashboard_operationnel.csv"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise SystemExit(
        "Aucun CSV de corrections. Dans le dashboard : Enregistrer, puis placer "
        "verdicts_manuels.csv dans le dossier verification (ou passer --csv)."
    )


def _load_csv_rows(path):
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = []
        if isinstance(data, dict):
            for nom, rec in data.items():
                if isinstance(rec, str):
                    rec = {"classe": rec}
                rec = dict(rec)
                rec["nom"] = rec.get("nom") or nom
                rows.append(rec)
        elif isinstance(data, list):
            rows = list(data)
        return rows
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        except csv.Error:
            class _Semi(csv.excel):
                delimiter = ";"
            dialect = _Semi if ";" in sample.split("\n", 1)[0] else csv.excel
        return list(csv.DictReader(f, dialect=dialect))


def load_corrections(path):
    rows = []
    for rec in _load_csv_rows(path):
        nom = str(rec.get("nom") or "").strip()
        classe = _norm_class(
            rec.get("classe_manuelle")
            or rec.get("classe")
            or rec.get("classification")
        )
        note = str(rec.get("note") or "").strip()
        n_raw = rec.get("n")
        try:
            n = int(n_raw) if n_raw not in (None, "") else None
        except (TypeError, ValueError):
            n = None
        if not nom and n is None:
            continue
        if classe is None and not note:
            continue
        rows.append({"nom": nom, "n": n, "classe": classe, "note": note})
    return rows


def _inventory_internal(xlsx_path, route="A31"):
    raw = pd.read_excel(xlsx_path)
    inv = load_inventory(xlsx_path)
    hmax = None
    hrem = None
    hdeb = None
    for c in raw.columns:
        cl = str(c).lower()
        if hmax is None and "h max" in cl:
            hmax = c
        if hrem is None and "h remblai" in cl:
            hrem = c
        if hdeb is None and "h d" in cl and "blai" in cl:
            hdeb = c
    out = inv.copy()
    out["mur_soutenement"] = inv["mur"]
    out["hauteur_affichee"] = raw[hmax].map(_parse_h) if hmax else np.nan
    out["hauteur_remblai_m"] = raw[hrem].map(_parse_h) if hrem else np.nan
    out["hauteur_deblai_m"] = raw[hdeb].map(_parse_h) if hdeb else np.nan
    out["route"] = route
    return out.reset_index(drop=True)


def _pr_from_nom(nom):
    m = NOM_PR.search(str(nom or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _match_index(df, rec):
    nom = rec.get("nom") or ""
    if nom:
        hits = df.index[df["nom"].astype(str) == nom].tolist()
        if hits:
            return hits[0]
    n = rec.get("n")
    if n is not None and 1 <= n <= len(df):
        return n - 1
    pr = _pr_from_nom(nom)
    if pr is None:
        return None
    p = _pos(*pr)
    for i, row in df.iterrows():
        s = _pos(row["PR_start"], row["abcisse_start"])
        e = _pos(row["PR_end"], row["abcisse_end"])
        if s <= p < e:
            return i
    return None


def apply_corrections(df, recs):
    log = []
    df = df.copy()
    for rec in recs:
        classe = rec.get("classe")
        if not classe:
            continue
        if classe not in CLASS_OK:
            log.append({**rec, "statut": f"classe inconnue ({classe})"})
            continue
        idx = _match_index(df, rec)
        if idx is None:
            log.append({**rec, "statut": "troncon introuvable"})
            continue
        avant = str(df.at[idx, "classification"])
        if avant == classe:
            log.append({**rec, "statut": "deja a jour", "avant": avant})
            continue
        df.at[idx, "classification"] = classe
        if classe != "mixte":
            df.at[idx, "hauteur_remblai_m"] = np.nan
            df.at[idx, "hauteur_deblai_m"] = np.nan
        log.append({
            **rec,
            "statut": "modifie",
            "avant": avant,
            "nom_ligne": df.at[idx, "nom"],
        })
    return df, log


def merge_adjacent(df):
    if df.empty:
        return df
    rows = df.to_dict("records")
    out = []
    n_merge = 0
    for r in rows:
        if out and out[-1]["classification"] == r["classification"]:
            a = out[-1]
            if _pos(a["PR_end"], a["abcisse_end"]) == _pos(r["PR_start"], r["abcisse_start"]):
                a["PR_end"] = r["PR_end"]
                a["abcisse_end"] = r["abcisse_end"]
                a["length"] = _pos(a["PR_end"], a["abcisse_end"]) - _pos(
                    a["PR_start"], a["abcisse_start"]
                )
                a["hauteur_affichee"] = _nanmax(a.get("hauteur_affichee"), r.get("hauteur_affichee"))
                a["hauteur_remblai_m"] = _nanmax(a.get("hauteur_remblai_m"), r.get("hauteur_remblai_m"))
                a["hauteur_deblai_m"] = _nanmax(a.get("hauteur_deblai_m"), r.get("hauteur_deblai_m"))
                mur_a = str(a.get("mur_soutenement") or "-")
                mur_b = str(r.get("mur_soutenement") or "-")
                if mur_b not in ("-", "", "nan") and mur_b not in mur_a:
                    a["mur_soutenement"] = mur_a if mur_a not in ("-", "", "nan") else mur_b
                    if mur_a not in ("-", "", "nan") and mur_b not in ("-", "", "nan") and mur_a != mur_b:
                        a["mur_soutenement"] = "D et G"
                n_merge += 1
                continue
        out.append(dict(r))
    merged = pd.DataFrame(out)
    if merged.empty:
        return merged, n_merge
    route = str(merged.iloc[0].get("route") or "A31")
    noms = []
    for _, r in merged.iterrows():
        noms.append(f"{route}_PR{int(r['PR_start'])}+{int(r['abcisse_start'])}")
    merged["nom"] = noms
    if "classification" in merged.columns:
        merged.loc[merged["classification"] != "mixte", ["hauteur_remblai_m", "hauteur_deblai_m"]] = np.nan
    return merged, n_merge


def _save_xlsx(df, path):
    try:
        save_styled_excel(df, path)
        print(f"Ecrit : {path} ({len(df)} sections)")
        return path
    except PermissionError:
        alt = path.replace(".xlsx", "_corrige.xlsx")
        save_styled_excel(df, alt)
        print(f"Fichier ouvert ailleurs. Ecrit : {alt} ({len(df)} sections)")
        return alt


def main():
    parser = argparse.ArgumentParser(
        description="Applique les corrections manuelles du dashboard sur l'Excel v10"
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--version", default="v10")
    parser.add_argument("--route", default="A31")
    parser.add_argument("--csv", default=None, help="Chemin du CSV/JSON de corrections")
    parser.add_argument("--no-merge", action="store_true", help="Ne pas fusionner les voisins de meme classe")
    args = parser.parse_args()

    dest = os.path.join(args.output_dir, "sections_apres_conditions", args.version)
    xlsx = os.path.join(dest, "inventaire_operationnel_ponts.xlsx")
    if not os.path.exists(xlsx):
        xlsx = os.path.join(dest, "inventaire_operationnel.xlsx")
    if not os.path.exists(xlsx):
        raise SystemExit(f"Inventaire introuvable dans {dest}")

    csv_path = _find_csv(dest, args.output_dir, args.csv)
    recs = load_corrections(csv_path)
    print(f"Corrections lues : {csv_path} ({len(recs)} ligne(s) renseignee(s))")

    df = _inventory_internal(xlsx, route=args.route)
    df, log = apply_corrections(df, recs)
    n_mod = sum(1 for x in log if x.get("statut") == "modifie")
    if n_mod == 0:
        print("Aucune classe a modifier (conforme / deja a jour / CSV vide).")
        return

    n_merge = 0
    if not args.no_merge:
        df, n_merge = merge_adjacent(df)

    backup = os.path.join(dest, "inventaire_operationnel_ponts_avant_manuel.xlsx")
    if not os.path.exists(backup):
        shutil.copy2(xlsx, backup)
        print(f"Copie de securite : {backup}")

    written = _save_xlsx(df, xlsx)
    other = os.path.join(dest, "inventaire_operationnel.xlsx")
    if os.path.abspath(other) != os.path.abspath(written):
        _save_xlsx(df, other)

    copy57 = os.path.join(args.output_dir, f"inventaire_{args.route}_57_{args.version}.xlsx")
    if os.path.exists(copy57):
        _save_xlsx(df, copy57)

    log_path = os.path.join(dest, "corrections_appliquees.csv")
    pd.DataFrame(log).to_csv(log_path, index=False, encoding="utf-8-sig")
    print(f"Journal : {log_path}")
    print(f"{n_mod} classe(s) changee(s), {n_merge} fusion(s) de voisins, {len(df)} sections.")
    print(df["classification"].value_counts().to_string())

    gpkg = os.path.join(dest, "inventaire_operationnel.gpkg")
    try:
        export_livrable_gpkg(df, args.output_dir, gpkg)
    except Exception as exc:
        print(f"GPKG non regenere : {exc}")


if __name__ == "__main__":
    main()

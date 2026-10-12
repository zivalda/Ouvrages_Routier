"""
Fusionne le recensement A31 departement 57 (lots output_A31_16 a 27)
en un inventaire unique, au format de output_A31/selected_ouvrages.xlsx.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from html import escape
from pathlib import Path

import geopandas as gpd
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill

SRC = Path(r"D:\INVENTAIRE_OT_57\Dossier Ghiles\A31-ouvrage\inventaireA31")
MNT_DIR = Path(r"D:\INVENTAIRE_OT_57\Dossier Ghiles\A31-ouvrage\mntA31")
LOTS = list(range(16, 28))
COLS = [
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
FILLS = {
    "remblai": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "deblai": PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid"),
    "rasant": PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),
    "mixte": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "mur de soutènement": PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"),
    "pont": PatternFill(start_color="E2D5F1", end_color="E2D5F1", fill_type="solid"),
}


def _xlsx_in_lot(folder: Path) -> Path:
    xs = [
        p
        for p in folder.glob("selected_ouvrages*.xlsx")
        if not p.name.startswith("~$")
    ]
    if not xs:
        raise FileNotFoundError(f"Pas d'Excel dans {folder}")
    return xs[0]


def _fix_pr_end(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    bad = df["PR_end"].astype(int) >= 900
    if not bad.any():
        return df
    start = df["PR_start"].astype(int) * 1000 + df["abcisse_start"].astype(int)
    end_m = start + df["length"].astype(int)
    df.loc[bad, "PR_end"] = (end_m.loc[bad] // 1000).astype(int)
    df.loc[bad, "abcisse_end"] = (end_m.loc[bad] % 1000).astype(int)
    return df


def _extract_pr(val, nom="") -> int | None:
    if isinstance(val, (int, float)) and not pd.isna(val) and not isinstance(val, bool):
        if 0 <= float(val) < 10000 and float(val) == int(float(val)):
            return int(val)
    text = f"{val} {nom}"
    m = re.search(r"PR(\d+)", str(text), re.I)
    if m:
        return int(m.group(1))
    return None


def _fix_gpkg_pr(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    pr_s = []
    for _, r in gdf.iterrows():
        pr_s.append(_extract_pr(r.get("PR_start"), r.get("nom", "")))
    gdf["PR_start"] = pr_s
    start = gdf["PR_start"].astype("Int64") * 1000 + gdf["abcisse_start"].fillna(0).astype(int)
    end_m = start + gdf["length"].fillna(0).astype(int)
    gdf["PR_end"] = (end_m // 1000).astype("Int64")
    gdf["abcisse_end"] = (end_m % 1000).astype(int)
    return gdf


def save_styled_excel(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    out.insert(0, "id", range(1, len(out) + 1))
    keep = [c for c in COLS if c in out.columns]
    out = out[keep]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="Ouvrages", index=False)
        ws = writer.sheets["Ouvrages"]
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        class_col = None
        for cell in ws[1]:
            if cell.value == "classification":
                class_col = cell.column
                break
        if class_col:
            for row in ws.iter_rows(min_row=2):
                fill = FILLS.get(str(row[class_col - 1].value).lower())
                if fill:
                    for cell in row:
                        cell.fill = fill
        for column_cells in ws.columns:
            letter = column_cells[0].column_letter
            maxlen = 0
            for cell in column_cells:
                if cell.value is not None:
                    maxlen = max(maxlen, len(str(cell.value)))
            ws.column_dimensions[letter].width = max(10, min(maxlen + 2, 22))
        ws.freeze_panes = "A2"
        for col in ws.columns:
            for cell in col:
                if isinstance(cell.value, float):
                    cell.number_format = "0.00"
                    cell.alignment = Alignment(horizontal="center")


def write_dashboard(df: pd.DataFrame, path: Path, titre: str) -> None:
    records = []
    for i, r in df.iterrows():
        records.append({
            "n": int(i) + 1 if "id" not in df.columns else int(r.get("id", i + 1)),
            "nom": str(r["nom"]),
            "cote": "G" if str(r["nom"]).endswith("_G") else "D",
            "classification": str(r["classification"]),
            "pr_s": int(r["PR_start"]),
            "ab_s": int(r["abcisse_start"]),
            "pr_e": int(r["PR_end"]),
            "ab_e": int(r["abcisse_end"]),
            "length": int(r["length"]) if pd.notna(r["length"]) else 0,
            "h": None if pd.isna(r.get("hauteur_talus_max")) else round(float(r["hauteur_talus_max"]), 2),
        })
    payload = json.dumps(records, ensure_ascii=True)
    html = DASHBOARD_HTML.replace("__TITLE__", escape(titre)).replace("__DATA__", payload)
    path.write_text(html, encoding="utf-8")


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
body { font-family: Calibri, Segoe UI, Arial, sans-serif; margin: 0; color: #222; background: #f7f7f7; }
header { background: #1f4e79; color: #fff; padding: 14px 22px; }
header h1 { margin: 0; font-size: 20px; }
nav { display: flex; background: #fff; border-bottom: 1px solid #ccc; position: sticky; top: 0; }
nav button { border: 0; background: none; padding: 10px 16px; cursor: pointer; border-bottom: 3px solid transparent; }
nav button.on { border-bottom-color: #1f4e79; color: #1f4e79; font-weight: 700; }
main { padding: 16px 22px 40px; }
.tab { display: none; }
.tab.on { display: block; }
.toolbar { margin-bottom: 10px; }
.toolbar input, .toolbar select, .toolbar button { margin-right: 8px; font-size: 14px; padding: 4px 8px; }
#save-status { color: #0a5; margin-left: 8px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px; }
th, td { border: 1px solid #ccc; padding: 4px 8px; }
th { background: #e8eef4; position: sticky; top: 42px; }
.remblai { background: #C6EFCE; } .deblai { background: #BDD7EE; }
.rasant { background: #FFF2CC; } .mixte { background: #FFC7CE; }
.pont { background: #E2D5F1; } .mur { background: #D9D9D9; }
.kpi { display: inline-block; background: #fff; border: 1px solid #ccc; padding: 8px 12px; margin: 0 8px 8px 0; }
.kpi b { display: block; font-size: 22px; color: #1f4e79; }
.muted { color: #555; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p class="muted" style="color:#dbe7f3;margin:4px 0 0">Lots 16 a 27, A31 en 57. Changez la classe, puis Enregistrer. Profils 10 m : <a href="verification/index.html" style="color:#fff">ouvrir le dashboard de verification</a>.</p>
</header>
<nav>
  <button type="button" class="on" data-tab="inv">Inventaire</button>
  <button type="button" data-tab="ch">Chiffres</button>
</nav>
<main>
<section id="inv" class="tab on">
  <div class="toolbar">
    Filtre classe <select id="f-class"><option value="">toutes</option></select>
    Cote <select id="f-cote"><option value="">D et G</option><option value="D">D</option><option value="G">G</option></select>
    PR min <input id="f-pr" type="number" style="width:70px">
    <button type="button" id="btn-save">Enregistrer</button>
    <label>Importer <input type="file" id="btn-import" accept=".csv,text/csv"></label>
    <span id="save-status"></span>
  </div>
  <p class="muted">Les choix restent dans le navigateur. Enregistrer telecharge corrections_A31_57.csv (a garder dans ce dossier). Puis recaler l'Excel : python apply_manual_corrections.py --output-dir output_A31_57 --version v10 --csv corrections_A31_57_operationnel.csv</p>
  <table>
    <thead><tr>
      <th>#</th><th>Nom</th><th>Cote</th><th>PR debut</th><th>PR fin</th>
      <th>Longueur</th><th>H talus</th><th>Classe avant</th><th>Classe (modifiable)</th><th>Note</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</section>
<section id="ch" class="tab">
  <div id="kpis"></div>
  <table id="stats"></table>
</section>
</main>
<script>
var DATA = __DATA__;
var KEY = "a31_57_corrections";
var CLASSES = ["remblai","deblai","rasant","mixte","pont","mur de soutenement"];
(function () {
  document.querySelectorAll("nav button").forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll("nav button").forEach(function (x) { x.classList.remove("on"); });
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      document.getElementById(b.getAttribute("data-tab")).classList.add("on");
    };
  });
  var sel = document.getElementById("f-class");
  CLASSES.forEach(function (c) {
    var o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o);
  });
  function saved() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; }
  }
  function persist() {
    var out = saved();
    document.querySelectorAll("tr[data-nom]").forEach(function (tr) {
      out[tr.getAttribute("data-nom")] = {
        classe: tr.querySelector(".v-cl").value,
        note: tr.querySelector(".v-note").value
      };
    });
    localStorage.setItem(KEY, JSON.stringify(out));
  }
  function cssClass(c) {
    c = (c || "").toLowerCase();
    if (c.indexOf("mur") === 0) return "mur";
    return CLASSES.indexOf(c) >= 0 ? c : "";
  }
  function filtered() {
    var clEl = document.getElementById("f-class");
    var coteEl = document.getElementById("f-cote");
    var prEl = document.getElementById("f-pr");
    var cl = clEl ? clEl.value : "";
    var cote = coteEl ? coteEl.value : "";
    var pr = prEl ? prEl.value : "";
    return DATA.filter(function (r) {
      if (cl && r.classification !== cl) return false;
      if (cote && r.cote && r.cote !== cote) return false;
      if (pr && r.pr_s < parseInt(pr, 10)) return false;
      return true;
    });
  }
  function render() {
    var store = saved();
    var tb = document.getElementById("tbody");
    tb.innerHTML = "";
    filtered().forEach(function (r) {
      var rec = store[r.nom] || {};
      var cl = rec.classe || r.classification;
      var tr = document.createElement("tr");
      tr.setAttribute("data-nom", r.nom);
      tr.className = cssClass(cl);
      var opts = CLASSES.map(function (c) {
        return "<option" + (c === cl ? " selected" : "") + ">" + c + "</option>";
      }).join("");
      tr.innerHTML =
        "<td>" + r.n + "</td><td>" + r.nom + "</td><td>" + r.cote + "</td>" +
        "<td>" + r.pr_s + "+" + String(r.ab_s).padStart(3,"0") + "</td>" +
        "<td>" + r.pr_e + "+" + String(r.ab_e).padStart(3,"0") + "</td>" +
        "<td>" + r.length + "</td><td>" + (r.h == null ? "-" : r.h) + "</td>" +
        "<td>" + r.classification + "</td>" +
        "<td><select class='v-cl'>" + opts + "</select></td>" +
        "<td><input class='v-note' value='" + (rec.note || "").replace(/'/g, "&#39;") + "'></td>";
      tb.appendChild(tr);
      tr.querySelector(".v-cl").addEventListener("change", function () {
        tr.className = cssClass(this.value);
        persist();
      });
      tr.querySelector(".v-note").addEventListener("input", persist);
    });
    stats();
  }
  function stats() {
    var store = saved();
    var counts = {};
    DATA.forEach(function (r) {
      var cl = (store[r.nom] && store[r.nom].classe) || r.classification;
      counts[cl] = (counts[cl] || 0) + 1;
    });
    var changed = DATA.filter(function (r) {
      return store[r.nom] && store[r.nom].classe && store[r.nom].classe !== r.classification;
    }).length;
    document.getElementById("kpis").innerHTML =
      "<div class='kpi'><b>" + DATA.length + "</b>lignes</div>" +
      "<div class='kpi'><b>" + changed + "</b>classes modifiees</div>";
    var html = "<tr><th>Classe</th><th>n</th></tr>";
    Object.keys(counts).sort().forEach(function (k) {
      html += "<tr class='" + cssClass(k) + "'><td>" + k + "</td><td>" + counts[k] + "</td></tr>";
    });
    document.getElementById("stats").innerHTML = html;
  }
  function toCsv() {
    var store = saved();
    var lines = ["nom;classification_avant;classification;note"];
    DATA.forEach(function (r) {
      var rec = store[r.nom] || {};
      var cl = rec.classe || "";
      var note = rec.note || "";
      lines.push([r.nom, r.classification, cl, note].join(";"));
    });
    return "\ufeff" + lines.join("\r\n");
  }
  document.getElementById("btn-save").onclick = async function () {
    persist();
    var blob = new Blob([toCsv()], {type: "text/csv;charset=utf-8"});
    if (window.showSaveFilePicker) {
      try {
        var h = await window.showSaveFilePicker({
          suggestedName: "corrections_A31_57.csv",
          types: [{description: "CSV", accept: {"text/csv": [".csv"]}}]
        });
        var w = await h.createWritable();
        await w.write(blob); await w.close();
        document.getElementById("save-status").textContent = "Enregistre.";
        return;
      } catch (e) { if (e && e.name === "AbortError") return; }
    }
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "corrections_A31_57.csv";
    a.click();
    document.getElementById("save-status").textContent = "Fichier telecharge.";
  };
  document.getElementById("btn-import").onchange = function () {
    var f = this.files && this.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result || "").replace(/^\ufeff/, "");
      var lines = text.split(/\r?\n/).filter(function (l) { return l.trim(); });
      var sep = lines[0].indexOf(";") >= 0 ? ";" : ",";
      var head = lines[0].split(sep);
      var iNom = head.indexOf("nom");
      var iCl = head.indexOf("classification");
      var iNote = head.indexOf("note");
      var store = saved();
      for (var i = 1; i < lines.length; i++) {
        var c = lines[i].split(sep);
        var nom = c[iNom];
        if (!nom) continue;
        store[nom] = {classe: iCl >= 0 ? c[iCl] : "", note: iNote >= 0 ? c[iNote] : ""};
      }
      localStorage.setItem(KEY, JSON.stringify(store));
      render();
    };
    reader.readAsText(f, "utf-8");
    this.value = "";
  };
  ["f-class","f-cote","f-pr"].forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", render);
    el.addEventListener("change", render);
  });
  render();
})();
</script>
</body>
</html>
"""


def merge_gdfs(paths: list[Path]) -> gpd.GeoDataFrame | None:
    frames = []
    for p in paths:
        if p.exists():
            frames.append(gpd.read_file(p))
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=frames[0].crs)


def _line_of(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "MultiLineString":
        return max(geom.geoms, key=lambda g: g.length)
    return geom


def _lot_axis(n: int) -> dict:
    from shapely.geometry import LineString

    folder = SRC / f"output_A31_{n}"
    cl = _line_of(gpd.read_file(folder / "central_line_profil.gpkg").geometry.iloc[0])
    xlsx = _fix_pr_end(pd.read_excel(_xlsx_in_lot(folder)))
    g = gpd.read_file(folder / "selected_ouvrages.gpkg")
    g = g.drop(columns=[c for c in ("PR_start", "abcisse_start", "PR_end", "abcisse_end", "length") if c in g.columns], errors="ignore")
    g = g.merge(
        xlsx[["nom", "PR_start", "abcisse_start", "PR_end", "abcisse_end", "length"]],
        on="nom",
        how="left",
    )
    g = g.sort_values(["PR_start", "abcisse_start"]).reset_index(drop=True)
    p0 = g.geometry.iloc[0].representative_point()
    p1 = g.geometry.iloc[-1].representative_point()
    if cl.project(p0) > cl.project(p1):
        cl = LineString(list(cl.coords)[::-1])
    g_d = g[g["nom"].astype(str).str.endswith("_D")]
    r0 = (g_d if len(g_d) else g).iloc[0]
    d_first = cl.project(r0.geometry.representative_point())
    d_pr_start = float(d_first) - float(r0["abcisse_start"])
    return {
        "n": n,
        "cl": cl,
        "d_pr_start": d_pr_start,
        "pr_anchor": int(r0["PR_start"]),
        "pr_min": int(xlsx["PR_start"].min()),
        "pr_max": int(xlsx["PR_end"].max()),
    }


def prepare_axis_and_mnt(dest: Path) -> Path:
    """Ligne centrale + PR projetes + liste des MNT 16-27, pour le recalcul talus."""
    from shapely.geometry import LineString, Point

    dest.mkdir(exist_ok=True)
    lots = [_lot_axis(n) for n in LOTS]
    merged_coords = []
    running = 0.0
    for i, lot in enumerate(lots):
        c = list(lot["cl"].coords)
        lot["offset"] = running
        if merged_coords:
            gap = Point(c[0]).distance(Point(merged_coords[-1]))
            print(f"  joint lot {lots[i-1]['n']}-{lot['n']} : {gap:.1f} m")
            if gap < 2:
                c = c[1:]
        merged_coords.extend(c)
        running += lot["cl"].length
        print(
            f"lot {lot['n']}: PR {lot['pr_min']}-{lot['pr_max']}  "
            f"cl {lot['cl'].length:.0f} m  PR{lot['pr_anchor']}+000 @ {lot['d_pr_start']:.1f} m"
        )
    axis_rows = []
    running = 0.0
    for lot in lots:
        axis_rows.append({
            "lot": lot["n"],
            "offset": running,
            "d_pr_start": lot["d_pr_start"],
            "pr_anchor": lot["pr_anchor"],
            "pr_min": lot["pr_min"],
            "pr_max": lot["pr_max"],
            "geometry": lot["cl"],
        })
        running += lot["cl"].length
    axis_gdf = gpd.GeoDataFrame(axis_rows, crs="EPSG:2154")
    axis_path = dest / "axis_lots.gpkg"
    axis_gdf.to_file(axis_path, driver="GPKG")
    print(f"Axes par lot : {axis_path}")

    merged = LineString(merged_coords)
    cl_gdf = gpd.GeoDataFrame(
        {"name": ["central_line"]},
        geometry=[merged],
        crs="EPSG:2154",
    )
    cl_path = dest / "central_line_profil.gpkg"
    cl_gdf.to_file(cl_path, driver="GPKG", layer="central_line")
    print(f"Ligne centrale fusionnee : {merged.length:.0f} m -> {cl_path}")

    pr_min = min(lot["pr_min"] for lot in lots)
    pr_max = max(lot["pr_max"] for lot in lots)
    rows = []
    for p in range(pr_min, pr_max + 1):
        cands = []
        for lot in lots:
            d_local = lot["d_pr_start"] + (p - lot["pr_anchor"]) * 1000
            if -50 <= d_local <= lot["cl"].length + 50:
                interior = 0 <= d_local <= lot["cl"].length
                cands.append((not interior, abs(d_local - lot["cl"].length / 2), lot, d_local))
        if not cands:
            continue
        cands.sort()
        lot, d_local = cands[0][2], cands[0][3]
        d_global = lot["offset"] + max(0.0, min(d_local, lot["cl"].length))
        geom = lot["cl"].interpolate(max(0.0, min(d_local, lot["cl"].length)))
        for cote in ("D", "G"):
            rows.append({
                "numero": p,
                "cote": cote,
                "distance_route": d_global,
                "geometry": geom,
            })
    pr_gdf = gpd.GeoDataFrame(rows, crs="EPSG:2154")
    pr_path = dest / "projected_PR.gpkg"
    pr_gdf.to_file(pr_path, driver="GPKG")
    print(f"PR projetes : {pr_min} a {pr_max} ({len(pr_gdf)//2} numeros) -> {pr_path}")

    mnt_paths = []
    for n in LOTS:
        tif = MNT_DIR / f"mnt{n}.tif"
        if not tif.exists():
            tif = MNT_DIR / f"mnt_{n}.tif"
        if not tif.exists():
            print(f"  MNT manquant : {tif}")
            continue
        mnt_paths.append(tif)
    list_path = dest / "mnt_sources.txt"
    list_path.write_text("\n".join(str(p) for p in mnt_paths) + "\n", encoding="utf-8")
    print(f"MNT : {len(mnt_paths)} tuiles (mnt16 a mnt27) -> {list_path}")
    return list_path


def main() -> None:
    dest = Path(__file__).resolve().parent / "output_A31_57"
    dest.mkdir(exist_ok=True)
    dest_d = SRC / "A31_57"
    dest_d.mkdir(exist_ok=True)

    tables = []
    rejected = []
    gpkgs = []
    ponts_s, ponts_l = [], []
    for n in LOTS:
        folder = SRC / f"output_A31_{n}"
        xlsx = _xlsx_in_lot(folder)
        df = pd.read_excel(xlsx)
        df = _fix_pr_end(df)
        tables.append(df)
        print(f"lot {n}: {len(df)} lignes  ({xlsx.name})")
        gpkg = folder / "selected_ouvrages.gpkg"
        if gpkg.exists():
            gpkgs.append(gpkg)
        rej = folder / "rejected_ouvrages.csv"
        if rej.exists():
            rejected.append(pd.read_csv(rej, sep=None, engine="python"))
        ponts_s.append(folder / "ponts_surface.gpkg")
        ponts_l.append(folder / "ponts_lineaire.gpkg")

    merged = pd.concat(tables, ignore_index=True)
    if "id" in merged.columns:
        merged = merged.drop(columns=["id"])
    merged = merged.sort_values(
        ["PR_start", "abcisse_start", "nom"], kind="mergesort"
    ).reset_index(drop=True)

    excel_path = dest / "selected_ouvrages.xlsx"
    save_styled_excel(merged, excel_path)
    print(f"Excel : {excel_path} ({len(merged)} lignes)")

    gdf = merge_gdfs(gpkgs)
    if gdf is not None:
        gdf = _fix_gpkg_pr(gdf)
        gpkg_path = dest / "selected_ouvrages.gpkg"
        gdf.to_file(gpkg_path, driver="GPKG")
        print(f"GPKG : {gpkg_path} ({len(gdf)} objets)")

    if rejected:
        rej = pd.concat(rejected, ignore_index=True)
        rej_path = dest / "rejected_ouvrages.csv"
        rej.to_csv(rej_path, sep=";", index=False)
        print(f"Rejected : {rej_path} ({len(rej)} lignes)")

    ps = merge_gdfs(ponts_s)
    if ps is not None:
        ps.to_file(dest / "ponts_surface.gpkg", driver="GPKG")
    pl = merge_gdfs(ponts_l)
    if pl is not None:
        pl.to_file(dest / "ponts_lineaire.gpkg", driver="GPKG")

    prepare_axis_and_mnt(dest)

    write_dashboard(
        merged,
        dest / "dashboard.html",
        "A31 (57) : inventaire fusionne, lots 16-27",
    )
    print(f"Dashboard : {dest / 'dashboard.html'}")

    for name in [
        "selected_ouvrages.xlsx",
        "selected_ouvrages.gpkg",
        "rejected_ouvrages.csv",
        "dashboard.html",
        "ponts_surface.gpkg",
        "ponts_lineaire.gpkg",
        "central_line_profil.gpkg",
        "projected_PR.gpkg",
        "mnt_sources.txt",
        "axis_lots.gpkg",
    ]:
        src = dest / name
        if src.exists():
            shutil.copy2(src, dest_d / name)
    print(f"Copie : {dest_d}")

    vc = merged["classification"].value_counts()
    print("Classes :")
    print(vc.to_string())
    print(
        f"PR {int(merged['PR_start'].min())} a {int(merged['PR_end'].max())}  "
        f"D={(merged['nom'].astype(str).str.endswith('_D')).sum()}  "
        f"G={(merged['nom'].astype(str).str.endswith('_G')).sum()}"
    )
    package_livrable(dest, dest_d)


def _copy_sheet(src_ws, dest_wb, title: str) -> None:
    from copy import copy as _copy

    ws = dest_wb.create_sheet(title)
    for row in src_ws.iter_rows():
        for cell in row:
            new = ws.cell(cell.row, cell.column, cell.value)
            if cell.has_style:
                new.font = _copy(cell.font)
                new.fill = _copy(cell.fill)
                new.border = _copy(cell.border)
                new.alignment = _copy(cell.alignment)
                new.number_format = cell.number_format
    for col, dim in src_ws.column_dimensions.items():
        ws.column_dimensions[col].width = dim.width
    ws.freeze_panes = src_ws.freeze_panes
    if src_ws.auto_filter and src_ws.auto_filter.ref:
        ws.auto_filter.ref = src_ws.auto_filter.ref


def write_op_dashboard(xlsx: Path, path: Path) -> None:
    df = pd.read_excel(xlsx)

    def _col(*needles):
        for c in df.columns:
            cl = str(c).lower()
            if all(n in cl for n in needles):
                return c
        raise KeyError(needles)

    c_nom = _col("tron")
    c_prs = _col("pr d")
    c_abs = _col("abs d")
    c_pre = _col("pr f")
    c_abe = _col("abs f")
    c_len = _col("longueur")
    c_cl = _col("classif")
    c_h = None
    for c in df.columns:
        if "h max" in str(c).lower():
            c_h = c
            break
    records = []
    for i, r in df.iterrows():
        hraw = r[c_h] if c_h else "-"
        htxt = None if pd.isna(hraw) or str(hraw) in ("-", "nan") else str(hraw)
        records.append({
            "n": int(i) + 1,
            "nom": str(r[c_nom]),
            "cote": "",
            "classification": str(r[c_cl]),
            "pr_s": int(r[c_prs]),
            "ab_s": int(str(r[c_abs]).replace(" ", "")),
            "pr_e": int(r[c_pre]),
            "ab_e": int(str(r[c_abe]).replace(" ", "")),
            "length": int(r[c_len]),
            "h": htxt,
        })
    payload = json.dumps(records, ensure_ascii=True)
    html = (
        DASHBOARD_HTML
        .replace("__TITLE__", "A31 (57) : inventaire operationnel")
        .replace("__DATA__", payload)
        .replace("a31_57_corrections", "a31_57_op_corrections")
        .replace("corrections_A31_57.csv", "corrections_A31_57_operationnel.csv")
        .replace(
            "Lots 16 a 27, A31 en 57. Changez la classe, puis Enregistrer.",
            "Une ligne = une section (D et G fusionnes). Changez la classe, puis Enregistrer.",
        )
        .replace(
            'Cote <select id="f-cote"><option value="">D et G</option><option value="D">D</option><option value="G">G</option></select>',
            "",
        )
    )
    path.write_text(html, encoding="utf-8")


def package_livrable(dest: Path, dest_d: Path) -> None:
    from openpyxl import load_workbook

    op = dest / "sections_apres_conditions" / "v1" / "inventaire_operationnel.xlsx"
    if not op.exists():
        print("Pas encore d'inventaire operationnel (lancer harmonize_ouvrages.py).")
        return

    write_op_dashboard(op, dest / "dashboard_operationnel.html")
    print(f"Dashboard operationnel : {dest / 'dashboard_operationnel.html'}")

    gpkg_dest = dest / "sections_apres_conditions" / "v1" / "inventaire_operationnel.gpkg"
    try:
        from harmonize_ouvrages import export_livrable_gpkg

        op_df = pd.read_excel(op)

        def _col(*needles):
            for c in op_df.columns:
                if all(n in str(c).lower() for n in needles):
                    return c
            raise KeyError(needles)

        internal = pd.DataFrame({
            "nom": op_df[_col("tron")],
            "PR_start": op_df[_col("pr d")],
            "abcisse_start": op_df[_col("abs d")],
            "PR_end": op_df[_col("pr f")],
            "abcisse_end": op_df[_col("abs f")],
            "length": op_df[_col("longueur")],
            "classification": op_df[_col("classif")],
        })
        export_livrable_gpkg(internal, dest, gpkg_dest)
    except Exception as exc:
        print(f"GPKG operationnel non genere : {exc}")

    combined = dest / "inventaire_A31_57.xlsx"
    wb_src = load_workbook(dest / "selected_ouvrages.xlsx")
    wb = load_workbook(op)
    if "Inventaire" not in wb.sheetnames:
        wb.active.title = "Inventaire"
    _copy_sheet(wb_src.active, wb, "Source_D_G")
    wb.save(combined)
    print(f"Excel unique : {combined}")

    names = [
        "inventaire_A31_57.xlsx",
        "dashboard_operationnel.html",
        "central_line_profil.gpkg",
        "projected_PR.gpkg",
        "mnt_sources.txt",
        "axis_lots.gpkg",
        "sections_apres_conditions/v1/inventaire_operationnel.xlsx",
        "sections_apres_conditions/v1/inventaire_operationnel.gpkg",
        "sections_apres_conditions/v1/inventaire_operationnel_ponts.xlsx",
        "sections_apres_conditions/v1/absorptions_ilots.csv",
        "sections_apres_conditions/v1/inventaire_A31_operationnel.docx",
    ]
    for name in names:
        src = dest / name
        if not src.exists():
            continue
        if name.startswith("sections_apres_conditions"):
            target = dest_d / "operationnel" / Path(name).name
            target.parent.mkdir(exist_ok=True)
        else:
            target = dest_d / Path(name).name
        shutil.copy2(src, target)
    print(f"Livrable recopie dans {dest_d}")
    publish_verification(dest, dest_d)


def publish_verification(dest: Path, dest_d: Path) -> None:
    src = dest / "sections_apres_conditions" / "v1" / "verification" / "index.html"
    if not src.exists():
        print("Pas de verification/index.html (lancer verify_livrable.py --all).")
        return
    src_dir = src.parent.resolve()

    link = dest / "verification"
    if link.exists() and link.resolve() != src_dir:
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    if not link.exists():
        try:
            os.symlink(src_dir, link, target_is_directory=True)
        except OSError:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(src_dir)],
                check=False,
            )
            if not link.exists():
                shutil.copytree(src_dir, link)

    target = dest_d / "verification"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src_dir, target)

    html = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=verification/index.html">
<title>A31 (57) dashboard</title>
</head>
<body>
<p>Dashboard avec profils tous les 10 m :
<a href="verification/index.html">verification/index.html</a>.
Cliquez un troncon pour ouvrir les profils.</p>
</body>
</html>
"""
    (dest / "dashboard_operationnel.html").write_text(html, encoding="utf-8")
    (dest_d / "dashboard_operationnel.html").write_text(html, encoding="utf-8")
    src_xlsx = dest / "selected_ouvrages.xlsx"
    if src_xlsx.exists():
        write_dashboard(
            pd.read_excel(src_xlsx),
            dest / "dashboard.html",
            "A31 (57) : inventaire fusionne, lots 16-27",
        )
        shutil.copy2(dest / "dashboard.html", dest_d / "dashboard.html")
    print(f"Dashboard profils : {target / 'index.html'}")


if __name__ == "__main__":
    import sys

    dest = Path(__file__).resolve().parent / "output_A31_57"
    dest_d = SRC / "A31_57"
    if "--refs-only" in sys.argv:
        dest.mkdir(exist_ok=True)
        dest_d.mkdir(exist_ok=True)
        prepare_axis_and_mnt(dest)
        for name in ("central_line_profil.gpkg", "projected_PR.gpkg", "mnt_sources.txt", "axis_lots.gpkg"):
            src = dest / name
            if src.exists():
                shutil.copy2(src, dest_d / name)
    elif "--package-only" in sys.argv:
        package_livrable(dest, dest_d)
    else:
        main()

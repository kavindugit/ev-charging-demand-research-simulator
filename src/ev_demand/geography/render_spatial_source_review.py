"""Render saved land-use and tourism geometries for the five-district review."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as PlotPath
from matplotlib.patches import PathPatch
import shapely
from shapely.geometry import shape
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.geography.build_district_feature_review import (
    digest, keyed, read_csv, read_json, require, write_csv,
)
from ev_demand.geography.extract_district_landuse import area_km2

PAIR_FIELDS = (
    "review_id", "osm_key_a", "osm_key_b",
    "name_a", "name_b", "kind_a", "kind_b",
    "geometry_status", "geometry_a", "geometry_b",
    "equal_geometry", "a_covers_b", "b_covers_a",
    "area_a_m2", "area_b_m2",
    "a_outside_b_m2", "b_outside_a_m2",
    "a_relation_roles_for_b_json",
    "b_relation_roles_for_a_json",
    "review_state",
)


def pair_facts(issue, audits, geometries):
    keys = json.loads(issue["osm_keys_json"])
    require(
        len(keys) == 2 and keys[0] != keys[1],
        "Expected a two-object tourism review pair.",
    )

    a, b = keys
    require(
        a in audits and b in audits,
        "Tourism pair/source link mismatch.",
    )

    ra, rb = audits[a], audits[b]

    row = dict(
        review_id=issue["review_id"],
        osm_key_a=a,
        osm_key_b=b,
        name_a=ra["name"],
        name_b=rb["name"],
        kind_a=ra["kind"],
        kind_b=rb["kind"],
        geometry_status=(
            "available"
            if a in geometries and b in geometries
            else "missing_source_geometry"
        ),
        **{k: None for k in PAIR_FIELDS[8:17]},
        a_relation_roles_for_b_json=json.dumps([
            m["role"]
            for m in json.loads(ra["relation_members_json"])
            if m["osm_key"] == b
        ]),
        b_relation_roles_for_a_json=json.dumps([
            m["role"]
            for m in json.loads(rb["relation_members_json"])
            if m["osm_key"] == a
        ]),
        review_state="pending",
    )

    if row["geometry_status"] == "available":
        ga, gb = geometries[a], geometries[b]
        row.update(
            geometry_a=ga.geom_type,
            geometry_b=gb.geom_type,
            equal_geometry=ga.equals(gb),
            a_covers_b=ga.covers(gb),
            b_covers_a=gb.covers(ga),
            area_a_m2=area_km2(ga) * 1e6,
            area_b_m2=area_km2(gb) * 1e6,
            a_outside_b_m2=area_km2(ga.difference(gb)) * 1e6,
            b_outside_a_m2=area_km2(gb.difference(ga)) * 1e6,
        )

    return row


def paint(ax, geometry, color, alpha=0.35, zorder=1):
    if geometry.is_empty:
        return

    if geometry.geom_type == "Point":
        ax.plot(
            geometry.x, geometry.y, "o",
            color=color, markersize=7, zorder=zorder,
        )

    elif geometry.geom_type == "Polygon":
        vertices, codes = [], []
        polygon = orient(geometry, sign=1)

        for ring in [polygon.exterior, *polygon.interiors]:
            points = list(ring.coords)
            vertices.extend(points)
            codes.extend(
                [PlotPath.MOVETO]
                + [PlotPath.LINETO] * (len(points) - 2)
                + [PlotPath.CLOSEPOLY]
            )

        ax.add_patch(
            PathPatch(
                PlotPath(vertices, codes),
                facecolor=color,
                edgecolor=color,
                alpha=alpha,
                lw=0.7,
                zorder=zorder,
            )
        )

    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            paint(ax, part, color, alpha, zorder)


def frame(ax, geometry):
    x0, y0, x1, y1 = geometry.bounds
    px = max(x1 - x0, 0.0001) * 0.15
    py = max(y1 - y0, 0.0001) * 0.15

    ax.set_xlim(x0 - px, x1 + px)
    ax.set_ylim(y0 - py, y1 + py)
    ax.set_aspect(
        1 / math.cos(math.radians((y0 + y1) / 2))
    )
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.tick_params(labelsize=7)
    ax.set_xlabel("Longitude", fontsize=8)
    ax.set_ylabel("Latitude", fontsize=8)
    ax.locator_params(axis="both", nbins=4)


def save_figure(fig, output, stem, title, caption):
    fig.suptitle(title, fontsize=12)
    fig.text(
        0.5, 0.035, caption,
        ha="center", fontsize=8,
    )
    fig.text(
        0.5, 0.012,
        "Saved-source view; no basemap or independent ground truth. "
        "© OpenStreetMap contributors.",
        ha="center", fontsize=7,
    )
    fig.tight_layout(rect=[0, 0.10, 1, 0.91])

    for extension in ("png", "svg"):
        fig.savefig(
            output / f"{stem}.{extension}",
            dpi=150,
        )

    plt.close(fig)


def render(packet_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}
    base = root / "data/processed/reference"

    def watch(value, expected=None):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )
        actual = watched.get(path) or digest(path)
        require(
            expected is None or actual == expected,
            "Input hash mismatch: " + str(value),
        )
        watched[path] = actual
        return path

    def pinned(value):
        path = (root / value).resolve()
        require(
            path in watched,
            "File is not pinned by the source packet: " + str(value),
        )
        return path

    watch("src/ev_demand/geography/render_spatial_source_review.py")
    watch("src/ev_demand/geography/build_district_feature_review.py")
    watch("src/ev_demand/geography/extract_district_landuse.py")

    folder = (root / packet_folder).resolve()
    require(
        folder.is_relative_to(base),
        "Use the processed public-reference review packet.",
    )

    packet = read_json(
        watch(folder / "spatial_source_review_report.json")
    )

    require(
        packet.get("schema_version") == "spatial_source_review_v1"
        and packet.get("status") == "human_review_packet_prepared"
        and packet.get("data_origin") == "PUBLIC_REFERENCE"
        and packet.get("human_review_completed") is False
        and packet.get("checks")
        and all(v is True for v in packet["checks"].values()),
        "Unexpected source-review packet.",
    )

    expected = {
        "district_selection.csv",
        "selected_district_features.csv",
        "selected_district_proxy_weights.csv",
        "source_example_coverage.csv",
        "spatial_worklist_with_links.csv",
        "manual_review_template.csv",
        "source_examples.csv",
        "review_guide.md",
    }

    require(
        packet.get("input_sha256")
        and set(packet.get("output_sha256", {})) == expected,
        "Incomplete source-packet manifests.",
    )

    for name, h in packet["input_sha256"].items():
        watch(name, h)

    for name, h in packet["output_sha256"].items():
        require(
            Path(name).name == name,
            "Unexpected export name.",
        )
        watch(folder / name, h)

    selection = read_csv(folder / "district_selection.csv")
    selected = keyed(
        selection, "district_id", "selected districts"
    )
    features = keyed(
        read_csv(folder / "selected_district_features.csv"),
        "district_id",
        "selected features",
    )

    require(
        len(selected) == 5 and set(features) == set(selected),
        "Incomplete five-district grid.",
    )

    review_folder = root / packet["source_review_folder"]
    review = read_json(
        pinned(review_folder / "district_feature_review_report.json")
    )

    land_folder = root / review["source_folders"]["landuse"]
    tourism_folder = root / review["source_folders"]["tourism"]

    land_path = pinned(
        land_folder / "district_landuse_unions.geojsonl.gz"
    )
    tourism_path = pinned(
        tourism_folder / "tourism_source_geometries.geojsonl.gz"
    )
    audits = keyed(
        read_csv(
            pinned(tourism_folder / "tourism_source_audit.csv")
        ),
        "osm_key",
        "tourism audit",
    )

    boundary_paths = [
        p for p in watched
        if p.name == "district_boundaries.geojson"
    ]
    require(
        len(boundary_paths) == 1,
        "Missing or ambiguous district boundary snapshot.",
    )

    boundary_features = read_json(
        boundary_paths[0]
    )["features"]

    boundaries = {
        f["properties"]["district_id"]: shape(f["geometry"])
        for f in boundary_features
    }
    require(
        set(selected) <= set(boundaries),
        "Selected district boundary missing.",
    )

    unions = {}

    with gzip.open(land_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            props = item["properties"]
            key = props["district_id"], props["landuse_group"]

            require(
                key not in unions
                and props["measurement_complete"] is True,
                "Duplicate or incomplete land-use union.",
            )

            g = shape(item["geometry"])
            require(
                g.geom_type in {"Polygon", "MultiPolygon"}
                and g.is_valid,
                "Invalid saved land-use area.",
            )
            unions[key] = g

    geometries = {}

    with gzip.open(tourism_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            key = item["properties"]["osm_key"]

            require(
                key in audits and key not in geometries,
                "Tourism geometry/source identity mismatch.",
            )

            g = shape(item["geometry"])
            require(
                not g.is_empty
                and g.is_valid
                and g.geom_type in {"Point", "Polygon", "MultiPolygon"},
                "Invalid saved tourism geometry.",
            )
            geometries[key] = g

    worklist = read_csv(
        folder / "spatial_worklist_with_links.csv"
    )
    require(
        len(worklist) == packet["original_worklist_items"],
        "Worklist count changed.",
    )

    related = [
        r for r in worklist
        if set(json.loads(r["related_district_ids_json"]))
        & set(selected)
    ]
    require(
        len(related) == packet["selected_district_worklist_items"],
        "Selected worklist count changed.",
    )

    pair_rows = [
        pair_facts(r, audits, geometries)
        for r in related
        if r["layer"] == "tourism"
        and r["review_topic"] == "potential_duplicate_representation"
    ]

    require(
        all(digest(p) == h for p, h in watched.items()),
        "Source changed before rendering.",
    )

    now = datetime.now(timezone.utc)
    output = base / (
        "spatial_geometry_views_"
        + now.strftime("%Y%m%dT%H%M%S_%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)

    overlap_rows = []
    views = []

    for d, district in selected.items():
        c = unions.get(
            (d, "commercial_retail"),
            shapely.GeometryCollection(),
        )
        r = unions.get(
            (d, "residential"),
            shapely.GeometryCollection(),
        )
        overlap = c.intersection(r)

        for column, g in (
            ("mapped_commercial_retail_km2", c),
            ("mapped_residential_km2", r),
            ("commercial_residential_overlap_km2", overlap),
        ):
            require(
                math.isclose(
                    area_km2(g),
                    float(features[d][column]),
                    rel_tol=1e-9,
                    abs_tol=1e-7,
                ),
                "Saved union/feature area mismatch: "
                + d + "/" + column,
            )

        overlap_rows.append(
            dict(
                district_id=d,
                district_name=district["district_name"],
                commercial_km2=area_km2(c),
                residential_km2=area_km2(r),
                overlap_km2=area_km2(overlap),
                reported_overlap_km2=float(
                    features[d]["commercial_residential_overlap_km2"]
                ),
                areas_reproduced=True,
                review_state="pending",
            )
        )

        parts = []

        def polygons(g):
            if g.geom_type == "Polygon" and not g.is_empty:
                parts.append(g)
            elif hasattr(g, "geoms"):
                for part in g.geoms:
                    polygons(part)

        polygons(overlap)
        parts.sort(key=area_km2, reverse=True)

        fig, axes = plt.subplots(
            1, 4, figsize=(13, 4)
        )

        paint(axes[0], r, "#48967c", 0.3)
        paint(axes[0], c, "#247aa7", 0.5, 2)
        frame(axes[0], boundaries[d])
        axes[0].set_title(
            "Mapped classes in district",
            fontsize=9,
        )

        for i, ax in enumerate(axes[1:]):
            if i >= len(parts):
                ax.text(
                    0.5, 0.5,
                    "No additional positive-area\n"
                    "overlap component",
                    ha="center", va="center",
                    fontsize=9,
                )
                ax.set_axis_off()
                continue

            paint(ax, r, "#48967c", 0.25)
            paint(ax, c, "#247aa7", 0.4, 2)
            paint(ax, parts[i], "#b12f5d", 0.7, 3)
            frame(ax, parts[i])
            ax.set_title(
                f"Overlap component {i + 1}\n"
                f"{area_km2(parts[i]) * 1e6:.1f} m²",
                fontsize=9,
            )

        stem = d + "_landuse"

        save_figure(
            fig, output, stem,
            district["district_name"] + " — saved land-use geometry",
            "Green: residential; blue: commercial/retail; red: overlap. "
            "Detail panels show up to three largest components.",
        )

        views.append(
            dict(
                view_id=stem,
                scope="district_landuse",
                district_ids_json=json.dumps([d]),
                review_id="",
                png=stem + ".png",
                svg=stem + ".svg",
                review_state="pending",
            )
        )

    for row in pair_rows:
        if row["geometry_status"] != "available":
            continue

        a, b = row["osm_key_a"], row["osm_key_b"]
        ga, gb = geometries[a], geometries[b]

        fig, ax = plt.subplots(figsize=(6, 5))
        paint(ax, gb, "#e3972c", 0.4)
        paint(ax, ga, "#2178a7", 0.65, 2)
        frame(ax, ga.union(gb))

        ax.set_title(
            f"Blue: {a}\nOrange: {b}",
            fontsize=10,
        )

        stem = row["review_id"]

        save_figure(
            fig, output, stem,
            stem + " — tourism source pair",
            "A geometry relationship alone does not confirm "
            "that two objects are one venue.",
        )

        issue = next(
            r for r in related
            if r["review_id"] == row["review_id"]
        )

        views.append(
            dict(
                view_id=stem,
                scope="tourism_pair",
                district_ids_json=issue["related_district_ids_json"],
                review_id=stem,
                png=stem + ".png",
                svg=stem + ".svg",
                review_state="pending",
            )
        )

    write_csv(
        output / "landuse_overlap_checks.csv",
        overlap_rows,
    )
    write_csv(
        output / "tourism_pair_geometry_checks.csv",
        pair_rows,
        PAIR_FIELDS,
    )
    write_csv(output / "view_index.csv", views)
    write_csv(
        output / "selected_worklist_unchanged.csv",
        related,
    )

    report = dict(
        schema_version="spatial_geometry_views_v1",
        status="saved_geometry_views_prepared",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        selected_districts=5,
        landuse_area_checks_reproduced=len(overlap_rows),
        selected_worklist_items=len(related),
        tourism_pair_checks=len(pair_rows),
        pairs_with_saved_geometry=sum(
            r["geometry_status"] == "available"
            for r in pair_rows
        ),
        figure_pairs=len(views),
        source_packet_folder=folder.relative_to(root).as_posix(),
        osm_data_timestamp=packet["osm_data_timestamp"],
        boundary_reference_year=packet["boundary_reference_year"],
        attribution=packet["attribution"],
        source_licence=packet["source_licence"],
        source_licence_url=packet["source_licence_url"],
        boundary_attribution=packet["boundary_attribution"],
        boundary_licence=packet["boundary_licence"],
        boundary_licence_url=packet["boundary_licence_url"],
        human_review_completed=False,
        venue_identity_decisions_made=0,
        source_objects_changed=0,
        production_weights_activated=False,
        district_demand_ready=False,
        new_simulations=0,
        interpretation=[
            "These figures display saved source geometries and reproduce "
            "area calculations; they do not validate mapping completeness "
            "or real-world land use.",
            "Tourism containment, equality and relation membership are "
            "observations, not venue-identity decisions. Raw counts are unchanged.",
            "Point area is zero by geometry type; it does not describe hotel "
            "size. Missing geometry metrics remain blank.",
            "Land-use detail panels show at most three largest overlap "
            "components, not all source objects.",
            "Road and other worklist issues remain in the unchanged selected "
            "worklist; individual road geometry is not available in the "
            "saved export used here.",
            "All human review tasks and source issue statuses remain pending. "
            "Source views are not new demand simulations.",
        ],
        software={
            p: version(p)
            for p in ("shapely", "pyproj", "matplotlib")
        },
        checks=dict(
            upstream_hashes_verified=True,
            selected_district_grid_verified=True,
            saved_landuse_areas_reproduced=True,
            tourism_source_links_verified=True,
            source_geometry_holes_preserved=True,
            selected_worklist_retained=True,
            input_files_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): h
            for p, h in watched.items()
        },
        output_sha256={
            p.name: digest(p)
            for p in sorted(output.iterdir())
        },
    )

    require(
        all(digest(p) == h for p, h in watched.items()),
        "Source changed during rendering.",
    )

    (output / "spatial_geometry_views_report.json").write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True)
    args = parser.parse_args()

    output, report = render(args.packet)

    print("Saved spatial source-geometry views complete.")

    for key in (
        "selected_districts",
        "landuse_area_checks_reproduced",
        "selected_worklist_items",
        "tourism_pair_checks",
        "pairs_with_saved_geometry",
        "figure_pairs",
    ):
        print(f"{key}: {report[key]}")

    print("Source hashes, saved areas and source-object links checked.")
    print("human_review_completed: False")
    print("production_weights_activated: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Saved-source viewing only; no venue merging, "
        "new simulations or empirical validation."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        raise SystemExit(
            f"Spatial geometry rendering failed: {exc}"
        )
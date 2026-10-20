"""Build a self-contained offline dashboard from a verified PP1 demo bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
GROUPS = ("T1", "T2", "T3", "T4", "TOTAL")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_constant(value):
    raise ValueError("Nonfinite JSON value: " + value)


def read_json(path):
    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        parse_constant=reject_constant,
    )


def build_dashboard(bundle, root=ROOT):
    root = Path(root).resolve()
    folder = (root / bundle).resolve()

    require(
        folder.is_relative_to(root / "outputs/synthetic")
        and folder.is_dir(),
        "Select a demo bundle folder under outputs/synthetic.",
    )

    data_path = folder / "demo_bundle.json"
    report_path = folder / "demo_bundle_report.json"
    before = {
        path: digest(path)
        for path in (data_path, report_path)
    }

    data = read_json(data_path)
    report = read_json(report_path)

    require(
        data["schema_version"] == "pp1_synthetic_demo_v1"
        and report["schema_version"] == "pp1_demo_bundle_report_v1"
        and report["status"] == "complete",
        "Expected a completed PP1 demo bundle.",
    )

    require(
        all(
            item["data_origin"] == "SYNTHETIC"
            for item in (data, report)
        ),
        "This dashboard supports synthetic bundles only.",
    )

    require(
        report["checks"]
        and all(v is True for v in report["checks"].values()),
        "Bundle packaging checks did not pass.",
    )

    require(
        before[data_path]
        == report["output_sha256"]["demo_bundle.json"],
        "Demo bundle changed after packaging. Rebuild the bundle.",
    )

    require(
        data["settings"] == report["settings"],
        "Bundle settings disagree with its report.",
    )

    for key in (
        "empirical_validation",
        "national_forecast_ready",
        "ml_model_trained",
    ):
        require(
            all(item[key] is False for item in (data, report)),
            "Unexpected claim: " + key,
        )

    settings = data["settings"]

    require(
        len(data["owner_profiles"]) == settings["owners_simulated"]
        and len(data["readiness"]) == settings["owners_retained"],
        "Owner counts disagree.",
    )

    grid = {
        (row["context"], row["hour_of_day"])
        for row in data["profiles"]
    }

    require(
        len(data["profiles"]) == 120
        and grid == {
            (context, hour)
            for context in GROUPS
            for hour in range(24)
        },
        "Expected a complete 24-hour profile for each context.",
    )

    for row in data["profiles"]:
        require(
            all(
                type(row[key]) in (int, float)
                and math.isfinite(row[key])
                for key in (
                    "reduced_mean_power_kw",
                    "rated_mean_power_kw",
                    "reduced_p05_kw",
                    "reduced_p95_kw",
                    "mean_difference_kw",
                    "difference_p05_kw",
                    "difference_p95_kw",
                )
            ),
            "Invalid profile value.",
        )

    for case in ("reduced", "rated"):
        trace = data["example_replicate"][case]

        require(
            1 <= trace["replicate"] <= settings["replicates_per_case"]
            and trace["warmup_days"] == settings["warmup_days"],
            "Unexpected trace settings.",
        )

    payload = dict(
        data,
        bundle_verification=dict(
            checks=report["checks"],
            bundle_sha256=before[data_path],
            covered_files=len(report["input_and_source_sha256"]),
        ),
    )

    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
    )
    encoded = (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )

    page = PAGE.replace("__PAYLOAD__", encoded)

    require(
        all(digest(path) == old for path, old in before.items()),
        "Bundle changed while rendering.",
    )

    output = folder / "pp1_dashboard.html"
    output.write_text(page, encoding="utf-8")

    saved = dict(
        schema_version="pp1_offline_dashboard_v1",
        data_origin="SYNTHETIC",
        status="complete",
        created_utc=datetime.now(timezone.utc).isoformat(),
        bundle_folder=folder.relative_to(root).as_posix(),
        settings=settings,
        input_sha256={
            path.name: old
            for path, old in before.items()
        },
        source_sha256={
            "src/ev_demand/demo/build_pp1_dashboard.py":
                digest(Path(__file__))
        },
        output_sha256={
            output.name: digest(output)
        },
        checks=dict(
            bundle_hash_matches=True,
            packaging_checks_passed=True,
            source_bundle_unchanged=True,
        ),
        empirical_validation=False,
        national_forecast_ready=False,
        ml_model_trained=False,
    )

    require(
        all(digest(path) == old for path, old in before.items()),
        "Bundle changed while saving.",
    )

    (folder / "dashboard_report.json").write_text(
        json.dumps(saved, indent=2) + "\n",
        encoding="utf-8",
    )

    return output, saved


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EV Charging Demand | PP1</title>
<style>
:root{font-family:Segoe UI,Arial,sans-serif;color:#172b3a;background:#f5f7fa;font-size:15px;--blue:#235de1;--orange:#d95d16;--muted:#526879;--line:#dce4ec}
*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button,select{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #7ba8ff;outline-offset:3px}
.layout{display:grid;grid-template-columns:225px minmax(0,1fr);min-height:100vh}aside{background:#102e40;color:#fff;padding:30px 18px;display:flex;flex-direction:column;gap:25px}aside h2{font-size:20px;margin:0 10px}aside p{color:#c0d3e2;font-size:13px;line-height:1.6;margin:8px 10px}
.badge{display:inline-block;border:1px solid #d2b65b;color:#ffe39d;background:#263f45;padding:6px 9px;border-radius:5px;font-size:11px;letter-spacing:1px;font-weight:700;margin:0 10px}nav{display:grid;gap:6px}nav button{border:0;background:transparent;color:#d3e1ec;padding:12px;text-align:left;border-radius:7px}nav button[aria-pressed=true]{background:#245575;color:white;font-weight:600}nav button:hover{background:#1a455f}.side-note{margin-top:auto;border-top:1px solid #315063;padding-top:16px}
main{padding:28px 34px 45px;max-width:1550px;width:100%}header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px}.eyebrow{color:var(--blue);font-weight:700;font-size:11px;letter-spacing:1.5px;text-transform:uppercase}h1{font-size:29px;letter-spacing:-.8px;margin:5px 0 8px}h2{font-size:19px;margin:0 0 10px}h3{font-size:15px;margin:0 0 12px}p{line-height:1.65;margin:8px 0}.muted,.note{color:var(--muted)}.note{font-size:12px}.snapshot{border:1px solid #c6d7e5;background:#edf5fb;color:#274b65;border-radius:6px;padding:8px 12px;white-space:nowrap;font-size:12px}
.ribbon{background:#fff8e7;border:1px solid #ead89e;border-radius:8px;padding:10px 14px;font-size:12px;color:#69551e;margin-bottom:22px}.view[hidden]{display:none}.controls{display:flex;flex-wrap:wrap;align-items:end;gap:15px;margin:16px 0}label{font-size:12px;color:var(--muted);display:grid;gap:6px}select,input[type=search]{min-width:165px;max-width:100%;border:1px solid #bbcbd9;background:white;border-radius:6px;padding:9px 11px;color:#172b3a}label.check{display:flex;align-items:center;gap:7px;padding-bottom:11px}.button{border:1px solid #bdcddd;border-radius:6px;background:white;padding:9px 12px;color:#1c4a73;font-size:12px}.button:hover{background:#edf4ff}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin:18px 0}.card,.panel{background:white;border:1px solid var(--line);border-radius:9px}.card{padding:17px 16px}.card small{color:var(--muted);font-size:12px;display:block}.value{font-weight:650;font-size:27px;letter-spacing:-.6px;margin:9px 0}.card .note{font-size:11px;min-height:16px}.panel{padding:21px;margin:17px 0}.split{display:grid;grid-template-columns:1fr 1fr;gap:17px}.split .panel{margin:0}.scroll{overflow:auto;max-width:100%}.chart svg{width:100%;min-width:580px;height:auto;display:block}.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:10px 0}.key{display:inline-block;width:13px;height:4px;margin-right:6px;vertical-align:middle}.chart-note{font-size:12px;color:var(--muted);margin-top:9px}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:10px 12px;border-bottom:1px solid #e6edf3;text-align:left;white-space:nowrap}th{font-weight:600;color:#415a6b;background:#f5f8fb}tbody tr:hover{background:#f8fbff}tbody tr:last-child td{border-bottom:0}#readinessTable,#sessionTable{max-height:420px}#readinessTable th,#sessionTable th{position:sticky;top:0}details{margin-top:16px}summary{cursor:pointer;color:#24537b;font-size:12px;padding:4px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7fa;padding:14px;border-radius:6px;font-size:11px;max-height:350px;overflow:auto}.bars{display:grid;gap:15px;margin:17px 0}.bar-label{display:flex;justify-content:space-between;gap:15px;font-size:12px;margin-bottom:6px}.track{height:8px;background:#edf2f7;border-radius:8px;overflow:hidden}.fill{height:100%;background:var(--blue);border-radius:8px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:15px 0}.step{border:1px solid var(--line);background:#f8fbfe;border-radius:6px;padding:12px;font-size:12px}.step strong{display:block;margin-bottom:5px}.sources{overflow-wrap:anywhere;font-size:12px;line-height:1.8}#fatal{background:#ffe9e7;color:#8f2017;padding:20px}footer{color:var(--muted);font-size:11px;border-top:1px solid var(--line);margin-top:25px;padding-top:14px}
@media(max-width:1050px){main{padding:24px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.split{grid-template-columns:1fr}header{align-items:flex-start}.snapshot{display:none}}
@media(max-width:760px){.layout{display:block}aside{padding:18px;gap:15px}aside h2{font-size:17px}aside .side-note,aside p{display:none}.badge{margin:0 0 10px}nav{display:flex;flex-wrap:wrap;gap:4px}nav button{padding:9px;font-size:12px}main{padding:21px 16px}.cards{gap:9px}.card{padding:13px}.value{font-size:23px}h1{font-size:25px}.steps{grid-template-columns:1fr 1fr}.panel{padding:15px}select{min-width:130px}}
</style>
</head>
<body>
<div id="fatal" hidden></div>
<div class="layout">
<aside>
<div>
<span class="badge">SYNTHETIC DEVELOPMENT</span>
<h2>EV demand lab</h2>
<p>Component 2<br>Progress Presentation 1</p>
</div>
<nav aria-label="Dashboard views">
<button data-tab="demand" aria-pressed="true">01 &nbsp; Demand profiles</button>
<button data-tab="power" aria-pressed="false">02 &nbsp; Power comparison</button>
<button data-tab="precision" aria-pressed="false">03 &nbsp; Mean precision</button>
<button data-tab="trace" aria-pressed="false">04 &nbsp; Owner / session trace</button>
<button data-tab="readiness" aria-pressed="false">05 &nbsp; Readiness &amp; method</button>
</nav>
<div class="side-note">
<p>Saved, audited reference results.<br>Controls explore this snapshot.<br>No new simulation runs here.</p>
</div>
</aside>

<main>
<header>
<div>
<div class="eyebrow">Sri Lanka EV research · Component 2</div>
<h1>Charging demand explorer</h1>
<div id="settings" class="muted"></div>
</div>
<div class="snapshot">Offline reference snapshot</div>
</header>

<div class="ribbon">
Synthetic owners and results · Development demonstration · Real-data calibration and independent validation pending
</div>

<section id="demand" class="view">
<h2>Explore the daily demand shape</h2>
<p class="muted">Hourly mean power for the current eligible owner pool. Change the charging context or saved power case.</p>
<div class="controls">
<label>Charging context<select id="profileContext"></select></label>
<label>Power case<select id="profileCase"></select></label>
<label class="check"><input id="showBand" type="checkbox" checked>Show reduced-case P05–P95 band</label>
<button id="exportProfile" class="button">Export profile CSV</button>
</div>
<div id="demandCards" class="cards"></div>
<div class="panel">
<h3 id="profileTitle"></h3>
<div id="profileChart"></div>
<p class="chart-note">Each curve averages the study-period mean profiles across replicates. The band, available for the reduced case, describes variation between those replicate means; it is not a confidence band or a range of individual daily peaks.</p>
<details>
<summary>Inspect hourly values</summary>
<div id="hourlyTable" class="scroll"></div>
</details>
</div>
<div class="panel">
<h3>Daily energy by charging context</h3>
<div id="contextBars" class="bars"></div>
<p class="note">T1–T4 are charging contexts. These results have not been scaled to districts or the national EV fleet.</p>
</div>
</section>

<section id="power" class="view" hidden>
<h2>Compare public charging power assumptions</h2>
<p class="muted">Both cases use verified matching candidate draws. Power changes can alter charging duration, owner-overlap decisions and energy crossing the study boundaries.</p>
<div class="controls">
<label>Charging context<select id="powerContext"></select></label>
</div>
<div id="powerCards" class="cards"></div>
<div class="panel">
<h3>Rated minus reduced: hourly mean power</h3>
<div id="differenceChart"></div>
<p class="chart-note">Positive values mean more power in the rated case at that hour. The band is P05–P95 of paired replicate mean-profile differences. Rated power is not an upper bound on every aggregate hourly value.</p>
</div>
<div class="panel">
<h3>Paired comparison by context</h3>
<div id="comparisonTable" class="scroll"></div>
<p class="note">Rejections are proposed starts blocked by the owner-overlap rule; they are not measured charger failures. Percentages below pool study starts across all replicates.</p>
</div>
</section>

<section id="precision" class="view" hidden>
<h2>Inspect Monte Carlo mean precision</h2>
<p class="muted">MCSE estimates simulation sampling uncertainty under the fixed synthetic inputs. It does not measure real-world forecasting accuracy.</p>
<div class="controls">
<label>Charging context<select id="precisionContext"></select></label>
<label>Quantity<select id="precisionCase"></select></label>
</div>
<div id="precisionCards" class="cards"></div>
<div class="panel">
<h3>Daily-energy MCSE as replicates are added</h3>
<div id="precisionChart"></div>
<p class="chart-note">MCSE = sample standard deviation / √replicates. Checkpoints reuse nested prefixes of the same batch. This is a precision diagnostic; no formal convergence threshold was set.</p>
</div>
<div class="panel">
<h3>Checkpoint estimates</h3>
<div id="precisionTable" class="scroll"></div>
</div>
</section>

<section id="trace" class="view" hidden>
<h2>Trace an owner through one replicate</h2>
<p id="traceCaption" class="muted"></p>
<div class="controls">
<label>Synthetic owner<select id="traceOwner"></select></label>
<label>Power case<select id="traceCase"></select></label>
<label class="check"><input id="includeWarmup" type="checkbox">Include warm-up starts</label>
</div>
<div id="traceCards" class="cards"></div>
<div class="panel">
<h3>Charging intervals and rejected proposed starts</h3>
<div id="timeline"></div>
<p class="chart-note">Bars are accepted charging intervals, clipped to the displayed horizon. Crosses mark rejected proposed starts. Day 0 is the first study day. Carry-in charging can contribute study energy even when its start occurred during warm-up.</p>
</div>
<div class="panel">
<h3>Owner inputs and sampled candidate rates</h3>
<p id="ownerBounds" class="note"></p>
<div id="ownerContexts" class="scroll"></div>
<details>
<summary>Inspect the saved owner parameter record (reduced case)</summary>
<pre id="ownerJson"></pre>
</details>
</div>
<div class="panel">
<h3 id="sessionTitle"></h3>
<div id="sessionTable" class="scroll"></div>
<p class="note">Drawn energy is the proposal's full-session energy. Rejected proposals receive no allocated energy. Study-energy totals include accepted carry-in charging.</p>
</div>
</section>

<section id="readiness" class="view" hidden>
<h2>Explain which inputs can enter the simulator</h2>
<p class="muted">Eligibility requires resolved bounds and consistent usable parameters. The ready pool is a completeness-selected subset; its composition cannot be extrapolated to Sri Lankan EV owners.</p>
<div id="readinessCards" class="cards"></div>
<div class="split">
<div class="panel">
<h3>First recorded deferral reasons</h3>
<div id="reasonBars" class="bars"></div>
<p class="note">One first reason per deferred owner is shown here. The separate readiness review detects additional issues, so these counts are not a complete issue inventory.</p>
</div>
<div class="panel">
<h3>Implemented data-to-demand path</h3>
<div class="steps">
<div class="step"><strong>1. Prepare</strong>Survey tables and quality checks</div>
<div class="step"><strong>2. Encode</strong>Answers into bounded features</div>
<div class="step"><strong>3. Review</strong>Owner readiness and parameters</div>
<div class="step"><strong>4. Simulate</strong>Candidate starts, power and duration</div>
<div class="step"><strong>5. Schedule</strong>Chronological owner-overlap checks</div>
<div class="step"><strong>6. Audit</strong>Hourly energy and replicate summaries</div>
</div>
<p class="note">Current energy-duration rule: active hours = drawn kWh / effective average kW. The engine does not yet track driving trips, battery SOC trajectories or station queues.</p>
<p class="note">Next data milestone: collect real responses, rerun preparation and readiness checks, review parameter assumptions, and assess simulated results against available independent observations. No trained forecasting model is included in this demo.</p>
</div>
</div>
<div class="panel">
<h3>Owner readiness register</h3>
<div class="controls">
<label>Status
<select id="readinessFilter">
<option value="all">All owners</option>
<option value="deferred_for_review">Deferred</option>
<option value="ready_for_bounded_test">Ready</option>
</select>
</label>
<label>Find an owner<input id="ownerSearch" type="search" placeholder="Owner ID"></label>
</div>
<p id="readinessCount" class="note"></p>
<div id="readinessTable" class="scroll"></div>
</div>
<div class="panel">
<h3>Snapshot provenance and interpretation</h3>
<div id="provenance" class="sources"></div>
<ul id="interpretation"></ul>
</div>
</section>

<footer>Synthetic simulator development reference · Controls filter saved results · A live engine demonstration is a separate step.</footer>
</main>
</div>

<script id="demo-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
try {
const D=JSON.parse(document.getElementById("demo-data").textContent), S=D.settings;
const $=id=>document.getElementById(id), groups=["TOTAL","T1","T2","T3","T4"];
const colors={reduced:"#235de1",rated:"#d95d16",difference:"#7850bb",T1:"#235de1",T2:"#16846b",T3:"#c28116",T4:"#7850bb"};
const names={reduced:"Reduced power",rated:"Rated ceiling",difference:"Rated minus reduced"};

const E=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const F=(v,n=2)=>v===null||v===undefined||v===""?"—":Number(v).toLocaleString("en-GB",{minimumFractionDigits:n,maximumFractionDigits:n});
const num=v=>Number(v);
const option=(v,t)=>`<option value="${E(v)}">${E(t)}</option>`;
const rowsFor=c=>D.profiles.filter(r=>r.context===c).sort((a,b)=>a.hour_of_day-b.hour_of_day);
const summary=c=>D.comparison.find(r=>r.context===c);
const daily=(c,k)=>D.daily_precision.filter(r=>r.context===c&&r.case===k).sort((a,b)=>a.n-b.n);

const cards=items=>items.map(([label,value,note])=>`
<div class="card">
<small>${E(label)}</small>
<div class="value">${E(value)}</div>
<div class="note">${E(note)}</div>
</div>`).join("");

function table(headers,rows){
    return `<table><thead><tr>${
        headers.map(h=>`<th scope="col">${E(h)}</th>`).join("")
    }</tr></thead><tbody>${
        rows.length
        ? rows.map(r=>`<tr>${r.map(v=>`<td>${E(v)}</td>`).join("")}</tr>`).join("")
        : `<tr><td colspan="${headers.length}">No matching records.</td></tr>`
    }</tbody></table>`;
}

function bars(items){
    const maximum=Math.max(1,...items.map(r=>r.value));
    return items.map(r=>`
    <div>
        <div class="bar-label">
            <span>${E(r.label)}</span><strong>${E(r.text)}</strong>
        </div>
        <div class="track">
            <div class="fill" style="width:${100*r.value/maximum}%;background:${r.color||colors.reduced}"></div>
        </div>
    </div>`).join("");
}

function plot(xs,series,{
    unit="kW",
    xlabel="Hour of day",
    ticks=null,
    band=null,
    step=false
}={}){
    const W=900,H=270,L=68,R=20,T=18,B=49;
    const all=series.flatMap(s=>s.values).concat(
        band?band.low.concat(band.high):[]
    );

    let low=Math.min(0,...all),high=Math.max(0,...all);
    if(high===low)high=low+1;
    const pad=(high-low)*.06;
    high+=pad;
    if(low<0)low-=pad;

    const x=v=>L+(v-xs[0])/(xs.at(-1)-xs[0]||1)*(W-L-R);
    const y=v=>T+(high-v)/(high-low)*(H-T-B);

    const points=vs=>vs.flatMap((v,i)=>
        step&&i<xs.length-1
        ? [[x(xs[i]),y(v)],[x(xs[i+1]),y(v)]]
        : [[x(xs[i]),y(v)]]
    );

    const path=ps=>ps.map(([a,b],i)=>
        `${i?"L":"M"}${a.toFixed(2)},${b.toFixed(2)}`
    ).join(" ");

    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${E(unit+" by "+xlabel)}">
    <title>${E(unit+" by "+xlabel)}</title>`;

    for(let i=0;i<=4;i++){
        const v=low+(high-low)*i/4,yy=y(v);
        svg+=`<line x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}" stroke="#e6edf3"/>
        <text x="${L-10}" y="${yy+4}" text-anchor="end" font-size="12" fill="#526879">${E(F(v,1))}</text>`;
    }

    for(const v of ticks||xs){
        svg+=`<text x="${x(v)}" y="${H-B+22}" text-anchor="middle" font-size="12" fill="#526879">${E(v)}</text>`;
    }

    if(band){
        svg+=`<path d="${path(points(band.high))} ${
            path(points(band.low).reverse()).replace(/^M/,"L")
        } Z" fill="${band.color||colors.reduced}" opacity=".12"/>`;
    }

    svg+=`<line x1="${L}" x2="${W-R}" y1="${y(0)}" y2="${y(0)}" stroke="#9db0c0" stroke-dasharray="4 4"/>`;

    for(const s of series){
        svg+=`<path d="${path(points(s.values))}" fill="none" stroke="${s.color}" stroke-width="2.6"/>`;

        if(!step){
            svg+=s.values.map((v,i)=>`
            <circle cx="${x(xs[i])}" cy="${y(v)}" r="4" fill="${s.color}">
            <title>${E(xs[i]+": "+F(v,4)+" "+unit)}</title>
            </circle>`).join("");
        }
    }

    if(step){
        for(let i=0;i<xs.length-1;i++){
            const title=`Hour ${xs[i]}–${xs[i+1]}: `+
                series.map(s=>
                    s.name+" "+F(s.values[i],4)+" "+unit
                ).join("; ");

            svg+=`<rect x="${x(xs[i])}" y="${T}" width="${x(xs[i+1])-x(xs[i])}" height="${H-T-B}" fill="transparent">
            <title>${E(title)}</title></rect>`;
        }
    }

    svg+=`<text x="${(L+W-R)/2}" y="${H-6}" text-anchor="middle" font-size="12" fill="#526879">${E(xlabel)}</text>
    <text x="18" y="${(T+H-B)/2}" transform="rotate(-90 18 ${(T+H-B)/2})" text-anchor="middle" font-size="12" fill="#526879">${E(unit)}</text>
    </svg>`;

    return `<div class="legend">${
        series.map(s=>`<span><i class="key" style="background:${s.color}"></i>${E(s.name)}</span>`).join("")
    }${
        band?`<span><i class="key" style="background:${band.color||colors.reduced};opacity:.35"></i>${E(band.label)}</span>`:""
    }</div><div class="chart scroll">${svg}</div>`;
}

const extended=(rows,key)=>
    rows.map(r=>r[key]).concat(rows.at(-1)[key]);

function renderDemand(){
    const c=$("profileContext").value;
    const k=$("profileCase").value;
    const rows=rowsFor(c),m=summary(c),p=daily(c,k).at(-1);
    const values=rows.map(r=>r[k+"_mean_power_kw"]);
    const peak=Math.max(...values);

    const hours=rows.filter(r=>
        Math.abs(r[k+"_mean_power_kw"]-peak)<1e-7
    ).map(r=>String(r.hour_of_day).padStart(2,"0")+":00");

    $("demandCards").innerHTML=cards([
        ["Mean daily energy",
         F(m[k+"_mean_daily_energy_kwh"])+" kWh",
         "Selected context · study-period mean"],
        ["Peak of mean profile",
         F(peak)+" kW",
         hours.join(", ")],
        ["Pooled start rejection",
         F(m[k+"_rejection_percent"])+"%",
         "Chronological owner-overlap model"],
        ["Daily-energy MCSE",
         F(p.mcse,3)+" kWh/day",
         F(p.relative_mcse_percent,3)+"% relative MCSE"]
    ]);

    $("profileTitle").textContent=D.context_labels[c]+" · "+names[k];
    $("showBand").disabled=k!=="reduced";

    const series=[
        k,
        k==="reduced"?"rated":"reduced"
    ].map(v=>({
        name:names[v],
        color:colors[v],
        values:extended(rows,v+"_mean_power_kw")
    }));

    const band=k==="reduced"&&$("showBand").checked
        ? {
            low:extended(rows,"reduced_p05_kw"),
            high:extended(rows,"reduced_p95_kw"),
            label:"Reduced P05–P95 of replicate means"
        }
        : null;

    $("profileChart").innerHTML=plot(
        Array.from({length:25},(_,i)=>i),
        series,
        {step:true,ticks:[0,4,8,12,16,20,24],band}
    );

    $("hourlyTable").innerHTML=table(
        ["Hour interval","Reduced mean kW","Rated mean kW","Difference kW"],
        rows.map(r=>[
            r.hour_of_day+"–"+(r.hour_of_day+1),
            F(r.reduced_mean_power_kw,4),
            F(r.rated_mean_power_kw,4),
            F(r.mean_difference_kw,4)
        ])
    );

    $("contextBars").innerHTML=bars(
        groups.slice(1).map(v=>({
            label:D.context_labels[v],
            value:summary(v)[k+"_mean_daily_energy_kwh"],
            text:F(summary(v)[k+"_mean_daily_energy_kwh"])+" kWh/day",
            color:colors[v]
        }))
    );
}

function renderPower(){
    const c=$("powerContext").value;
    const m=summary(c),rows=rowsFor(c);
    const delta=m.mean_daily_energy_difference_kwh;

    $("powerCards").innerHTML=cards([
        ["Daily-energy change",
         F(delta,3)+" kWh",
         F(m.daily_energy_change_percent,3)+"% · rated minus reduced"],
        ["Reduced rejection",
         F(m.reduced_rejection_percent)+"%",
         "Pooled study starts"],
        ["Rated rejection",
         F(m.rated_rejection_percent)+"%",
         "Pooled study starts"],
        ["Matched candidates",
         F(D.matched_candidates_including_warmup,0),
         "All contexts · includes warm-up"]
    ]);

    $("differenceChart").innerHTML=plot(
        Array.from({length:25},(_,i)=>i),
        [{
            name:"Paired mean difference",
            color:colors.difference,
            values:extended(rows,"mean_difference_kw")
        }],
        {
            step:true,
            ticks:[0,4,8,12,16,20,24],
            band:{
                low:extended(rows,"difference_p05_kw"),
                high:extended(rows,"difference_p95_kw"),
                color:colors.difference,
                label:"P05–P95 of paired replicate differences"
            }
        }
    );

    $("comparisonTable").innerHTML=table(
        [
            "Context","Reduced kWh/day","Rated kWh/day",
            "Change kWh/day","Change %",
            "Reduced rejection %","Rated rejection %"
        ],
        groups.map(v=>{
            const r=summary(v);
            return [
                D.context_labels[v],
                F(r.reduced_mean_daily_energy_kwh),
                F(r.rated_mean_daily_energy_kwh),
                F(r.mean_daily_energy_difference_kwh,3),
                F(r.daily_energy_change_percent,3),
                F(r.reduced_rejection_percent),
                F(r.rated_rejection_percent)
            ];
        })
    );
}

function renderPrecision(){
    const c=$("precisionContext").value;
    const k=$("precisionCase").value;
    const rows=daily(c,k),last=rows.at(-1);

    const hourly=D.hourly_precision.filter(r=>
        r.context===c&&r.case===k
    );
    const maximum=Math.max(...hourly.map(r=>r.mcse));

    $("precisionCards").innerHTML=cards([
        ["Mean daily energy",
         F(last.mean,3)+" kWh/day",
         names[k]],
        ["Daily-energy MCSE",
         F(last.mcse,3)+" kWh/day",
         "Sampling unit: complete replicate"],
        ["Relative daily MCSE",
         last.relative_mcse_percent===null
            ? "—"
            : F(last.relative_mcse_percent,3)+"%",
         k==="difference"
            ? "Omitted for signed differences"
            : "Conditional on fixed synthetic inputs"],
        ["Largest hourly MCSE",
         F(maximum,3)+" kW",
         "Pointwise hourly mean precision"]
    ]);

    $("precisionChart").innerHTML=plot(
        rows.map(r=>r.n),
        [{
            name:names[k]+" MCSE",
            color:colors[k],
            values:rows.map(r=>r.mcse)
        }],
        {
            unit:"kWh/day",
            xlabel:"Replicates (nested checkpoints)"
        }
    );

    $("precisionTable").innerHTML=table(
        ["Replicates","Mean kWh/day","MCSE kWh/day","Relative MCSE %"],
        rows.map(r=>[
            r.n,
            F(r.mean,4),
            F(r.mcse,4),
            F(r.relative_mcse_percent,3)
        ])
    );
}

function timeline(accepted,rejected,minimum){
    const W=900,H=220,L=65,R=20,T=20;
    const span=S.study_days*24-minimum;
    const x=v=>L+(v-minimum)/span*(W-L-R);
    const codes=groups.slice(1);
    const y=c=>T+codes.indexOf(c)*35;

    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Accepted intervals and rejected proposed starts">
    <title>Accepted intervals and rejected proposed starts</title>`;

    for(const c of codes){
        svg+=`<text x="${L-12}" y="${y(c)+16}" text-anchor="end" fill="#526879" font-size="12">${c}</text>
        <line x1="${L}" x2="${W-R}" y1="${y(c)+12}" y2="${y(c)+12}" stroke="#e6edf3"/>`;
    }

    for(let day=Math.ceil(minimum/24);day<=S.study_days;day++){
        if(day%7!==0&&day!==S.study_days)continue;

        svg+=`<line x1="${x(day*24)}" x2="${x(day*24)}" y1="${T}" y2="165" stroke="#e6edf3"/>
        <text x="${x(day*24)}" y="185" text-anchor="middle" font-size="12" fill="#526879">${day}</text>`;
    }

    for(const r of accepted){
        const start=Math.max(minimum,num(r.start_hour_absolute));
        const end=Math.min(S.study_days*24,num(r.end_hour_absolute));

        if(end<=start)continue;

        svg+=`<rect x="${x(start)}" y="${y(r.context)+5}" width="${Math.max(.7,x(end)-x(start))}" height="14" rx="2" fill="${colors[r.context]}">
        <title>${E(
            r.session_id+" · "+r.context+" · "+
            F(r.active_duration_hours)+" h · "+
            F(r.effective_average_power_kw)+" kW"
        )}</title></rect>`;
    }

    for(const r of rejected){
        const start=num(r.start_hour_absolute);

        if(start<minimum||start>=S.study_days*24)continue;

        const xx=x(start),yy=y(r.context)+12;

        svg+=`<path d="M${xx-3},${yy-4}L${xx+3},${yy+4}M${xx-3},${yy+4}L${xx+3},${yy-4}" stroke="#b52d42" stroke-width="1.8">
        <title>${E(r.session_id+" · "+r.rejection_reason)}</title></path>`;
    }

    return `<div class="legend">
        <span>Colored bars: accepted charging</span>
        <span style="color:#b52d42">× Rejected proposed start</span>
        </div>
        <div class="chart scroll">${svg}
        <text x="470" y="211" text-anchor="middle" font-size="12" fill="#526879">Study day index</text>
        </svg></div>`;
}

function renderTrace(){
    const id=$("traceOwner").value;
    const k=$("traceCase").value;
    const trace=D.example_replicate[k];
    const owner=D.owner_profiles.find(r=>r.owner_id===id);

    const a=trace.accepted_sessions.filter(r=>r.owner_id===id);
    const b=trace.rejected_sessions.filter(r=>r.owner_id===id);
    const studyA=a.filter(r=>r.start_phase==="study");
    const studyB=b.filter(r=>r.start_phase==="study");
    const rates=trace.owner_rates.filter(r=>r.owner_id===id);
    const energy=a.reduce(
        (sum,r)=>sum+num(r.energy_in_study_kwh),0
    );
    const include=$("includeWarmup").checked;

    $("traceCaption").textContent=
        `Replicate ${trace.replicate} · Seed ${trace.seed} · `+
        `${S.study_days} study days · ${S.warmup_days} warm-up days. `+
        `This explorer shows one history; aggregate charts use all `+
        `${S.replicates_per_case} replicates.`;

    $("traceCards").innerHTML=cards([
        ["Study candidate starts",
         F(studyA.length+studyB.length,0),
         "Selected owner · one replicate"],
        ["Accepted / rejected starts",
         studyA.length+" / "+studyB.length,
         "Study starts only"],
        ["Study energy",
         F(energy)+" kWh",
         "Includes accepted warm-up carry-in"],
        ["Sampled candidate rate",
         F(rates[0].weekly_total_sampled)+" /week",
         "Accepted start rate: "+
         F(studyA.length*7/S.study_days)+" /week"]
    ]);

    $("timeline").innerHTML=timeline(
        a,b,include?-S.warmup_days*24:0
    );

    $("ownerBounds").textContent=
        `Session-energy bounds: ${F(owner.session_energy_kwh_min)}–`+
        `${F(owner.session_energy_kwh_max)} kWh. `+
        `Feasible total frequency: ${F(owner.weekly_total_feasible_min)}–`+
        `${F(owner.weekly_total_feasible_max)} sessions/week. `+
        `These are synthetic parameter bounds, not fitted distributions.`;

    $("ownerContexts").innerHTML=table(
        [
            "Context","Charger type","Weekly bounds",
            "Start window (hour)","Sampled candidate rate/week"
        ],
        owner.active_contexts.map(r=>[
            D.context_labels[r.context],
            r.charger_code,
            F(r.weekly_rate_min)+"–"+F(r.weekly_rate_max),
            F(r.window_start_hour)+" → "+F(r.window_end_hour),
            F(
                rates.find(v=>
                    v.context===r.context
                ).weekly_context_rate,
                3
            )
        ])
    );

    $("ownerJson").textContent=JSON.stringify(owner,null,2);

    const rows=a.map(r=>({...r,status:"Accepted"}))
        .concat(b.map(r=>({...r,status:"Rejected"})))
        .filter(r=>include||r.start_phase==="study")
        .sort((a,b)=>
            num(a.start_hour_absolute)-num(b.start_hour_absolute)
        );

    $("sessionTitle").textContent=
        (include?"Study and warm-up starts":"Study starts")+
        " · "+rows.length+" records";

    $("sessionTable").innerHTML=table(
        [
            "Session ID","Context","Status","Start day / hour",
            "Duration h","Power kW","Drawn kWh","Study kWh",
            "Reason / blocking session"
        ],
        rows.map(r=>[
            r.session_id,
            r.context,
            r.status,
            r.start_day_index+" / "+F(r.start_hour_of_day),
            F(r.active_duration_hours),
            F(r.effective_average_power_kw),
            F(r.session_energy_kwh),
            r.status==="Accepted"
                ? F(r.energy_in_study_kwh)
                : "—",
            r.rejection_reason
                ? r.rejection_reason+" / "+
                  (r.conflicting_session_id||"—")
                : "—"
        ])
    );
}

const reasons={
    usual_energy_kwh_unresolved_bounds:
        "Usual energy: unresolved bounds",
    sessions_per_week_unresolved_bounds:
        "Weekly frequency: unresolved bounds",
    home_power_kw_unresolved_bounds:
        "Home power: unresolved bounds",
    total_and_context_frequencies_do_not_overlap:
        "Total/context frequencies disagree"
};

const reasonLabel=v=>reasons[v]||v.replace(/_/g," ");

function renderReadiness(){
    $("readinessCards").innerHTML=cards([
        ["Owners retained",
         F(S.owners_retained,0),
         "All synthetic input owners"],
        ["Ready for bounded test",
         F(S.owners_simulated,0),
         "Current simulated population"],
        ["Deferred for review",
         F(S.owners_deferred,0),
         "Preserved in the audit register"],
        ["Replicates per power case",
         F(S.replicates_per_case,0),
         "Same owner pool in every replicate"]
    ]);

    $("reasonBars").innerHTML=bars(
        Object.entries(D.first_deferral_counts)
        .sort((a,b)=>b[1]-a[1])
        .map(([label,value])=>({
            label:reasonLabel(label),
            value,
            text:value+" owners"
        }))
    );

    const filter=$("readinessFilter").value;
    const search=$("ownerSearch").value.trim().toLowerCase();

    const rows=D.readiness.filter(r=>
        (filter==="all"||r.status===filter)
        &&r.owner_id.toLowerCase().includes(search)
    );

    $("readinessCount").textContent=
        rows.length+" of "+S.owners_retained+" owners shown";

    $("readinessTable").innerHTML=table(
        ["Owner ID","Data origin","Readiness","First deferral reason"],
        rows.map(r=>[
            r.owner_id,
            r.data_origin,
            r.status==="ready_for_bounded_test"?"Ready":"Deferred",
            r.first_deferral_reason
                ? reasonLabel(r.first_deferral_reason)
                : "—"
        ])
    );

    $("provenance").innerHTML=`
    <p>Bundle hash matches its completed packaging report.
    Packaging recorded ${E(D.bundle_verification.covered_files)}
    covered input/source files. This dashboard verifies the frozen
    bundle; it does not repeat every historical run audit.</p>
    <p>
    <strong>Comparison:</strong> ${E(D.source_folders.comparison)}<br>
    <strong>Precision:</strong> ${E(D.source_folders.precision)}<br>
    <strong>Bundle SHA-256:</strong> ${E(D.bundle_verification.bundle_sha256)}
    </p>`;

    $("interpretation").innerHTML=D.interpretation.map(v=>
        `<li class="note">${E(v)}</li>`
    ).join("");
}

$("settings").textContent=
    `${S.owners_simulated} synthetic owners · `+
    `${S.study_days} study days · `+
    `${S.warmup_days} warm-up days · `+
    `${S.replicates_per_case} replicates per case`;

for(const id of [
    "profileContext","powerContext","precisionContext"
]){
    $(id).innerHTML=groups.map(c=>
        option(c,D.context_labels[c])
    ).join("");
}

for(const id of ["profileCase","traceCase","precisionCase"]){
    $(id).innerHTML=(
        id==="precisionCase"
        ? ["reduced","rated","difference"]
        : ["reduced","rated"]
    ).map(k=>option(k,names[k])).join("");
}

$("traceOwner").innerHTML=D.owner_profiles
    .map(r=>r.owner_id)
    .sort()
    .map(id=>option(id,id))
    .join("");

for(const id of ["profileContext","profileCase","showBand"]){
    $(id).addEventListener("change",renderDemand);
}

$("powerContext").addEventListener("change",renderPower);

for(const id of ["precisionContext","precisionCase"]){
    $(id).addEventListener("change",renderPrecision);
}

for(const id of ["traceOwner","traceCase","includeWarmup"]){
    $(id).addEventListener("change",renderTrace);
}

$("readinessFilter").addEventListener("change",renderReadiness);
$("ownerSearch").addEventListener("input",renderReadiness);

document.querySelectorAll("[data-tab]").forEach(button=>
    button.addEventListener("click",()=>{
        document.querySelectorAll(".view").forEach(view=>
            view.hidden=view.id!==button.dataset.tab
        );

        document.querySelectorAll("[data-tab]").forEach(b=>
            b.setAttribute("aria-pressed",String(b===button))
        );
    })
);

$("exportProfile").addEventListener("click",()=>{
    const context=$("profileContext").value;

    const header=[
        "data_origin","context","hour_of_day",
        "study_days","replicates_per_case",
        "reduced_mean_power_kw","rated_mean_power_kw",
        "mean_difference_kw"
    ];

    const rows=rowsFor(context).map(r=>[
        "SYNTHETIC",
        context,
        r.hour_of_day,
        S.study_days,
        S.replicates_per_case,
        r.reduced_mean_power_kw,
        r.rated_mean_power_kw,
        r.mean_difference_kw
    ]);

    const csv=[header,...rows].map(row=>
        row.map(v=>
            '"'+String(v).replace(/"/g,'""')+'"'
        ).join(",")
    ).join("\r\n");

    const url=URL.createObjectURL(
        new Blob([csv],{type:"text/csv;charset=utf-8"})
    );

    const a=document.createElement("a");
    a.href=url;
    a.download="synthetic_profile_"+context+".csv";
    a.click();

    setTimeout(()=>URL.revokeObjectURL(url),1000);
});

renderDemand();
renderPower();
renderPrecision();
renderTrace();
renderReadiness();

} catch(error) {
    document.getElementById("fatal").hidden=false;
    document.getElementById("fatal").textContent=
        "Dashboard could not load: "+error.message;
    console.error(error);
}
</script>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        required=True,
        help="Folder containing demo_bundle.json and its report",
    )
    args = parser.parse_args()

    output, report = build_dashboard(Path(args.bundle))

    print("PP1 offline dashboard complete.")
    print("views: 5")

    for key in (
        "owners_simulated",
        "study_days",
        "warmup_days",
        "replicates_per_case",
    ):
        print(f"{key}: {report['settings'][key]}")

    print(
        "Bundle hash, packaging checks and "
        "input-preservation checks passed."
    )
    print(f"Dashboard: {output.relative_to(ROOT)}")
    print(
        f"Report: "
        f"{(output.parent / 'dashboard_report.json').relative_to(ROOT)}"
    )
    print(
        "Open the HTML file in your browser. "
        "Internet and a web server are not required."
    )
    print("Saved synthetic results only; no new simulation or AI training.")


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        OverflowError,
    ) as error:
        print(
            f"PP1 dashboard creation failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)
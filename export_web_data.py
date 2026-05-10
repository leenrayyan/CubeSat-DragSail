"""
Export simulation results to docs/data.json for the ORION-1 dashboard.

The dashboard (docs/index.html) is fully static and consumes this single
JSON file at page load. Re-run this script any time the simulation
parameters change.

Schema (top-level keys):
    params              : mission parameters used
    forecast            : F10.7 history + GP median + 5-95% band
    scenarios           : 5 deorbit trajectories (baseline, naive, point, robust, riskaverse)
    optimizer           : score landscape + chosen days per strategy
    flight_policy       : metadata about the distilled embedded model
    kpis                : headline numbers for the KPI table
    references          : citations for ESA rule, atmosphere, sail heritage
"""

import warnings; warnings.filterwarnings("ignore")
import os, json, datetime as dt
import numpy as np

from orbit_decay import decay_trajectory
from solar_forecast import (build_trajectories, load_history, MISSION_EPOCH)
from ai_optimizer import optimize_robust, time_in_band

# --- Mission constants (must match main_sim.py) -----------------------------
ALTITUDE_KM = 600.0
MASS_KG = 8.0
BASE_AREA = 0.03
SAIL_AREA = 0.40
CD = 2.2
MISSION_DAYS = 1096
WINDOW_DAYS = 360
N_SAMPLES = 30
ESA_LIMIT_YR = 5.0

OUT_DIR = "docs"
os.makedirs(OUT_DIR, exist_ok=True)


def downsample(t, y, n_target=200):
    """Reduce a trajectory to ~n_target points for compact JSON."""
    if len(t) <= n_target:
        return t.tolist(), y.tolist()
    idx = np.linspace(0, len(t) - 1, n_target).astype(int)
    return t[idx].tolist(), y[idx].tolist()


def main():
    print("Building forecast distribution...")
    fc = build_trajectories(max_day=10*365, n_samples=N_SAMPLES, dt_days=15.0)

    # --- Baseline (no sail) ------------------------------------------------
    print("Baseline decay...")
    t_b, h_b = decay_trajectory(ALTITUDE_KM, BASE_AREA, MASS_KG, Cd=CD,
                                max_years=120.0, dt_days=10.0,
                                f107_func=fc["median_trajectory"])

    # --- Optimizer over 4 strategies --------------------------------------
    print("Robust optimization (fine grid)...")
    opt = optimize_robust(MISSION_DAYS, fc, window_days=WINDOW_DAYS,
                          step_days=7, area_m2=SAIL_AREA, mass_kg=MASS_KG,
                          Cd=CD, altitude_km=ALTITUDE_KM, max_years=10.0,
                          w_band=4.0)  # weight crowded-band higher to differentiate strategies

    # ----------------------------------------------------------------------
    # Build scenarios list
    # ----------------------------------------------------------------------
    def make_scenario(name, label, deploy_day, t, h, decay_yr, band_yr,
                      score_mean=None, score_p95=None,
                      post_eom_mean=None, post_eom_p95=None):
        # absolute mission years for plotting (deploy_day + sim time)
        t_yr_abs = (np.asarray(t) + deploy_day) / 365.25
        td, hd = downsample(t_yr_abs, np.asarray(h), n_target=200)
        ok = bool((post_eom_mean if post_eom_mean is not None
                   else max(0.0, decay_yr - MISSION_DAYS/365.25)) <= ESA_LIMIT_YR)
        return {
            "key": name, "label": label,
            "deploy_day": int(deploy_day),
            "deploy_year": deploy_day / 365.25,
            "decay_years": decay_yr,
            "post_eom_years": (post_eom_mean if post_eom_mean is not None
                               else max(0.0, decay_yr - MISSION_DAYS/365.25)),
            "post_eom_p95":   post_eom_p95,
            "band_years": band_yr,
            "score_mean": score_mean,
            "score_p95": score_p95,
            "esa_5yr_compliant": ok,
            "trajectory_t_yr": td,
            "trajectory_alt_km": hd,
        }

    scenarios = [make_scenario(
        "baseline", "Baseline (no sail)", 0, t_b, h_b,
        decay_yr=float(t_b[-1]/365.25),
        band_yr=time_in_band(t_b, h_b))]

    pretty = {"naive": "Naive (deploy at EOL)",
              "point": "AI point (median forecast)",
              "robust": "AI robust (E[score])",
              "riskaverse": "AI risk-averse (p95)"}

    for key in ("naive", "point", "robust", "riskaverse"):
        c = opt[key]
        scenarios.append(make_scenario(
            key, pretty[key], int(c["deploy_day"]),
            c["times_days"], c["altitudes_km"],
            decay_yr=c["deorbit_mean"],
            band_yr=c["band_mean"],
            score_mean=c["score_mean"], score_p95=c["score_p95"],
            post_eom_mean=c["post_eom_mean"],
            post_eom_p95=c["post_eom_p95"]))

    # ----------------------------------------------------------------------
    # Forecast (history + median + 5-95 band)
    # ----------------------------------------------------------------------
    hist_months, hist_y = load_history()
    grid_yr = (fc["grid_days"] / 365.25).tolist()
    forecast = {
        "epoch": MISSION_EPOCH.strftime("%Y-%m-%d"),
        "history_year": (hist_months / 12.0).tolist(),  # year offset (negative=past)
        "history_f107": hist_y.tolist(),
        "forecast_year": grid_yr,
        "forecast_median": fc["median"].tolist(),
        "forecast_p05": fc["p05"].tolist(),
        "forecast_p95": fc["p95"].tolist(),
        "n_samples": int(N_SAMPLES),
    }

    # ----------------------------------------------------------------------
    # Optimizer landscape
    # ----------------------------------------------------------------------
    cands = opt["candidates"]
    optimizer = {
        "candidate_days":   [int(c["deploy_day"]) for c in cands],
        "score_mean":       [c["score_mean"]    for c in cands],
        "score_std":        [c["score_std"]     for c in cands],
        "score_median":     [c["score_median"]  for c in cands],
        "score_p95":        [c["score_p95"]     for c in cands],
        "post_eom_mean":    [c["post_eom_mean"] for c in cands],
        "post_eom_p95":     [c["post_eom_p95"]  for c in cands],
        "band_mean":        [c["band_mean"]     for c in cands],
        "chosen": {k: int(opt[k]["deploy_day"])
                   for k in ("naive", "point", "robust", "riskaverse")},
    }

    # ----------------------------------------------------------------------
    # Flight policy metadata
    # ----------------------------------------------------------------------
    flight_meta_path = os.path.join("flight", "policy_meta.json")
    if os.path.exists(flight_meta_path):
        with open(flight_meta_path) as f:
            flight_policy = json.load(f)
        # also embed the C source for the website to display
        c_path = os.path.join("flight", "policy.c")
        if os.path.exists(c_path):
            with open(c_path) as f:
                flight_policy["c_source"] = f.read()
    else:
        flight_policy = {"note": "Run python flight_policy.py to populate."}

    # ----------------------------------------------------------------------
    # KPI table (mirror the printed table)
    # ----------------------------------------------------------------------
    kpis = []
    for s in scenarios:
        kpis.append({
            "scenario":         s["label"],
            "deploy_day":       s["deploy_day"],
            "post_eom_years":   round(s["post_eom_years"], 2),
            "post_eom_p95":     (round(s["post_eom_p95"], 2)
                                 if s["post_eom_p95"] is not None else None),
            "band_years":       round(s["band_years"], 3),
            "score_mean":       (round(s["score_mean"], 3)
                                 if s["score_mean"] is not None else None),
            "esa_5yr_compliant": s["esa_5yr_compliant"],
        })

    # ----------------------------------------------------------------------
    # References (cite in website)
    # ----------------------------------------------------------------------
    references = [
        {"id": "ESA-SDM",
         "title": "ESA Space Debris Mitigation Requirements (ESSB-ST-U-007 Issue 1)",
         "section": "Req. SDM-50 — 5-yr post-mission disposal in LEO",
         "url": "https://technology.esa.int/upload/media/ESSB-ST-U-007-Issue1.pdf",
         "in_force": "2023-11-01"},
        {"id": "FCC-22-74",
         "title": "FCC 22-74 — Space Innovation: 5-Year Deorbit Rule",
         "url": "https://docs.fcc.gov/public/attachments/FCC-22-74A1.pdf",
         "in_force": "2022-09"},
        {"id": "InflateSail",
         "title": "Underwood et al., InflateSail de-orbit demonstration, Acta Astronautica 162 (2019)",
         "url": "https://doi.org/10.1016/j.actaastro.2019.05.054",
         "note": "Fastest cubesat reentry: 72 days from 505 km with 10 m^2 sail."},
        {"id": "CanX-7",
         "title": "Bonin/Risi/Zee, CanX-7 Drag Sail Deorbit Mission, AIAA/USU SmallSat 2018",
         "url": "https://digitalcommons.usu.edu/smallsat/2018/all2018/376/"},
        {"id": "ADEO-N",
         "title": "DLR/HPS ADEO-N drag-sail flight (ION SCV-004, Dec 2022)",
         "url": "https://www.dlr.de/en/latest/news/2023/adeo-braking-sail"},
        {"id": "Vallado-atmos",
         "title": "Vallado, Fundamentals of Astrodynamics 4th ed., Table 8-4 exponential atmosphere",
         "note": "Used as base density model; F10.7 modulation calibrated to NRLMSIS-2.1 published values."},
        {"id": "pymsis",
         "title": "Lucas et al., pymsis (NRLMSIS-2.1 Python wrapper)",
         "url": "https://github.com/SWxTREC/pymsis",
         "note": "Recommended production replacement for our exponential model."},
        {"id": "Furano-AI-Edge",
         "title": "Furano et al., Towards the Use of Artificial Intelligence on the Edge in Space Systems, IEEE A&E Systems Mag 2020",
         "url": "https://doi.org/10.1109/MAES.2020.3008468",
         "note": "Justifies ground-train / flight-infer split with TMR + watchdog."},
        {"id": "Phi-Sat-1",
         "title": "ESA Phi-Sat-1 (Intel Movidius Myriad 2) — first AI in orbit",
         "url": "https://www.esa.int/Applications/Observing_the_Earth/Ph-sat"},
        {"id": "m2cgen",
         "title": "BayesWatch m2cgen — sklearn -> portable C codegen",
         "url": "https://github.com/BayesWitnesses/m2cgen"},
        {"id": "NOAA-SWPC",
         "title": "NOAA SWPC Observed Solar Cycle Indices",
         "url": "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json",
         "note": "21 yr of monthly F10.7 used to train our GP forecaster."},
    ]

    # ----------------------------------------------------------------------
    # Final bundle
    # ----------------------------------------------------------------------
    bundle = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "params": {
            "altitude_km": ALTITUDE_KM, "mass_kg": MASS_KG,
            "base_area_m2": BASE_AREA, "sail_area_m2": SAIL_AREA,
            "Cd": CD, "mission_days": MISSION_DAYS,
            "window_days": WINDOW_DAYS, "n_forecast_samples": N_SAMPLES,
            "esa_post_eom_limit_yr": ESA_LIMIT_YR,
            "sail_area_ratio": round(SAIL_AREA / BASE_AREA, 1),
        },
        "forecast": forecast,
        "scenarios": scenarios,
        "optimizer": optimizer,
        "flight_policy": flight_policy,
        "kpis": kpis,
        "references": references,
    }

    out_path = os.path.join(OUT_DIR, "data.json")
    with open(out_path, "w") as f:
        json.dump(bundle, f, indent=2, default=float)
    sz = os.path.getsize(out_path) / 1024
    print(f"\nWrote {out_path} ({sz:.1f} KB)")
    print(f"  {len(scenarios)} scenarios, {len(optimizer['candidate_days'])} optimizer candidates")
    print(f"  forecast: {len(forecast['history_year'])} historical + "
          f"{len(forecast['forecast_year'])} forecast points")


if __name__ == "__main__":
    main()

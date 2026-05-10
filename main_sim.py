"""
End-to-end simulation: run all scenarios, generate plots, print KPIs.

Pipeline:
  1. Fetch & cache NOAA F10.7 history (offline-safe after first run).
  2. Fit a Gaussian Process forecaster on that history.
  3. Sample N=30 future F10.7 trajectories from the GP posterior.
  4. Run baseline (no sail) decay using the median forecast.
  5. Run 4 sail-deployment strategies inside a +/-90 day window:
       naive       : deploy on day 1096
       point       : best day under median GP forecast
       robust      : best day minimizing E[score] over the 30 samples
       risk-averse : best day minimizing 95th-percentile worst-case score
  6. Save 4 plots to results/ and print the KPI table.

Usage:
    python main_sim.py
"""

import os, warnings
warnings.filterwarnings("ignore")
import numpy as np
import matplotlib.pyplot as plt

from orbit_decay import decay_trajectory
from solar_forecast import (build_trajectories, load_history,
                             MISSION_EPOCH)
from ai_optimizer import (optimize_robust, evaluate_with_median,
                           time_in_band)

# --- Mission parameters -----------------------------------------------------
# Aligned with ORION-1 hardware spec (8 kg 6U, ADEO-class deployable sail).
# 0.40 m^2 sail (~13x stowed area) sized to safely meet the ESA 5-year rule
# (ESSB-ST-U-007 Issue 1, Req. SDM-50, in force 2023-11-01).
ALTITUDE_KM = 600.0
MASS_KG = 8.0
BASE_AREA = 0.03          # 6U one face stowed
SAIL_AREA = 0.40          # ADEO-class deployable sail (~13x stowed area)
CD = 2.2
MISSION_DAYS = 1096       # 3 years operational mission
WINDOW_DAYS = 360         # +/- 6 months operator flexibility
N_FORECAST_SAMPLES = 30
ESA_LIMIT_YEARS = 25.0
ESA_NEW_LIMIT_YEARS = 5.0

RESULTS_DIR = "results"
os.makedirs(RESULTS_DIR, exist_ok=True)


# --- Plots ------------------------------------------------------------------
def plot_forecast(forecast, strategy_days, save):
    """History + GP forecast fan chart + deployment dates marked."""
    hist_months, hist_y = load_history()

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(hist_months / 12, hist_y, "k.", ms=3, label="NOAA F10.7 (observed)")

    months_fwd = forecast["grid_days"] / (365.25 / 12.0)
    yrs_fwd = months_fwd / 12.0
    ax.plot(yrs_fwd, forecast["median"], color="tab:blue",
            lw=1.6, label="GP median forecast")
    ax.fill_between(yrs_fwd, forecast["p05"], forecast["p95"],
                    color="tab:blue", alpha=0.18, label="GP 5-95% band")
    for s in forecast["samples"][:8]:
        ax.plot(yrs_fwd, s, color="tab:blue", alpha=0.10, lw=0.6)

    ax.axvline(0, color="red", ls=":", alpha=0.7, label="mission start")
    colors = {"naive": "tab:orange", "point": "tab:purple",
              "robust": "tab:green", "riskaverse": "tab:cyan"}
    for name, day in strategy_days.items():
        ax.axvline(day / 365.25, color=colors[name], ls="--", lw=1.2,
                   alpha=0.8, label=f"{name} deploy")
    ax.set_xlim(-22, 8)
    ax.set_xlabel("Years (0 = mission start, ~2026)")
    ax.set_ylabel("F10.7 (SFU)")
    ax.set_title("GP forecast trained on 21 yr of NOAA observations")
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(save, dpi=130); plt.close(fig)


def plot_score_landscape(opt, save):
    days = [c["deploy_day"]   for c in opt["candidates"]]
    mean = np.array([c["score_mean"]  for c in opt["candidates"]])
    std  = np.array([c["score_std"]   for c in opt["candidates"]])
    p95  = np.array([c["score_p95"]   for c in opt["candidates"]])
    med  = np.array([c["score_median"] for c in opt["candidates"]])

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.fill_between(days, mean - std, mean + std, alpha=0.2,
                    color="tab:green", label="E[score] +/- 1sigma (across forecasts)")
    ax.plot(days, mean, "o-", color="tab:green", label="E[score] (robust)")
    ax.plot(days, med, "s--", color="tab:purple", label="median-forecast score (point)")
    ax.plot(days, p95, "^:",  color="tab:cyan",   label="95th-pctile score (risk-averse)")

    for name, color in [("naive", "tab:orange"), ("point", "tab:purple"),
                        ("robust", "tab:green"), ("riskaverse", "tab:cyan")]:
        ax.axvline(opt[name]["deploy_day"], color=color, ls=":", alpha=0.6)
    ax.set_xlabel("Sail deployment day from mission start")
    ax.set_ylabel("Score (lower = better)")
    ax.set_title("Score landscape across deployment-day candidates")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(save, dpi=130); plt.close(fig)


def plot_altitudes(scenarios, save):
    fig, ax = plt.subplots(figsize=(10, 5))
    colors = {"Baseline (no sail)": "#888", "Naive": "tab:orange",
              "AI point": "tab:purple", "AI robust": "tab:green",
              "AI risk-averse": "tab:cyan"}
    for s in scenarios:
        t_yr = (s["times_days"] + s["deploy_day"]) / 365.25
        ax.plot(t_yr, s["altitudes_km"], color=colors[s["name"]],
                lw=1.6, label=s["name"])
    ax.axhspan(400, 500, color="red", alpha=0.07,
               label="crowded LEO (400-500 km)")
    ax.axhline(100, color="k", ls="--", alpha=0.5)
    ax.set_ylim(0, 650); ax.set_xlim(0, 25)
    ax.set_xlabel("Years from mission start")
    ax.set_ylabel("Altitude (km)")
    ax.set_title("Altitude trajectories: baseline vs naive vs AI strategies")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(save, dpi=130); plt.close(fig)


def plot_kpis(scenarios, save):
    names = [s["name"] for s in scenarios]
    deorbit = [s["total_years"] for s in scenarios]
    band = [s["band_years"] for s in scenarios]
    x = np.arange(len(names))
    cmap = ["#888", "tab:orange", "tab:purple", "tab:green", "tab:cyan"]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.5))
    a1.bar(x, deorbit, color=cmap)
    a1.set_xticks(x); a1.set_xticklabels(names, fontsize=8, rotation=15)
    a1.set_ylabel("Total time on orbit (years)")
    a1.axhline(ESA_LIMIT_YEARS, color="red", ls="--", alpha=0.6, label="ESA 25-yr")
    a1.axhline(ESA_NEW_LIMIT_YEARS, color="darkred", ls=":", alpha=0.6, label="ESA 5-yr")
    a1.legend(fontsize=8); a1.grid(alpha=0.3, axis="y")
    a1.set_title("Total mission lifetime")

    a2.bar(x, band, color=cmap)
    a2.set_xticks(x); a2.set_xticklabels(names, fontsize=8, rotation=15)
    a2.set_ylabel("Years in 400-500 km band")
    a2.set_title("Crowded-altitude residence")
    a2.grid(alpha=0.3, axis="y")
    fig.tight_layout(); fig.savefig(save, dpi=130); plt.close(fig)


# --- Main -------------------------------------------------------------------
def main():
    print("Step 1: build GP forecast distribution "
          f"(N={N_FORECAST_SAMPLES} samples)...")
    forecast = build_trajectories(max_day=10 * 365,
                                  n_samples=N_FORECAST_SAMPLES, dt_days=15.0)

    print("Step 2: baseline (no sail) decay using median forecast...")
    t_b, h_b = decay_trajectory(ALTITUDE_KM, BASE_AREA, MASS_KG, Cd=CD,
                                max_years=120.0, dt_days=5.0,
                                f107_func=forecast["median_trajectory"])
    baseline = {
        "name": "Baseline (no sail)", "deploy_day": 0,
        "times_days": t_b, "altitudes_km": h_b,
        "decay_years": float(t_b[-1] / 365.25),
        "total_years": float(t_b[-1] / 365.25),
        "band_years": time_in_band(t_b, h_b),
    }
    print(f"  -> {baseline['decay_years']:.1f} yr to reentry")

    print(f"Step 3: optimize across +/- {WINDOW_DAYS//2} day window "
          f"({N_FORECAST_SAMPLES} sims per candidate)...")
    opt = optimize_robust(nominal_day=MISSION_DAYS, forecast_data=forecast,
                          window_days=WINDOW_DAYS, step_days=14,
                          area_m2=SAIL_AREA, mass_kg=MASS_KG, Cd=CD,
                          altitude_km=ALTITUDE_KM)

    # Bundle each strategy as a scenario for plotting / KPIs.
    strategy_scenarios = []
    pretty = {"naive": "Naive", "point": "AI point",
              "robust": "AI robust", "riskaverse": "AI risk-averse"}
    for key in ("naive", "point", "robust", "riskaverse"):
        c = opt[key]
        strategy_scenarios.append({
            "name": pretty[key],
            "deploy_day": int(c["deploy_day"]),
            "times_days": c["times_days"],
            "altitudes_km": c["altitudes_km"],
            "decay_years": c["deorbit_mean"],
            "total_years": c["deorbit_mean"] + c["deploy_day"] / 365.25,
            "band_years": c["band_mean"],
            "score_mean": c["score_mean"],
            "score_std": c["score_std"],
            "score_p95": c["score_p95"],
        })

    all_scenarios = [baseline] + strategy_scenarios

    # KPI table -------------------------------------------------------------
    # ESA Zero Debris (ESSB-ST-U-007 Issue 1, Req. SDM-50, in force 2023-11-01):
    # spacecraft must reenter within 5 years of END-OF-MISSION, not launch.
    print("\n" + "=" * 100)
    print(f"{'Scenario':<22} {'Deploy d':>9} {'Decay yr':>9} "
          f"{'PostEOM yr':>11} {'Band yr':>8} {'E[score]':>9} "
          f"{'p95 score':>10} {'5-yr OK':>9}")
    print("-" * 100)
    for s in all_scenarios:
        # PostEOM = (reentry_day - MISSION_DAYS) / 365.25
        # = (deploy_day - MISSION_DAYS)/365.25 + decay_years
        post_eom = (s["deploy_day"] - MISSION_DAYS) / 365.25 + s["decay_years"]
        post_eom = max(post_eom, s["decay_years"])  # baseline has deploy_day=0
        if s["name"] == "Baseline (no sail)":
            post_eom = max(0.0, s["decay_years"] - MISSION_DAYS / 365.25)
        ok5 = "YES" if post_eom <= ESA_NEW_LIMIT_YEARS else "no"
        score_mean = s.get("score_mean", float("nan"))
        score_p95  = s.get("score_p95", float("nan"))
        print(f"{s['name']:<22} {s['deploy_day']:>9d} "
              f"{s['decay_years']:>9.2f} {post_eom:>11.2f} "
              f"{s['band_years']:>8.3f} "
              f"{score_mean:>9.3f} {score_p95:>10.3f} {ok5:>9}")
    print("=" * 100)
    print("PostEOM yr = years from end-of-mission (day 1096) to reentry.")
    print("ESA Zero Debris (ESSB-ST-U-007 Req. SDM-50): PostEOM <= 5 years.")

    # Plots -----------------------------------------------------------------
    print("\nSaving plots to results/ ...")
    strategy_days = {k: int(opt[k]["deploy_day"]) for k in
                     ("naive", "point", "robust", "riskaverse")}
    plot_forecast(forecast, strategy_days,
                  os.path.join(RESULTS_DIR, "solar_forecast.png"))
    plot_score_landscape(opt,
                  os.path.join(RESULTS_DIR, "score_landscape.png"))
    plot_altitudes(all_scenarios,
                  os.path.join(RESULTS_DIR, "altitude_vs_time.png"))
    plot_kpis(all_scenarios,
                  os.path.join(RESULTS_DIR, "kpi_comparison.png"))
    print("  results/solar_forecast.png")
    print("  results/score_landscape.png")
    print("  results/altitude_vs_time.png")
    print("  results/kpi_comparison.png")
    print("Done.")


if __name__ == "__main__":
    main()

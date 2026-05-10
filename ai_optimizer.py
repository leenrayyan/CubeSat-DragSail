"""
Robust optimization of sail deployment timing under F10.7 forecast
uncertainty.

This is the real "AI" of the project: the GP forecaster from
solar_forecast.py gives us a *distribution* of possible future solar
cycles. We compare three strategies for choosing the sail deployment
day inside a 6-month flexibility window:

    naive          : always deploy at the nominal end-of-mission day.
                     Ignores forecast entirely.

    point_optimal  : grid search assuming the GP median forecast is
                     truth. Smart but overconfident -- if reality
                     deviates from the median, performance can be poor.

    robust         : grid search minimizing the *expected* score across
                     every sampled forecast trajectory. Equivalent to
                     hedging against forecast uncertainty.

The score for one trajectory is

    score = w_total * deorbit_years + w_band * years_in_400_to_500_km

For comparing strategies we report not just the expected score but
also the 95th-percentile (CVaR-style) worst-case score, which is what a
risk-averse mission planner would actually optimize for.
"""

import numpy as np
from orbit_decay import decay_trajectory


def time_in_band(times_days, alts_km, low=400.0, high=500.0):
    """Time (years) the trajectory spent with altitude in [low, high]."""
    in_band = (alts_km >= low) & (alts_km <= high)
    if not np.any(in_band):
        return 0.0
    dt = np.diff(times_days)
    mid = 0.5 * (in_band[:-1].astype(float) + in_band[1:].astype(float))
    return float(np.sum(mid * dt) / 365.25)


def _simulate(deploy_day, f107_traj, area_m2, mass_kg, Cd, altitude_km,
              max_years):
    """One sail-deployed decay sim under a specific F10.7 trajectory."""
    t, h = decay_trajectory(altitude_km, area_m2, mass_kg, Cd=Cd,
                            start_day=deploy_day, max_years=max_years,
                            dt_days=1.0, f107_func=f107_traj)
    return t, h


def evaluate_candidate(deploy_day, trajectories, area_m2=5.0, mass_kg=12.0,
                       Cd=2.2, altitude_km=600.0,
                       w_total=1.0, w_band=2.0, max_years=10.0,
                       eom_day=1096):
    """
    Run the sail-deployed decay sim once per F10.7 trajectory and
    summarize the resulting score distribution.

    The operationally meaningful objective is **post-EOM time** = the
    interval from end-of-mission to reentry, because that's what ESA's
    Zero Debris Charter (ESSB-ST-U-007 Req. SDM-50) regulates. Waiting
    longer than EOM to deploy eats directly into the 5-yr budget, so
    we score on post-EOM time, not raw post-deploy decay time.

        score = w_total * post_eom_years + w_band * band_years
        post_eom_years = max(0, (deploy_day - eom_day)/365.25) + decay_years
    """
    deorbits, post_eoms, bands, scores = [], [], [], []
    median_traj = None
    wait_yr = max(0.0, (deploy_day - eom_day) / 365.25)
    for i, traj in enumerate(trajectories):
        t, h = _simulate(deploy_day, traj, area_m2, mass_kg, Cd,
                         altitude_km, max_years)
        d_yr = float(t[-1] / 365.25)
        b_yr = time_in_band(t, h)
        post_eom = wait_yr + d_yr
        s = w_total * post_eom + w_band * b_yr
        deorbits.append(d_yr); post_eoms.append(post_eom)
        bands.append(b_yr); scores.append(s)
        if i == 0:
            median_traj = (t, h)

    deorbits = np.array(deorbits); post_eoms = np.array(post_eoms)
    bands = np.array(bands); scores = np.array(scores)
    return {
        "deploy_day":   float(deploy_day),
        "deorbit_mean": float(deorbits.mean()),
        "deorbit_std":  float(deorbits.std()),
        "deorbit_p95":  float(np.percentile(deorbits, 95)),
        "post_eom_mean": float(post_eoms.mean()),
        "post_eom_std":  float(post_eoms.std()),
        "post_eom_p95":  float(np.percentile(post_eoms, 95)),
        "band_mean":    float(bands.mean()),
        "band_std":     float(bands.std()),
        "band_p95":     float(np.percentile(bands, 95)),
        "score_mean":   float(scores.mean()),
        "score_std":    float(scores.std()),
        "score_p95":    float(np.percentile(scores, 95)),
        "scores_all":   scores,
        "times_days":   median_traj[0],
        "altitudes_km": median_traj[1],
    }


def evaluate_with_median(deploy_day, median_trajectory, area_m2=5.0,
                         mass_kg=12.0, Cd=2.2, altitude_km=600.0,
                         w_total=1.0, w_band=2.0, max_years=10.0,
                         eom_day=1096):
    """Score a candidate using only the GP MEDIAN forecast (point estimate)."""
    t, h = _simulate(deploy_day, median_trajectory, area_m2, mass_kg,
                     Cd, altitude_km, max_years)
    d_yr = float(t[-1] / 365.25); b_yr = time_in_band(t, h)
    wait_yr = max(0.0, (deploy_day - eom_day) / 365.25)
    post_eom = wait_yr + d_yr
    return {
        "deploy_day":   float(deploy_day),
        "deorbit_years": d_yr,
        "post_eom_years": post_eom,
        "band_years":    b_yr,
        "score":         w_total * post_eom + w_band * b_yr,
        "times_days":    t,
        "altitudes_km":  h,
    }


def optimize_robust(nominal_day, forecast_data, window_days=180,
                    step_days=14, area_m2=5.0, mass_kg=12.0, Cd=2.2,
                    altitude_km=600.0, w_total=1.0, w_band=2.0,
                    max_years=10.0, verbose=False, eom_day=None):
    """
    Run all 3 strategies (naive / point-optimal / robust) over the same
    candidate grid and return them in a single results dict.

    Parameters
    ----------
    nominal_day    : naive deployment day (e.g., 1096 for 3-yr mission)
    forecast_data  : dict from solar_forecast.build_trajectories()
    window_days    : full width of the flexibility window
    step_days      : grid resolution

    Returns
    -------
    dict with:
      candidates : list of evaluate_candidate() dicts (one per day)
      naive      : strategy result at nominal_day
      point      : strategy result at best-under-median day
      robust     : strategy result at best-under-expected-score day
    """
    if eom_day is None:
        eom_day = nominal_day
    half = window_days // 2
    candidate_days = np.arange(nominal_day - half,
                               nominal_day + half + 1, step_days)

    trajectories = forecast_data["trajectories"]
    median_traj = forecast_data["median_trajectory"]

    # 1. Distribution-based evaluation per candidate (expensive: N sims each)
    candidates = []
    for d in candidate_days:
        c = evaluate_candidate(d, trajectories, area_m2, mass_kg, Cd,
                               altitude_km, w_total, w_band, max_years,
                               eom_day=eom_day)
        # Also stash the score under the median forecast for the
        # point-optimal strategy.
        med = evaluate_with_median(d, median_traj, area_m2, mass_kg, Cd,
                                   altitude_km, w_total, w_band, max_years,
                                   eom_day=eom_day)
        c["score_median"] = med["score"]
        c["deorbit_median"] = med["deorbit_years"]
        c["post_eom_median"] = med["post_eom_years"]
        c["band_median"] = med["band_years"]
        candidates.append(c)
        if verbose:
            print(f"  day {int(d):5d}: "
                  f"E[score]={c['score_mean']:.3f} +/- {c['score_std']:.3f}, "
                  f"p95={c['score_p95']:.3f}, "
                  f"median-forecast score={c['score_median']:.3f}")

    # 2. Pick the four strategies' chosen days
    naive_idx     = int(np.argmin([abs(c["deploy_day"] - nominal_day) for c in candidates]))
    point_idx     = int(np.argmin([c["score_median"] for c in candidates]))
    robust_idx    = int(np.argmin([c["score_mean"]   for c in candidates]))
    riskaverse_idx = int(np.argmin([c["score_p95"]   for c in candidates]))

    return {
        "candidates":     candidates,
        "candidate_days": candidate_days,
        "naive":          candidates[naive_idx],
        "point":          candidates[point_idx],   # trusts median forecast
        "robust":         candidates[robust_idx],  # min E[score] over forecast samples
        "riskaverse":     candidates[riskaverse_idx],  # min 95th-percentile (CVaR)
        "weights":        (w_total, w_band),
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import warnings; warnings.filterwarnings("ignore")
    from solar_forecast import build_trajectories

    print("Building 30 forecast trajectories...")
    fc = build_trajectories(max_day=5*365, n_samples=30, dt_days=15.0)

    print("Optimizing across +/- 90 day window (this runs 30 sims per "
          "candidate)...")
    res = optimize_robust(nominal_day=1096, forecast_data=fc,
                          window_days=180, step_days=14, verbose=True)

    print(f"\n--- Strategies on the same problem ---")
    for name in ("naive", "point", "robust", "riskaverse"):
        c = res[name]
        print(f"{name:>11s}: day {int(c['deploy_day']):4d}   "
              f"E[score]={c['score_mean']:.3f}   "
              f"p95(score)={c['score_p95']:.3f}   "
              f"E[deorbit]={c['deorbit_mean']:.2f} yr")

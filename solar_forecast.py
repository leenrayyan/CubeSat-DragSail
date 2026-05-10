"""
Probabilistic F10.7 solar flux forecast.

Approach
--------
We fit a Gaussian Process (GP) regression model to ~21 years of NOAA SWPC
monthly F10.7 observations (Oct 2004 - Apr 2026) covering most of solar
cycles 24 and rising 25. The kernel is

    Constant * ExpSineSquared(period=11 yr) + WhiteKernel

so the model captures the dominant 11-year cycle while letting the
WhiteKernel absorb short-term (rotation-scale) noise.

The GP gives us not just a point forecast but a *posterior distribution*
over future trajectories. We sample N candidate futures from it; the
optimizer in `ai_optimizer.py` then picks the deployment day that
performs best *in expectation across those samples*, instead of trusting
a single forecast (which is the real engineering claim).

Data
----
data/f107_history.csv -- NOAA SWPC observed monthly F10.7
                         (https://services.swpc.noaa.gov/json/solar-cycle/
                          observed-solar-cycle-indices.json)

If the CSV is missing, fetch_and_cache() pulls it once.
"""

import os, csv, json, urllib.request
import numpy as np
from datetime import datetime
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import (
    ConstantKernel, ExpSineSquared, WhiteKernel, RBF
)

# Mission epoch: day 0 of simulation = 2026-05-01 (just after latest data).
MISSION_EPOCH = datetime(2026, 5, 1)
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "f107_history.csv")

NOAA_URL = ("https://services.swpc.noaa.gov/json/solar-cycle/"
            "observed-solar-cycle-indices.json")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def fetch_and_cache(path=DATA_PATH):
    """Pull NOAA monthly F10.7 archive, drop missing values, write CSV."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.loads(urllib.request.urlopen(NOAA_URL, timeout=15).read())
    valid = [(d["time-tag"], d["f10.7"]) for d in raw
             if d.get("f10.7") not in (None, -1, -1.0)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time_tag", "f107"])
        w.writerows(valid)
    return path


def _ensure_data():
    if not os.path.exists(DATA_PATH):
        fetch_and_cache()


def load_history():
    """Return (months_since_epoch, f107) numpy arrays of observed data.

    months_since_epoch is signed: negative = past, 0 = mission start.
    """
    _ensure_data()
    months, vals = [], []
    with open(DATA_PATH) as f:
        next(f)  # header
        for row in csv.reader(f):
            tag, val = row
            yr, mo = (int(x) for x in tag.split("-"))
            d = datetime(yr, mo, 1)
            dm = (d.year - MISSION_EPOCH.year) * 12 + (d.month - MISSION_EPOCH.month)
            months.append(dm)
            vals.append(float(val))
    return np.array(months, dtype=float), np.array(vals, dtype=float)


# ---------------------------------------------------------------------------
# GP forecaster (fit once, cache)
# ---------------------------------------------------------------------------
_GP_CACHE = {}

def _get_gp():
    """Fit (or retrieve cached) GP over historical F10.7."""
    if "gp" in _GP_CACHE:
        return _GP_CACHE["gp"], _GP_CACHE["X"], _GP_CACHE["y"], _GP_CACHE["y_mean"]

    X_months, y = load_history()
    y_mean = float(np.mean(y))
    y_centered = y - y_mean   # GP assumes zero-mean prior; subtract mean

    # Kernel: amplitude * periodic(period=132 months, length_scale=1.0)
    #         + slow trend (RBF) + observation noise (white)
    kernel = (
        ConstantKernel(50.0, (5.0, 5000.0))
        * ExpSineSquared(length_scale=1.5,
                         periodicity=132.0,                # 11 yr in months
                         length_scale_bounds=(0.1, 10.0),
                         periodicity_bounds=(108.0, 156.0))
        + ConstantKernel(20.0, (0.5, 1000.0))
          * RBF(length_scale=60.0, length_scale_bounds=(5.0, 500.0))
        + WhiteKernel(noise_level=20.0, noise_level_bounds=(0.5, 500.0))
    )
    gp = GaussianProcessRegressor(kernel=kernel, normalize_y=False,
                                  n_restarts_optimizer=4,
                                  random_state=0)
    gp.fit(X_months.reshape(-1, 1), y_centered)
    _GP_CACHE.update(gp=gp, X=X_months, y=y, y_mean=y_mean)
    return gp, X_months, y, y_mean


def forecast_distribution(days_ahead, n_samples=30, seed=42):
    """
    Return forecast for the requested mission days.

    Parameters
    ----------
    days_ahead : 1-D array of mission days (>=0).
    n_samples  : number of posterior trajectories to draw.
    seed       : RNG seed for reproducible sampling.

    Returns
    -------
    median      : (D,) median forecast in SFU
    p05, p95    : (D,) 5th and 95th percentile bands
    samples     : (n_samples, D) array of full sampled trajectories
    """
    gp, _, _, y_mean = _get_gp()
    days = np.asarray(days_ahead, dtype=float)
    months = days / (365.25 / 12.0)
    Xq = months.reshape(-1, 1)

    # GP gives mean+cov at the month grid; sample trajectories from that.
    samples_centered = gp.sample_y(Xq, n_samples=n_samples,
                                   random_state=seed)         # shape (D, N)
    samples = samples_centered.T + y_mean                     # (N, D)
    samples = np.clip(samples, 60.0, 250.0)

    median = np.median(samples, axis=0)
    p05 = np.percentile(samples, 5,  axis=0)
    p95 = np.percentile(samples, 95, axis=0)
    return median, p05, p95, samples


# ---------------------------------------------------------------------------
# Per-trajectory callable factory (for use as f107_func in orbit_decay)
# ---------------------------------------------------------------------------
class F107Trajectory:
    """Wrap a sampled F10.7 array as a callable f(day) -> SFU.

    The decay integrator calls this with arbitrary float days; we
    interpolate against the daily-resolution sample.
    """
    def __init__(self, days, values):
        self.days = np.asarray(days, dtype=float)
        self.values = np.asarray(values, dtype=float)

    def __call__(self, day):
        return float(np.interp(day, self.days, self.values,
                               left=self.values[0], right=self.values[-1]))


def build_trajectories(max_day, n_samples=30, seed=42, dt_days=15.0):
    """
    Pre-build N callable F107 trajectories covering [0, max_day].

    We sample on a coarse grid (every dt_days) for GP efficiency, then the
    F107Trajectory class linearly interpolates to whatever day the
    integrator queries.
    """
    grid = np.arange(0, int(max_day) + dt_days, dt_days)
    median, p05, p95, samples = forecast_distribution(
        grid, n_samples=n_samples, seed=seed)
    trajs = [F107Trajectory(grid, samples[i]) for i in range(n_samples)]
    median_traj = F107Trajectory(grid, median)
    return {
        "grid_days": grid,
        "median": median,
        "p05": p05,
        "p95": p95,
        "samples": samples,
        "trajectories": trajs,
        "median_trajectory": median_traj,
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Loading NOAA history...")
    X, y = load_history()
    print(f"  {len(y)} monthly records, range "
          f"{X.min():.0f} to {X.max():.0f} months from epoch")

    print("Fitting GP (this can take 5-15 sec)...")
    gp, _, _, _ = _get_gp()
    print(f"  fitted log-marg-likelihood: "
          f"{gp.log_marginal_likelihood_value_:.1f}")
    print(f"  learned kernel: {gp.kernel_}")

    print("Sampling 30 forecast trajectories over 12 years...")
    out = build_trajectories(max_day=12 * 365, n_samples=30, dt_days=15.0)
    print(f"  median forecast at year 5:  {out['median'][int(5*365/15)]:.0f} SFU")
    print(f"  5-95% band at year 5:       "
          f"[{out['p05'][int(5*365/15)]:.0f}, "
          f"{out['p95'][int(5*365/15)]:.0f}] SFU")

    # Verify trajectory callable works at arbitrary day
    t = out["trajectories"][0]
    print(f"  sample-0 F10.7 at day 1234.5: {t(1234.5):.1f} SFU")

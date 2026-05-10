"""
Orbital decay simulation for a LEO cubesat.

Uses a piecewise-exponential atmosphere (Vallado, "Fundamentals of
Astrodynamics and Applications", 4th ed., Table 8-4) for mean solar
conditions, with an empirical F10.7 multiplier so the same trajectory
can be re-run across the 11-year solar cycle.

Drag model: da/dt = -(Cd*A/m) * rho * v * a   (circular orbit assumption)
which is the standard secular decay derived from energy dissipation
P = -0.5 * rho * v^3 * Cd * A. Eccentricity is assumed ~0 throughout;
this is acceptable for sun-sync LEO where drag circularizes any small e.
"""

import numpy as np

MU_EARTH = 3.986004418e14  # m^3/s^2
R_EARTH = 6378.137e3       # m, equatorial

# Vallado Table 8-4: exponential atmosphere reference values (mean solar).
# (h0_km, rho0_kg_m3, scale_height_km)
_ATMOS_TABLE = [
    (0,    1.225,      7.249),
    (25,   3.899e-2,   6.349),
    (30,   1.774e-2,   6.682),
    (40,   3.972e-3,   7.554),
    (50,   1.057e-3,   8.382),
    (60,   3.206e-4,   7.714),
    (70,   8.770e-5,   6.549),
    (80,   1.905e-5,   5.799),
    (90,   3.396e-6,   5.382),
    (100,  5.297e-7,   5.877),
    (110,  9.661e-8,   7.263),
    (120,  2.438e-8,   9.473),
    (130,  8.484e-9,  12.636),
    (140,  3.845e-9,  16.149),
    (150,  2.070e-9,  22.523),
    (180,  5.464e-10, 29.740),
    (200,  2.789e-10, 37.105),
    (250,  7.248e-11, 45.546),
    (300,  2.418e-11, 53.628),
    (350,  9.518e-12, 53.298),
    (400,  3.725e-12, 58.515),
    (450,  1.585e-12, 60.828),
    (500,  6.967e-13, 63.822),
    (600,  1.454e-13, 71.835),
    (700,  3.614e-14, 88.667),
    (800,  1.170e-14, 124.64),
    (900,  5.245e-15, 181.05),
    (1000, 3.019e-15, 268.00),
]


def density(h_km, F107=140.0):
    """
    Atmospheric density (kg/m^3) at altitude h (km) for solar flux F10.7 (SFU).

    Base table is for mean solar (F10.7 ~ 140). F10.7 modulation uses an
    empirical altitude-weighted factor: at 100 km the atmosphere is
    insensitive to F10.7; by 700 km density swings ~10x between solar
    min (~70 SFU) and solar max (~200 SFU), consistent with NRLMSISE-00.
    """
    h = max(h_km, 0.0)
    # find lower bracket in table
    h0, rho0, H = _ATMOS_TABLE[0]
    for entry in _ATMOS_TABLE:
        if entry[0] <= h:
            h0, rho0, H = entry
        else:
            break
    rho_mean = rho0 * np.exp(-(h - h0) / H)

    # F10.7 modulation: scaling weight grows linearly from 100->700 km.
    # Multiplier = 10^((F107-140)/100 * weight) gives ~10x swing at 700km.
    weight = np.clip((h - 100.0) / 600.0, 0.0, 1.0)
    factor = 10.0 ** ((F107 - 140.0) / 100.0 * weight)
    return rho_mean * factor


def decay_trajectory(altitude_km, area_m2, mass_kg, Cd=2.2,
                     start_day=0.0, max_years=60.0,
                     dt_days=1.0, f107_func=None,
                     stop_altitude_km=100.0):
    """
    Integrate circular-orbit decay until altitude < stop_altitude_km
    or max_years elapses.

    Parameters
    ----------
    altitude_km : initial altitude (km)
    area_m2     : drag cross-section (m^2)
    mass_kg     : spacecraft mass (kg)
    Cd          : drag coefficient (2.2 typical for cubesats, free-molecular)
    start_day   : simulation start time in days (used to index f107_func)
    max_years   : hard cap on integration
    dt_days     : timestep in days. Reduced 100x once altitude < 250 km.
    f107_func   : callable f(day) -> F10.7 SFU. If None, use mean=140.
    stop_altitude_km : terminate when altitude drops below this.

    Returns
    -------
    times_days  : np.ndarray of elapsed days from start_day
    altitudes_km: np.ndarray of altitudes (km)
    """
    if f107_func is None:
        f107_func = lambda d: 140.0

    BC_inv = Cd * area_m2 / mass_kg  # inverse ballistic coefficient (m^2/kg)
    SECONDS_PER_DAY = 86400.0

    a = (R_EARTH + altitude_km * 1e3)  # semi-major axis, m
    t_days = 0.0
    times = [t_days]
    alts = [altitude_km]

    max_days = max_years * 365.25
    MAX_DA_PER_STEP = 2000.0   # m; clamps Euler step near reentry

    while t_days < max_days:
        h_km = (a - R_EARTH) / 1e3
        if h_km < stop_altitude_km:
            break

        F107 = float(f107_func(start_day + t_days))
        rho = density(h_km, F107)
        v = np.sqrt(MU_EARTH / a)
        da_dt = -BC_inv * rho * v * a    # m/s (negative)

        # Adaptive timestep: cap altitude change per step at MAX_DA_PER_STEP
        # so Euler stays stable even when rho explodes near reentry.
        dt_s = dt_days * SECONDS_PER_DAY
        if abs(da_dt) * dt_s > MAX_DA_PER_STEP:
            dt_s = MAX_DA_PER_STEP / abs(da_dt)

        a = a + da_dt * dt_s
        t_days += dt_s / SECONDS_PER_DAY
        times.append(t_days)
        alts.append((a - R_EARTH) / 1e3)

    return np.array(times), np.array(alts)


if __name__ == "__main__":
    # Sanity test: 6U cubesat, no sail, mean solar -> expect decades
    print("=== Baseline cubesat (no sail) at 600 km, mean solar ===")
    t, h = decay_trajectory(altitude_km=600, area_m2=0.06, mass_kg=12,
                            Cd=2.2, max_years=120.0, dt_days=5.0)
    print(f"  Decay to 100 km: {t[-1]/365.25:.1f} years")
    print(f"  Final altitude: {h[-1]:.1f} km after {t[-1]:.0f} days")

    # Repeat with sinusoidal F10.7 (solar cycle averaged)
    f107_avg = lambda d: 140.0 + 70.0 * np.sin(2*np.pi*d/(11*365.25))
    t2, h2 = decay_trajectory(altitude_km=600, area_m2=0.06, mass_kg=12,
                              Cd=2.2, max_years=120.0, dt_days=5.0,
                              f107_func=f107_avg)
    print(f"  With 11-yr solar cycle: {t2[-1]/365.25:.1f} years")

    # Sail deployed: 5 m^2 -> should be a few years
    print("\n=== Sail deployed (5 m^2) at 600 km, mean solar ===")
    t3, h3 = decay_trajectory(altitude_km=600, area_m2=5.0, mass_kg=12,
                              Cd=2.2, max_years=20.0, dt_days=1.0)
    print(f"  Decay to 100 km: {t3[-1]/365.25:.2f} years")

    # Density spot checks
    print("\n=== Density spot checks (kg/m^3) ===")
    for h in [400, 500, 600, 700]:
        r_lo = density(h, 70)
        r_md = density(h, 140)
        r_hi = density(h, 200)
        print(f"  {h} km:  F107=70 {r_lo:.2e}   "
              f"F107=140 {r_md:.2e}   F107=200 {r_hi:.2e}   "
              f"(hi/lo = {r_hi/r_lo:.1f}x)")

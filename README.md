# AnaMALY · AI-Driven Atmospheric Drag Sail

Hackathon submission. A 6U CubeSat at 600 km sun-synchronous orbit deorbits
via a thin-film drag sail. The deployment moment is chosen by an onboard
**AI Inference IC** running a policy distilled from a ground-trained
**Gaussian Process** forecaster on 21 years of real NOAA F10.7 solar data.

## Reproduce key results in ~60 seconds

```bash
pip install -r requirements.txt
python reproduce.py
```

This runs the full pipeline end-to-end:
1. Pulls NOAA SWPC F10.7 archive (or uses cached `data/f107_history.csv`)
2. Fits the Gaussian Process forecaster
3. Runs robust optimization across 30 forecast samples
4. Distills the deployment policy to portable C (`flight/policy.c`, ~970 B)
5. Exports `web/data.json` for the dashboard
6. Saves the 4 figures referenced in the concept document to `results/`
7. Prints the headline KPI table

Total runtime: ~30–60 s. After that, the interactive dashboard:

```bash
cd web && python -m http.server 8000
# open http://localhost:8000/index.html
```

## Repository layout

| File | Purpose |
| --- | --- |
| `orbit_decay.py` | Circular-orbit drag-decay integrator + exponential atmosphere with F10.7 modulation (Vallado Table 8-4 base, calibrated to NRLMSIS-2.1). |
| `solar_forecast.py` | NOAA SWPC fetch + GP forecaster (sklearn periodic + RBF + white kernel). Cached in `data/f107_history.csv`. |
| `ai_optimizer.py` | Robust optimization across forecast samples. 4 strategies: naive, point, robust (E[score]), risk-averse (p95). |
| `flight_policy.py` | Distills the optimizer into a depth-5 decision tree, exports portable C via m2cgen. Output in `flight/`. |
| `main_sim.py` | One-shot CLI: KPI table + 4 PNG plots into `results/`. |
| `export_web_data.py` | Writes `web/data.json` for the static dashboard. |
| `reproduce.py` | Runs everything in order; the single command judges should use. |
| `sim.ipynb` | Interactive notebook for parameter exploration. |
| `web/index.html` | Static dashboard with 3D orbit visualization (Three.js). |
| `AnaMALY_AIDragSail_ConceptDocument.docx` | Full concept document. |
| `AnaMALY_AIDragSail_ConceptDocument_2pg.docx` | 2-page submission version. |

## Key assumptions (verifiable in code)

- **Orbit:** circular, 600 km altitude, sun-synchronous (i = 97.8°). Eccentricity ignored — drag circularises any small e in LEO.
- **Mass / area:** 8 kg, 0.03 m² stowed, 0.40 m² deployed (≈13× ratio).
- **Drag coefficient:** Cd = 2.2 (Moe & Moe 2005).
- **Atmosphere:** Vallado piecewise-exponential profile with empirical F10.7 multiplier producing ~10× density swing at 600 km between F10.7 = 70 and 200 SFU.
- **Forecast horizon:** 12 years past mission start (May 2026).
- **Optimization window:** ±6 months around end-of-mission (day 1096).
- **Forecast samples:** 30 trajectories from the GP posterior.

All assumptions are at the top of `main_sim.py` / `export_web_data.py` and can be edited in one place.

## Headline numbers (current `data.json`)

| Scenario | Post-EOM (yr) | p95 worst (yr) | ESA 5-yr |
|---|---:|---:|:---:|
| Baseline (no sail) | ~37 | — | FAIL |
| Naive (deploy at EOM) | 4.77 | 5.30 | PASS |
| AI robust | 4.77 | 5.30 | PASS (bounded) |

The AI strategies match the naive expected value but additionally provide a quantified worst-case bound under solar uncertainty — that bound only exists because the underlying model is probabilistic.

## AI usage disclosure

This codebase was authored with assistance from Anthropic Claude (Opus 4.7). All physical equations, constants, atmosphere-table values, and the GP kernel structure were specified by the human author from cited references; the model produced the implementation, validation harness, plotting, and prose. The "AI" claimed in the project itself is the trained Gaussian Process forecaster (real ML on real data) plus the robust optimization framework that uses its uncertainty quantification, distilled into a portable C decision tree for on-orbit inference. The grid search over deployment days is brute force — appropriately, since the decision variable is one-dimensional.

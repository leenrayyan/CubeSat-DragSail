# AnaMALY · AI-Driven Atmospheric Drag Sail

A 6U CubeSat at 600 km that deorbits using a drag sail, with the deployment moment chosen by an onboard AI Inference IC. The policy is trained on the ground from 21 years of real NOAA F10.7 solar-flux observations and distilled into ~1 KB of portable C.

## Live dashboard

**https://leenrayyan.github.io/CubeSat-DragSail/**

## Concept site

**https://ayhamshaloudi90-bit.github.io/CubeSat-site/**

The team's original concept site — mission overview, lifecycle, mini-game. Predates the AI simulation work in this repository.

## Run it

```bash
pip install -r requirements.txt
python reproduce.py
```

Runs in about a minute. Regenerates the simulation outputs, plots, distilled C policy, and the dashboard's `data.json`.

To view the dashboard locally:

```bash
cd docs
python -m http.server 8000
# then open http://localhost:8000
```

## What's in here

| Path | What it is |
| --- | --- |
| `orbit_decay.py` | Drag-decay integrator with an exponential atmosphere modulated by F10.7. |
| `solar_forecast.py` | Pulls NOAA F10.7 history, fits a Gaussian Process forecaster. |
| `ai_optimizer.py` | Searches for the best sail-deployment day across 30 sampled futures. |
| `flight_policy.py` | Distills the optimizer output into a small decision tree, exports portable C. |
| `main_sim.py` | Runs the simulation and saves four PNG plots to `results/`. |
| `export_web_data.py` | Writes `docs/data.json` for the dashboard. |
| `reproduce.py` | Runs everything in order. |
| `sim.ipynb` | Notebook for exploring parameters. |
| `docs/` | Static dashboard served by GitHub Pages. |
| `flight/policy.c` | The auto-generated 970-byte deployment policy. |
| `data/f107_history.csv` | Cached NOAA data so things work offline. |
| `results/` | Generated plots. |

## Key parameters

Edit the constants at the top of `main_sim.py` and `export_web_data.py`:

- 600 km circular sun-synchronous orbit
- 8 kg, 0.03 m² stowed, 0.40 m² deployed sail
- 3-year operational mission, ±6-month deployment flexibility
- 30 Gaussian Process forecast samples

## AI disclosure

The Python simulation code was written with help from Anthropic Claude. The hardware design, block diagram, concept document, presentation, and the original concept site are the team's work.

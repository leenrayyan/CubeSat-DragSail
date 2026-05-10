"""
Flight-segment deployment policy: distilled from the ground-segment
optimizer into a tiny model deployable to the AI Inference IC.

WHY THIS LAYER EXISTS
---------------------
The ORION-1 hardware design (see architecture diagram) places a dedicated
"AI Inference IC" inside the De-Orbiting Subsystem, governed by a
Subsystem MCU with ECC and a watchdog. That IC is power- and
memory-constrained; it cannot run our 30-sample GP Monte Carlo each
time it wants a deploy/wait decision. Instead, the heavy
forecasting + robust optimization happens once on the GROUND, and the
resulting policy is distilled into a small decision tree that fits in
a few KB and answers a single question fast:

    Given (mission_day, current_F10.7, altitude_km, days_since_eom),
    should we DEPLOY the sail right now, or WAIT another orbit?

This split mirrors real spacecraft autonomy practice (Furano et al.,
"Towards the Use of Artificial Intelligence on the Edge in Space
Systems," IEEE A&E Systems Mag 2020, doi:10.1109/MAES.2020.3008468)
and matches what flew on Phi-Sat-1 (Intel Movidius Myriad 2) and
OPS-SAT-1.

We export the trained tree to portable C using m2cgen (BayesWatch,
https://github.com/BayesWitnesses/m2cgen) so it can be cross-compiled
for whatever MCU/IC the integrator picks.
"""

import warnings; warnings.filterwarnings("ignore")
import os, json
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import accuracy_score

from solar_forecast import build_trajectories, _get_gp
from ai_optimizer import optimize_robust


# ---------------------------------------------------------------------------
# 1. Generate training data by querying the ground-segment optimizer
# ---------------------------------------------------------------------------
def generate_training_set(n_scenarios=20, area_m2=0.40, mass_kg=8.0,
                          eom_day=1096, window_days=360):
    """
    For N synthetic mission scenarios, run the ground optimizer to find
    the optimal deployment day, then build labelled examples.

    Each (mission_day, F10.7, altitude, days_since_eom) tuple inside the
    feasible window gets labelled DEPLOY (1) if it's within +/-15 days of
    that scenario's optimum, else WAIT (0).
    """
    print(f"Generating training set from {n_scenarios} scenarios...")
    rows = []
    fc = build_trajectories(max_day=10*365, n_samples=20, dt_days=15.0,
                            seed=42)
    for s in range(n_scenarios):
        # Vary the optimization seed and altitude perturbation for variety.
        fc_s = build_trajectories(max_day=10*365, n_samples=20, dt_days=15.0,
                                  seed=42 + s)
        for alt0 in [580, 600, 620]:
            opt = optimize_robust(eom_day, fc_s, window_days=window_days,
                                  step_days=14, area_m2=area_m2,
                                  mass_kg=mass_kg, altitude_km=alt0,
                                  max_years=10.0)
            star_day = opt["robust"]["deploy_day"]
            for cand in opt["candidates"]:
                day = cand["deploy_day"]
                # Use median-trajectory F10.7 at this day as the "current"
                # observation the IC would see.
                f107_now = float(fc_s["median_trajectory"](day))
                f107_30d = float(fc_s["median_trajectory"](day + 30))
                f107_slope = (f107_30d - f107_now) / 30.0  # SFU / day
                label = 1 if abs(day - star_day) <= 14 else 0
                rows.append([day, f107_now, f107_slope, alt0,
                             day - eom_day, label])
    X = np.array([r[:-1] for r in rows])
    y = np.array([r[-1] for r in rows], dtype=int)
    feature_names = ["mission_day", "f107_now", "f107_slope_30d",
                     "altitude_km", "days_since_eom"]
    return X, y, feature_names


# ---------------------------------------------------------------------------
# 2. Fit a small tree and export to C
# ---------------------------------------------------------------------------
def distill_policy(X, y, feature_names, max_depth=5, out_dir="flight"):
    """
    Train a depth-limited DecisionTreeClassifier and dump portable C code.

    A depth-5 tree has at most 32 leaves and ~31 split nodes. Distilled to
    C with m2cgen, the binary fits comfortably under 5 KB on any modern
    MCU/IC -- well inside what an inference accelerator can hold.
    """
    os.makedirs(out_dir, exist_ok=True)

    tree = DecisionTreeClassifier(
        max_depth=max_depth, min_samples_leaf=10, random_state=0,
        class_weight="balanced",  # DEPLOY is rare relative to WAIT
    )
    tree.fit(X, y)
    pred = tree.predict(X)
    acc = accuracy_score(y, pred)
    print(f"  trained tree: depth<={max_depth}, "
          f"{tree.get_n_leaves()} leaves, "
          f"train accuracy {acc:.3f}")

    # Export to C (m2cgen produces a single function `score(double *input)`
    # returning a 2-element class-score array -- the IC picks argmax).
    try:
        import m2cgen as m2c
        c_code = m2c.export_to_c(tree, function_name="orion_deploy_policy")
        c_path = os.path.join(out_dir, "policy.c")
        with open(c_path, "w") as f:
            f.write("/* AUTO-GENERATED by flight_policy.py via m2cgen.\n")
            f.write(" * Inputs (5 doubles, in order):\n")
            for i, fn in enumerate(feature_names):
                f.write(f" *   input[{i}] = {fn}\n")
            f.write(" * Output: 2-element array [WAIT_score, DEPLOY_score].\n")
            f.write(" * IC fires burn wire if DEPLOY_score > WAIT_score.\n")
            f.write(" */\n\n")
            f.write(c_code)
        print(f"  wrote {c_path} ({os.path.getsize(c_path)} bytes)")
    except ImportError:
        print("  WARNING: m2cgen not available; skipping C export")
        c_path = None

    # Also dump a JSON description for the website to display.
    desc = {
        "model_type": "DecisionTreeClassifier",
        "max_depth": max_depth,
        "n_leaves": int(tree.get_n_leaves()),
        "n_nodes": int(tree.tree_.node_count),
        "n_features": len(feature_names),
        "feature_names": feature_names,
        "training_samples": int(len(y)),
        "training_accuracy": float(acc),
        "c_file_bytes": (os.path.getsize(c_path) if c_path else None),
        "exported_to_c": c_path is not None,
    }
    json_path = os.path.join(out_dir, "policy_meta.json")
    with open(json_path, "w") as f:
        json.dump(desc, f, indent=2)
    print(f"  wrote {json_path}")

    return tree, desc


# ---------------------------------------------------------------------------
# 3. Demo: end-to-end distillation
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    X, y, fnames = generate_training_set(n_scenarios=8)
    print(f"\nGot {len(y)} training samples "
          f"({y.sum()} DEPLOY, {(y==0).sum()} WAIT)")

    print("\nDistilling policy...")
    tree, meta = distill_policy(X, y, fnames, max_depth=5)

    print("\n=== Flight Policy Summary ===")
    for k, v in meta.items():
        print(f"  {k:>20s}: {v}")
    print()
    print("This C file would be cross-compiled for the AI Inference IC and")
    print("called by the Subsystem MCU each orbit (~90 min). Inference is a")
    print("handful of floating-point comparisons -- microseconds even on a")
    print("Cortex-M4-class part.")

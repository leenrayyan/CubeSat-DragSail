"""
One-shot reproduction script for judges.

Runs the full pipeline in order and reports timing for each stage.
After this completes, web/data.json and results/*.png are populated and
the dashboard at web/index.html will show the latest numbers.

    python reproduce.py            # default: skip distillation if cached
    python reproduce.py --full     # force re-distill the flight policy (~7 min)
"""
import os, sys, time, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
POLICY_C = os.path.join(ROOT, "flight", "policy.c")
FORCE_FULL = "--full" in sys.argv


def run(label, script):
    print(f"\n=== {label} ===")
    t0 = time.time()
    # Force UTF-8 stdout in subprocess so Windows codepage can't kill the run
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, script], cwd=ROOT, env=env)
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"  FAILED ({dt:.1f}s) — exit code {r.returncode}")
        sys.exit(r.returncode)
    print(f"  done ({dt:.1f}s)")


def main():
    print("AnaMALY · reproducing simulation results")
    print("(use --full to also re-run the 7-minute policy distillation)")

    # Stage 1: distill flight policy. Skip if already present unless --full.
    if FORCE_FULL or not os.path.exists(POLICY_C):
        run("Distill flight policy (970 B C, ~7 min)", "flight_policy.py")
    else:
        print(f"\n=== Distill flight policy — SKIPPED ===")
        print(f"  using cached {POLICY_C} ({os.path.getsize(POLICY_C)} B)")
        print("  re-run with `python reproduce.py --full` to regenerate")

    # Stage 2: main simulation + KPI table + 4 figures
    run("Run main simulation + KPI table + figures", "main_sim.py")

    # Stage 3: export dashboard JSON
    run("Export dashboard JSON", "export_web_data.py")

    print("\nAll stages complete.")
    print("  results/         — 4 PNG plots")
    print("  flight/policy.c  — distilled embedded C")
    print("  web/data.json    — dashboard data")
    print("\nServe the dashboard:")
    print("  cd web && python -m http.server 8000")
    print("  open http://localhost:8000/")


if __name__ == "__main__":
    main()

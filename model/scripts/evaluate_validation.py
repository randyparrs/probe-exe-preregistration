"""Validation of the prediction model (d): pre-registered predictions vs measured windows.

    py -3.12 scripts/evaluate_validation.py --windows v2,v3,v4,v5,v6 [--prereg-dir DIR] [--internal]

Fixed before any evaluated window was run (design document FASE1-DISENO.md, decisions 11.5 to 11.9).
For every window w and contract c it reads the pre-registered file window-<w>.json and measures the
window with the frozen model code (METRICAS v5 first-attempt acceptance, valid rows, execution
re-read applied):

1. Criterion, judged on the primary model (variant C) only. Both conditions are needed:
   a. the predicted first-attempt rate lies inside the 95% Clopper-Pearson interval of the measured
      rate in at least 80% of the contract-window pairs (12 of 15 with 5 windows x 3 contracts);
   b. the mean absolute error of C is lower than that of the naive prediction stored in the same
      file (mean first-attempt rate of the earlier windows).
   Reported with all windows and without the windows that crossed an epoch change (an epoch change
   between the first and the last tx sent, or seen by any tx of the window).
2. Same numbers for the secondary model (variant B) and any other prediction in the files
   (reference only, not judged).
3. Leader timeout at the first attempt: predicted vs measured, per pair.
4. Calibration: pairs grouped by predicted first-attempt rate in 10-point bins; mean predicted vs
   pooled measured rate with its interval.
5. --internal: operator health window against window (TIMEOUT, DV, leader timeouts), for the
   internal report only (decision 11.3).
Windows without data yet are listed as pending.
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.stats import cp_lower, cp_upper  # noqa: E402
from scripts.network_report import OUT, load, tx_epochs  # noqa: E402
from scripts.predict_model import fit, measured  # noqa: E402

CONTRACTS = ("wizard", "company", "dvB")
SHARE = 0.8


def crossed_epoch(tag):
    """True when the window saw an epoch change (eligibility log of the run, or any tx)."""
    f = OUT / "network-eligibility.jsonl"
    if f.exists():
        for line in open(f, encoding="utf-8"):
            e = json.loads(line)
            if e.get("campaign") == tag and e.get("epochChanged"):
                return True
    epochs = set()
    for c in CONTRACTS:
        rows, _ = load(c, tag)
        for r in rows:
            epochs |= tx_epochs(r)
    return len(epochs) > 1


def has_data(tag):
    return all((OUT / f"campaign-{c}-{tag}.jsonl").exists() for c in CONTRACTS)


def pairs_for(windows, prereg_dir):
    pairs, pending = [], []
    for w in windows:
        pf = prereg_dir / f"window-{w}.json"
        if not pf.exists():
            pending.append(f"{w} (no pre-registration file)")
            continue
        if not has_data(w):
            pending.append(f"{w} (no measured data yet)")
            continue
        doc = json.load(open(pf, encoding="utf-8"))
        cross = crossed_epoch(w)
        for c in doc["predictions"]:
            m = measured(w, c)
            p = doc["predictions"][c]
            pairs.append({"window": w, "contract": c, "crossedEpoch": cross, "n": m["n"],
                          "first": m["firstAttempt"], "leaderTimeout": m["leaderTimeoutFirstAttempt"],
                          "predictions": {k: v for k, v in p.items() if isinstance(v, dict)},
                          "naive": p.get("naiveFirstAttempt")})
    return pairs, pending


def judge(pairs, key, label):
    """Criterion 1 for the prediction `key` ('primary', 'secondary', ...)."""
    rows = [x for x in pairs if key in x["predictions"]]
    if not rows:
        return None
    inside, err, err_naive = 0, [], []
    for x in rows:
        pred = x["predictions"][key]["firstAttempt"]
        lo, hi = cp_lower(x["first"], x["n"]), cp_upper(x["first"], x["n"])
        inside += lo <= pred <= hi
        rate = x["first"] / x["n"]
        err.append(abs(pred - rate))
        if x["naive"] is not None:
            err_naive.append(abs(x["naive"] - rate))
    need = math.ceil(SHARE * len(rows))
    mae = sum(err) / len(err)
    mae_naive = sum(err_naive) / len(err_naive) if len(err_naive) == len(rows) else None
    ok = inside >= need and mae_naive is not None and mae < mae_naive
    print(f"  {label}: inside the interval {inside}/{len(rows)} (needs {need}); MAE {100 * mae:.1f} points "
          f"vs naive {100 * mae_naive:.1f} points" if mae_naive is not None else
          f"  {label}: inside the interval {inside}/{len(rows)} (needs {need}); MAE {100 * mae:.1f} points; naive missing")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="v2,v3,v4,v5,v6")
    ap.add_argument("--prereg-dir", default=str(ROOT.parent / "probe-exe-preregistration"),
                    help="folder with window-<w>.json (the pre-registration repository)")
    ap.add_argument("--internal", action="store_true", help="add operator health window against window")
    args = ap.parse_args()
    windows = [w for w in args.windows.split(",") if w]
    pairs, pending = pairs_for(windows, Path(args.prereg_dir))
    if pending:
        print(f"pending: {', '.join(pending)}")
    if not pairs:
        print("no evaluated pairs yet")
        return

    print("\n== pairs (first-attempt acceptance, METRICAS v5)")
    for x in pairs:
        lo, hi = cp_lower(x["first"], x["n"]), cp_upper(x["first"], x["n"])
        preds = "  ".join(f"{k} {100 * v['firstAttempt']:.1f}" for k, v in x["predictions"].items())
        c = x["predictions"].get("primary", {}).get("firstAttempt")
        mark = "" if c is None else ("inside" if lo <= c <= hi else "OUTSIDE")
        print(f"  {x['window']:4s} {x['contract']:8s} measured {x['first']}/{x['n']} = {100 * x['first'] / x['n']:5.1f} % "
              f"(CP {100 * lo:.1f}-{100 * hi:.1f})  {preds}  naive {100 * x['naive']:.1f}  {mark}"
              f"{'  [epoch change]' if x['crossedEpoch'] else ''}")

    for title, subset in (("all windows", pairs),
                          ("without windows that crossed an epoch change", [x for x in pairs if not x["crossedEpoch"]])):
        print(f"\n== criterion, {title} ({len(subset)} pairs)")
        if not subset:
            print("  no pairs")
            continue
        verdict = judge(subset, "primary", "primary (C, judged)")
        for key in sorted({k for x in subset for k in x["predictions"]} - {"primary"}):
            judge(subset, key, f"{key} (reference, not judged)")
        print(f"  verdict for the primary model: {'PASS' if verdict else 'FAIL'}"
              + ("" if not pending else " (provisional: windows pending)"))

    print("\n== leader timeout at the first attempt, primary model")
    for x in pairs:
        p = x["predictions"].get("primary", {}).get("leaderTimeoutFirstAttempt")
        if p is None:
            continue
        lo, hi = cp_lower(x["leaderTimeout"], x["n"]), cp_upper(x["leaderTimeout"], x["n"])
        print(f"  {x['window']:4s} {x['contract']:8s} predicted {100 * p:5.1f} %  measured {x['leaderTimeout']}/{x['n']} "
              f"(CP {100 * lo:.1f}-{100 * hi:.1f}) {'inside' if lo <= p <= hi else 'OUTSIDE'}")

    print("\n== calibration, primary model (10-point bins of the prediction)")
    bins = defaultdict(list)
    for x in pairs:
        p = x["predictions"].get("primary", {}).get("firstAttempt")
        if p is not None:
            bins[min(9, int(p * 10))].append((p, x))
    for b in sorted(bins):
        grp = bins[b]
        mean_p = sum(p for p, _ in grp) / len(grp)
        k, n = sum(x["first"] for _, x in grp), sum(x["n"] for _, x in grp)
        print(f"  {10 * b:3d}-{10 * b + 10:3d} %: {len(grp)} pairs, mean predicted {100 * mean_p:.1f} %, measured "
              f"{k}/{n} = {100 * k / n:.1f} % (CP {100 * cp_lower(k, n):.1f}-{100 * cp_upper(k, n):.1f})")

    if args.internal:
        print("\n== operator health window against window (internal)")
        health = {w: fit(w, list(CONTRACTS), "C")["operators"] for w in sorted({x["window"] for x in pairs})}
        ops = sorted({a for h in health.values() for a in h})
        for a in ops:
            cells = []
            for w, h in health.items():
                cnt = h.get(a, {}).get("counts", {})
                cells.append(f"{w}: TO {cnt.get('to', 0)}/{cnt.get('n', 0)} DV {cnt.get('dv', 0)} "
                             f"lead {cnt.get('lfail', 0)}/{cnt.get('lead', 0)}")
            print(f"  {a[:10]}  " + " | ".join(cells))


if __name__ == "__main__":
    main()

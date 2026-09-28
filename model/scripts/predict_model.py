"""Prediction model (c), design document (FASE1-DISENO.md) section 6 and decisions 11.5 to 11.7.

    py -3.12 scripts/predict_model.py --fit v1 [--variant A|B|C] [--snapshot-campaign v1 --snapshot-label run-start]
    py -3.12 scripts/predict_model.py --fit v1 --check v1      (in-sample check of the structure)
    py -3.12 scripts/predict_model.py --fit v1 --prereg v2 --snapshot-campaign prereg-v2 --snapshot-label prereg
        writes the public pre-registration file of window v2 (English, no per-operator rates) to
        results/prereg/window-v2.json; variant C is primary, B secondary (decision 11.7)

Parameters come only from the fit window k (chained prediction, 11.5.1):
- per operator, LLM contracts pooled (11.6.5): t_i = TIMEOUT / validator votes; d_i = DV / non-TIMEOUT
  validator votes; l_i = leader timeouts / attempts led. Each count gets 3 pseudo-votes at the rate
  of the other operators (11.6.4); an operator absent from window k gets the rate of all of them.
- per contract (11.6.3): q_c = DISAGREE / validator votes that are neither TIMEOUT nor DV.
- variant B: d_i scaled per contract by d_c / d_all (DV rate of the contract over the pooled rate).
- variant C: B, and "DV type 2" (all validators against the leader) is an attempt-level event with
  probability p2_c per contract instead of independent DV votes.
Committees: leader and 4 validators drawn without replacement with probability proportional to
weight = (0.6 self + 0.4 delegated) ^ 0.5 over the eligible set of a network snapshot (hypothesis
checked in module b). Acceptance: leader does not time out and AGREE (leader included) >= 3 of 5.
Later attempts redraw the whole committee (approximation: the chain keeps the validators).
Monte Carlo with a fixed seed.
"""

import argparse
import hashlib
import json
import platform
import random
import sys
from datetime import datetime, timezone
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.stats import cp_lower, cp_upper  # noqa: E402
from scripts.campaign_report import attempts_v5  # noqa: E402
from scripts.network_report import OUT, attempt_views, load  # noqa: E402

MODEL_VERSION = "c-1"
PSEUDO = 3
SEED = 20260928
SAMPLES = 200_000
MAX_ATTEMPTS = 4  # first attempt and up to 3 rotations


def is_dv(label):
    return bool(label) and label.startswith("DV")


def is_type2(label):
    return bool(label) and label.startswith("DV type 2")


def attempt_records(row):
    """(leader, leader_failed, [(validator, label)], type2) per attempt; leader seat excluded."""
    out = []
    for committee, leader, labels, lto in attempt_views(row) or []:
        votes = []
        if committee and labels:
            votes = [(a, lab) for a, lab in zip(committee, labels) if a and lab and a != leader]
        out.append((leader, lto, votes, any(is_type2(lab) for _, lab in votes)))
    return out


def fit(tag, contracts, variant):
    """Parameters of window `tag` (counts and shrunk rates)."""
    op = defaultdict(Counter)       # per operator: n, to, dv, lead, lfail
    ct = defaultdict(Counter)       # per contract: n, to, dv, dis, att, t2
    for c in contracts:
        rows, _ = load(c, tag)
        for r in rows:
            for leader, lto, votes, t2 in attempt_records(r):
                if leader:
                    op[leader]["lead"] += 1
                    op[leader]["lfail"] += lto
                if lto:
                    continue
                ct[c]["att"] += 1
                if variant == "C" and t2:
                    ct[c]["t2"] += 1
                    continue  # the whole attempt is one event, not independent DV votes
                for a, lab in votes:
                    op[a]["n"] += 1
                    ct[c]["n"] += 1
                    if lab == "TIMEOUT":
                        op[a]["to"] += 1
                        ct[c]["to"] += 1
                    elif is_dv(lab):
                        op[a]["dv"] += 1
                        ct[c]["dv"] += 1
                    elif lab == "DISAGREE":
                        ct[c]["dis"] += 1

    def shrunk(x, n, X, N):
        rest = (X - x) / (N - n) if N > n else 0.0
        return (x + PSEUDO * rest) / (n + PSEUDO)

    tot = Counter()
    for v in op.values():
        tot.update(v)
    n_nt = {a: v["n"] - v["to"] for a, v in op.items()}
    N_nt = tot["n"] - tot["to"]
    ops = {}
    for a, v in op.items():
        ops[a] = {"t": shrunk(v["to"], v["n"], tot["to"], tot["n"]),
                  "d": shrunk(v["dv"], n_nt[a], tot["dv"], N_nt),
                  "l": shrunk(v["lfail"], v["lead"], tot["lfail"], tot["lead"]),
                  "counts": dict(v)}
    absent = {"t": tot["to"] / tot["n"] if tot["n"] else 0.0,
              "d": tot["dv"] / N_nt if N_nt else 0.0,
              "l": tot["lfail"] / tot["lead"] if tot["lead"] else 0.0}
    d_all = absent["d"]
    per_contract = {}
    for c in contracts:
        v = ct[c]
        clean = v["n"] - v["to"] - v["dv"]
        d_c = v["dv"] / (v["n"] - v["to"]) if v["n"] > v["to"] else 0.0
        per_contract[c] = {"q": v["dis"] / clean if clean else 0.0,
                           "dvScale": (d_c / d_all) if (variant in ("B", "C") and d_all) else 1.0,
                           "p2": v["t2"] / v["att"] if (variant == "C" and v["att"]) else 0.0,
                           "counts": dict(v)}
    return {"window": tag, "variant": variant, "operators": ops, "absent": absent, "contracts": per_contract}


def snapshot(campaign, label):
    snaps = [json.loads(line) for line in open(OUT / "network-snapshots.jsonl", encoding="utf-8")]
    snaps = [s for s in snaps if s.get("campaign") == campaign and s["label"] == label]
    if not snaps:
        raise SystemExit(f"no snapshot campaign={campaign!r} label={label!r}")
    s = snaps[-1]
    elig = [(v["address"], v["weight"]) for v in s["validators"]
            if not v.get("banned") and not v.get("quarantined") and not v.get("error")]
    return s, elig


def draw(rng, elig, k):
    pool = list(elig)
    out = []
    for _ in range(k):
        total = sum(w for _, w in pool)
        x = rng.random() * total
        for j, (a, w) in enumerate(pool):
            x -= w
            if x <= 0:
                out.append(a)
                pool.pop(j)
                break
        else:
            out.append(pool.pop()[0])
    return out


def predict(params, elig, contract, samples=SAMPLES, seed=SEED):
    rng = random.Random(f"{seed}-{contract}")
    ops, absent, pc = params["operators"], params["absent"], params["contracts"][contract]
    rate = lambda a, k: ops[a][k] if a in ops else absent[k]  # noqa: E731
    first = within = leader_fail = 0
    for _ in range(samples):
        for att in range(MAX_ATTEMPTS):
            leader, *vals = draw(rng, elig, 5)
            if rng.random() < rate(leader, "l"):
                ok = False
                if att == 0:
                    leader_fail += 1
            elif rng.random() < pc["p2"]:
                ok = False
            else:
                agree = 1
                for a in vals:
                    t, d = rate(a, "t"), min(1.0, rate(a, "d") * pc["dvScale"])
                    x = rng.random()
                    if x < t:
                        continue
                    if rng.random() < d:
                        continue
                    if rng.random() < pc["q"]:
                        continue
                    agree += 1
                ok = agree >= 3
            if ok:
                first += att == 0
                within += 1
                break
    return {"firstAttempt": first / samples, "withinRotations": within / samples,
            "leaderTimeoutFirstAttempt": leader_fail / samples}


def measured(tag, contract):
    rows, _ = load(contract, tag)
    n = len(rows)
    first = sum(1 for r in rows if r["outcome"] == "accepted" and not attempts_v5(r))  # METRICAS.md v5
    lto = 0
    for r in rows:
        recs = attempt_records(r)
        lto += bool(recs) and recs[0][1]
    return {"n": n, "firstAttempt": first, "leaderTimeoutFirstAttempt": lto}


CODE_FILES = ["scripts/predict_model.py", "scripts/network_report.py", "scripts/campaign_report.py",
              "scripts/final_report.py", "harness/stats.py"]


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prereg(fit_tags, window, contracts, snap, elig, samples):
    """Public pre-registration file (decision 11.7): predictions per contract, naive prediction,
    model version, network snapshot, seed, samples and SHA-256 of fit data and code. No
    per-operator rates (11.3)."""
    fit_tag = fit_tags[-1]
    preds = {}
    for variant in ("C", "B"):
        params = fit(fit_tag, contracts, variant)
        for c in contracts:
            preds.setdefault(c, {})[variant] = predict(params, elig, c, samples=samples)
    naive = {}
    for c in contracts:
        rates = []
        for t in fit_tags:
            m = measured(t, c)
            rates.append(m["firstAttempt"] / m["n"])
        naive[c] = sum(rates) / len(rates)
    data_files = []
    for t in fit_tags:
        for c in contracts:
            data_files.append(f"results/bradbury/campaign-{c}-{t}.jsonl")
        if (OUT / f"execution-recheck-{t}.jsonl").exists():
            data_files.append(f"results/bradbury/execution-recheck-{t}.jsonl")
    snap_row = json.dumps(snap, sort_keys=True, separators=(",", ":"))
    return {
        "window": window,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": "GenLayer testnet Bradbury (chain 4221)",
        "metric": ("first-attempt acceptance, METRICAS v5: the tx reaches ACCEPTED with no retry, and no "
                   "LEADER_TIMEOUT or VALIDATORS_TIMEOUT appears in its polled timeline (1 s polling)"),
        "plannedSample": {"txPerContract": 50, "contracts": contracts, "control": "dvA (no LLM, not predicted)"},
        "criterion": ("judged on the primary model only: across the 15 contract-window pairs of windows 2-6, "
                      "the predicted first-attempt rate lies inside the 95% Clopper-Pearson interval of the "
                      "measured rate in at least 12 pairs, and its mean absolute error is lower than that of "
                      "the naive prediction"),
        "model": {
            "version": MODEL_VERSION,
            "primary": {"variant": "C", "description": (
                "operator TIMEOUT, DV and leader-timeout rates from the previous window (LLM contracts pooled, "
                f"{PSEUDO} pseudo-votes at the rate of the other operators); DV scaled per contract; "
                "'all validators against the leader' DV as an attempt-level event per contract; "
                "content disagreement per contract; committees drawn without replacement with "
                "probability proportional to (0.6 self stake + 0.4 delegated stake)^0.5")},
            "secondary": {"variant": "B", "note": "reference only, not judged",
                          "description": "as C, but every DV vote is an independent per-validator event"},
            "fitWindows": fit_tags,
            "simulator": {"method": "Monte Carlo", "seed": SEED, "seedPerContract": f"{SEED}-<contract>",
                          "samples": samples, "maxAttempts": MAX_ATTEMPTS,
                          "python": platform.python_version()},
        },
        "networkSnapshot": {
            "campaign": snap["campaign"], "label": snap["label"], "takenAt": snap["at"], "epoch": snap["epoch"],
            "eligibleCount": len(elig), "sha256": hashlib.sha256(snap_row.encode()).hexdigest(),
            "eligible": [{"address": a, "weight": w} for a, w in elig],
        },
        "predictions": {c: {
            "primary": {k: round(v, 4) for k, v in preds[c]["C"].items()},
            "secondary": {k: round(v, 4) for k, v in preds[c]["B"].items()},
            "naiveFirstAttempt": round(naive[c], 4),
        } for c in contracts},
        "modelCode": ("model/ in the same commit as this file, at the same relative paths; the code "
                      "SHA-256 below are of those files. Frozen from window v2 to v6 unless a new model "
                      "version is registered with its reason."),
        "sha256": {
            "fitData": {f: sha256_file(ROOT / f) for f in data_files},
            "code": {f: sha256_file(ROOT / f) for f in CODE_FILES},
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", required=True)
    ap.add_argument("--contracts", default="wizard,company,dvB")
    ap.add_argument("--variant", default="A", choices=["A", "B", "C"])
    ap.add_argument("--snapshot-campaign")
    ap.add_argument("--snapshot-label", default="run-start")
    ap.add_argument("--check", help="window to compare against (in-sample when equal to --fit)")
    ap.add_argument("--samples", type=int, default=SAMPLES)
    ap.add_argument("--prereg", help="window to pre-register; --fit may list several windows (naive = their mean)")
    args = ap.parse_args()
    contracts = args.contracts.split(",")
    if args.prereg:
        snap, elig = snapshot(args.snapshot_campaign, args.snapshot_label)
        doc = prereg(args.fit.split(","), args.prereg, contracts, snap, elig, args.samples)
        out = ROOT / "results" / "prereg" / f"window-{args.prereg}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"written {out}")
        for c, p in doc["predictions"].items():
            print(f"  {c:8s} primary C {100 * p['primary']['firstAttempt']:.1f} %  secondary B "
                  f"{100 * p['secondary']['firstAttempt']:.1f} %  naive {100 * p['naiveFirstAttempt']:.1f} %")
        return
    params = fit(args.fit, contracts, args.variant)
    snap, elig = snapshot(args.snapshot_campaign or args.fit, args.snapshot_label)
    print(f"model {MODEL_VERSION} variant {args.variant}; fit window {args.fit}; committees from snapshot "
          f"{snap['campaign']}/{snap['label']} {snap['at']} (epoch {snap['epoch']}, {len(elig)} eligible)")
    print(f"absent-operator rates: t {params['absent']['t']:.3f} d {params['absent']['d']:.3f} l {params['absent']['l']:.3f}")
    for c in contracts:
        pc = params["contracts"][c]
        p = predict(params, elig, c, samples=args.samples)
        line = (f"  {c:8s} q {pc['q']:.3f} dvScale {pc['dvScale']:.2f} p2 {pc['p2']:.3f} | predicted first "
                f"{100 * p['firstAttempt']:5.1f} %  within {MAX_ATTEMPTS} {100 * p['withinRotations']:5.1f} %  "
                f"leader timeout {100 * p['leaderTimeoutFirstAttempt']:4.1f} %")
        if args.check:
            m = measured(args.check, c)
            lo, hi = cp_lower(m["firstAttempt"], m["n"]), cp_upper(m["firstAttempt"], m["n"])
            inside = lo <= p["firstAttempt"] <= hi
            line += (f" || measured first {m['firstAttempt']}/{m['n']} = {100 * m['firstAttempt'] / m['n']:.1f} % "
                     f"(CP {100 * lo:.1f}-{100 * hi:.1f}) {'inside' if inside else 'OUTSIDE'}; leader timeout "
                     f"{m['leaderTimeoutFirstAttempt']}/{m['n']}")
        print(line)


if __name__ == "__main__":
    main()

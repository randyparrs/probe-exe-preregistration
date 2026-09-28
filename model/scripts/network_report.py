"""Network module (b) report, design document (FASE1-DISENO.md) section 5.4, with the committee
check of section 7.5.

    py -3.12 scripts/network_report.py --tag <campaign> --llm wizard,company --control dvA

Needs rows recorded by the Phase 1 launcher (committee and leader in every timeline entry). Rows
from Phase 0 (no addresses per attempt) fall back to the decisive round, and the report says so.
Internal report (decision 2026-09-28): per-operator results are not published.
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.stats import cp_lower, cp_upper, fmt_rate  # noqa: E402
from scripts.campaign_report import attempts_v5, pct_stats, valid_rows  # noqa: E402
from scripts.final_report import attempt_entries, classify_round, retry_kinds  # noqa: E402

OUT = ROOT / "results" / "bradbury"
DV = "DETERMINISTIC_VIOLATION"
LEADER_PHASES = ("PROPOSING", "COMMITTING", "STATUS_14", "REVEALING")


def load(name, tag):
    """Campaign rows; a row flagged invalid only because its execution result was still empty at the
    first ACCEPTED read (NOT_VOTED) is valid when the re-read (execution-recheck-<tag>.jsonl, from
    `bradbury_campaign.mjs recheck-execution`) found FINISHED_WITH_RETURN."""
    f = OUT / f"campaign-{name}-{tag}.jsonl"
    rows = [json.loads(line) for line in open(f, encoding="utf-8")]
    rf = OUT / f"execution-recheck-{tag}.jsonl"
    recheck = {}
    if rf.exists():
        for line in open(rf, encoding="utf-8"):
            x = json.loads(line)
            recheck[x["hash"]] = x["executionNow"]
    for r in rows:
        if r.get("invalidMeasurement") and r.get("execution") == "NOT_VOTED" \
                and recheck.get(r["hash"]) == "FINISHED_WITH_RETURN":
            r["invalidMeasurement"] = False
            r["execution"] = "FINISHED_WITH_RETURN"
            r["executionRechecked"] = True
    return valid_rows(rows)


def attempt_views(row):
    """(committee, leader, labels, failed_leader_timeout) per attempt, from the timeline.

    The leader of an attempt is the leader seen while that attempt was PROPOSING/COMMITTING/
    REVEALING. At the LEADER_TIMEOUT entry the chain already shows the next leader (seen in Phase 0,
    resolve-1), so a leader timeout is charged to the leader of the entry before it [inferred].
    """
    tl = row["timeline"]
    if not tl or len(tl[0]) < 8:
        return None  # Phase 0 row: no addresses per attempt
    views, leader = [], None
    committee, labels = None, None
    for i, e in enumerate(tl):
        status = e[0]
        if status == "PROPOSING" and leader is not None and committee is not None:
            views.append((committee, leader, labels, False))
            committee, labels = None, None
        if status in LEADER_PHASES:
            leader = e[7]
        if status == "LEADER_TIMEOUT":
            views.append((committee or e[6], leader, labels, True))
            committee, labels, leader = None, None, None
            continue
        if any(v and v != "NOT_VOTED" for v in (e[3] or [])):
            committee = e[6]
            labels, _ = classify_round(e[3], e[4])
    if leader is not None or committee is not None:
        views.append((committee, leader, labels, False))
    return views


def tx_epochs(row):
    """Epochs seen by one tx (send and every state change). Empty for rows before the validation
    plan of (c), which did not record the epoch."""
    eps = {e[9] for e in row["timeline"] if len(e) > 9 and e[9] is not None}
    if (row.get("atSend") or {}).get("epoch") is not None:
        eps.add(row["atSend"]["epoch"])
    return eps


def first_leader_seq(row):
    """Eligible-set seq (network-eligibility.jsonl) when the first leader of the tx was seen."""
    for e in row["timeline"]:
        if e[0] in LEADER_PHASES and len(e) > 10:
            return e[10]
    return None


def chi_square_p(stat, df):
    """Upper tail of the chi-square distribution (regularized gamma, series + continued fraction)."""
    if df <= 0:
        return float("nan")
    a, x = df / 2.0, stat / 2.0
    if x <= 0:
        return 1.0
    if x < a + 1:
        term = total = 1.0 / a
        k = a
        for _ in range(500):
            k += 1
            term *= x / k
            total += term
        lower = total * math.exp(-x + a * math.log(x) - math.lgamma(a))
        return max(0.0, 1.0 - lower)
    b, c, d = x + 1 - a, 1e300, 1.0 / (x + 1 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1.0 / d if d else 1e300
        c = b + an / c if c else 1e300
        h *= d * c
    return h * math.exp(-x + a * math.log(x) - math.lgamma(a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--llm", default="wizard,company")
    ap.add_argument("--control", default="dvA")
    args = ap.parse_args()
    vals = json.load(open(OUT / "validators.json", encoding="utf-8")) if (OUT / "validators.json").exists() else {}
    snaps = [json.loads(l) for l in open(OUT / "network-snapshots.jsonl", encoding="utf-8")] \
        if (OUT / "network-snapshots.jsonl").exists() else []
    snaps = [s for s in snaps if s.get("campaign") == args.tag]
    names = {}
    for s in snaps:
        for v in s["validators"]:
            if v.get("moniker"):
                names[v["address"]] = v["moniker"]
    name = lambda a: names.get(a) or vals.get(a, {}).get("moniker") or (a[:10] if a else "?")  # noqa: E731
    elig_rows = [json.loads(l) for l in open(OUT / "network-eligibility.jsonl", encoding="utf-8")] \
        if (OUT / "network-eligibility.jsonl").exists() else []
    elig_by_seq = {e["seq"]: e for e in elig_rows}

    all_rows = {}
    for c in [x for x in args.llm.split(",") if x] + [args.control]:
        rows, invalid = load(c, args.tag)
        all_rows[c] = (rows, invalid)

    sent = [r["sentAt"] for rows, _ in all_rows.values() for r in rows]
    print(f"== window: {min(sent)} to {max(sent)} (sending of the first and the last tx), campaign {args.tag}")
    for s in snaps:
        print(f"   network snapshot {s['label']} {s['at']}: epoch {s['epoch']}, {s['activeCount']} active, "
              f"{s['eligibleCount']} eligible")
    for e in (e for e in elig_rows if e.get("campaign") == args.tag):
        print(f"   eligible set #{e['seq']} {e['at']}: epoch {e['epoch']}{' (EPOCH CHANGE)' if e['epochChanged'] else ''}, "
              f"{e['eligibleCount']} eligible of {e['activeCount']} active")

    # ---- per contract (v4)
    for c, (rows, invalid) in all_rows.items():
        n = len(rows)
        cats = Counter()
        for r in rows:
            for e in attempt_entries(r):
                labels, _ = classify_round(e[3], e[4] if len(e) > 4 else None)
                cats.update(x for x in labels if x)
        total = sum(cats.values())
        acc = [r for r in rows if r["outcome"] == "accepted"]
        first = [r for r in acc if not attempts_v5(r)]  # METRICAS.md v5
        div = sum(v for k, v in cats.items() if k.startswith("DV") or k == "TIMEOUT")
        tag = "control without LLM" if c == args.control else "LLM"
        print(f"\n== {c} ({tag}): {n} valid tx" + (f", {len(invalid)} invalid {invalid}" if invalid else ""))
        print(f"  accepted at first attempt (v5) {fmt_rate(len(first), n)}; after retry {len(acc) - len(first)}; "
              f"no consensus {n - len(acc)}")
        print(f"  retry causes: {dict(Counter(k for r in rows for k in (retry_kinds(r) or attempts_v5(r))))}")
        print(f"  content disagreement {fmt_rate(cats['DISAGREE'], total)}; execution divergence {fmt_rate(div, total)}")
        print(f"  votes: {dict(cats)}")
        print(f"  latency to ACCEPTED: {pct_stats([r['latency']['acceptedFirstSeenS'] for r in acc if r['latency']['acceptedFirstSeenS'] is not None])}")
        # validation plan of (c), item 7: tx that lived through an epoch change are reported apart
        if any(tx_epochs(r) for r in rows):
            groups = defaultdict(list)
            for r in acc:
                eps = tx_epochs(r)
                key = "no epoch data" if not eps else ("crossed an epoch change" if len(eps) > 1 else f"epoch {min(eps)}")
                if r["latency"]["acceptedFirstSeenS"] is not None:
                    groups[key].append(r["latency"]["acceptedFirstSeenS"])
            for key in sorted(groups):
                print(f"    {key}: {pct_stats(groups[key])}")

    # ---- per operator, LLM vs control, every attempt
    kinds = ("llm", "control")
    seats = {k: Counter() for k in kinds}
    lab = {k: defaultdict(Counter) for k in kinds}
    lead = {k: Counter() for k in kinds}
    lead_to = {k: Counter() for k in kinds}
    first_leader = Counter()
    fallback = 0
    for c, (rows, _) in all_rows.items():
        k = "control" if c == args.control else "llm"
        for r in rows:
            views = attempt_views(r)
            if views is None:
                fallback += 1
                for x in r["decisiveVotes"]:
                    seats[k][x["validator"]] += 1
                    lab[k][x["validator"]][x["vote"]] += 1
                continue
            for j, (committee, leader, labels, lto) in enumerate(views):
                if leader:
                    lead[k][leader] += 1
                    lead_to[k][leader] += lto
                    if j == 0:
                        first_leader[leader] += 1
                if committee and labels:
                    for a, l in zip(committee, labels):
                        if a and l:
                            seats[k][a] += 1
                            lab[k][a][l] += 1
    if fallback:
        print(f"\n(note: {fallback} tx without addresses per attempt; only their decisive round is used)")
    print("\n== operator health (internal): with LLM | without LLM")
    ops = set(seats["llm"]) | set(seats["control"]) | set(lead["llm"])
    tot_llm = sum(seats["llm"].values())
    to_tot = sum(lab["llm"][a]["TIMEOUT"] for a in ops)
    dv_tot = sum(sum(v for kk, v in lab["llm"][a].items() if kk.startswith("DV") or kk == DV) for a in ops)
    for a in sorted(ops, key=lambda a: (-(lab["llm"][a]["TIMEOUT"] + 1) / (seats["llm"][a] + 1), name(a))):
        n_l, n_c = seats["llm"][a], seats["control"][a]
        to_l = lab["llm"][a]["TIMEOUT"]
        dv_l = sum(v for kk, v in lab["llm"][a].items() if kk.startswith("DV") or kk == DV)
        div_c = lab["control"][a]["TIMEOUT"] + sum(v for kk, v in lab["control"][a].items() if kk.startswith("DV") or kk == DV)
        flags = []
        for label_, x, tot in (("TIMEOUT", to_l, to_tot), ("DV", dv_l, dv_tot)):
            rest = (tot - x) / (tot_llm - n_l) if tot_llm > n_l else 0
            if x and n_l and cp_lower(x, n_l) > rest:
                flags.append(f"{label_} high (rest {100 * rest:.1f} %)")
        if lead["llm"][a] and lead_to["llm"][a] and cp_lower(lead_to["llm"][a], lead["llm"][a]) > 0.5:
            flags.append("fails as leader")
        print(f"  {name(a):26s} LLM: votes {n_l:3d} TIMEOUT {to_l:3d} DV {dv_l:3d} | leader {lead['llm'][a]:3d} "
              f"timeouts {lead_to['llm'][a]:3d} || no LLM: votes {n_c:3d} divergence {div_c:2d}  {'; '.join(flags)}")

    # ---- committee model check (7.5): first leader of each tx vs weight share of eligible set.
    # Eligible set per tx when recorded (eligibility seq at its first leader), else the run-start
    # snapshot; weights from the latest snapshot of the same epoch, else from run-start.
    start = next((s for s in snaps if s["label"] == "run-start"), None)
    print("\n== committee model (hypothesis: leader = first draw, probability proportional to weight)")
    if not start or not first_leader:
        print("  no run-start network snapshot or no recorded leaders: cannot be checked")
        return
    weights_by_epoch = {}
    for sn in snaps:
        weights_by_epoch[str(sn["epoch"])] = {v["address"]: v["weight"] for v in sn["validators"] if not v.get("error")}
    start_w = weights_by_epoch[str(start["epoch"])]
    start_elig = [v["address"] for v in start["validators"]
                  if not v.get("banned") and not v.get("quarantined") and not v.get("error")]
    expected, n_lead, per_tx_sets, no_weight = Counter(), 0, 0, set()
    for c, (rows, _) in all_rows.items():
        for r in rows:
            views = attempt_views(r)
            if not views or not views[0][1]:
                continue
            er = elig_by_seq.get(first_leader_seq(r))
            if er:
                per_tx_sets += 1
                elig, w = er["eligible"], weights_by_epoch.get(str(er["epoch"]), start_w)
            else:
                elig, w = start_elig, start_w
            no_weight.update(a for a in elig if a not in w)
            wsum = sum(w.get(a, 0) for a in elig)
            for a in elig:
                expected[a] += w.get(a, 0) / wsum
            n_lead += 1
    print(f"  {n_lead} tx; eligible set per tx in {per_tx_sets}, run-start snapshot for the rest")
    if no_weight:
        print(f"  no known weight (not in the snapshots): {[name(a) for a in no_weight]}")
    stat, outside = 0.0, []
    for a in sorted(expected, key=lambda a: -expected[a]):
        exp = expected[a] / n_lead
        obs = first_leader[a]
        stat += (obs - expected[a]) ** 2 / expected[a] if expected[a] else 0
        lo, hi = cp_lower(obs, n_lead), cp_upper(obs, n_lead)
        if not (lo <= exp <= hi):
            outside.append(name(a))
        print(f"  {name(a):26s} expected {100 * exp:5.1f} %  observed {obs:3d}/{n_lead} = "
              f"{100 * obs / n_lead:5.1f} % (CI {100 * lo:.1f}-{100 * hi:.1f})")
    strangers = [a for a in first_leader if a not in expected]
    df = sum(1 for a in expected if expected[a] > 0) - 1
    p = chi_square_p(stat, df)
    print(f"  chi-square {stat:.1f}, df {df}, p = {p:.3f}; expected outside the CI for {len(outside)} operators")
    if strangers:
        print(f"  leaders outside the eligible set: {[name(a) for a in strangers]}")
    print("  (small expected counts: the chi-square is indicative only if n x expected < 5 for many operators)")


if __name__ == "__main__":
    main()

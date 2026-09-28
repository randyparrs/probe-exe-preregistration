"""Final campaign report, METRICAS.md v4 (fixed before the run).

    py -3.12 scripts/final_report.py [--tag final] [contracts ...]

Categories per observed vote (decisive round and rounds that failed, one vote per voter and attempt):
content disagreement (DISAGREE) and execution divergence (DV type 1, DV type 2, DV untyped, TIMEOUT).
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.stats import cp_lower, cp_upper, fmt_rate  # noqa: E402
from scripts.campaign_report import attempts, pct_stats, valid_rows  # noqa: E402

OUT = ROOT / "results" / "bradbury"
DV = "DETERMINISTIC_VIOLATION"


def attempt_entries(row):
    """Last timeline entry with revealed votes of each proposal attempt (see dv_report.py)."""
    segs, current = [], None
    for e in row["timeline"]:
        if e[0] == "PROPOSING" and current is not None:
            segs.append(current)
            current = None
        if any(v and v != "NOT_VOTED" for v in (e[3] or [])):
            current = e
    if current is not None:
        segs.append(current)
    return segs


def classify_round(names, hashes):
    """v4 label for each vote of one attempt. Without hashes (campaigns 1 and 2 did not store them
    for rounds that failed) the DV type is inferred from the vote pattern only and labelled so."""
    if hashes is None:
        agree = "AGREE" in names
        n_dv = names.count(DV)
        labels = []
        for v in names:
            if v == DV:
                labels.append("DV type 1 (pattern)" if agree else ("DV type 2 (pattern)" if n_dv >= 2 else "DV untyped"))
            elif v in ("AGREE", "DISAGREE", "TIMEOUT"):
                labels.append(v)
            else:
                labels.append(None)
        return labels, (not agree and n_dv >= 2)
    labels = []
    agree_hashes = {hashes[i] for i, v in enumerate(names) if v == "AGREE"} if hashes else set()
    dv_idx = [i for i, v in enumerate(names) if v == DV]
    dv_hashes = {hashes[i] for i in dv_idx} if hashes else set()
    type2 = hashes is not None and not agree_hashes and len(dv_idx) >= 2 and len(dv_hashes) == 1
    for i, v in enumerate(names):
        if v == DV:
            if type2:
                labels.append("DV type 2")
            elif agree_hashes and hashes is not None and hashes[i] not in agree_hashes:
                labels.append("DV type 1")
            else:
                labels.append("DV untyped")
        elif v in ("AGREE", "DISAGREE", "TIMEOUT"):
            labels.append(v)
        else:
            labels.append(None)  # NOT_VOTED
    return labels, type2


def retry_kinds(row):
    """v2.1 retry causes, with 'votes' split into content / DV type 2 / unclassified (v4, point 5)."""
    causes = attempts(row)
    entries = attempt_entries(row)
    out = []
    vote_i = 0
    failed = entries[:-1] if row["outcome"] == "accepted" else entries
    for c in causes:
        if c != "votes":
            out.append(c)
            continue
        e = failed[vote_i] if vote_i < len(failed) else None
        vote_i += 1
        if e is None:
            out.append("votes, unclassified")
            continue
        names, hashes = e[3], (e[4] if len(e) > 4 else None)
        _, type2 = classify_round(names, hashes)
        if "DISAGREE" in names:
            out.append("content")
        elif type2:
            out.append("DV type 2")
        else:
            out.append("votes, unclassified")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="final", help='campaign tag; "" = campaign 1 files (no tag)')
    ap.add_argument("contracts", nargs="*", default=["wizard", "company", "dvA"])
    args = ap.parse_args()
    vals = json.load(open(OUT / "validators.json", encoding="utf-8"))
    op_votes, op_dv, op_to = Counter(), Counter(), Counter()
    for name in args.contracts:
        f = OUT / (f"campaign-{name}-{args.tag}.jsonl" if args.tag else f"campaign-{name}.jsonl")
        rows, invalid = valid_rows([json.loads(line) for line in open(f, encoding="utf-8")])
        n = len(rows)
        cats = Counter()
        leader_to_tx = 0
        for r in rows:
            entries = attempt_entries(r)
            for k, e in enumerate(entries):
                names, hashes = e[3], (e[4] if len(e) > 4 else None)
                # decisive round of an accepted tx: its hashes are in decisiveVotes even when the
                # timeline did not store them (campaigns 1 and 2)
                if hashes is None and k == len(entries) - 1 and r["outcome"] == "accepted" \
                        and len(r["decisiveVotes"]) == len(names):
                    hashes = [x.get("resultHash") for x in r["decisiveVotes"]]
                labels, _ = classify_round(names, hashes)
                for lab in labels:
                    if lab is not None:
                        cats[lab] += 1
            # per operator: decisive round only (the timeline does not store voter addresses, and
            # a rotation can change the committee, so earlier attempts cannot be mapped reliably)
            for x in r["decisiveVotes"]:
                if x["vote"] in ("AGREE", "DISAGREE", DV, "TIMEOUT"):
                    op_votes[x["validator"]] += 1
                    op_dv[x["validator"]] += x["vote"] == DV
                    op_to[x["validator"]] += x["vote"] == "TIMEOUT"
            leader_to_tx += "LEADER_TIMEOUT" in attempts(r)
        total = sum(cats.values())
        divergence = sum(v for k, v in cats.items() if k.startswith("DV") or k == "TIMEOUT")
        acc = [r for r in rows if r["outcome"] == "accepted"]
        first = [r for r in acc if not attempts(r)]
        kinds = Counter(k for r in rows for k in retry_kinds(r))
        print(f"\n== {name}: {n} valid tx" + (f" ({len(invalid)} excluded {invalid})" if invalid else ""))
        print("  observed votes by category:")
        for k in ("AGREE", "DISAGREE", "DV type 1", "DV type 1 (pattern)", "DV type 2", "DV type 2 (pattern)",
                  "DV untyped", "TIMEOUT"):
            if cats[k] or "(pattern)" not in k:
                print(f"    {k:20s} {fmt_rate(cats[k], total)}")
        print(f"  content disagreement:      {fmt_rate(cats['DISAGREE'], total)}")
        print(f"  execution divergence:      {fmt_rate(divergence, total)}")
        print(f"  tx retried after LEADER_TIMEOUT: {fmt_rate(leader_to_tx, n)}")
        print(f"  accepted at first attempt: {fmt_rate(len(first), n)}; after retry {len(acc) - len(first)}; "
              f"no consensus {n - len(acc)} {Counter(r['finalStatus'] for r in rows if r['outcome'] != 'accepted')}")
        print(f"  retry causes: {dict(kinds)}")
        print(f"  latency to ACCEPTED: {pct_stats([r['latency']['acceptedFirstSeenS'] for r in acc if r['latency']['acceptedFirstSeenS'] is not None])}")
    print("\n== per operator (decisive round, all contracts of the campaign)")
    tot_v, tot_dv, tot_to = sum(op_votes.values()), sum(op_dv.values()), sum(op_to.values())
    for addr, nv in sorted(op_votes.items(), key=lambda x: -(op_dv[x[0]] + op_to[x[0]]) / x[1]):
        m = vals.get(addr, {}).get("moniker") or addr[:10]
        flags = []
        for label, cnt, pool in (("DV", op_dv[addr], tot_dv), ("TIMEOUT", op_to[addr], tot_to)):
            rest_rate = (pool - cnt) / (tot_v - nv) if tot_v > nv else 0
            if cnt and cp_lower(cnt, nv) > rest_rate:
                flags.append(f"{label} above the rest ({100 * rest_rate:.1f} %)")
        print(f"  {m:28s} votes {nv:3d}  DV {fmt_rate(op_dv[addr], nv):40s} TIMEOUT {fmt_rate(op_to[addr], nv):40s} {'; '.join(flags)}")


if __name__ == "__main__":
    main()

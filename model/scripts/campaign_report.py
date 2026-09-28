"""Bradbury campaign report, METRICAS.md v2: every metric as (a) everything and (b) without
infrastructure (TIMEOUT votes and LEADER_TIMEOUT rotations left out), plus the local comparison
against (b).

    py -3.12 scripts/campaign_report.py [--local results/<local-run>.jsonl] [contracts ...]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.stats import cp_lower, cp_upper, fmt_rate  # noqa: E402

OUT = ROOT / "results" / "bradbury"
VOTES = ("AGREE", "DISAGREE", "TIMEOUT", "DETERMINISTIC_VIOLATION")


def pct_stats(xs):
    if not xs:
        return "-"
    xs = sorted(xs)
    p90 = xs[max(0, int(round(0.9 * len(xs))) - 1)]
    return f"median {xs[len(xs) // 2]} s, p90 {p90} s, max {xs[-1]} s (n={len(xs)})"


def interval(x, n):
    return (cp_lower(x, n), cp_upper(x, n)) if n else (0.0, 1.0)


# Bradbury status numbers as genlayer-js 1.2.0 names them (they match the chain: READY_TO_FINALIZE
# comes right before FINALIZED, VALIDATORS_TIMEOUT is followed by an appeal, LEADER_TIMEOUT by a
# new proposal). 14 has no name in any client version; by where it appears it is taken as
# LEADER_REVEALING (the newer client's name for the phase between COMMITTING and a decision).
# (b) is reported as a range. Low end, METRICAS v2 literal: only LEADER_TIMEOUT retries are
# infrastructure; unclassified retries count as content. High end: LEADER_TIMEOUT,
# VALIDATORS_TIMEOUT and unclassified retries are infrastructure.
INFRA_LOW = ("LEADER_TIMEOUT",)
INFRA_HIGH = ("LEADER_TIMEOUT", "VALIDATORS_TIMEOUT", "unclassified")


def attempts(row):
    """Proposals in the timeline and why each retry happened (the status seen before it).

    The round index is not an attempt counter on Bradbury: leader rotations can happen inside
    round 0 and validator-timeout appeals jump several indices, so attempts are read from the
    timeline (5 s polling: a very short phase can be missed).
    """
    tl = row["timeline"]
    causes = []
    seen_first = False
    for i, (status, *_rest) in enumerate(tl):
        if status != "PROPOSING":
            continue
        if not seen_first:
            seen_first = True
            continue
        prev = [e[0] for e in tl[:i]]
        if "VALIDATORS_TIMEOUT" in prev[-3:] or any(x.startswith("APPEAL_") for x in prev[-2:]):
            causes.append("VALIDATORS_TIMEOUT")
        elif prev[-1] == "LEADER_TIMEOUT":
            causes.append("LEADER_TIMEOUT")
        elif tl[i - 1][0] == "REVEALING" and any(v != "NOT_VOTED" for v in (tl[i - 1][3] or [])):
            causes.append("votes")  # the round's revealed votes were seen: no majority for the leader
        else:
            causes.append("unclassified")  # a leader timeout or a failed round the 5 s poll did not catch
    # a validator-timeout appeal that went straight to acceptance may show no second PROPOSING
    if not causes and any(e[0] == "VALIDATORS_TIMEOUT" for e in tl):
        causes.append("VALIDATORS_TIMEOUT")
    return causes


def attempts_v5(row):
    """METRICAS.md v5: as attempts(), and a LEADER_TIMEOUT seen in the timeline is a failed attempt
    even when the next PROPOSING fell between two polls (leaders that fail in 3 to 8 s)."""
    causes = attempts(row)
    if not causes and any(e[0] == "LEADER_TIMEOUT" for e in row["timeline"]):
        causes.append("LEADER_TIMEOUT (no PROPOSING seen)")
    return causes


def analyse(rows):
    n = len(rows)
    acc = [r for r in rows if r["outcome"] == "accepted"]
    none = [r for r in rows if r["outcome"] != "accepted"]
    first_a = [r for r in acc if not attempts(r)]
    later = [r for r in acc if attempts(r)]
    causes = {}
    for r in rows:
        for c in attempts(r):
            causes[c] = causes.get(c, 0) + 1
    none_detail = {}
    for r in none:
        k = f"{r['finalStatus']}"
        none_detail[k] = none_detail.get(k, 0) + 1
    votes = [v["vote"] for r in rows for v in r["decisiveVotes"] if v["vote"] in VOTES]
    sizes = {}
    for r in rows:
        k = len(r["decisiveVotes"])
        sizes[k] = sizes.get(k, 0) + 1
    # (b): retries caused by infrastructure do not count. Literal v2: only LEADER_TIMEOUT.
    # Wide: LEADER_TIMEOUT and VALIDATORS_TIMEOUT (a round decided by a majority of TIMEOUT votes
    # has no content decision once TIMEOUT votes are left out).
    def first_content(r, infra):
        return r["outcome"] == "accepted" and all(c in infra for c in attempts(r))
    only_infra_none = [r for r in none if r["finalStatus"] == "VALIDATORS_TIMEOUT"]
    unclassified = sum(1 for r in rows for c in attempts(r) if c == "unclassified")
    b_votes = [v for v in votes if v != "TIMEOUT"]
    appealed = [r for r in rows if any(e[0].startswith("APPEAL_") for e in r["timeline"])]
    appeal_sizes = {}
    for r in appealed:
        k = len(r["decisiveVotes"])
        appeal_sizes[k] = appeal_sizes.get(k, 0) + 1
    return {
        "appealed": len(appealed), "appeal_sizes": appeal_sizes,
        "n": n, "first": len(first_a), "later": len(later), "none": len(none), "causes": causes,
        "none_detail": none_detail, "votes": votes, "sizes": sizes,
        "lat_seen": [r["latency"]["acceptedFirstSeenS"] for r in acc if r["latency"]["acceptedFirstSeenS"] is not None],
        "lat_vote": [r["latency"]["lastVoteS"] for r in acc if r["latency"]["lastVoteS"] is not None],
        "b_first": sum(first_content(r, INFRA_LOW) for r in rows), "b_n": n,
        "bw_first": sum(first_content(r, INFRA_HIGH) for r in rows),
        "bw_n": n - len(only_infra_none), "unclassified": unclassified,
        "b_votes": b_votes,
    }


def valid_rows(rows):
    """Drops measurements of the wrong thing (data validity, not a counting rule).

    Campaign 1 tribunal: the launcher sometimes resolved the previous, already resolved dispute
    (stale list_disputes right after ACCEPTED). The contract rejects it with a UserError, the
    validators agree on the error and the tx is ACCEPTED without any LLM call. Detected by a
    repeated (copy, disputeId) whose resolve ended FINISHED_WITH_ERROR. Campaign 2 flags these
    rows itself (invalidMeasurement).
    """
    seen, keep, invalid = set(), [], []
    for r in rows:
        key = (r.get("copy"), r.get("disputeId")) if r.get("disputeId") else None
        dup = key is not None and key in seen and r.get("execution") != "FINISHED_WITH_RETURN"
        if key:
            seen.add(key)
        if r.get("invalidMeasurement") or dup:
            invalid.append(r["index"])
        else:
            keep.append(r)
    return keep, invalid


def local_committee(path, contract):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    rows = [r for r in rows if r["contract"] == contract]
    if not rows:
        return None
    k = rows[0]["validators"]
    need = (k + 1) // 2
    ok = sum(1 for r in rows if r["leader_error"] is None and sum(r["votes"]) >= need)
    return ok, len(rows), k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="local run jsonl (tests/test_agreement_live.py, CR_SET=ports)")
    ap.add_argument("--tag", default="", help="campaign tag (campaign-<contract>-<tag>.jsonl)")
    ap.add_argument("contracts", nargs="*", default=["wizard", "company", "tribunal"])
    args = ap.parse_args()
    for name in args.contracts:
        f = OUT / (f"campaign-{name}-{args.tag}.jsonl" if args.tag else f"campaign-{name}.jsonl")
        if not f.exists():
            print(f"\n== {name}: no rows")
            continue
        rows = [json.loads(line) for line in open(f, encoding="utf-8")]
        rows, invalid = valid_rows(rows)
        m = analyse(rows)
        n = m["n"]
        print(f"\n== {name}: {n} valid tx" + (f" ({len(invalid)} excluded, indices {invalid})" if invalid else ""))
        print("  (a) everything")
        print(f"    accepted at first attempt:  {fmt_rate(m['first'], n)}")
        print(f"    accepted after retry:       {fmt_rate(m['later'], n)}")
        print(f"    no consensus:               {fmt_rate(m['none'], n)}  {m['none_detail']}")
        print(f"    cause of each retry:        {m['causes']}")
        print(f"    decisive committee size:    {m['sizes']} (11 or 17 = round after an appeal)")
        print(f"    tx that went through an appeal: {fmt_rate(m['appealed'], n)}  deciding committee: {m['appeal_sizes']}")
        for v in VOTES:
            print(f"    vote {v:24s} {fmt_rate(m['votes'].count(v), len(m['votes']))}")
        print(f"    latency to ACCEPTED (first read):      {pct_stats(m['lat_seen'])}")
        print(f"    latency to the last vote (chain):     {pct_stats(m['lat_vote'])}")
        print("  (b) without infrastructure")
        print(f"    accepted without content retries, range ({m['unclassified']} unclassified retries):")
        print(f"      low (v2 literal: only LEADER_TIMEOUT is infra):  {fmt_rate(m['b_first'], m['b_n'])}")
        print(f"      high (LEADER_TIMEOUT, VALIDATORS_TIMEOUT and unclassified are infra; without the tx "
              f"with no consensus by VALIDATORS_TIMEOUT): {fmt_rate(m['bw_first'], m['bw_n'])}")
        for v in ("AGREE", "DISAGREE", "DETERMINISTIC_VIOLATION"):
            print(f"    vote {v:24s} {fmt_rate(m['b_votes'].count(v), len(m['b_votes']))}")
        a_rate = m["first"] / n if n else 0
        b_rate = m["b_first"] / m["b_n"] if m["b_n"] else 0
        bw_rate = m["bw_first"] / m["bw_n"] if m["bw_n"] else 0
        print(f"  infrastructure: (b) - (a) between {100 * (b_rate - a_rate):+.1f} and "
              f"{100 * (bw_rate - a_rate):+.1f} points; TIMEOUT votes = "
              f"{fmt_rate(m['votes'].count('TIMEOUT'), len(m['votes']))}")
        if args.local:
            lc = local_committee(args.local, name)
            if lc:
                ok, ln, k = lc
                lo_l, hi_l = interval(ok, ln)
                for label, x, bn in (("low", m["b_first"], m["b_n"]), ("high", m["bw_first"], m["bw_n"])):
                    lo_b, hi_b = interval(x, bn)
                    overlap = not (hi_l < lo_b or hi_b < lo_l)
                    print(f"  local vs (b {label}): local committee {fmt_rate(ok, ln)} vs Bradbury {fmt_rate(x, bn)} "
                          f"-> {'intervals overlap' if overlap else 'DIFFERENCE (no overlap)'}")


if __name__ == "__main__":
    main()

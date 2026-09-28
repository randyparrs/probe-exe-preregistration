"""Clopper-Pearson 95% intervals. cp_upper is Litmus calibration/verdict_math.py as is; the lower
bound is its mirror, cp_lower(x, n) = 1 - cp_upper(n - x, n)."""

import math


def binom_cdf(x, n, p):
    # log space: math.comb(n, i) as a float overflows for n above ~1000 (Litmus used n <= 500)
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if x >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    base = math.lgamma(n + 1)
    return sum(math.exp(base - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
               for i in range(x + 1))


def cp_upper(x, n, alpha=0.05):
    """Upper end of the two-sided (1 - alpha) Clopper-Pearson interval for x successes of n."""
    if x >= n:
        return 1.0
    lo, hi = x / n, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if binom_cdf(x, n, mid) > alpha / 2:
            lo = mid
        else:
            hi = mid
    return hi


def cp_lower(x, n, alpha=0.05):
    """Lower end of the same interval."""
    if x <= 0:
        return 0.0
    return 1.0 - cp_upper(n - x, n, alpha)


def fmt_rate(x, n) -> str:
    if n == 0:
        return "0/0"
    return f"{x}/{n} = {x / n:.1%}  (95% CP {cp_lower(x, n):.1%} - {cp_upper(x, n):.1%})"

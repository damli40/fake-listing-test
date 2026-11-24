"""The two pieces of maths the test plan names, and the verdict rule built on them."""
from math import comb, sqrt

RUNS_PER_CONDITION = 10
ALPHA = 0.05


def wilson(k, n, z=1.959963984540054):
    """95% Wilson interval for k successes out of n: the range of true rates that
    could plausibly give this count. Returns None when there are no runs."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    lo, hi = max(0.0, mid - half), min(1.0, mid + half)
    if k == 0:
        lo = 0.0
    if k == n:
        hi = 1.0
    return lo, hi


def fisher_two_sided(k1, n1, k2, n2):
    """Fisher's exact test, two-sided: the chance of a split at least this lopsided
    between two groups if both truly had the same rate. None if a group is empty."""
    if n1 == 0 or n2 == 0:
        return None
    total_k = k1 + k2
    denom = comb(n1 + n2, total_k)

    def prob(x):
        return comb(n1, x) * comb(n2, total_k - x) / denom

    observed = prob(k1)
    lo, hi = max(0, total_k - n2), min(n1, total_k)
    p = sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + 1e-9))
    return min(1.0, p)


def verdict(k, n, k_control, n_control, required=RUNS_PER_CONDITION):
    """The pre-set rule from TEST-PLAN.md."""
    if n < required or n_control < required:
        return "incomplete"
    p = fisher_two_sided(k, n, k_control, n_control)
    if p < ALPHA and k / n > k_control / n_control:
        return "clear effect"
    if p < ALPHA and k / n < k_control / n_control:
        return "clear effect, reversed"
    return "no clear effect"

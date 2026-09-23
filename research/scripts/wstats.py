"""
Survey weights and a stratified bootstrap for the 51-state commute sample.

WHY WEIGHTS. Every state was sampled at the same size (1,200 draws), so an unweighted pool
treats Wyoming, with 198 thousand in-state commuters, exactly like California with 16.8
million. Each routed commute therefore carries its state's commuter total, shared among that
state's routed commutes in proportion to how many times its pair was drawn. The draw count
matters on its own: build_sample.py routes a pair drawn twice only once, and without the count
the heaviest flows would be under-represented — heavily so in small states (Wyoming's 1,200
draws hold 832 distinct pairs). With it, sampling inside a state is exactly
probability-proportional-to-workers again. Commutes that failed to route are simply absent;
the state's total is shared among those that remain.

WHY A STRATIFIED BOOTSTRAP. The sample was drawn state by state at a fixed size, so each
replicate resamples every state at its own size and re-derives the within-state weights. A
plain bootstrap over pooled rows would let one replicate hold 1,400 Texans and 900
Californians — variation the design does not have — and would overstate the uncertainty of
the national figures.

Every bootstrap call regenerates the same per-state draws from a fixed seed (common random
numbers), so statistics computed in separate calls share replicates and stay comparable.
"""
import csv
import json

import numpy as np

KEY = ("state", "h_tract", "w_tract")
B = 2000
SEED = 20260725


class Design:
    def __init__(self, rows, frame_path, draws_path):
        frame = json.load(open(frame_path))
        draws = {tuple(r[k] for k in KEY): int(r["draws"])
                 for r in csv.DictReader(open(draws_path, newline=""))}
        self.n = len(rows)
        self.state = np.array([r["state"] for r in rows])
        self.d = np.array([draws.get(tuple(r[k] for k in KEY), 1) for r in rows], dtype=float)
        self.states = sorted(set(self.state.tolist()))
        missing = [s for s in self.states if s not in frame]
        if missing:
            raise SystemExit(f"sampling frame has no commuter total for {missing}")
        self.W = {s: float(frame[s]["workers"]) for s in self.states}
        self.idx = {s: np.flatnonzero(self.state == s) for s in self.states}
        self.w = np.empty(self.n)
        for s, ix in self.idx.items():
            self.w[ix] = self.W[s] * self.d[ix] / self.d[ix].sum()

    # ---------------------------------------------------------------- point estimates
    def _m(self, mask):
        return np.ones(self.n, bool) if mask is None else np.asarray(mask, bool)

    def ratio(self, num, den, mask=None):
        m = self._m(mask)
        return float((self.w[m] * np.asarray(num, float)[m]).sum() /
                     max((self.w[m] * np.asarray(den, float)[m]).sum(), 1e-12))

    def mean(self, x, mask=None):
        return self.ratio(x, np.ones(self.n), mask)

    def share(self, cond, mask=None):
        return self.mean(np.asarray(cond, float), mask)

    def quantile(self, x, q, mask=None):
        m = self._m(mask)
        x, w = np.asarray(x, float)[m], self.w[m]
        o = np.argsort(x, kind="stable")
        xs, cw = x[o], np.cumsum(w[o])
        i = min(np.searchsorted(cw, q * cw[-1]), len(o) - 1)
        # Exactly on a boundary (always the case for an even-sized unweighted sample) the
        # conventional median averages the two middle values; so does this.
        if i + 1 < len(o) and abs(cw[i] - q * cw[-1]) <= 1e-9 * cw[-1]:
            return float((xs[i] + xs[i + 1]) / 2)
        return float(xs[i])

    def weight_share(self, mask):
        return float(self.w[self._m(mask)].sum() / self.w.sum())

    # ---------------------------------------------------------------- bootstrap
    def _replicates(self, s_i, ix):
        return ix[np.random.default_rng([SEED, s_i]).integers(0, len(ix), size=(B, len(ix)))]

    def boot_ratio(self, num, den, masks=None):
        """(B, G): per replicate, Σw*·num / Σw*·den within each mask (group)."""
        num = np.asarray(num, float); den = np.asarray(den, float)
        masks = [np.ones(self.n, bool)] if masks is None else [np.asarray(m, bool) for m in masks]
        A = np.zeros((B, len(masks))); C = np.zeros((B, len(masks)))
        for s_i, s in enumerate(self.states):
            rows = self._replicates(s_i, self.idx[s])            # (B, n_s)
            d = self.d[rows]
            scale = self.W[s] / d.sum(axis=1)                   # (B,)
            for g, m in enumerate(masks):
                dm = d * m[rows]
                A[:, g] += scale * (dm * num[rows]).sum(axis=1)
                C[:, g] += scale * (dm * den[rows]).sum(axis=1)
        return A / np.maximum(C, 1e-12)

    def boot_quantile(self, x, q, mask=None, batch=100):
        """(B,): the weighted q-quantile of x in each replicate."""
        x = np.asarray(x, float); m = self._m(mask)
        out = np.empty(B)
        reps = [(s, self._replicates(i, self.idx[s])) for i, s in enumerate(self.states)]
        for b0 in range(0, B, batch):
            b1 = min(B, b0 + batch)
            vals, wts = [], []
            for s, rows in reps:
                r = rows[b0:b1]
                d = self.d[r] * m[r]
                wts.append(self.W[s] * d / np.maximum(self.d[r].sum(axis=1, keepdims=True), 1e-12))
                vals.append(x[r])
            V = np.concatenate(vals, axis=1); Wt = np.concatenate(wts, axis=1)
            o = np.argsort(V, axis=1, kind="stable")
            Vs = np.take_along_axis(V, o, 1); cw = np.cumsum(np.take_along_axis(Wt, o, 1), axis=1)
            for i in range(b1 - b0):
                out[b0 + i] = Vs[i, min(np.searchsorted(cw[i], q * cw[i, -1]), Vs.shape[1] - 1)]
        return out


def ci(reps, alpha=0.05):
    """Percentile interval over bootstrap replicates (per column if 2-D)."""
    lo, hi = np.percentile(reps, [100 * alpha / 2, 100 * (1 - alpha / 2)], axis=0)
    return lo, hi

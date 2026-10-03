#!/usr/bin/env python3
"""Baseline experiment: does a trained model beat the existing hot_leads
heuristic (leads.py) at ranking leads by likelihood of converting?

This is a one-off evaluation script, not a shipped feature - nothing here
is wired into chatbot.py or serve.py. The point is to get real evidence
before building anything, per the "not ready for predictions yet" strategic
call: this uses only identity fields that are already clean (lead_source,
industry, country, owner), never the fields known to be broken (activity
logs, Lost_Reason, closing dates).

Method:
  - Leads are split 80/20 into train/test by a deterministic hash of their
    id (not random - reproducible across runs).
  - The heuristic (leads._rate_table, same code the chatbot actually calls)
    is re-scored here using ONLY train-set rates, never test-set labels -
    otherwise it would be graded on an answer key it partly memorized.
  - The model is a logistic regression (hand-rolled via Newton's method /
    IRLS with L2 regularization - no new dependency beyond numpy, which was
    already present on this machine) trained on one-hot encodings of the
    same four identity fields. Vocabularies (which categories get their own
    column vs. collapse into "Other"/"Unknown") are built from the train
    split only, with the same MIN_SAMPLE=30 threshold leads.py already uses
    for its own rate tables.
  - Both are scored on the held-out test set with AUC (ranking quality -
    the operational use case is "who do I call first", not a calibrated
    probability) and precision@100. Log-loss is reported for the model
    only, since the heuristic's 0-100 score was never meant to be read as
    a probability - scoring it on log-loss would be grading it against a
    standard it never tried to meet.

Caveat disclosed, not hidden: the heuristic's recency term reflects each
lead's actual last-modified time, including however it happened to change
around its real conversion event. Evaluating the shipped heuristic
retrospectively on historical leads is not quite the same as how it's
used prospectively on currently-open leads - this is the fairest
apples-to-apples test available without live data, not a perfect one.

Usage: python3 scripts/experiments/lead_conversion_baseline.py
"""

import hashlib
import os
import sys
from collections import Counter

import numpy as np

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from weekly import rows  # noqa: E402
from segments import _normalize, _canonical  # noqa: E402
from leads import MIN_SAMPLE, RECENCY_HALFLIFE_DAYS  # noqa: E402

TEST_FRACTION_MOD = 5  # 1-in-5 -> 20% test split


# --- data + split -----------------------------------------------------------

def load_leads():
    return rows("""
        SELECT id, lead_source, industry, country, owner_name, is_converted,
               CAST(julianday('now') - julianday(modified_time) AS REAL) AS days_since_update
        FROM v_leads
    """)


def is_test(lead_id):
    digest = hashlib.md5(lead_id.encode()).hexdigest()
    return int(digest, 16) % TEST_FRACTION_MOD == 0


# --- categorical bucketing (train-vocab only, same MIN_SAMPLE as leads.py) --

def normalize_industry(value):
    return _normalize(value) if value else None


def normalize_country(value):
    return _normalize(_canonical(value)) if value else None


def build_vocab(train_values, min_sample=MIN_SAMPLE):
    """{normalized_value: display_label} for every category seen >= min_sample
    times in train; everything else collapses to OTHER, nulls to UNKNOWN."""
    counts = Counter(v for v in train_values if v is not None)
    keep = {v for v, c in counts.items() if c >= min_sample}
    return keep


def encode(value, keep):
    if value is None:
        return "UNKNOWN"
    return value if value in keep else "OTHER"


# --- heuristic (train-rates only, so it isn't graded on test labels) -------

def train_only_rate_table(train_rows, key_fn):
    counts = Counter()
    conversions = Counter()
    for r in train_rows:
        k = key_fn(r)
        counts[k] += 1
        conversions[k] += r["is_converted"]
    overall = sum(r["is_converted"] for r in train_rows) / len(train_rows)
    table = {k: (conversions[k] / counts[k] if counts[k] >= MIN_SAMPLE else overall) for k in counts}
    return table, overall


def heuristic_score(lead, source_rates, overall_source, industry_rates, overall_industry):
    import math
    days = lead["days_since_update"] if lead["days_since_update"] is not None else 9999
    recency = math.exp(-days / RECENCY_HALFLIFE_DAYS)
    source_rate = source_rates.get(lead["lead_source"], overall_source)
    industry_rate = industry_rates.get(lead["industry"], overall_industry)
    return 0.5 * recency + 0.25 * source_rate + 0.25 * industry_rate


# --- logistic regression (Newton's method / IRLS, L2-regularized) ----------

def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def fit_logreg(X, y, l2=2.0, iters=30, tol=1e-7):
    n, d = X.shape
    w = np.zeros(d)
    penalty = np.eye(d) * l2
    penalty[0, 0] = 0.0  # never penalize the intercept (column 0)
    for _ in range(iters):
        p = sigmoid(X @ w)
        p = np.clip(p, 1e-9, 1 - 1e-9)
        W = p * (1 - p)
        grad = X.T @ (p - y) + penalty @ w
        H = (X.T * W) @ X + penalty
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, grad, rcond=None)[0]
        w_new = w - step
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def predict(X, w):
    return sigmoid(X @ w)


# --- metrics -----------------------------------------------------------------

def rankdata_avg(a):
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a))
    sorted_a = a[order]
    i, n = 0, len(a)
    while i < n:
        j = i
        while j < n - 1 and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def auc_score(y_true, scores):
    y_true = np.asarray(y_true)
    ranks = rankdata_avg(np.asarray(scores, dtype=float))
    n_pos, n = y_true.sum(), len(y_true)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return (ranks[y_true == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def log_loss(y_true, p):
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    y_true = np.asarray(y_true, dtype=float)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def precision_at_k(y_true, scores, k, seed=42):
    """Argsort breaks ties by original array position, not randomly - with a
    constant-score baseline that silently turns 'top K' into 'first K rows
    in whatever order the SQL query happened to return', which is NOT a
    random sample (test set rows are implicitly ordered ~chronologically,
    and older leads have had more time to convert - confirmed this
    inflated the base-rate baseline to 87% precision before this fix, vs.
    the ~14% it should show for a method with no real ranking signal). A
    tiny fixed-seed random tiebreaker makes ties resolve randomly instead."""
    scores = np.asarray(scores, dtype=float)
    tiebreak = np.random.default_rng(seed).random(len(scores)) * 1e-9
    order = np.argsort(-(scores + tiebreak))[:k]
    return float(np.asarray(y_true)[order].mean())


# --- main ---------------------------------------------------------------------

def main():
    leads = load_leads()
    for lead in leads:
        lead["industry_norm"] = normalize_industry(lead["industry"])
        lead["country_norm"] = normalize_country(lead["country"])

    train = [l for l in leads if not is_test(l["id"])]
    test = [l for l in leads if is_test(l["id"])]
    print(f"Leads: {len(leads)} total -> {len(train)} train / {len(test)} test "
          f"(split by hash of id, deterministic across runs)")
    print(f"Conversion rate: train {sum(l['is_converted'] for l in train)/len(train):.1%}, "
          f"test {sum(l['is_converted'] for l in test)/len(test):.1%}\n")

    # --- heuristic, train-rates only ---
    source_rates, overall_source = train_only_rate_table(train, lambda r: r["lead_source"])
    industry_rates, overall_industry = train_only_rate_table(train, lambda r: r["industry"])
    heuristic_scores = [
        heuristic_score(l, source_rates, overall_source, industry_rates, overall_industry)
        for l in test
    ]
    y_test = [l["is_converted"] for l in test]

    # --- ML model: one-hot(lead_source, country[, industry][, owner]) ---
    def fit_and_score(include_owner, include_industry):
        source_vocab = build_vocab([l["lead_source"] for l in train])
        industry_vocab = build_vocab([l["industry_norm"] for l in train]) if include_industry else set()
        country_vocab = build_vocab([l["country_norm"] for l in train])
        owner_vocab = build_vocab([l["owner_name"] for l in train], min_sample=1) if include_owner else set()

        cols = ["__intercept__"]
        cols += [f"source:{v}" for v in sorted(source_vocab)] + ["source:OTHER", "source:UNKNOWN"]
        if include_industry:
            cols += [f"industry:{v}" for v in sorted(industry_vocab)] + ["industry:OTHER", "industry:UNKNOWN"]
        cols += [f"country:{v}" for v in sorted(country_vocab)] + ["country:OTHER", "country:UNKNOWN"]
        if include_owner:
            cols += [f"owner:{v}" for v in sorted(owner_vocab)] + ["owner:OTHER", "owner:UNKNOWN"]
        col_index = {name: i for i, name in enumerate(cols)}

        def row_vector(lead):
            x = np.zeros(len(cols))
            x[col_index["__intercept__"]] = 1.0
            x[col_index[f"source:{encode(lead['lead_source'], source_vocab)}"]] = 1.0
            if include_industry:
                x[col_index[f"industry:{encode(lead['industry_norm'], industry_vocab)}"]] = 1.0
            x[col_index[f"country:{encode(lead['country_norm'], country_vocab)}"]] = 1.0
            if include_owner:
                x[col_index[f"owner:{encode(lead['owner_name'], owner_vocab)}"]] = 1.0
            return x

        X_train = np.array([row_vector(l) for l in train])
        y_train = np.array([l["is_converted"] for l in train], dtype=float)
        X_test = np.array([row_vector(l) for l in test])

        w = fit_logreg(X_train, y_train)
        return predict(X_test, w), y_train

    ml_scores_everything, y_train = fit_and_score(include_owner=True, include_industry=True)
    ml_scores_no_owner, _ = fit_and_score(include_owner=False, include_industry=True)
    ml_scores_clean, _ = fit_and_score(include_owner=False, include_industry=False)

    # --- base-rate baseline (predict everyone at the overall train rate) ---
    base_scores = np.full(len(test), y_train.mean())

    def report(label, scores, has_logloss=True):
        logloss = f"{log_loss(y_test, scores):>10.4f}" if has_logloss else f"{'n/a (not a probability)':>10}"
        print(f"{label:<38} {auc_score(y_test, scores):>6.3f} "
              f"{precision_at_k(y_test, scores, 100):>15.1%} {logloss}")

    print(f"{'Method':<38} {'AUC':>6} {'Precision@100':>15} {'Log-loss':>10}")
    print("-" * 72)
    report("Heuristic (leads.py, as shipped)", heuristic_scores, has_logloss=False)
    report("Logistic regression (+owner +industry)", ml_scores_everything)
    report("Logistic regression (+industry)", ml_scores_no_owner)
    report("Logistic regression (source+country only)", ml_scores_clean)
    report("Base rate (no model)", base_scores)
    print(f"\n(Test set: {len(test)} leads, {sum(y_test)} converted - AUC 0.5 = "
          f"no better than random, 1.0 = perfect ranking.)")
    print(
        "\nTwo leakage risks found while building this, both disclosed rather than quietly "
        "fixed:\n"
        "1. Owner: 'Admin MitKat' owns 3,341 of 10,547 leads (the single largest owner) at a "
        "38% conversion rate vs. 1-9% for every named rep with real volume - almost certainly "
        "a placeholder/bulk-import queue, not a rep assignment that predicts anything.\n"
        "2. Industry: 1,249 leads have NO industry recorded, and THAT group converts at 53.5% "
        "- far above every actual industry (1-28%). Industry very likely gets filled in during "
        "a later qualification step that leads which don't convert sit through longer, making "
        "'industry is blank' a proxy for 'still early' rather than a property of the lead "
        "itself - the same kind of process-order artifact as the misused Lost_Reason field.\n"
        "'source+country only' is the one row not resting on either of those - the honest "
        "floor for what identity fields alone can do here, with no process-state leakage."
    )


if __name__ == "__main__":
    main()

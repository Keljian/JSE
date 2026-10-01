"""Learned pre-triage filter: skip ads the scorer would certainly reject.

Leaf layer, same rules as `screening`: pure logic, no database, no LLM. The
caller supplies labelled rows (ads the LLM has already scored for a lane) and
gets back a model that can say "this ad is near-certain to score under 35" for
new ads, so the triage call can be skipped.

Why it is safe to skip at all, given that false negatives cost more than false
positives in this search:

- It is per lane. An ad that is noise for the IT lane may be the target for
  the engineering lane, so every lane learns from its own scores.
- The threshold is chosen on held-out ads, not assumed. It must lose at most
  MAX_KEEPER_LOSS of the ads the scorer passed through to full analysis
  (score >= KEEPER_SCORE), none at all of the ads it scored PROTECTED_SCORE or
  higher, and at least MIN_SKIP_PRECISION of what it skips must have scored
  under KEEPER_SCORE (so would never have reached full analysis anyway). If no
  threshold meets all three, or the lane has too few scored ads to measure
  them, the lane is not filtered.
- A fixed AUDIT_SHARE of would-be skips still goes to the LLM, chosen by job
  id so the same ad is always audited or always not. A skipped ad the audit
  later scores highly is the measured miss rate, reported every run.
- A skipped ad keeps its row and a readable reason. Re-analysing it runs the
  full chain as normal.

The model is Bernoulli naive Bayes over title and opening-text tokens. It is
crude and its probabilities are badly calibrated, which does not matter here:
only the ordering is used, and the cut-off is measured on held-out data.
"""

import math
import re
import zlib

REJECT_SCORE = 35          # an ad scored under this is a clear no
KEEPER_SCORE = 60          # an ad scored at or above this earned full analysis
PROTECTED_SCORE = 70       # nothing at or above this may be lost in validation
MAX_KEEPER_LOSS = 0.01     # at most 1% of held-out keepers may fall past the cut
MIN_SKIP_PRECISION = 0.97  # at least 97% of held-out skips must have scored under KEEPER_SCORE
MIN_COVERAGE = 0.05        # a model that skips under 5% of ads is not worth running
MIN_LABELLED = 400
MIN_KEEPERS = 40
AUDIT_SHARE = 0.05
VALIDATION_SHARE = 0.25
MIN_DOC_FREQ = 3
DESCRIPTION_CHARS = 1200

_TOKEN = re.compile(r"[a-z][a-z0-9+#.-]{1,30}")
_STOP = frozenset(
    "and the for with you your our are will this that from have has can all "
    "who what when where they their them its was were been being into about "
    "role job work team join apply position opportunity company".split()
)


def tokens(title, description):
    """Title words are kept apart from body words: 'nurse' in a title is a far
    stronger signal than 'nurse' in a paragraph about the employer."""
    out = set()
    for word in _TOKEN.findall(str(title or "").lower()):
        if word not in _STOP:
            out.add("t:" + word)
    for word in _TOKEN.findall(str(description or "")[:DESCRIPTION_CHARS].lower()):
        if word not in _STOP:
            out.add(word)
    return out


def _bucket(job_id):
    return zlib.crc32(str(job_id).encode("utf-8")) % 1000 / 1000.0


def is_audit(job_id):
    """Deterministic audit selection, so re-runs do not re-roll the dice."""
    return zlib.crc32(f"audit:{job_id}".encode("utf-8")) % 1000 < AUDIT_SHARE * 1000


class _NaiveBayes:
    def __init__(self, docs, labels):
        reject_docs = sum(1 for y in labels if y)
        other_docs = len(labels) - reject_docs
        counts = {}
        for doc, is_reject in zip(docs, labels):
            for tok in doc:
                pair = counts.setdefault(tok, [0, 0])
                pair[0 if is_reject else 1] += 1
        self.prior = math.log((reject_docs + 1) / (other_docs + 1))
        self.weights = {}
        for tok, (in_reject, in_other) in counts.items():
            if in_reject + in_other < MIN_DOC_FREQ:
                continue
            p_reject = (in_reject + 1) / (reject_docs + 2)
            p_other = (in_other + 1) / (other_docs + 2)
            self.weights[tok] = math.log(p_reject / p_other)

    def score(self, doc):
        """Log-odds that the ad is a reject. Higher means more certain."""
        return self.prior + sum(self.weights.get(tok, 0.0) for tok in doc)


class LanePrefilter:
    """A trained model plus the cut-off measured for it, or a disabled stub."""

    def __init__(self, model=None, threshold=None, stats=None, reason=""):
        self.model = model
        self.threshold = threshold
        self.stats = stats or {}
        self.reason = reason

    @property
    def enabled(self):
        return self.model is not None and self.threshold is not None

    def decide(self, job_id, title, description):
        """None to analyse normally, else a verdict dict.

        verdict "skip": do not triage. verdict "audit": would have skipped, but
        this one is in the audit sample, so triage it and compare.
        """
        if not self.enabled:
            return None
        value = self.model.score(tokens(title, description))
        if value < self.threshold:
            return None
        verdict = "audit" if is_audit(job_id) else "skip"
        return {
            "verdict": verdict,
            "score": round(value, 2),
            "reason": (
                f"Prefilter: near-certain reject for this lane (score {value:.1f} against a "
                f"cut-off of {self.threshold:.1f}, measured on {self.stats.get('validation', 0)} "
                f"held-out ads). Not analysed; re-analyse to score it."
            ),
        }


def _choose_threshold(scored):
    """Lowest cut-off meeting every guard on held-out (score, llm_score) pairs."""
    keepers = sum(1 for _, s in scored if s >= KEEPER_SCORE)
    if not keepers:
        return None, {}
    best = None
    ordered = sorted(scored, key=lambda pair: pair[0], reverse=True)
    skipped = harmless = rejects = lost = 0
    for index, (value, llm_score) in enumerate(ordered):
        skipped += 1
        if llm_score < REJECT_SCORE:
            rejects += 1
        if llm_score >= KEEPER_SCORE:
            lost += 1
        else:
            harmless += 1
        if llm_score >= PROTECTED_SCORE:
            break  # every lower cut-off would also lose this one
        # Ties: only a cut-off strictly between distinct values is realisable.
        if index + 1 < len(ordered) and ordered[index + 1][0] == value:
            continue
        if lost / keepers > MAX_KEEPER_LOSS or harmless / skipped < MIN_SKIP_PRECISION:
            continue
        best = (value, {
            "validation": len(scored),
            "skip_share": round(skipped / len(scored), 3),
            "skip_precision": round(harmless / skipped, 3),
            "skip_reject_share": round(rejects / skipped, 3),
            "keepers_lost": lost,
            "keepers": keepers,
        })
    if not best:
        return None, {"validation": len(scored), "keepers": keepers}
    return best


def train(rows):
    """rows: iterable of (job_id, title, description, llm_score). Returns a LanePrefilter."""
    rows = [r for r in rows if r[3] is not None]
    keepers = sum(1 for r in rows if r[3] >= KEEPER_SCORE)
    if len(rows) < MIN_LABELLED or keepers < MIN_KEEPERS:
        return LanePrefilter(reason=f"not enough scored ads yet ({len(rows)} scored, {keepers} at {KEEPER_SCORE}+)")
    fit, held = [], []
    for row in rows:
        (held if _bucket(row[0]) < VALIDATION_SHARE else fit).append(row)
    model = _NaiveBayes(
        [tokens(r[1], r[2]) for r in fit],
        [r[3] < REJECT_SCORE for r in fit],
    )
    scored = [(model.score(tokens(r[1], r[2])), r[3]) for r in held]
    threshold, stats = _choose_threshold(scored)
    if threshold is None:
        return LanePrefilter(stats=stats, reason="no cut-off met the loss and precision guards")
    if stats["skip_share"] < MIN_COVERAGE:
        return LanePrefilter(stats=stats, reason=f"would skip only {stats['skip_share']:.0%} of ads")
    stats["trained_on"] = len(fit)
    return LanePrefilter(model=model, threshold=threshold, stats=stats)

"""Phase 7 DNA engine — the anti-horoscope guarantees (pure, no DB).

These tests encode the philosophy: no insight below its gate, every template
falsifiable and fillable, and DNA that actually MOVES when the reader changes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import dna_signals as S
from app.services.dna_insights import REGISTRY, build_dna, generate_insights
from app.services.dna_signals import GATES, MIN_BOOKS_FOR_DNA

NOW = datetime.now(timezone.utc)


def sig(emotions, intensity=5, days=0, status="finished", arc_start=None, arc_end=None):
    return S.EntrySig(emotions=list(emotions), intensity=intensity,
                      ts=NOW - timedelta(days=days), status=status,
                      arc_start=arc_start, arc_end=arc_end)


def categories(sigs, reads_for=None):
    """All insight categories build_dna would emit (uncapped)."""
    res = build_dna(sigs, reads_for, insight_limit=99)
    if not res.get("enough"):
        return set()
    return {i["category"] for i in res["insights"]}


# ── Below the floor: honest "not enough yet" (B7.6, DoD) ──

def test_below_five_books_returns_not_enough():
    res = build_dna([sig(["comfort"]) for _ in range(4)])
    assert res["enough"] is False
    assert res["book_count"] == 4
    assert res["needed"] == MIN_BOOKS_FOR_DNA
    assert "insights" not in res and "archetype" not in res


# ── The anti-horoscope test: nothing emitted below its gate ──

# (category, builder producing N books that DO exhibit the signal, reads_for)
def _blind(n):        return [sig(["comfort"]) for _ in range(n)]                       # 12 never-tagged
def _intensity(n):    return [sig(["comfort"], intensity=9) for _ in range(n)]          # share_high=1
def _range(n):        return [sig(["comfort"]) for _ in range(n)]
def _pairing(n):      return [sig(["comfort", "dread"]) for _ in range(n)]              # co-occur
def _contra(n):       return [sig(["devastation"], intensity=9) for _ in range(n)]      # vs stated comfort
def _abandon(n):      return ([sig(["comfort"]) for _ in range(n - 3)]
                              # `abandoned`, not `reading`: a book in progress has not
                              # been put down. Migration 022 added the explicit status.
                              + [sig(["dread"], status="abandoned") for _ in range(3)])  # 3 abandoned dread
def _arc(n):          return [sig(["grief"], arc_start="dread", arc_end="catharsis") for _ in range(n)]

CASES = [
    ("blind_spot", _blind, None),
    ("intensity_signature", _intensity, None),
    ("range", _range, None),
    ("pairing", _pairing, None),
    ("contradiction", _contra, ["comfort"]),
    ("abandonment", _abandon, None),
    ("arc", _arc, None),
]


@pytest.mark.parametrize("category,builder,reads_for", CASES)
def test_no_insight_below_its_gate(category, builder, reads_for):
    gate = GATES[category]
    # One book under the gate: the category must NOT appear, even though the data
    # would support it. This is the whole defence against confident nonsense.
    below = builder(gate - 1)
    if len(below) >= MIN_BOOKS_FOR_DNA:
        assert category not in categories(below, reads_for)
    # At the gate: it's allowed to appear (data is present by construction).
    assert category in categories(builder(gate), reads_for)


# ── Every template is signed off and its slots are fillable (B7.7) ──

def test_every_template_is_signed_off():
    for t in REGISTRY:
        assert t.signed_off, f"{t.category}/{t.variant} not signed off"


def test_every_applicable_template_renders_without_error():
    """Every template's slots must fill to a real string. Some variants are mutually
    exclusive (e.g. 8-or-nothing vs careful rater), so we try each template against
    both an 'intense' and a 'careful' context and require at least one to apply."""
    base = {
        # Gates and "based on N books" read tagged_count; book_count is only for
        # copy about the shelf itself. Every book in this fixture carries a tag.
        "book_count": 40,
        "tagged_count": 40,
        "arc_count": 20,          # arc carries its own denominator
        "range": {"entropy": 0.4, "distinct": 4},
        "range_prev_distinct": 9,
        "blind_spots": ["tenderness"],
        "rare": [("amusement", 0.03)],
        "top_pair": (("comfort", "dread"), 12),
        "stated": {"stated": "comfort", "revealed_top": "devastation",
                   "revealed_hi": "devastation", "delta": 2.3,
                   "verdict": "contradicted", "reason": None,
                   # Frequency claims compare COUNTS, not the rank — a tie in
                   # `revealed_top` is not "more often".
                   "stated_books": 12, "revealed_top_books": 20,
                   "evidence": {"stated": {"emotion": "comfort", "books": 12, "avg": 6.1},
                                "compared": {"emotion": "devastation", "books": 9, "avg": 8.4}}},
        "abandonment": {"emotion": "amusement", "fraction": 0.8,
                        "dnf_reason": "lost_me", "dnf_reason_books": 4},
        "arc": {"start": "dread", "end": "catharsis", "fraction": 0.7, "n_arc": 20},
        "drift": 0.4, "has_two_snapshots": True,
        "old_top": "comfort", "new_top": "grief",
    }
    ctx_intense = {**base, "intensity_signature": {"mean": 8.5, "variance": 0.5, "skew": 0.0,
                   "share_high": 0.8, "band_lo": 8, "band_hi": 9}}
    ctx_careful = {**base, "intensity_signature": {"mean": 6.5, "variance": 1.0, "skew": 0.0,
                   "share_high": 0.1, "band_lo": 6, "band_hi": 7}}

    # The stated-vs-revealed verdicts are mutually exclusive by construction, so
    # the confirmation templates need their own contexts to be reachable at all.
    confirmed = {**base["stated"], "verdict": "confirmed", "delta": -2.3,
                 "revealed_hi": "devastation",
                 "evidence": {"stated": {"emotion": "comfort", "books": 12, "avg": 8.4},
                              "compared": {"emotion": "devastation", "books": 9, "avg": 6.1}}}
    ctx_confirmed = {**ctx_intense, "stated": {**confirmed, "revealed_top": "comfort"}}
    ctx_confirmed_elsewhere = {**ctx_intense, "stated": {**confirmed, "revealed_top": "devastation"}}

    # The two abandonment variants are mutually exclusive by construction: the
    # emotion-only sentence is suppressed whenever the reader told us why, so it
    # needs a context where they didn't.
    ctx_no_dnf_reason = {**ctx_intense,
                         "abandonment": {"emotion": "amusement", "fraction": 0.8,
                                         "dnf_reason": None, "dnf_reason_books": 0}}

    # The three DNF-tally variants partition on the top reason's share, so each
    # needs its own context: all-one-reason, a dominant one, and a flat spread.
    def _dnf_ctx(counts):
        stated = sum(n for _, n in counts)
        return {**ctx_intense, "dnf_stated_count": stated, "dnf_reasons": {
            "counts": [{"reason": r, "books": n} for r, n in counts],
            "top_reason": counts[0][0], "top_books": counts[0][1],
            "stated": stated, "dnf_total": stated,
            "unanimous": len(counts) == 1,
            "share": round(counts[0][1] / stated, 2),
        }}

    ctx_dnf_unanimous = _dnf_ctx([("bored", 4)])
    ctx_dnf_dominant = _dnf_ctx([("bored", 5), ("lost_me", 1), ("too_much", 1)])
    ctx_dnf_spread = _dnf_ctx([("bored", 2), ("lost_me", 2), ("too_much", 2)])

    candidates = [ctx_intense, ctx_careful, ctx_confirmed, ctx_confirmed_elsewhere,
                  ctx_no_dnf_reason, ctx_dnf_unanimous, ctx_dnf_dominant,
                  ctx_dnf_spread]
    for t in REGISTRY:
        ctx = next((c for c in candidates if t.applicable(c)), None)
        assert ctx is not None, f"{t.category}/{t.variant} applicable to no crafted ctx"
        text = t.render(ctx)
        assert isinstance(text, str) and len(text) > 10
        assert "{" not in text and "}" not in text  # no unfilled slots
        assert 0.0 <= float(t.surprise(ctx)) <= 1.0


# ── DNA moves when the reader reads (B7.3, DoD) ──

def test_dna_moves_when_recent_books_flip():
    # A comfort reader whose comfort reading is now ~a year in the past…
    sigs = [sig(["comfort", "tenderness"], intensity=6, days=330 + i * 10) for i in range(15)]
    settled = build_dna(sigs)
    assert settled["archetype"]["id"] == "comfort_architect"
    # …then three recent devastating books. Recency weighting lets the fresh reading
    # dominate once the old has decayed — the headline moves (DoD).
    sigs += [sig(["devastation", "grief"], intensity=9, days=i) for i in range(3)]
    moved = build_dna(sigs)

    assert moved["drift"] > 0.1                       # the profile genuinely moved
    assert moved["archetype"]["id"] != settled["archetype"]["id"]  # headline changed


def test_uniform_reader_shows_no_drift():
    sigs = [sig(["comfort"], days=i * 10) for i in range(20)]
    res = build_dna(sigs)
    assert res["drift"] < 0.05


# ── stated_vs_revealed only fires with a stated preference ──

def test_contradiction_requires_reads_for():
    sigs = [sig(["devastation"], intensity=9) for _ in range(12)]
    assert "contradiction" not in categories(sigs, reads_for=None)
    assert "contradiction" in categories(sigs, reads_for=["comfort"])


# ── Locked list is honest and always includes seasonality ──

def test_locked_list_names_what_unlocks_and_includes_seasonality():
    _, locked, _ = generate_insights({"book_count": 6, "tagged_count": 6, "arc_count": 0, "blind_spots": [], "range": {"distinct": 3, "entropy": 0.3},
                                   "intensity_signature": {"share_high": 0.1, "variance": 3.0},
                                   "stated": None, "abandonment": None, "arc": None, "top_pair": None,
                                   "rare": [], "drift": 0.0, "has_two_snapshots": False,
                                   "old_top": None, "new_top": None, "range_prev_distinct": None})
    cats = {l["category"] for l in locked}
    assert "seasonality" in cats
    assert "pairing" in cats  # gate 15, not reached at 6 books
    for l in locked:
        assert l["reason"] and l["unlocks_at"]
        assert l["label"] and "opens" in l


def test_earned_column_is_the_mirror_image_of_locked():
    """Every gate is either earned or locked — never both, never neither —
    except seasonality, which is locked until 12 months regardless of count."""
    from app.services.dna_insights import GATES
    ctx = {"book_count": 12, "tagged_count": 12, "arc_count": 6, "dnf_stated_count": 0,
           "blind_spots": [], "range": {"distinct": 3, "entropy": 0.3},
           "intensity_signature": {"share_high": 0.1, "variance": 3.0},
           "stated": None, "abandonment": None, "arc": None, "top_pair": None,
           "rare": [], "drift": 0.0, "has_two_snapshots": False,
           "old_top": None, "new_top": None, "range_prev_distinct": None}
    _, locked, earned = generate_insights(ctx)
    gated = {c for c in GATES if c not in ("frequency", "intensity")}
    earned_cats = {r["category"] for r in earned}
    locked_cats = {l["category"] for l in locked}
    # intensity_signature/range/blind_spot/contradiction/abandonment gate at ≤12 → earned
    assert {"intensity_signature", "range", "blind_spot", "abandonment"} <= earned_cats
    # pairing/drift (15) and dnf_reason (3 stated, have 0) → locked
    assert {"pairing", "drift", "dnf_reason"} <= locked_cats
    assert "seasonality" not in earned_cats and "seasonality" in locked_cats
    # partition: no gate is in both, none is missing (bar contradiction, which is
    # emitted as a rendered insight here and so appears in neither list)
    assert not (earned_cats & locked_cats)
    for r in earned:
        assert r["have"] >= r["need"] and r["label"]


# ── The TBR pile is not a reading (B2.2 fast-add) ──

def test_want_to_read_changes_nothing_about_the_dna():
    """Shelving a book must not move a single claim DNA makes about books.

    A `want_to_read` carries no emotions, so it never touched the emotion
    vectors — but it is still a row with a placeholder `intensity`, and that was
    enough to inflate `book_count` and rewrite the reader's rating style. One-tap
    fast-add makes shelving cheap and high-volume, which turns that from a rounding
    error into a profile written by books nobody opened.

    Asserted over the whole payload rather than the two fields known to have
    broken, so a signal added later that forgets the boundary fails here.
    """
    read = ([sig(["grief", "longing"], intensity=9) for _ in range(3)]
            + [sig(["comfort"], intensity=8) for _ in range(3)])
    pile = [sig([], intensity=5, status="want_to_read") for _ in range(20)]

    assert build_dna(read + pile) == build_dna(read)


def test_want_to_read_alone_never_unlocks_a_rating_style():
    """A shelf of pure intention stays below the floor instead of inventing a reader.

    Twenty placeholder 5s clear every count-based gate (rating style needs 8) while
    saying nothing about how this person actually rates.
    """
    res = build_dna([sig([], intensity=5, status="want_to_read") for _ in range(20)])
    assert res["enough"] is False
    assert res["book_count"] == 0


def test_opened_statuses_is_every_status_but_want_to_read():
    """The boundary is defined by exclusion, so a new status is opened by default.

    A status added later is a way of reading until someone says otherwise; only
    the pile is not.
    """
    import typing

    from app.schemas.entry import EntryStatus

    assert S.OPENED_STATUSES == set(typing.get_args(EntryStatus)) - {"want_to_read"}


# ── #4: DNF reasons — the reader's own answers, counted ──

def dnf(reason, status="abandoned"):
    """A book put down, optionally with a stated reason."""
    return S.EntrySig(emotions=["grief"], intensity=7, ts=NOW,
                      status=status, dnf_reason=reason)


def test_dnf_tally_needs_three_stated_reasons():
    """Two answers are an anecdote. The gate counts STATED REASONS, not books —
    a reader with 400 books and two stated reasons has not earned this claim,
    and one with 12 books and three has."""
    assert S.dnf_reasons([dnf("bored"), dnf("bored")]) is None
    assert S.dnf_reasons([dnf("bored")] * 3) is not None


def test_dnf_tally_ignores_books_put_down_without_a_reason():
    """An unanswered 'why?' is not evidence of anything and must not pad the
    denominator — "5 of the 7 you put down" has to be checkable by hand."""
    res = S.dnf_reasons([dnf("bored")] * 3 + [dnf(None)] * 4)
    assert res["stated"] == 3
    assert res["dnf_total"] == 7


def test_dnf_tally_ignores_finished_books():
    """Only books actually put down. A finished book carrying a stale dnf_reason
    (edited from abandoned back to finished) must not count as an abandonment."""
    finished_with_reason = S.EntrySig(
        emotions=["comfort"], intensity=8, ts=NOW, status="finished", dnf_reason="bored"
    )
    assert S.dnf_reasons([dnf("bored")] * 3 + [finished_with_reason])["stated"] == 3


def test_dnf_tally_counts_paused_as_put_down():
    """Same rule abandonment() uses: paused is a book the reader stopped reading."""
    res = S.dnf_reasons([dnf("bored", status="paused")] * 3)
    assert res["stated"] == 3


def test_dnf_insight_fires_without_any_emotion_correlation():
    """The whole point of #4.

    `abandonment()` only speaks when some emotion correlates with not finishing.
    A reader whose DNFs share no emotion but share a stated reason was told
    nothing, despite having answered the question every time.
    """
    # Every DNF carries a DIFFERENT emotion, so no emotion can correlate.
    varied = [
        S.EntrySig(emotions=[e], intensity=7, ts=NOW, status="abandoned", dnf_reason="bored")
        for e in ("grief", "awe", "confusion", "desire")
    ]
    sigs = [sig(["comfort"]) for _ in range(8)] + varied
    res = build_dna(sigs, insight_limit=99)

    cats = {i["category"] for i in res["insights"]}
    assert "dnf_reason" in cats


def test_dnf_insight_text_is_countable_by_hand():
    """Every figure in the sentence must be one the reader can verify on their
    own shelf — the falsifiability rule (B7.7)."""
    sigs = ([sig(["comfort"]) for _ in range(8)]
            + [dnf("bored")] * 5 + [dnf("lost_me")] * 2)
    res = build_dna(sigs, insight_limit=99)
    text = next(i["text"] for i in res["insights"] if i["category"] == "dnf_reason")

    assert "7 books" in text          # stated reasons, not the whole shelf
    assert "5 bored you" in text
    assert "2 lost you" in text


def test_dnf_tally_agrees_in_number():
    """"1 were too much" is the seam that makes hand-written copy read as generated."""
    sigs = ([sig(["comfort"]) for _ in range(8)]
            + [dnf("bored")] * 3 + [dnf("too_much")] + [dnf("badly_written")])
    text = next(i["text"] for i in build_dna(sigs, insight_limit=99)["insights"]
                if i["category"] == "dnf_reason")

    assert "1 was too much" in text
    assert "1 was badly written" in text
    assert "were too much" not in text


def test_dnf_gate_population_is_stated_reasons_not_tagged_books():
    """A claim carries its own denominator.

    Gating on tagged_count would deny this finding to a reader who answers "why
    did you put it down?" every time but tags few books.
    """
    from app.services.dna_insights import GATE_POPULATION
    assert GATE_POPULATION["dnf_reason"] == "dnf_stated_count"


def test_every_gate_population_key_is_produced(monkeypatch):
    """`_population` tolerates a missing key so partial contexts don't crash.

    That tolerance would also swallow a typo in GATE_POPULATION — the category
    would silently stay locked forever, with no error anywhere. So every
    denominator named there must actually be present in the context build_dna
    really constructs, captured here rather than reconstructed.
    """
    from app.services import dna_insights as DI

    seen = {}
    real = DI.generate_insights
    def _capture(ctx, **kw):
        seen.update(ctx)
        return real(ctx, **kw)
    monkeypatch.setattr(DI, "generate_insights", _capture)

    sigs = ([sig(["comfort"], arc_start="comfort", arc_end="catharsis")
             for _ in range(12)]
            + [dnf("bored")] * 3)
    assert DI.build_dna(sigs, insight_limit=99)["enough"] is True

    for category, key in DI.GATE_POPULATION.items():
        assert key in seen, f"{category}'s denominator {key!r} is never set"
        assert isinstance(seen[key], int) and seen[key] >= 0

    assert seen["dnf_stated_count"] == 3

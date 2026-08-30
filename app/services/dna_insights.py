"""Insight templates — hand-written sentences, hard data in the slots (Phase 7, B7.7).

NOT LLM-generated. Generated prose about someone's emotional life is
nondeterministic, occasionally hallucinatory, and reads as *more* artificial, not
less. The voice is Shruti's; the facts are the reader's. That's what makes it feel
like a reader wrote it — because one did.

Every template obeys the falsifiability rule: could this sentence be true of a
different reader? If yes, it doesn't ship. Each fills its slots from the reader's
own numbers. Ranking is by internal `surprise` magnitude — NOT population-relative
(rarity is deferred; a rarity claim would need a population baseline we don't have).
"""

from dataclasses import dataclass
from typing import Callable

from app.services import dna_signals as sig
from app.services.dna_signals import GATES, MIN_BOOKS_FOR_DNA
from app.utils.emotions import EMOTIONS_BY_SLUG

# What each locked insight is actually counting, in plain words (B7.6).
#
# NONE of these gates count titles on the shelf — they count the books that could
# supply THAT insight's evidence (see GATE_POPULATION). So the copy must never say
# a bare "5 books": a reader with 6 books shelved and 1 tagged would read "Arc
# needs 5 books", check their shelf, and think the app is broken. Each string
# below names the specific population, and the locked row is rendered with the
# reader's current count against it ("you have 2"), so "why is this still locked?"
# answers itself.
#
# `unit` follows "waits on {need} " and is the requirement in the reader's terms.
# `note` is an optional tail for a second condition that isn't just a count.
UNLOCK_UNITS: dict[str, tuple[str, str]] = {
    "intensity_signature": ("books with a feeling tagged", "enough to read your rating style"),
    "range": ("books with a feeling tagged", "enough to measure how wide you reach"),
    "blind_spot": ("books with a feeling tagged", "before an untouched feeling means anything"),
    "contradiction": ("books with a feeling tagged", "plus telling it what you read for"),
    "abandonment": ("books with a feeling tagged", "a few of them left unfinished"),
    "pairing": ("books with a feeling tagged", "to see which feelings travel together"),
    "drift": ("books with a feeling tagged", "across two snapshots, to see movement"),
    "dnf_reason": ("books you've set down and named a reason for", ""),
    "arc": ("books finished through the three-beat flow", "not just marked done"),
    "seasonality": ("books across a full year of reading", ""),
}

# The Register (the profile's progress ledger) lists every gate as one row —
# earned or not-yet — so each needs a short reader-facing name and a one-line
# "what it shows you once it's earned". `opens` completes "shows you …".
GATE_LABELS: dict[str, str] = {
    "intensity_signature": "Intensity signature",
    "range": "Range",
    "blind_spot": "Blind spot",
    "contradiction": "Contradiction",
    "abandonment": "Abandonment",
    "pairing": "Pairing",
    "drift": "Drift",
    "dnf_reason": "DNF reason",
    "arc": "Arc",
    "seasonality": "Seasonality",
}
GATE_OPENS: dict[str, str] = {
    "intensity_signature": "how you use the 1–10 scale — all-or-nothing, or careful",
    "range": "how wide across the vocabulary your reading reaches",
    "blind_spot": "the feelings you never once reach for",
    "contradiction": "where what you read for and what you rate highest disagree",
    "abandonment": "what a book tends to be doing when you put it down",
    "pairing": "which feelings travel together on your shelf",
    "drift": "how the shape of your reading has moved over time",
    "dnf_reason": "the reason your unfinished books have in common",
    "arc": "where your books start and where they leave you",
    "seasonality": "how your reading changes with the calendar",
}


def _name(slug: str | None) -> str:
    if not slug:
        return ""
    meta = EMOTIONS_BY_SLUG.get(slug)
    return meta["name"] if meta else slug.title()


@dataclass
class InsightTemplate:
    category: str
    variant: str
    min_n: int
    signed_off: bool                       # a human (Shruti) approved this sentence
    applicable: Callable[[dict], bool]     # is the data present to fill it?
    render: Callable[[dict], str]
    surprise: Callable[[dict], float]      # ranking magnitude, 0..1 (not population)


# ── 1. Contradiction — the gold (gate 10, needs reads_for) ──
#
# `stated` now carries a VERDICT, not just a gap: contradicted / confirmed /
# inconclusive. Every template reading it must check which, or a reader who was
# right about themselves gets handed the accusation copy.
def _verdict(c) -> str | None:
    s = c.get("stated")
    return s.get("verdict") if s else None


def _out_frequented(c) -> bool:
    """Strictly more books, not merely a higher rank. `revealed_top` ties silently,
    and "you reach for it more often" is false on equal counts."""
    s = c["stated"]
    return bool(s.get("revealed_top") and s["revealed_top"] != s["stated"]
                and s.get("revealed_top_books", 0) > s.get("stated_books", 0))


def _contra_ok(c):
    s = c.get("stated")
    return bool(s and _verdict(c) == "contradicted"
                and s.get("revealed_hi") and s["revealed_hi"] != s["stated"])

CONTRADICTION = [
    InsightTemplate(
        "contradiction", "intensity_gap", GATES["contradiction"], True, _contra_ok,
        lambda c: (f"You said you read for {_name(c['stated']['stated'])}. "
                   f"The books you rate highest are the ones that gut you — "
                   f"you give them {c['stated']['delta']} more points."),
        lambda c: min(1.0, c["stated"]["delta"] / 4.0),
    ),
    InsightTemplate(
        # A FREQUENCY claim, not an intensity one, so it stands on its own for an
        # inconclusive verdict. But it must not fire alongside `confirmed`: "your
        # centre of gravity is elsewhere" filed under contradiction, next to a
        # confirmation, is the two halves of the payload arguing with each other.
        "contradiction", "center_of_gravity", GATES["contradiction"], True,
        lambda c: bool(c.get("stated") and _verdict(c) != "confirmed"
                       and _out_frequented(c)),
        lambda c: (f"You told me {_name(c['stated']['stated'])}. "
                   f"Your shelf's center of gravity is {_name(c['stated']['revealed_top'])}."),
        lambda c: 0.7,
    ),
]


# ── 1b. Confirmation — the same measurement, the other sign ──
#
# Deliberately NOT a compliment. "You know yourself well" is a sentence that could
# be true of any reader; "your comfort books average 8.1 and the nearest thing to
# them averages 6.4" is true of this one. Both numbers are shown so the reader can
# check the verdict rather than accept it.
def _confirmed_ok(c):
    s = c.get("stated")
    return bool(s and _verdict(c) == "confirmed" and s.get("evidence"))


def _ev(c):
    return c["stated"]["evidence"]


CONFIRMATION = [
    InsightTemplate(
        "confirmation", "holds_up", GATES["contradiction"], True,
        lambda c: _confirmed_ok(c) and not _out_frequented(c),
        lambda c: (f"You said you read for {_name(_ev(c)['stated']['emotion'])}. "
                   f"Those books average {_ev(c)['stated']['avg']} across "
                   f"{_ev(c)['stated']['books']}; the closest thing to them, "
                   f"{_name(_ev(c)['compared']['emotion'])}, averages "
                   f"{_ev(c)['compared']['avg']}."),
        lambda c: min(0.55, abs(c["stated"]["delta"]) / 4.0),
    ),
    InsightTemplate(
        # Reaches for one thing more often, rates another higher. Both facts, one
        # sentence — and the reason `center_of_gravity` is suppressed here.
        "confirmation", "rates_above_what_it_reaches_for", GATES["contradiction"], True,
        lambda c: _confirmed_ok(c) and _out_frequented(c),
        lambda c: (f"You reach for {_name(c['stated']['revealed_top'])} more often — "
                   f"{c['stated']['revealed_top_books']} books against "
                   f"{c['stated']['stated_books']}. "
                   f"You rate {_name(_ev(c)['stated']['emotion'])} higher: "
                   f"{_ev(c)['stated']['avg']} against "
                   f"{_ev(c)['compared']['avg']} for "
                   f"{_name(_ev(c)['compared']['emotion'])}."),
        lambda c: min(0.55, abs(c["stated"]["delta"]) / 4.0),
    ),
]

# ── 2. Blind spot (gate 10) ──
BLIND_SPOT = [
    InsightTemplate(
        "blind_spot", "never", GATES["blind_spot"], True,
        lambda c: bool(c.get("blind_spots")),
        # "tagged N books", not "logged N books": the claim is about books the
        # reader put a feeling on, and an untagged import cannot evidence a
        # never-reached-for emotion. Quoting the raw shelf here was the sentence
        # that made a 5-book finding look like a 30-book one.
        lambda c: (f"You've tagged {c['tagged_count']} books. "
                   f"You have never once reached for {_name(c['blind_spots'][0])}."),
        lambda c: 0.9,
    ),
    InsightTemplate(
        "blind_spot", "rare", GATES["blind_spot"], True,
        lambda c: bool(c.get("rare")),
        lambda c: (f"{_name(c['rare'][0][0])} shows up in under "
                   f"{max(1, round(c['rare'][0][1] * 100))}% of what you read. "
                   f"Not never. Close."),
        lambda c: 0.6,
    ),
]

# ── 3. Drift (gate 15 + two snapshots) ──
DRIFT = [
    InsightTemplate(
        "drift", "gave_way", GATES["drift"], True,
        lambda c: bool(c.get("has_two_snapshots") and c.get("drift", 0) >= 0.15
                       and c.get("old_top") and c.get("new_top") and c["old_top"] != c["new_top"]),
        lambda c: (f"Your reading moved. {_name(c['old_top'])} gave way to "
                   f"{_name(c['new_top'])} across your recent books."),
        lambda c: min(1.0, c.get("drift", 0) * 2),
    ),
]

# ── 4. Intensity signature (gate 8) ──
INTENSITY = [
    InsightTemplate(
        "intensity_signature", "eight_or_nothing", GATES["intensity_signature"], True,
        lambda c: c.get("intensity_signature", {}).get("share_high", 0) >= 0.5,
        lambda c: (f"You don't have mild opinions. "
                   f"{round(c['intensity_signature']['share_high'] * 100)}% of your books "
                   f"land at 8 or above."),
        lambda c: c["intensity_signature"]["share_high"],
    ),
    InsightTemplate(
        "intensity_signature", "careful", GATES["intensity_signature"], True,
        lambda c: (c.get("intensity_signature", {}).get("share_high", 1) < 0.2
                   and c.get("intensity_signature", {}).get("variance", 99) < 2.0),
        lambda c: (f"You're a careful rater. Most of your books sit at "
                   f"{c['intensity_signature']['band_lo']}–{c['intensity_signature']['band_hi']}; "
                   f"you save the top of the scale."),
        lambda c: 0.5,
    ),
]

# ── 5. Pairing (gate 15) ──
def _pair_ok(c):
    top = c.get("top_pair")
    return bool(top and top[1] >= 3)

PAIRING = [
    InsightTemplate(
        "pairing", "side_by_side", GATES["pairing"], True, _pair_ok,
        lambda c: (f"{_name(c['top_pair'][0][0])} and {_name(c['top_pair'][0][1])} arrive "
                   f"together for you — tagged side by side in {c['top_pair'][1]} of your books."),
        lambda c: min(1.0, c["top_pair"][1] / 10.0),
    ),
]

# ── 6. Abandonment (gate 10, ≥3 abandoned) ──
#
# Reasons the reader gave for putting a book down, in their own vocabulary
# (migration 022's dnf_reason constraint). Rendered as a clause, so the sentence
# reads as one thought rather than a label bolted on.
DNF_REASON_CLAUSE: dict[str, str] = {
    "bored": "you were bored",
    "too_much": "it was too much",
    "badly_written": "it was badly written",
    "wrong_time": "it was the wrong time",
    "lost_me": "it lost you",
    "drifted": "you drifted away",
}


def _dnf_reason_clause(c) -> str | None:
    a = c.get("abandonment") or {}
    return DNF_REASON_CLAUSE.get(a.get("dnf_reason"))


ABANDONMENT = [
    InsightTemplate(
        # Preferred whenever the reader told us why. "You said it lost you" is a
        # far stronger sentence than naming a correlated emotion, because it is
        # the reader's own stated reason rather than our inference from one.
        "abandonment", "dnf_reason", GATES["abandonment"], True,
        lambda c: bool(c.get("abandonment") and _dnf_reason_clause(c)
                       and c["abandonment"].get("dnf_reason_books", 0) >= 2),
        lambda c: (f"The books you put down are the ones you tag "
                   f"{_name(c['abandonment']['emotion'])} — and on "
                   f"{c['abandonment']['dnf_reason_books']} of them you said "
                   f"{_dnf_reason_clause(c)}."),
        lambda c: min(1.0, c["abandonment"]["fraction"] + 0.1),
    ),
    InsightTemplate(
        # Suppressed when the reason variant applies, rather than left to the
        # deterministic rotation — otherwise the weaker sentence would replace the
        # stronger one on half of visits. Same idiom as center_of_gravity above.
        "abandonment", "dnf_emotion", GATES["abandonment"], True,
        lambda c: bool(c.get("abandonment")) and not (
            _dnf_reason_clause(c) and c["abandonment"].get("dnf_reason_books", 0) >= 2
        ),
        lambda c: (f"The books you don't finish are the ones you tag "
                   f"{_name(c['abandonment']['emotion'])}."),
        lambda c: c["abandonment"]["fraction"],
    ),
]

# ── 6b. DNF reasons — the reader's own answers, counted (gate 3 stated) ──
#
# The gate is 3 STATED REASONS, not books on the shelf: this claim reads the
# dnf_reason column and nothing else, so a reader who has answered the question
# three times has earned it whether their shelf holds 12 books or 400. See
# GATE_POPULATION — a claim carries its own denominator.
# (singular, plural). The tally puts a number in front of every one of these, and
# "1 were too much" is the kind of seam that makes generated-sounding copy — the
# whole point of hand-writing these sentences is that they read as written.
DNF_REASON_NOUN: dict[str, tuple[str, str]] = {
    "bored": ("bored you", "bored you"),
    "too_much": ("was too much", "were too much"),
    "badly_written": ("was badly written", "were badly written"),
    "wrong_time": ("caught you at the wrong time", "caught you at the wrong time"),
    "lost_me": ("lost you", "lost you"),
    "drifted": ("you just drifted from", "you just drifted from"),
}


def _reason_noun(reason: str | None, n: int = 2) -> str:
    forms = DNF_REASON_NOUN.get(reason or "")
    if not forms:
        return ""
    return forms[0] if n == 1 else forms[1]


def _reason_tally(c) -> str:
    """"5 bored you, 2 lost you, 1 was the wrong time" — every figure countable."""
    return ", ".join(
        f"{row['books']} {_reason_noun(row['reason'], row['books'])}"
        for row in c["dnf_reasons"]["counts"]
        if _reason_noun(row["reason"])
    )


DNF_REASONS = [
    InsightTemplate(
        # One reason accounts for every book they put down. Much stronger than a
        # breakdown, and rarer, so it outranks it.
        "dnf_reason", "unanimous", GATES["dnf_reason"], True,
        lambda c: bool(c.get("dnf_reasons") and c["dnf_reasons"]["unanimous"]),
        lambda c: (f"You've put down {c['dnf_reasons']['stated']} books and given "
                   f"the same reason every time: they "
                   f"{_reason_noun(c['dnf_reasons']['top_reason'])}."),
        lambda c: 0.9,
    ),
    InsightTemplate(
        # A dominant reason, with the rest still visible in the tally.
        "dnf_reason", "dominant", GATES["dnf_reason"], True,
        lambda c: bool(c.get("dnf_reasons")
                       and not c["dnf_reasons"]["unanimous"]
                       and c["dnf_reasons"]["share"] >= 0.5),
        lambda c: (f"Of the {c['dnf_reasons']['stated']} books you've put down and "
                   f"said why: {_reason_tally(c)}. Mostly it isn't the book — it's "
                   f"that they {_reason_noun(c['dnf_reasons']['top_reason'])}."
                   if c["dnf_reasons"]["top_reason"] in ("wrong_time", "drifted")
                   else f"Of the {c['dnf_reasons']['stated']} books you've put down "
                        f"and said why: {_reason_tally(c)}."),
        lambda c: min(1.0, c["dnf_reasons"]["share"]),
    ),
    InsightTemplate(
        # No single reason dominates — the breakdown IS the finding.
        "dnf_reason", "spread", GATES["dnf_reason"], True,
        lambda c: bool(c.get("dnf_reasons")
                       and not c["dnf_reasons"]["unanimous"]
                       and c["dnf_reasons"]["share"] < 0.5),
        lambda c: (f"You put books down for no one reason: {_reason_tally(c)}. "
                   f"That's {c['dnf_reasons']['stated']} books, "
                   f"{len(c['dnf_reasons']['counts'])} different reasons."),
        lambda c: 0.5,
    ),
]

# ── 7. Range (gate 8; narrowing variant needs a prior snapshot) ──
RANGE = [
    InsightTemplate(
        "range", "narrowing", GATES["range"], True,
        lambda c: (c.get("range_prev_distinct") is not None
                   and c["range"]["distinct"] < c["range_prev_distinct"]),
        lambda c: (f"Your range narrowed — you used to reach for "
                   f"{c['range_prev_distinct']} feelings; lately {c['range']['distinct']}."),
        lambda c: 0.75,
    ),
    InsightTemplate(
        "range", "breadth", GATES["range"], True,
        lambda c: bool(c.get("range")),
        lambda c: (f"You reach across {c['range']['distinct']} of {len(EMOTIONS_BY_SLUG)} feelings."
                   + (" That's a wide emotional range." if c["range"]["entropy"] >= 0.7
                      else " You stay in a tight band.")),
        lambda c: abs(c["range"]["entropy"] - 0.5),
    ),
]

# ── 8. Arc (gate 5, arc data) ──
ARC = [
    InsightTemplate(
        "arc", "start_to_end", GATES["arc"], True,
        lambda c: bool(c.get("arc")),
        # "of the books you logged an arc for", not "of the books you finish":
        # arc_shape's fraction is over books carrying arc_start/arc_end, which is
        # the same scope bug one level down from the gate.
        lambda c: (f"You start in {_name(c['arc']['start'])} and end in "
                   f"{_name(c['arc']['end'])} — {round(c['arc']['fraction'] * 100)}% "
                   f"of the {c['arc']['n_arc']} books you logged an arc for."),
        lambda c: c["arc"]["fraction"],
    ),
]

# Registry, ordered by category power (B7.7). Seasonality is intentionally absent
# from the *renderable* registry — it is only ever a locked entry this pass.
REGISTRY: list[InsightTemplate] = (
    CONTRADICTION + CONFIRMATION + BLIND_SPOT + DRIFT + INTENSITY + PAIRING
    + ABANDONMENT + DNF_REASONS + RANGE + ARC
)

# Category order for stable ranking ties + the locked list.
#
# `confirmation` sits here because the sort indexes into this list, but it is
# deliberately absent from GATES: the locked loop skips gate-less categories, and
# since it is the same measurement as `contradiction` with the sign reversed,
# listing both would tell a 6-book reader twice that one thing isn't ready yet.
CATEGORY_ORDER = [
    "contradiction", "confirmation", "blind_spot", "drift", "intensity_signature",
    "pairing", "abandonment", "dnf_reason", "range", "arc", "seasonality",
]


# Which ctx count each category's gate and `n` read.
#
# A gate exists to stop a claim being made on too little evidence, so it has to
# count the books that could have supplied THAT claim's evidence. Nearly every
# insight here is a claim about tagged emotions, so tagged_count is the default —
# but `arc` reads the Finish-Flow arc columns and never looks at emotions at all.
# A reader who logs arcs on books they never tagged has genuinely earned an arc
# finding, and reporting it as "based on 5 books" when it rests on 20 understates
# their own evidence back at them.
#
# The rule this encodes: a claim carries its own scope. Adding an insight that
# reads some other column means adding its denominator here too.
GATE_POPULATION: dict[str, str] = {
    "arc": "arc_count",
    # The DNF tally reads the dnf_reason column and nothing else. Gating it on
    # tagged books would deny the finding to a reader who has answered "why did
    # you put it down?" ten times but tags few books — and would report it as
    # "based on N books" where N counts books that could never have contributed.
    "dnf_reason": "dnf_stated_count",
}


def _population(ctx: dict, category: str) -> int:
    """How many books could have supplied THIS category's evidence.

    Absent key → 0 → the category is locked rather than raising. Callers build
    ctx by hand (the locked list is generated from partial contexts), and a
    missing denominator honestly means "no evidence for this yet", not a crash.
    `test_every_gate_population_key_is_produced` keeps that tolerance from
    hiding a typo in GATE_POPULATION.
    """
    return ctx.get(GATE_POPULATION.get(category, "tagged_count"), 0)


def generate_insights(ctx: dict, *, limit: int = 4) -> tuple[list[dict], list[dict], list[dict]]:
    """From a computed signal context, return (unlocked_insights, locked, earned).

    - An insight is emitted only if its population ≥ its gate AND its data is present.
    - At most one variant per category (rotates by that population so visits vary).
    - Ranked by surprise; the strongest `limit` are returned — never a dump.
    - Locked: every category whose gate the reader hasn't reached, with an honest
      reason (B7.6). This is the curiosity gap, at zero integrity cost.
    - Earned: every gated category the reader HAS reached — the positive
      counterpart the Register needs, independent of whether a template rendered
      this visit. Seasonality is never here: it needs 12 months no count stands in
      for, so it stays a locked row until that lands.

    GATES COUNT BOOKS THAT CARRY A FEELING, not titles on the shelf. Every gate
    here exists to keep a claim from being made on too little evidence, and an
    untagged book is not evidence — it is a title we know nothing about. Gating on
    the raw shelf let a 30-book import with 5 tagged books clear the 10-book
    blind-spot gate and announce "You've logged 30 books. You have never once
    reached for devastation", a sentence built on five books. ``book_count`` stays
    in ``ctx`` for copy that is genuinely about the shelf; nothing gates on it.

    Which count is "the books that carry a feeling" is per-category, though — see
    ``GATE_POPULATION``. Gating everything on tagged_count was itself an
    over-correction for the one insight that never reads emotions.
    """
    # Group applicable, signed-off, gated candidates by category.
    by_cat: dict[str, list[InsightTemplate]] = {}
    for t in REGISTRY:
        if not t.signed_off or _population(ctx, t.category) < t.min_n \
                or not t.applicable(ctx):
            continue
        by_cat.setdefault(t.category, []).append(t)

    chosen: list[dict] = []
    for cat, variants in by_cat.items():
        n = _population(ctx, cat)
        t = variants[n % len(variants)]   # deterministic rotation
        chosen.append({
            "category": cat,
            "variant": t.variant,
            "text": t.render(ctx),
            # The population the claim actually covers — the client renders this
            # as "based on N books", so it must not be the raw shelf size and must
            # not be the tagged count for a claim that did not read emotions.
            "n": n,
            "surprise": round(float(t.surprise(ctx)), 3),
        })

    chosen.sort(key=lambda i: (-i["surprise"], CATEGORY_ORDER.index(i["category"])))
    unlocked = chosen[:limit]

    locked: list[dict] = []
    shown = {i["category"] for i in chosen}
    for cat in CATEGORY_ORDER:
        gate = GATES.get(cat)
        if gate is None:
            continue
        if _population(ctx, cat) < gate and cat not in shown:
            locked.append(_locked_row(cat, gate, _population(ctx, cat)))
    # Seasonality is always locked this pass, even past 25 books (it needs the 12
    # months of history no book count can stand in for).
    if "seasonality" not in {l["category"] for l in locked}:
        locked.append(_locked_row("seasonality", GATES["seasonality"], _population(ctx, "seasonality")))

    earned: list[dict] = []
    for cat in CATEGORY_ORDER:
        gate = GATES.get(cat)
        if gate is None or cat == "seasonality":
            continue
        have = _population(ctx, cat)
        if have >= gate:
            earned.append(_earned_row(cat, gate, have))

    return unlocked, locked, earned


def _locked_row(cat: str, need: int, have: int) -> dict:
    """One 'Not yet' row, specific enough that 'why is this still locked?' answers itself.

    `have`/`need` count the SAME population (GATE_POPULATION), never shelf size, so
    the client can render 'you have 2' against 'waits on 10 books with a feeling
    tagged' without the two numbers being about different things.
    """
    unit, note = UNLOCK_UNITS.get(cat, ("books with a feeling tagged", ""))
    reason = f"{need} {unit}"
    if note:
        reason += f" — {note}"
    # `have` is the reader's count against the SAME population as `need`. Time-gated
    # seasonality is the exception: it isn't a shortfall you close by logging books,
    # so it carries no count and the client shows no "you have N".
    label = GATE_LABELS.get(cat, cat.replace("_", " ").title())
    opens = GATE_OPENS.get(cat, "")
    if cat == "seasonality":
        return {"category": cat, "label": label, "opens": opens,
                "unlocks_at": "25 books + 12 months",
                "reason": f"{need} {unit}, once you've read here that long",
                "have": None, "need": need}
    return {"category": cat, "label": label, "opens": opens,
            "unlocks_at": reason, "reason": reason,
            "have": have, "need": need}


def _earned_row(cat: str, need: int, have: int) -> dict:
    """One earned Register row — the mirror-image of ``_locked_row``.

    Emitted for every gate the reader has passed, whether or not a rendered
    insight came out of it: passing the gate is the achievement; whether this
    visit's data happened to trip a template is not. ``have`` is the reader's
    count against the gate's own population, so the row can say "read across
    23 books" without the number meaning something different from the lock.
    """
    return {
        "category": cat,
        "label": GATE_LABELS.get(cat, cat.replace("_", " ").title()),
        "opens": GATE_OPENS.get(cat, ""),
        "have": have,
        "need": need,
    }


def _top_slug(vec: dict[str, float]) -> str | None:
    ranked = sorted(vec.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[0][0] if ranked and ranked[0][1] > 0 else None


def build_dna(
    sigs: list["sig.EntrySig"],
    reads_for: list[str] | None = None,
    *,
    journal_sigs: list["sig.EntrySig"] | None = None,
    prev_snapshot: dict | None = None,
    snapshot_count: int = 0,
    insight_limit: int = 4,
) -> dict:
    """The one entry point: turn a reader's entries into the DNA payload (B7.5/B7.6).

    Below 5 books it returns an honest "not enough yet" — not a failure state, but
    anticipation. Above it: recency-weighted profiles, a demoted archetype, the
    strongest few falsifiable insights, and the honestly-locked rest.

    ``sigs`` is books. ``journal_sigs`` is named journal days, and it feeds exactly
    the signals that are about *who you are* — the emotion vectors, their drift,
    the archetype, and the never-named blind spots — so DNA spans reading and life
    (VISION §6). It deliberately does NOT feed anything that makes a claim about
    books: ``book_count``, rating style, abandonment, arcs, pairing, and
    stated-vs-revealed all stay book-only, because "you've logged 12 books" and
    "the books you rate highest" have to remain true sentences.

    "Book-only" also means *opened* books. A `want_to_read` is a shelved
    intention, not a reading, so it is dropped here — once, at the boundary,
    rather than re-filtered by each signal that happens to remember to.
    """
    # Drop the pile before anything counts rows or averages intensity.
    sigs = sig.opened_only(sigs)
    book_count = len(sigs)
    # Every signal below reads one of these two lists, and which one it reads is
    # the whole editorial decision above.
    vector_sigs = sigs + (journal_sigs or [])
    journal_count = len(journal_sigs or [])
    # The gate counts books that carry a feeling, not books. Five untagged imports
    # are five titles we know nothing about — computing a profile from them would
    # be reading tea leaves in an empty cup.
    tagged = [s for s in sigs if s.emotions]
    if len(tagged) < MIN_BOOKS_FOR_DNA:
        return {
            "enough": False,
            "book_count": book_count,
            "tagged_count": len(tagged),
            "needed": MIN_BOOKS_FOR_DNA,
            "message": f"{len(tagged)} books with a feeling logged. "
                       f"At {MIN_BOOKS_FOR_DNA}, the mirror starts to see you.",
            # Present on both branches so the client never has to check `enough`
            # before reading it.
            "snapshot_count": snapshot_count,
            "has_two_snapshots": snapshot_count >= 2,
            "journal_entry_count": journal_count,
        }

    enduring = sig.frequency_vector(vector_sigs, weighted=False)
    current = sig.frequency_vector(vector_sigs, weighted=True)
    drift_val = sig.drift(enduring, current)

    # Book-share for the "rare" blind-spot variant. The denominator is TAGGED books,
    # not the shelf: only a tagged book could have carried the emotion, so dividing
    # by titles that carry no feelings at all manufactures rarity out of untagged
    # imports. One tagged book in 30 reads as 3% and trips the <5% rare band; the
    # same book among 20 tagged is 5% and is not rare.
    book_share: dict[str, float] = {}
    for slug in sig._ALL_SLUGS:
        n = sum(1 for s in tagged if slug in s.emotions)
        book_share[slug] = n / len(tagged)
    rare = sorted(((s, v) for s, v in book_share.items() if 0 < v < 0.05), key=lambda kv: kv[1])

    pairs = sig.co_occurrence(sigs)
    top_pair = pairs.most_common(1)[0] if pairs else None

    # Books only, and computed once: the tally is both a signal and its own gate
    # population, and the two must be the same number.
    dnf_tally = sig.dnf_reasons(sigs)

    prev_current = (prev_snapshot or {}).get("current_vector")
    prev_enduring = (prev_snapshot or {}).get("enduring_vector")
    range_prev_distinct = (
        sum(1 for v in prev_enduring.values() if v > 0) if prev_enduring else None
    )

    ctx = {
        # Both, and they mean different things. `tagged_count` is what every gate
        # and every "based on N books" reads; `book_count` is only for copy that is
        # genuinely about the size of the shelf.
        "book_count": book_count,
        "tagged_count": len(tagged),
        # arc reads the Finish-Flow columns, not emotions, so it carries its own
        # denominator (see GATE_POPULATION). Books tagged with a feeling and books
        # logged with an arc are different populations that happen to overlap.
        "arc_count": sum(1 for s in sigs if s.arc_start and s.arc_end),
        "intensity_signature": sig.intensity_signature(sigs),
        "range": sig.range_entropy(sigs),
        "range_prev_distinct": range_prev_distinct,
        # An emotion is only a blind spot if it's absent from the journal too —
        # "you have never named this" is a stronger and more honest claim when it
        # covers everywhere the reader names feelings, not just the shelf.
        "blind_spots": sig.blind_spots(vector_sigs),
        "rare": rare,
        "top_pair": top_pair,
        "stated": sig.stated_vs_revealed(sigs, reads_for),
        "abandonment": sig.abandonment(sigs),
        "dnf_reasons": dnf_tally,
        # This claim's own denominator: books put down WITH a stated reason. Not
        # tagged_count — see GATE_POPULATION.
        "dnf_stated_count": dnf_tally["stated"] if dnf_tally else 0,
        "arc": sig.arc_shape(sigs),
        "drift": drift_val,
        "has_two_snapshots": snapshot_count >= 2,
        "old_top": _top_slug(prev_current) if prev_current else None,
        "new_top": _top_slug(current),
    }

    unlocked, locked, earned = generate_insights(ctx, limit=insight_limit)
    archetype_id, scores, gap = sig.score_archetype(current)

    return {
        "enough": True,
        "book_count": book_count,
        "tagged_count": len(tagged),
        "insights": unlocked,
        "locked": locked,
        # The gates the reader has passed — the Register's positive column. See
        # generate_insights: this is independent of whether a template rendered.
        "earned": earned,
        # None is a legitimate answer here: the reader can be past the gate and
        # still have a tally that names nobody. The client must handle it.
        "archetype": sig.archetype_dict(archetype_id) if archetype_id else None,
        "archetype_scores": scores,
        # Renamed from the old `margin`: scores are now centered on the population
        # baseline and signed, so a *fraction of the leader's score* is meaningless
        # (the leader's score can be negative). This is the absolute lead over
        # second place, in frequency-vector units, comparable between readers.
        "margin": gap,
        # When the leader barely clears the field, say so rather than pretending
        # the label was decisive.
        "runner_up": (
            sig.archetype_dict(sorted(scores, key=scores.get, reverse=True)[1])["name"]
            if archetype_id and gap < sig.HEDGE_ARCHETYPE_GAP else None
        ),
        # The receipt. Books only — `sigs`, not `vector_sigs` — because this line
        # is rendered on public surfaces and counts things it calls "your books".
        "basis": sig.basis_for(archetype_id, sigs) if archetype_id else None,
        # `current_books` is the recency-weighted vector over books ALONE. The
        # other two span the journal, and the journal is private: this is the only
        # vector a public surface is allowed to read (see card_payload).
        "profiles": {"enduring": enduring, "current": current,
                     "current_books": sig.frequency_vector(sigs, weighted=True)},
        "drift": drift_val,
        "reads_for": sig._canon_list(reads_for),
        # Already known here (it gates `has_two_snapshots` above), so returning it
        # costs nothing. Without it the DNA tab had to spend a whole extra
        # GET /dna/evolution purely to learn a list length.
        "snapshot_count": snapshot_count,
        "has_two_snapshots": snapshot_count >= 2,
        # How much of the profile above came from named days rather than books. The
        # client can say so; without it, a reader couldn't tell why their vectors
        # moved after a week of journalling.
        "journal_entry_count": journal_count,
    }


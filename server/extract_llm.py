"""The model layer: five calls, a strict schema on each, and one deterministic control.

The model is asked for **mappings and fields, never re-typed numbers**. Where a table
exists, code reads the table; the model's job is to say which column is the unit id and
which account line is "Payroll". That division is the whole reason this is safe enough to
put on a public page: a mislabelled column produces a visibly wrong row, while a
hallucinated rent produces a plausible one.

Three controls, in order of how much they actually buy:

1. **The verbatim-quote check.** Every field from the offering memorandum arrives with a
   quote, and a quote that does not appear in the extracted text discards the field. This
   is deterministic and runs on our side, so it does not depend on the model being honest
   about its own confidence. It is the anti-hallucination control.
2. **The vendored category list.** A T-12 mapping that names a category outside
   ``schema.T12_*_CATEGORIES`` is rejected by ``schema.assert_categories``, loudly, rather
   than flowing into a workbook as a new row nobody defined.
3. **The delimited document region.** Uploaded text sits inside ``<document>`` in the user
   turn and the system prompt says everything in there is content. No tools, no network,
   no filesystem are available to the call, so the worst a hostile document can do is
   produce values that then fail (1) and (2).

Mock mode (``DEMO_MODEL_MODE=mock``, the default) carries the whole pipeline with no key
and no spend. It is not a stub that returns fixed blobs: it runs deterministic rules over
the same inputs and produces quotes copied out of the real text, so the verification path
above is exercised rather than bypassed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

import schema  # noqa: E402

from server.errors import ApiError

log = logging.getLogger("upload_demo.model")

PROMPTS = Path(__file__).resolve().parent / "prompts"

# --------------------------------------------------------------------------------------
# Models, prices, limits
# --------------------------------------------------------------------------------------

EXTRACT_MODEL = "claude-opus-5"
CLASSIFY_MODEL = "claude-haiku-4-5"

#: US dollars per million tokens, as published 2026-09-02. Cache rates follow the
#: documented multipliers: a write costs 1.25x the input rate, a read 0.1x.
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    EXTRACT_MODEL: {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    CLASSIFY_MODEL: {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}

MAX_TOKENS_OM = 8_000
#: 4,000, not the 2,000 the round-8 measurement sized for ~1,200 tokens of JSON: Claude
#: Opus 5 thinks by default and its thinking is counted INSIDE ``max_tokens``, so the first
#: live T-12 chunk (2026-09-03) spent the whole 2,000 and returned JSON cut off mid-string
#: (``output_tokens=2000``, exactly the ceiling). The mapping and notes calls also name an
#: ``effort`` now, which is what bounds the thinking; the ceiling is the backstop.
MAX_TOKENS_MAPPING = 4_000
MAX_TOKENS_CLASSIFY = 256

#: Account labels per T-12 mapping call. One real statement has 204 labels and
#: only two of them resolve deterministically — its vocabulary is ALL-CAPS and
#: vendor-specific ("NET RENTAL INCOME", "VALET TRASH REIMBURSEMENTS") — so 202 mappings
#: would need roughly 5,000 output tokens and be truncated by the 2,000-token ceiling.
#: Structured output turns a truncation into a cut-off answer — ``OutputTruncated`` since
#: the first live run, a JSON parse error before — so this is a hard failure, not a
#: silent one. Forty labels comes to ~1,200 output tokens of JSON, which leaves room for
#: the subtotals object and, since the ceiling went to 4,000, for the model's thinking.
T12_LABELS_PER_CALL = 40

TIMEOUT_SECONDS = 90.0
#: SDK-level retries, pinned OFF.
#:
#: The SDK applies its timeout PER ATTEMPT, so one retry turns a 90-second budget into
#: nearly 180 plus backoff — a call that starts inside the 120-second run deadline can
#: finish well outside it, holding a worker slot the whole time. The retries this service
#: wants are the ones it manages itself, in `_map_t12_chunk` and `map_rent_roll`, which
#: check the remaining time before starting a second attempt.
MAX_RETRIES = 0

#: One token per UTF-8 BYTE: the fallback bound, and a real one.
#:
#: Round 12 replaced a 3-characters-per-token AVERAGE with 2 for ASCII, which is a better
#: guess and still a guess — byte-level BPE leaves ASCII bytes unmerged in the worst case,
#: so a 4,000-byte prompt can be 4,000 tokens and was being priced at 2,000. The bound is now the arithmetic worst case: a merge only ever REDUCES the token
#: count, so no text tokenises to more tokens than it has bytes.
#:
#: It is deliberately pessimistic, which is why it is not what a live call is priced by:
#: `count_input_tokens` asks the provider for the exact figure, and a call that cannot be
#: priced that way is REFUSED rather than sent against this bound — see `call_json`.


def estimate_input_tokens(text: str) -> int:
    """The most tokens ``text`` could possibly cost: its length in UTF-8 bytes.

    Not an estimate and not an average — a bound. Every tokenizer this service could meet
    is byte-level BPE, whose vocabulary contains all 256 single bytes and whose merges only
    ever join two tokens into one. So the token count starts at the byte count and goes
    down; it cannot go up.

    The result is a number to REFUSE a call by, not a number to report. What was actually
    spent comes from the response's own usage.
    """
    return len((text or "").encode("utf-8", "replace"))


def om_request_blocks(document_text: str, task_text: str) -> list[dict]:
    """The user turn for an OM call: the DOCUMENT first, then the task. Not cache-controlled.

    Two calls read the same ~30K-token memorandum (the summary, then the comparables), and
    An earlier version first marked the document block ``cache_control: ephemeral`` so the second would
    read it at a tenth of the input price. The first live run (2026-09-05) measured
    ``cache_r=0`` on that second call, and a five-call experiment
    (``outputs/upload_qa/cache_experiment_2026-09-05.txt``) showed why: the structured-
    output schema is rendered INTO the cached prefix — the same document wrote 3,213 tokens
    under the summary schema and 3,153 under the comparables schema — and a different
    ``effort`` invalidates the entry too. Two schemas therefore never share an entry, and
    the marker only bought the 1.25× cache-WRITE premium on both calls. So: no marker, the
    input rate on both, and the document still first so a single-schema design could cache
    later. Pinned by ``test_om_comps`` item 3 and ``test_model_layer``.
    """
    return [
        {"type": "text", "text": document_region(document_text)},
        {"type": "text", "text": task_text},
    ]


def user_text(user) -> str:
    """The user turn as plain text, for the token bound — a string or content blocks."""
    if isinstance(user, str):
        return user
    return "\n\n".join(str(b.get("text", "")) for b in user if isinstance(b, dict))


def count_input_tokens(client, model: str, system: str, user,
                       output_config: "dict | None" = None) -> "int | None":
    """The provider's own count for this exact request, or ``None`` if it cannot be had.

    ``messages.count_tokens`` is free and exact, which makes it strictly better than any
    bound this file can compute — so in live mode it is what the cap is checked against.
    Every failure is the same answer: no count, use the bound. A billing guard that raises
    because its own free pre-flight was unavailable would turn a provider hiccup into a
    failed run.

    "This exact request" includes ``output_config``: the JSON schema is compiled into the
    request and billed as input, and a count without it was short by the schema's size on
    every call of the first live run — 799 tokens on the OM call, 861 on the rent roll
    (2026-09-03). Counted with the same ``output_config`` the figure matched the bill.
    """
    if client is None:
        return None
    try:
        counter = client.messages.count_tokens
    except AttributeError:
        return None
    try:
        extra = {"output_config": output_config} if output_config else {}
        result = counter(
            model=model,
            system=system or "",
            messages=[{"role": "user", "content": user or ""}],
            **extra,
        )
        tokens = int(getattr(result, "input_tokens", None))
    except Exception as exc:  # noqa: BLE001 - any failure means "no count"
        # The TYPE only. An API error's message can quote the request that produced it,
        # and the request is the visitor's document.
        log.warning("count_tokens was unavailable (%s); pricing from the bound instead",
                    type(exc).__name__)
        return None
    if tokens < 0:
        return None
    return tokens


def price_call(model: str, counted: "int | None", system: str, user,
               max_tokens: int, output_config: "dict | None" = None) -> float:
    """Dollars for one call, from the provider's count when there is one and the bound
    when there is not. The one place the two are turned into money.

    The bound covers ``output_config`` too: the schema is billed as input (the live leg
    measured it at 200–860 tokens a call), and a bound that left it out could price a call
    under the money left and send it over. Serialised with
    sorted keys so the same schema is always the same bytes.
    """
    rates = PRICES_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    if counted is not None:
        input_tokens = float(counted)
    else:
        input_tokens = float(
            estimate_input_tokens(system or "") + estimate_input_tokens(user_text(user) or "")
        )
        if output_config:
            input_tokens += estimate_input_tokens(json.dumps(output_config, sort_keys=True))
    # A turn that carries a cache-controlled block is billed at the cache-WRITE rate on a
    # miss (1.25x input), and the count says nothing about hits. The reservation is the
    # worst case, so every input token of such a turn is priced at the write rate; the ledger records what was actually charged afterwards.
    input_rate = rates["cache_write"] if has_cache_control(user) else rates["input"]
    return (input_tokens * input_rate + max_tokens * rates["output"]) / 1_000_000


def has_cache_control(user) -> bool:
    return isinstance(user, list) and any(isinstance(b, dict) and b.get("cache_control") for b in user)


def estimate_call_dollars(
    model: str, system: str, user: str, max_tokens: int, client=None,
    output_config: "dict | None" = None,
) -> float:
    """The worst case for one call: all the input, and ``max_tokens`` of output.

    The cap was checked against spend SO FAR and the usage recorded afterwards, so a call
    was never measured against the money left before it was sent. One maximum-size OM call
    is ~$0.20 of output alone and can be far more in input, which is enough on its own to
    walk through a $1.50 run cap — or through a day-headroom grant of forty cents.

    ``client`` is the live SDK client, when there is one. Given it, the input is priced by
    the provider's own token count; without it — mock mode, or a counting call that
    failed — by :func:`estimate_input_tokens`, which is a bound rather than an average.
    Three characters per token was neither: punctuation-heavy and non-ASCII text tokenises
    far more densely than that, so a call could be priced under the cap, sent, and only
    then found to have crossed both the run cap and the day's reservation.
    """
    return price_call(
        model, count_input_tokens(client, model, system, user, output_config),
        system, user, max_tokens, output_config,
    )

#: Refusal fallbacks on the Opus calls. A policy decline on a deal document is unlikely
#: and it is not a reason to hand a visitor a blank page, so the request re-runs itself
#: server-side on a fallback model rather than failing the run.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

MODE_MOCK = "mock"
MODE_LIVE = "live"

#: The env file live mode reads its key from. Defaults to ``.env`` at the repository root
#: (gitignored); ``PORTFOLIO_DEMO_ENV`` points it elsewhere.
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

MAX_QUOTE_CHARS = 200

#: What one run of this demo may spend on model calls before it stops itself.
#:
#: This defaulted to ``None`` — no cap at all — which made ``UsageLedger.check_cap()`` a
#: control that had never protected anything in production. $1.50 is roughly ten times the
#: measured shape of a full three-document run and still an order of magnitude under the
#: $10 day budget, so it stops a runaway (a pathological statement that chunks forever, a
#: retry storm) without ever firing on an honest deal.
DEFAULT_RUN_CAP_USD = 1.50

#: Column headers whose values are people. Masked before any row is sent to the model —
#: a rent roll carries resident names and there is no version of this demo where those
#: need to leave the machine to work out which column holds the rent.
#:
#: This list is now the SECOND line of defence, not the first: :func:`column_profiles`
#: sends no values at all from a free-text column, whatever it is called. It stays because
#: a short, low-cardinality people column ("Occupant" holding initials, say) could profile
#: as a code, and because a header list is the one thing a reviewer can check by eye.
PERSON_HEADERS = (
    "name", "resident", "tenant", "occupant", "lessee", "guarantor", "applicant",
    "prospect", "email", "e mail", "e-mail", "phone", "mobile", "contact", "customer",
    "leaseholder", "cosigner", "co-signer", "co signer", "responsible party", "person",
    "emergency", "employer", "ssn", "birth", "dob", "occupied by",
    # Whoever the unit BELONGS to, or who signs and is paid, is as much a person as
    # whoever lives in it. "Unit Owner" was on no list and `value_bearing` allowed it on
    # its `unit` token, so a column of owner names short enough to profile as codes
    # travelled to the model as that column's vocabulary. ``holder``
    # covers leaseholder, lienholder and account holder; ``signer`` covers co-signer.
    "owner", "landlord", "proprietor", "holder", "payee", "signer", "signatory",
    "trustee", "attorney", "beneficiary", "manager", "management",
)

#: A header whose last token is ``by`` names a PERSON, whatever precedes it.
#:
#: "Occupied By" holds resident names, and none of the words above appears in it — while
#: ``occupied`` is squarely in `STATUS_HEADERS`, so the column profiled as a status ROLE
#: and up to `STATUS_MAX_VOCABULARY` names travelled to the model as its vocabulary
#:. A status word is a fact about the unit; the same word followed by
#: "by" names the person the fact is about, and that is a different column entirely.
#:
#: The first version of this rule listed the state words that could precede the ``by``.
#: The coordinator's ruling widened it, and the widening is right: "Entered By",
#: "Sorted By", "Approved By" and "Last Modified By" are all a person, none of them is a
#: state word, and "Entered By" was not merely mis-profiled — `redact_people` sent its
#: names in every row, and a model that named it as the status column was answered with
#: the whole set through `expose_status_vocabulary`. English puts the agent after "by",
#: and an agent is a person.
#:
#: The cost of the wider rule is a column of VALUES withheld from the mapping prompt, and
#: no rent-roll role is spelled "<something> by". A header that merely CONTAINS the
#: letters — "Byte Count", "Standby Rent", "Bypass" — is untouched: the match is on the
#: last TOKEN, and a bare "By" on its own is not enough either.
PERSON_BY_TOKEN = "by"


#: The only two values ``DEMO_MODEL_MODE`` may hold. Anything else refuses to start.
MODEL_MODES: tuple[str, ...] = (MODE_MOCK, MODE_LIVE)


def normalise_mode(raw: "str | None") -> "str | None":
    """One reading of ``DEMO_MODEL_MODE``, for everything that reads it.

    ``None`` means the value is neither mode — which is a deployment error rather than a
    default, and :meth:`runs.Settings.from_env` refuses to start on it.

    There were two readings before: startup compared
    ``value.strip().lower() == "live"`` and demanded the live credentials, while the
    selector below compared the raw value to ``"live"`` and silently chose mock. So
    ``DEMO_MODEL_MODE=LIVE`` passed every live check at boot and then served canned
    extraction results — the one failure a demo of an extraction pipeline cannot have.
    """
    value = str(raw or "").strip().lower()
    if not value:
        return MODE_MOCK
    return value if value in MODEL_MODES else None


def mode() -> str:
    """``live`` only when explicitly asked for. Mock is the default everywhere.

    A value that is neither is read as MOCK here and refused at STARTUP: this function is
    on the hot path of every run and must not raise, and a process that got past
    ``Settings.from_env`` cannot be holding one anyway.
    """
    return normalise_mode(os.environ.get("DEMO_MODEL_MODE")) or MODE_MOCK


# --------------------------------------------------------------------------------------
# Env
# --------------------------------------------------------------------------------------


def load_env(path: Path | None = None) -> list[str]:
    """Load the project env file into ``os.environ``; return the KEY NAMES it set.

    Names only, never values — the return value is logged and a return value that carried
    a key would put it in a transcript. Existing environment entries win, so a shell
    export can still override the file.
    """
    target = Path(path or os.environ.get("PORTFOLIO_DEMO_ENV") or DEFAULT_ENV_FILE)
    if not target.is_file():
        log.info("env: no file at %s", target)
        return []
    loaded: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value
        loaded.append(key)
    log.info("env: loaded %d name(s) from %s: %s", len(loaded), target, ", ".join(loaded))
    return loaded


def have_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


# --------------------------------------------------------------------------------------
# Usage ledger
# --------------------------------------------------------------------------------------


@dataclass
class UsageEntry:
    call: str
    model: str
    mode: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0

    @property
    def dollars(self) -> float:
        rates = PRICES_PER_MTOK.get(self.model)
        if rates is None or self.mode != MODE_LIVE:
            return 0.0
        return (
            self.input_tokens * rates["input"]
            + self.output_tokens * rates["output"]
            + self.cache_read_tokens * rates["cache_read"]
            + self.cache_write_tokens * rates["cache_write"]
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "call": self.call,
            "model": self.model,
            "mode": self.mode,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "seconds": round(self.seconds, 2),
            "dollars": round(self.dollars, 6),
        }


@dataclass
class UsageLedger:
    """Per-run record of what was spent, and the hand-off to the global daily ledger.

    A hard ``cap_dollars`` exists so an unattended live run cannot walk past a stated
    budget: the check happens *before* each call, and crossing it raises rather than
    truncating, because a half-finished extraction that looks complete is worse than a
    stopped one. It defaults to :data:`DEFAULT_RUN_CAP_USD` — it used to default to
    ``None``, which meant the only ledger production ever built had no cap on it.

    ``on_record`` is the other half of the same problem. This ledger is per-run and
    private; ``budget.BudgetGate`` owns the day, and nothing joined them, so real spend
    never moved the $10/day guard and ``/api/meta.accepting`` never reacted to it. The
    callback fires as each call completes — not at the end of the run — so a run that
    fails half way has still paid for what it used, and the next request sees it.
    """

    entries: list[UsageEntry] = field(default_factory=list)
    cap_dollars: float | None = DEFAULT_RUN_CAP_USD
    on_record: "Callable[[UsageEntry], Any] | None" = None
    #: Seconds left of the run's hard stop. The per-run budget is money AND time — a call
    #: given the SDK's own 90-second timeout when the run has ten seconds left takes the
    #: run past its advertised deadline and holds a worker slot while it does.
    remaining_seconds: "Callable[[], float] | None" = None
    #: Worst-case dollars of calls reserved and not yet recorded. The T-12 chunks run
    #: concurrently (2026-09-03), and four workers each checking the cap before any of
    #: them had recorded a cent all saw the same balance and all sent. A
    #: reservation is taken under the lock before a call is sent and released when its
    #: real usage is recorded, so the cap bounds the calls in flight, not just the ones
    #: already billed.
    _reserved: float = field(default=0.0, repr=False, compare=False)
    _lock: "threading.Lock" = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def model_calls(self) -> int:
        return len(self.entries)

    @property
    def dollars(self) -> float:
        return sum(e.dollars for e in self.entries)

    @property
    def input_tokens(self) -> int:
        return sum(e.input_tokens + e.cache_read_tokens + e.cache_write_tokens for e in self.entries)

    @property
    def output_tokens(self) -> int:
        return sum(e.output_tokens for e in self.entries)

    def check_cap(self, upcoming: float = 0.0) -> None:
        """Refuse when the cap is reached, or when the NEXT call would pass it.

        ``upcoming`` is the worst case of the call about to be made. Without it this only
        ever answered "has the money already gone?", which lets a single large call
        overshoot the cap by its whole size — the cap bounds the spend before a call, not
        the spend after one.
        """
        if self.cap_dollars is None:
            return
        if self.dollars >= self.cap_dollars:
            raise BudgetExhausted(
                f"live spend has reached ${self.dollars:.4f} against a cap of "
                f"${self.cap_dollars:.2f}; stopping before the next call"
            )
        if upcoming and self.dollars + upcoming > self.cap_dollars:
            raise BudgetExhausted(
                f"the next call could cost ${upcoming:.4f}, which would take this run to "
                f"${self.dollars + upcoming:.4f} against a cap of "
                f"${self.cap_dollars:.2f}; refusing to send it"
            )

    def reserve(self, upcoming: float) -> None:
        """Hold ``upcoming`` against the cap, atomically, or refuse like :meth:`check_cap`.

        Refuses when the money already spent PLUS every reservation in flight PLUS this
        call would pass the cap — the check four concurrent chunks used to race.
        """
        with self._lock:
            if self.cap_dollars is None:
                return
            if self.dollars >= self.cap_dollars:
                raise BudgetExhausted(
                    f"live spend has reached ${self.dollars:.4f} against a cap of "
                    f"${self.cap_dollars:.2f}; stopping before the next call"
                )
            committed = self.dollars + self._reserved
            if upcoming and committed + upcoming > self.cap_dollars:
                raise BudgetExhausted(
                    f"the next call could cost ${upcoming:.4f}, which with "
                    f"${committed:.4f} spent or in flight would pass the cap of "
                    f"${self.cap_dollars:.2f}; refusing to send it"
                )
            self._reserved += float(upcoming or 0.0)

    def release(self, upcoming: float) -> None:
        """Let a reservation go — its real cost is on the ledger now, or it was never sent."""
        with self._lock:
            self._reserved = max(0.0, self._reserved - float(upcoming or 0.0))

    def seconds_left(self) -> float:
        """What the run has left, or the per-call default when nothing is watching."""
        if self.remaining_seconds is None:
            return TIMEOUT_SECONDS
        try:
            return max(0.0, float(self.remaining_seconds()))
        except Exception:  # noqa: BLE001 - a clock that raises must not fail a run
            return TIMEOUT_SECONDS

    def call_timeout(self) -> float:
        """The timeout for the next call: the shorter of the default and what is left."""
        return max(0.5, min(TIMEOUT_SECONDS, self.seconds_left()))

    def out_of_time(self) -> bool:
        return self.remaining_seconds is not None and self.seconds_left() <= 0.5

    def record(self, entry: UsageEntry) -> UsageEntry:
        self.entries.append(entry)
        log.info(
            "model call %s on %s [%s]: in=%d out=%d cache_r=%d cache_w=%d %.2fs $%.6f",
            entry.call, entry.model, entry.mode, entry.input_tokens, entry.output_tokens,
            entry.cache_read_tokens, entry.cache_write_tokens, entry.seconds, entry.dollars,
        )
        if self.on_record is not None:
            try:
                self.on_record(entry)
            except Exception:  # noqa: BLE001
                # Bookkeeping must not become an outage: the money is still counted in
                # THIS ledger and the run continues. The log line is how anyone finds out
                # the day ledger is behind.
                log.exception("usage recorder failed for %s; the day ledger is now behind",
                              entry.call)
        return entry

    def as_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "dollars": round(self.dollars, 6),
            "calls": [e.as_dict() for e in self.entries],
        }

    def write_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for entry in self.entries:
                handle.write(json.dumps(entry.as_dict()) + "\n")
            handle.write(json.dumps({"total": self.as_dict()}) + "\n")
        return path


class BudgetExhausted(RuntimeError):
    """Raised before a call that would take the run past its dollar cap."""


class DeadlineExceeded(RuntimeError):
    """Raised before a call the run has no time left to make."""


class ModelRefused(RuntimeError):
    """Every model in the chain declined the request."""


class MappingIncomplete(ValueError):
    """A mapping call did not come back with what it was asked about.

    ``labels`` is the operator-grade detail — the account labels the model did not return,
    or the status values it did not name. It travels on the EXCEPTION rather than in its
    message because the message reaches a log and the labels are lines out of the
    visitor's own statement. ``runs.py`` puts them on the run's error
    detail, which needs the run token to read.

    A ``ValueError`` still, because that is what every caller of these mappings already
    catches and what the refusal has always been.
    """

    def __init__(self, message: str, labels: "list[str] | None" = None) -> None:
        super().__init__(message)
        self.labels: list[str] = [str(item) for item in (labels or [])]


class OutputTruncated(RuntimeError):
    """The provider stopped at ``max_tokens``: the answer is incomplete, not malformed.

    Raised instead of the ``JSONDecodeError`` a cut-off structured output produces, so the
    cause is named and a "short answer, retry once" path does not re-send the same request
    against the same ceiling. The tokens were billed and are on the ledger.
    """


class PricingUnavailable(RuntimeError):
    """Raised when a call could not be PRICED and the bound does not fit the grant.

    Distinct from :class:`BudgetExhausted` on purpose. "This run has spent its budget" and
    "nobody could tell me what this call costs, and the arithmetic worst case does not fit"
    are different sentences to a visitor: the first is final for this run, the second is a
    provider hiccup they can retry through. Sending the call anyway is the option this
    class exists to refuse — that is exactly the guess that lets one call cross both the
    run cap and the day's reservation.
    """


# --------------------------------------------------------------------------------------
# The delimited regions
# --------------------------------------------------------------------------------------


def document_region(text: str, limit: int = 400_000) -> str:
    """Wrap document text for the user turn.

    The closing tag is stripped out of the content first. Without that, a document
    containing the literal string ``</document>`` could end the region early and put the
    rest of its own text where instructions go — the delimiter is only a boundary if the
    content cannot write one.
    """
    body = (text or "")[:limit].replace("</document>", "[/document]")
    return f"<document>\n{body}\n</document>"


def excerpt_region(text: str, limit: int = 8_000) -> str:
    """Wrap a classification excerpt. Same rule as :func:`document_region`.

    This one was written without the guard and it is the easier target of the two: the
    excerpt is short, so a document only has to contain the literal closing tag near the
    top for everything after it to land outside the region the system prompt calls
    untrusted — where an instruction reads as an instruction.
    """
    body = (text or "")[:limit].replace("</excerpt>", "[/excerpt]")
    return f"<excerpt>\n{body}\n</excerpt>"


def notes_region(notes: str, limit: int = 2_000) -> str:
    body = (notes or "")[:limit].replace("</notes>", "[/notes]")
    return f"<notes>\n{body}\n</notes>"


def redact_people(header: list[str], rows: list[list]) -> list[list]:
    """Blank the columns that hold people before any row leaves the machine."""
    people = person_columns(header)
    if not people:
        return [list(row) for row in rows]
    return [
        ["[redacted]" if index in people else cell for index, cell in enumerate(row)]
        for row in rows
    ]


def person_header(name) -> bool:
    """Does this header name a PERSON rather than something about the unit?

    Two rules. The eye-checkable list above, matched as a substring the way it always
    was — and the structural one: a header whose last token is ``by``, with something in
    front of it. "Occupied By" is a resident's name and ``occupied`` is a status word, so
    without the second rule the column was profiled as a status ROLE and its whole
    distinct set — up to 64 residents — travelled to the model as that column's
    vocabulary.
    """
    lowered = str(name or "").lower()
    if any(word in lowered for word in PERSON_HEADERS):
        return True
    tokens = header_tokens(lowered)
    return len(tokens) > 1 and tokens[-1] == PERSON_BY_TOKEN


def person_columns(header: list[str]) -> set[int]:
    """Indexes of columns whose header names a person."""
    return {index for index, name in enumerate(header) if person_header(name)}


# --------------------------------------------------------------------------------------
# Column profiles — what the rent-roll mapping call is allowed to see
#
# The mapping call used to send eight complete rows with a short header blacklist masked.
# That is the wrong shape twice over. It leaks: "Primary Applicant", "Prospect", "Email"
# and "Comments" are all free text about a person and none of them was on the list. And it
# under-informs: eight rows off the top of a roll sorted occupied-first contain no "V", so
# the status vocabulary missed it and every vacant unit downstream read as Non-revenue.
#
# A profile fixes both. Per column the model gets the header, an inferred type, how many
# distinct values there are, and — only where the values are numbers, dates or short codes
# — up to three of them. A free-text column contributes its NAME and nothing else, which
# is all the model needed from it anyway: nobody works out which column holds the rent by
# reading a resident's name. Status-like columns get their whole distinct set, scanned
# over the entire grid rather than the first eight rows.
# --------------------------------------------------------------------------------------

#: Values sampled from a number / date / code column.
PROFILE_SAMPLES = 3
#: A column with more distinct short values than this is not a vocabulary.
PROFILE_MAX_VOCABULARY = 40
#: Longest value that can still be a "code" rather than prose.
PROFILE_CODE_CHARS = 16
#: Rows scanned to build the profile. Enough to see every status on any real roll, bounded
#: so a pathological upload cannot turn profiling into the expensive part of the run.
#: Rows the profile scans.
#:
#: Must be at least what the parser RETAINS, or a status code that first appears in the
#: rows between the two limits is invisible to the mapping and every unit carrying it
#: falls through to Non-revenue. Read from the parser rather than restated, so the two
#: cannot drift apart again.
def _profile_row_cap() -> int:
    from server.parse_worker import MAX_ROWS

    return MAX_ROWS


PROFILE_MAX_ROWS = _profile_row_cap()

#: Headers whose column is a vocabulary the mapper has to see in full.
#:
#: Status ROLES, not shapes. ``code`` was on this list and matched ``Unit Code`` — an
#: ordinary unit-identifier column — so a roll of more than 64 units was refused as
#: `status_vocabulary_too_large` before the mapper could look at it, and a smaller one had
#: its unit IDs sent to the model as a status vocabulary. ``unit type``
#: and ``lease type`` went with it: a type column that really is a status still reaches the
#: model through `expose_status_vocabulary`, which asks the roll rather than the header.
STATUS_HEADERS = ("status", "occupancy", "occupied", "lease status", "unit status")

#: The headers that ARE a status role, matched as the whole header rather than as a
#: substring of one.
#:
#: `STATUS_HEADERS` is a substring list, and "Status Reason" and "Occupancy Detail"
#: contain a member of it — so both were classified as status columns and their whole
#: distinct set went to the model, which for those columns is sentences about residents
#:. A column that IS the status answers in a vocabulary; a column that
#: explains the status answers in prose, and prose about a resident is the one thing this
#: profiler exists not to send.
#: Written as the header appears; matched after `header_tokens` normalises punctuation, so
#: "Occ." and "Occ" are one entry and "Vacant/Occupied" is "vacant occupied".
STATUS_ROLE_HEADERS: frozenset = frozenset({
    "status", "occupancy", "occupied", "lease status", "unit status",
    "unit occupancy", "occupancy status", "lease state", "unit state",
    # The abbreviations and code-column spellings a real export uses (the coordinator's
    # ruling on round 18). Still EXACT headers: "Status Code" is a status column and
    # "Unit Code" is a unit identifier, and matching `code` as a substring is what sent a
    # roll's unit ids to the model as a status vocabulary in round 14.
    "occ", "occupancy code", "status code", "unit status code", "lease status code",
    "vacancy", "vacant occupied", "occupied vacant",
    # The abbreviation-CODE spellings, added on the same ruling. Still exact: "Unit Sts"
    # is a status column and "Unit Cd" is a unit identifier, one letter apart.
    "occ cd", "occ code", "sts cd", "sts code", "status cd", "unit sts", "lease sts",
})

#: Words that make a header a NOTE about something rather than the thing itself. Denied
#: outright — these columns contribute a name and a shape and nothing else.
DETAIL_WORDS: frozenset = frozenset({
    "reason", "detail", "details", "note", "notes", "comment", "comments", "remark",
    "remarks", "memo", "memos", "description", "desc", "explanation", "commentary",
})


def status_role_header(name) -> bool:
    """Is this header a status ROLE — the column that holds the status itself?

    Whole-header, normalised: "Unit Status", "unit_status" and "UNIT STATUS" are the same
    role and "Status Reason" is not that role at all. A trailing "by" is a person (round
    15) and a detail word is a note about the status, and neither may show its values.
    """
    tokens = header_tokens(name)
    if not tokens:
        return False
    if tokens[-1] == PERSON_BY_TOKEN:
        return False
    if any(token in DETAIL_WORDS for token in tokens):
        return False
    return " ".join(tokens) in STATUS_ROLE_HEADERS

#: A status column's own bounds, independent of the short-code classification above.
#:
#: A roll writing ``Occupied - No Notice`` profiles as TEXT — the value is longer than a
#: code — and the generic rule then withheld every value in the column. The mapper could
#: still pick the column by its header, but `_vocabulary_gap` had nothing to verify, so an
#: unmapped ``Occupied - No Notice`` fell through `_status_of` to **Non-revenue** and took
#: occupancy, vacancy and the occupied-unit rents with it. A column the header names as a
#: status gets its vocabulary whatever shape the values are; what bounds it is the COUNT
#: and the LENGTH, not whether the values look like codes.
STATUS_MAX_VOCABULARY = 64
#: Each value truncated to this before it travels. 64 x 40 is 2.5 KB of prompt; a roll with
#: a paragraph in its status column would otherwise put all of it in the request.
STATUS_VALUE_CHARS = 40

#: Characters of digest appended when two values truncate to the same key.
STATUS_HASH_CHARS = 6


def hashed_status_key(value: str) -> str:
    """A bounded key that is unique to this FULL value, for a prefix two values share.

    ``Occupied - resident in place and current on rent (renewal signed)`` and the same
    sentence ending ``(notice served)`` truncate to one key, and one key gets one meaning:
    the model was shown a single value and `assemble` then applied that meaning to both,
    reversing the occupancy of every unit in the second group. The digest
    is of the whole value, so the keys differ wherever the values do — and it is still
    bounded, still readable, and still a prefix of the original, which is what
    `expand_truncated_statuses` matches on.
    """
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:STATUS_HASH_CHARS]
    keep = STATUS_VALUE_CHARS - STATUS_HASH_CHARS - 1
    return f"{value[:keep]}~{digest}"


def bounded_status_values(distinct: list[str]) -> "list[str] | None":
    """The keys a status column's values travel as. ``None`` if they cannot be made unique.

    Plain truncation where it is unambiguous — most rolls, and the form every earlier round
    produced — and a digest-suffixed key for any value that would otherwise collide with
    another. The result is checked for uniqueness rather than assumed: a caller that gets
    ``None`` refuses the mapping instead of mapping two statuses onto one meaning.
    """
    counts: dict[str, int] = {}
    for value in distinct:
        plain = value[:STATUS_VALUE_CHARS]
        counts[plain] = counts.get(plain, 0) + 1
    keys = [
        value[:STATUS_VALUE_CHARS] if counts[value[:STATUS_VALUE_CHARS]] == 1
        else hashed_status_key(value)
        for value in distinct
    ]
    return keys if len(set(keys)) == len(distinct) else None

#: Headers whose values NEVER travel, whatever else they look like. Checked first, and it
#: wins over any allow match: "Lease Notes" contains a role word and is free text about a
#: resident, and shape cannot tell those apart — five short surnames profile as a code.
FREE_TEXT_HEADERS: tuple[str, ...] = (
    "note", "notes", "comment", "comments", "remark", "remarks", "memo", "memos",
    "description", "desc", "address", "name", "email", "e-mail", "mail", "phone",
    "mobile", "tel", "contact", "tenant", "resident", "applicant", "guarantor",
    "occupant", "lessee", "prospect", "customer", "party", "reference", "instructions",
    # A note ABOUT something is free text whatever it is a note about. Round 18 withheld
    # "Status Reason"'s SENTENCES by shape; two short codes under the same header still
    # travelled as samples, because shape was doing the work and the header was not.
    # These are the `DETAIL_WORDS` this list did not already carry.
    "reason", "reasons", "detail", "details", "remark", "remarks", "memo", "memos",
    "explanation", "commentary",
)

#: The ONLY columns whose values may be shown to a model, by header.
#:
#: The profiler used to decide this by SHAPE — anything with at most 40 short distinct
#: values profiled as a "code" and had its samples, or its whole vocabulary, sent. A
#: ``Notes`` or ``Comments`` column of short surnames is exactly that shape. Shape cannot
#: tell a floor-plan code from a person, so it is the wrong question: the right one is
#: whether the column has a semantic role the mapping actually needs, and that is a list
#: someone can read. Everything not on it travels as a header and a shape, nothing more.
VALUE_BEARING_TOKENS: frozenset[str] = frozenset({
    "unit", "units", "apt", "apartment", "space", "door", "bldg", "building",
    "status", "occupancy", "occupied", "vacant",
    # The abbreviations `STATUS_ROLE_HEADERS` now names. A header has to be value-BEARING
    # before it can be a status role, so widening one list without the other left "Occ"
    # and "Vacancy" silently sending nothing.
    "occ", "vacancy", "sts",
    "plan", "floorplan", "type", "style", "model",
    "bed", "beds", "bedroom", "bedrooms", "bath", "baths", "bathroom", "bathrooms",
    "sqft", "sf", "area", "size", "footage",
    "rent", "rents", "market", "lro", "amount", "charge", "balance", "deposit",
    "concession", "concessions",
    "lease", "move", "movein", "expiration", "expires", "start", "end", "begin",
    "date", "term", "renewal",
})

#: Multi-word roles, matched as adjacent tokens.
VALUE_BEARING_PHRASES: tuple[tuple[str, ...], ...] = (
    ("sq", "ft"), ("square", "feet"), ("square", "footage"), ("floor", "plan"),
    ("unit", "type"), ("lease", "type"),
)

_HEADER_SPLIT = re.compile(r"[^a-z0-9]+")


def header_tokens(header: str) -> list[str]:
    return [t for t in _HEADER_SPLIT.split(str(header or "").lower()) if t]


def value_bearing(header: str) -> bool:
    """May this column's values be shown to a model at all?

    Two rules, in order.

    **Free text always loses.** A header naming notes, comments, an address or a person
    contributes no values, whatever else it also says. This is checked first because the
    old substring allowlist let "Lease Notes" and "Move-in Comments" through on ``lease``
    and ``move`` — and a Notes column of five short surnames profiles as a *code*, so the
    shape test that follows would then have sent the whole vocabulary.

    **The allow match is on TOKENS, not substrings.** The header has to BE a role phrase
    rather than merely contain one. That also removes the accidents the substring form
    carried: "Balance" no longer matched on ``ba``, "Formatted" no longer on ``mat``.
    """
    tokens = header_tokens(header)
    if not tokens:
        return False
    if any(token in FREE_TEXT_HEADERS for token in tokens):
        return False
    if any(token in VALUE_BEARING_TOKENS for token in tokens):
        return True
    for phrase in VALUE_BEARING_PHRASES:
        width = len(phrase)
        if any(tuple(tokens[i:i + width]) == phrase for i in range(len(tokens) - width + 1)):
            return True
    return False

_DATEISH = re.compile(
    r"^\s*(\d{4}-\d{1,2}(-\d{1,2})?([ T].*)?|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"[A-Za-z]{3,9}[\s\-/,']+\d{2,4})\s*$"
)


def _looks_numeric(value: str) -> bool:
    cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


def _too_many_status_values(name, count: int) -> ApiError:
    """One sentence for a status column too wide to map, raised from both places."""
    return ApiError(
        422,
        "status_vocabulary_too_large",
        f"The {name or 'status'} column holds {count:,} different values, past the "
        f"{STATUS_MAX_VOCABULARY} this demo will map. That reads as free text rather "
        f"than a status column — check the roll has one status column and try again.",
    )


def column_values(index: int, rows: list[list]) -> tuple[list[str], list[str]]:
    """One column's non-empty values, and its distinct set in first-seen order.

    Shared by the profiler and by the late vocabulary exposure in :func:`map_rent_roll`,
    so the set a column is judged on is the same set that travels.
    """
    values: list[str] = []
    for row in rows[:PROFILE_MAX_ROWS]:
        if index >= len(row):
            continue
        cell = row[index]
        if cell is None:
            continue
        text = str(cell).strip()
        if text:
            values.append(text)
    distinct: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            distinct.append(value)
    return values, distinct


def column_profiles(header: list[str], rows: list[list]) -> list[dict]:
    """One description per column: header, type, distinct count, and safe samples only."""
    people = person_columns(header)
    profiles: list[dict] = []
    for index, name in enumerate(header):
        values, distinct = column_values(index, rows)

        if not values:
            kind = "empty"
        elif sum(1 for v in values if _looks_numeric(v)) >= 0.8 * len(values):
            kind = "number"
        elif sum(1 for v in values if _DATEISH.match(v)) >= 0.8 * len(values):
            kind = "date"
        elif (max((len(v) for v in values), default=0) <= PROFILE_CODE_CHARS
              and len(distinct) <= PROFILE_MAX_VOCABULARY):
            kind = "code"
        else:
            kind = "text"

        # The deny lists are asked FIRST and every question below is conditioned on the
        # answer. Order is the whole control here: `status_role` matches on a header word,
        # and "Occupied By" carries one — so a question about statuses asked before the
        # question about people turns a column of residents into a vocabulary.
        allowed = index not in people and value_bearing(name)
        # A column the HEADER names as a status, whatever LENGTH its values are — but not
        # whatever KIND they are. One real roll has a column headed "Occupied"
        # holding 279 distinct MOVE-IN DATES: a header word alone is not enough, and
        # calling that a status column refused the whole roll. Codes and prose can be
        # statuses; dates and numbers cannot.
        status_role = (
            allowed
            and kind in ("code", "text")
            and status_role_header(name)
        )
        status_like = (
            allowed
            and kind == "code"
            and (status_role or len(distinct) <= 12)
        )
        if status_role and len(distinct) > STATUS_MAX_VOCABULARY:
            raise _too_many_status_values(name, len(distinct))

        profile: dict = {
            "index": index,
            "header": str(name or ""),
            "type": kind,
            "distinct": len(distinct),
            "samples": [],
            "values": [],
        }
        # Values travel only from an allowlisted semantic column, and only when they are
        # numbers, dates or short codes. A column that is neither — a Notes column, a
        # Comments column, anything whose header names no role the mapping needs — is
        # described and never quoted, whatever shape its values take.
        if allowed and kind in ("number", "date", "code"):
            profile["samples"] = distinct[:PROFILE_SAMPLES]
            if status_like:
                profile["values"] = distinct[:PROFILE_MAX_VOCABULARY]
        if status_role:
            # The whole set, bounded by count above and by length here. Truncation is
            # deliberate and load-bearing downstream: what the model is shown is what it
            # answers with, so `_vocabulary_gap` and the status map both key on the same
            # bounded form, and `assemble` resolves it back to the roll's full cell value.
            # UNIQUE, though — two values sharing a 40-character prefix used to arrive as
            # one key and leave with one meaning.
            keys = bounded_status_values(distinct)
            if keys is None:
                raise ApiError(
                    422,
                    "status_vocabulary_ambiguous",
                    f"The {name or 'status'} column holds values this demo cannot tell "
                    f"apart within the {STATUS_VALUE_CHARS} characters it shows a model. "
                    f"Rather than map two different statuses onto one meaning, this run "
                    f"stopped.",
                )
            profile["values"] = keys
            profile["samples"] = profile["values"][:PROFILE_SAMPLES]
        profiles.append(profile)
    return profiles


def render_profiles(profiles: list[dict]) -> str:
    """The profile table as the model sees it."""
    lines = []
    for profile in profiles:
        parts = [
            f"[{profile['index']}] {profile['header'] or '(no header)'}",
            f"type={profile['type']}",
            f"distinct={profile['distinct']}",
        ]
        if profile["values"]:
            parts.append("values=" + " | ".join(profile["values"]))
        elif profile["samples"]:
            parts.append("samples=" + " | ".join(profile["samples"]))
        else:
            parts.append("samples=(withheld)")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def status_values(profiles: list[dict]) -> dict[int, list[str]]:
    """Every distinct value of each column that carries a vocabulary, by column index."""
    return {p["index"]: list(p["values"]) for p in profiles if p["values"]}


def blind_status_column(result: dict, profiles: list[dict]) -> "int | None":
    """The index of a SELECTED status column whose values were never shown, or None.

    This is the hole `_vocabulary_gap` could not see. It checks the returned vocabulary
    against the values the profile carries — so a column with NO profiled values has
    nothing to check, returns an empty gap, and reads as complete. The mapping is then
    accepted with an empty status map, every code in the column misses in `_status_of`,
    and a roll of "O" and "V" reports every unit as Non-revenue: occupancy, vacancy and
    the occupied rents all wrong, confidently, with nothing saying so. an earlier review.

    A column is only "selected" here when the mapper named it and the sheet has it; an
    index outside the header is caught earlier by :func:`out_of_range_columns`.
    """
    index = (result.get("columns") or {}).get("status")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    profile = next((p for p in profiles if p["index"] == index), None)
    if profile is None or profile.get("values"):
        return None
    return index


def expose_status_vocabulary(
    profiles: list[dict], index: int, header: list[str], rows: list[list]
) -> bool:
    """Give the mapper the whole vocabulary of the column it chose, if it may travel.

    Returns True when the profile now carries the column's complete distinct set, so the
    call can be retried and validated the ordinary way. Returns False when those values
    may never be shown — which is a refusal, not a retry: a Notes column's values are
    resident information, and no amount of asking again makes them safe to send.

    The eligibility rules are the profiler's own, applied to a column the MODEL named
    rather than one a header word did: the values must not be a person's, the header must
    not name free text, and the shape must be one a status can have — codes or short
    prose, never a date and never a number. A column of move-in dates is not a status
    vocabulary however confidently it was selected.
    """
    profile = next((p for p in profiles if p["index"] == index), None)
    if profile is None:
        return False
    name = header[index] if 0 <= index < len(header) else ""
    if index in person_columns(header):
        return False
    tokens = header_tokens(name)
    if any(token in FREE_TEXT_HEADERS for token in tokens):
        return False
    # A note ABOUT the status is not the status. The header-named path refuses these in
    # `status_role_header`; a model that names one as its status column gets the same
    # answer here, because the values are the same sentences either way.
    if any(token in DETAIL_WORDS for token in tokens):
        return False
    if profile.get("type") not in ("code", "text"):
        return False
    _values, distinct = column_values(index, rows)
    if not distinct:
        return False
    if len(distinct) > STATUS_MAX_VOCABULARY:
        raise _too_many_status_values(name, len(distinct))
    # Bounded exactly as the header-named path bounds, so what the model answers with is
    # what `_vocabulary_gap` checks and what `assemble.expand_truncated_statuses` resolves
    # back to the roll's full cell value — including the digest suffix that keeps two
    # values sharing a prefix apart.
    keys = bounded_status_values(distinct)
    if keys is None:
        return False
    profile["values"] = keys
    profile["samples"] = profile["values"][:PROFILE_SAMPLES]
    # Index and counts, never the header or the values: both are the visitor's.
    log.info("status column %d was selected without a vocabulary; re-asking with its "
             "%d distinct values", index, len(distinct))
    return True


# --------------------------------------------------------------------------------------
# The verbatim-quote check
# --------------------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Whitespace-collapsed, case-folded. Nothing else.

    PDF extraction inserts line breaks mid-sentence and doubles spaces, so an exact
    ``in`` test would reject honest quotes. Collapsing whitespace and case is as far as
    this goes: normalising punctuation or dropping characters would start accepting
    near-misses, and a near-miss is the shape a hallucination actually takes.
    """
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def quote_appears(quote: str, source: str) -> bool:
    if not quote or not source:
        return False
    if len(quote) > MAX_QUOTE_CHARS:
        return False
    return _normalise(quote) in _normalise(source)


UNVERIFIED_NOTE = (
    "Dropped: the supporting quote could not be found in the document text, so the value "
    "was not trusted."
)

MISMATCHED_NOTE = (
    "Dropped: the value does not appear in the sentence given as its source, so it was "
    "not trusted."
)

_DIGITS = re.compile(r"[0-9]+")
_DATE_PATTERNS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y",
    "%B %Y", "%b %Y", "%m-%d-%Y",
)


#: Written magnitudes and the suffixes a marketing package uses for each.
_MAGNITUDES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1_000_000_000, ("billion", "bn", "b")),
    (1_000_000, ("million", "mm", "m")),
    (1_000, ("thousand", "k")),
)

#: How far a written magnitude may sit from the value and still be the same claim.
#:
#: Half a percent, so "12.4 million" verifies 12,400,000 and "12 million" does NOT — the
#: second is a rounded number, and a check that accepts a rounded number is a check that
#: accepts a different one. The suffix is always required: a bare "12.4" would match the
#: "12.4" in "12.4% vacancy".
_MAGNITUDE_TOLERANCE = 0.005


def _magnitude_forms(number: float) -> set[str]:
    """"12.4 million", "12.4M", "450k" — the ways an OM actually writes a big number."""
    forms: set[str] = set()
    magnitude = abs(number)
    for scale, suffixes in _MAGNITUDES:
        if magnitude < scale:
            continue
        quotient = number / scale
        for decimals in (0, 1, 2, 3):
            text = f"{quotient:.{decimals}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            try:
                if abs(float(text) * scale - number) > abs(number) * _MAGNITUDE_TOLERANCE:
                    continue
            except ValueError:
                continue
            for suffix in suffixes:
                forms.add(f"{text} {suffix}")
                forms.add(f"{text}{suffix}")
    return forms


def _number_forms(value) -> list[str]:
    """Every ordinary written form of a number, for looking it up in a sentence.

    The plain and comma-grouped spellings, plus the written magnitudes — because
    "$12.4 million" is how an offering memorandum states a price, and dropping a CORRECT
    value to MISSING because it was written the normal way is still a wrong answer on the
    page. Currency symbols need no form of their own: every form here is a substring of
    the quote with the symbol in front of it.
    """
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return []
    forms = set()
    if number == int(number):
        whole = int(number)
        forms.update({str(whole), f"{whole:,}"})
    else:
        forms.update({f"{number:g}", f"{number:,.2f}", f"{number:.2f}"})
    forms |= _magnitude_forms(number)
    return sorted(forms)


def _date_forms(value) -> list[str]:
    import datetime as _dt

    text = str(value).strip()
    parsed = None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = _dt.datetime.strptime(text, pattern).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return []
    return sorted({parsed.strftime(p) for p in _DATE_PATTERNS} | {text})


#: Characters that, sitting against a matched number, mean it was part of a BIGGER one.
#:
#: "200" is inside "1200" and "12m" is inside "112m", and a plain substring test called
#: both of those a match — so a model could return any number that happened to be a
#: fragment of a real one in its own quote and the anti-hallucination check would pass it.
_NUMBER_EDGE = "0123456789.,"
_MAGNITUDE_LETTERS = "kmb"


def _continues_left(haystack: str, index: int) -> bool:
    """Is the character before ``index`` part of the same number?"""
    if index <= 0:
        return False
    char = haystack[index - 1]
    if char.isdigit():
        return True
    if char in ".,":
        # A separator only continues a number when a DIGIT sits on its far side.
        return index >= 2 and haystack[index - 2].isdigit()
    return char in _MAGNITUDE_LETTERS


def _continues_right(haystack: str, index: int, ends_in_magnitude: bool) -> bool:
    """Is the character at ``index`` part of the same number?

    A trailing full stop is the case that matters: "Year built: 2021." ends a SENTENCE,
    and reading that period as a decimal point rejected a perfectly good quote. A
    separator continues a number only when a digit follows it.
    """
    if index >= len(haystack):
        return False
    char = haystack[index]
    if char.isdigit():
        return True
    if char in ".,":
        return index + 1 < len(haystack) and haystack[index + 1].isdigit()
    if ends_in_magnitude:
        return False
    return char in _MAGNITUDE_LETTERS


def _form_appears(form: str, haystack: str) -> bool:
    """``form`` in ``haystack``, but only as a whole number.

    Neither side of a hit may be a digit, a decimal point, a thousands comma, or — where
    the form does not already end in one — a magnitude letter.
    """
    if not form:
        return False
    start = 0
    ends_in_magnitude = form[-1] in _MAGNITUDE_LETTERS
    while True:
        index = haystack.find(form, start)
        if index == -1:
            return False
        after_index = index + len(form)
        left_ok = not _continues_left(haystack, index)
        right_ok = not _continues_right(haystack, after_index, ends_in_magnitude)
        if left_ok and right_ok:
            return True
        start = index + 1


def numeric_value(value):
    """The number this value states, or None when it is not a number at all.

    ``$1,234.56`` and ``12.4`` are numbers written the way a document writes them. They
    used to produce NO forms and were then accepted unconditionally — the check waving
    through exactly the values it exists to test.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def value_supported_by(value, quote: str) -> bool:
    """Does the QUOTE actually say the value, for a number or a date?

    The quote check proved the sentence was in the document and stopped there, so
    ``units_total=200`` beside "The community contains 220 units" passed — a real sentence
    attached to a number that is not in it. For anything numeric or date-shaped the value
    has to appear in its own quote, as a WHOLE number; text keeps the presence check,
    because a name or an address is legitimately a paraphrase of the sentence around it.

    A value that IS a number and yields no usable form is MISSING, never accepted: "no
    form to check" is not evidence, and treating it as a pass is how a currency-formatted
    value walked straight through.
    """
    haystack = _normalise(quote)
    if isinstance(value, bool):
        return True

    number = numeric_value(value)
    if number is not None:
        forms = _number_forms(number)
        if not forms:
            return False
        return any(_form_appears(_normalise(form), haystack) for form in forms)

    forms = _date_forms(str(value).strip())
    if forms:
        return any(_normalise(form) in haystack for form in forms)
    # Genuinely text.
    return True


#: Summary fields that are IDENTIFIERS written in digits — a parcel number, a tax-area
#: code. They look numeric and are not: "0311" is a different code from "311", and the
#: numeric check normalises the zero away and then rejects the digits as part of a larger
#: number. These are verified as text, exactly as printed.
TEXT_IDENTIFIER_KEYS: frozenset = frozenset({"tax_parcel_id", "tax_area_code"})


#: The words a quote must carry for a bill or a basis to be what the field says it is.
#: The mock's regexes enforce these on the mock path only; a live model that maps "Payroll
#: taxes paid: $94,000" to the bill, or "Taxable value: $1,600,000" to the basis, passes the
#: number-in-quote check — so the semantics are enforced HERE, after the quote check, on
#: every path. Georgia's "taxable value" is the post-ratio
#: figure; reading it as the basis applies the 40% twice.
_PROPERTY_TAX_CONTEXT = re.compile(r"\b(?:property|real[ -]estate|ad[ -]valorem)\s+tax", re.I)
_NOT_THE_BILL = re.compile(
    r"\b(?:transfer|sales|payroll|income|franchise|intangible|documentary)\s+tax|\bpro[ -]?forma\b|"
    r"\bprojected\b|\bunderwritten\b", re.I)
_FULL_VALUE = re.compile(
    r"\b(?:fair market|appraised(?: market)?|market|full cash|limited property|just|true)\s+value"
    r"|\b(?:LPV|FCV)\b", re.I)
_NOT_THE_BASIS = re.compile(r"\b(?:assessed|taxable|net taxable)\s+value\b", re.I)


def _semantics_hold(key: str, quote: str) -> bool:
    """Key-specific meaning checks on the QUOTE, beyond "the number is in it"."""
    if key in ("tax_billed_amount", "tax_billed_year"):
        return bool(_PROPERTY_TAX_CONTEXT.search(quote)) and not _NOT_THE_BILL.search(quote)
    if key == "assessed_value_basis":
        return bool(_FULL_VALUE.search(quote)) and not _NOT_THE_BASIS.search(quote)
    return True


def _supported(key: str, value, quote: str) -> bool:
    if key in TEXT_IDENTIFIER_KEYS:
        # Text, whole token, exactly as printed: "311" inside "0311" is a different code,
        # and a number returned for an identifier has already lost its zeros. The
        # boundary treats / . _ - as continuations of the identifier: "123" is not "123/456".
        if not isinstance(value, str) or not value.strip():
            return False
        token = re.escape(value.strip())
        # A dot continues the identifier only when a letter or digit follows it ("12.34");
        # the full stop that ends the sentence does not.
        return re.search(
            rf"(?<![A-Za-z0-9./_-]){token}(?!(?:[A-Za-z0-9/_-]|\.[A-Za-z0-9]))", quote
        ) is not None
    if key == "assessed_value_basis":
        # The label printed AGAINST the figure decides, not a label anywhere in the quote:
        # "Limited Property Value: $5,120,000  Assessed Value (10%): $512,000" holds a
        # post-ratio label, and it belongs to the other figure. Where the figure
        # cannot be located as money, the whole-quote rule stands.
        import property_tax_table as ptt
        labels = ptt.basis_value_labels(value, quote)
        if labels:
            return bool(labels & ptt.FULL_VALUE_LABELS) and value_supported_by(value, quote)
    if not _semantics_hold(key, quote):
        return False
    return value_supported_by(value, quote)


BILL_YEAR_UNPAIRED_NOTE = (
    "The document states a tax year and a billed amount in different sentences, and neither "
    "sentence carries the other's figure, so the year was not paired with this amount."
)


def couple_tax_bill(fields: dict[str, dict]) -> dict[str, dict]:
    """A billed amount and its year must come from ONE record.

    Two independent reads can pair an undated bill with a year that belongs to another
    sentence ("Property taxes paid: $90,000" beside "2025 property taxes paid: $100,000"),
    and the tie-out then runs on a pairing nobody stated. Kept only when one quote
    carries the other's figure; otherwise the year is dropped and the amount says why — the
    assembly then records an undated bill and withholds the tie-out.
    """
    amount = fields.get("tax_billed_amount") or {}
    year = fields.get("tax_billed_year") or {}
    if amount.get("value") in (None, "", schema.MISSING) or year.get("value") in (None, "", schema.MISSING):
        return fields
    a_quote, y_quote = _normalise(amount.get("quote") or ""), _normalise(year.get("quote") or "")
    year_in_amount = _normalise(str(year["value"])) in a_quote
    number = numeric_value(amount["value"])
    amount_in_year = number is not None and any(
        _form_appears(_normalise(form), y_quote) for form in _number_forms(number))
    if year_in_amount or amount_in_year:
        return fields
    fields["tax_billed_year"] = {"value": schema.MISSING, "page": year.get("page"), "quote": None,
                                 "note": BILL_YEAR_UNPAIRED_NOTE}
    fields["tax_billed_amount"] = {**amount, "note": BILL_YEAR_UNPAIRED_NOTE}
    return fields


def verify_fields(fields: dict[str, dict], source: str) -> tuple[dict[str, dict], list[str]]:
    """Downgrade every field whose quote is not in the source. Returns (fields, dropped).

    This runs on every extraction, live or mock. It is the only control here that does
    not depend on the model behaving, which is why it is deterministic and why it is
    applied to the mock path too — a mock that skipped it would leave the control
    untested until the day it mattered.
    """
    dropped: list[str] = []
    out: dict[str, dict] = {}
    for key, entry in (fields or {}).items():
        value = entry.get("value")
        quote = entry.get("quote") or ""
        if value in (None, "", schema.MISSING):
            out[key] = {"value": schema.MISSING, "page": entry.get("page"), "quote": None,
                        "note": entry.get("note")}
            continue
        if quote_appears(quote, source):
            if _supported(key, value, quote):
                out[key] = {"value": value, "page": entry.get("page"), "quote": quote,
                            "note": entry.get("note")}
                continue
            # The sentence is real and the number is not in it.
            dropped.append(key)
            out[key] = {
                "value": schema.MISSING,
                "page": entry.get("page"),
                "quote": None,
                "note": MISMATCHED_NOTE,
            }
            continue
        dropped.append(key)
        out[key] = {
            "value": schema.MISSING,
            "page": entry.get("page"),
            "quote": None,
            "note": UNVERIFIED_NOTE,
        }
    if dropped:
        log.warning("extraction: %d field(s) dropped for unverifiable quotes: %s",
                    len(dropped), ", ".join(sorted(dropped)))
    return out, dropped


# --------------------------------------------------------------------------------------
# The field inventory — generated from schema.py, never typed twice
# --------------------------------------------------------------------------------------

#: Summary keys the model is never asked for: they are computed, stamped, or supplied by
#: a source other than the marketing package.
NOT_FROM_OM: frozenset[str] = frozenset(
    {
        "address_provenance",
        "tax_divisor",
        "fair_market_value",
        "school_elementary_rating",
        "school_middle_rating",
        "school_high_rating",
    }
)


#: The pricing field the OM call asks for OUTSIDE the summary inventory.
#:
#: ``summary.pricing_guidance`` is prose — the schema types it ``str`` — and asking a
#: generic per-field schema for "the value" threw away the one thing a price cannot be
#: read without: what it is a price OF. A marketing package's "$12.4 million" came back as
#: the bare number 12,400,000 in a field that requires a sentence, which failed validation
#: and lost the whole run; and any total that happened to land between $50k and $600k came
#: back inside the per-unit sale-price band and was used as a price per unit, producing a
#: confident, wrong cap rate. So the number travels in its own typed field with the basis
#: attached and its own quote to verify.
PRICING_KEY = "asking_price"

#: What a document can say a price is. ``unstated`` is the honest answer for a figure with
#: nothing beside it saying what it covers, and it licenses nothing downstream.
PRICING_BASES: tuple[str, ...] = ("per_unit", "total", "unstated")

UNSTATED_BASIS_NOTE = (
    "Dropped: the document states a figure but not whether it is the price of one home "
    "or of the whole asset, so it was not used."
)

BAD_BASIS_NOTE = (
    "Dropped: the pricing basis returned was not one of per_unit, total or unstated."
)

REFUSED_PER_UNIT_NOTE = (
    "Dropped: the figure was returned as a price per unit, and it is larger than any "
    "per-unit sale price. A claim that wrong about a number is not evidence about it, so "
    "the figure was not used at all."
)

TOO_SMALL_NOTE = (
    "Dropped: the figure is below the smallest per-unit sale price and is not the price "
    "of a whole asset either, so it is not a price."
)

#: The per-unit sale-price band, and the only phrasings that make a figure inside it a
#: PER-UNIT price. The band is the toolkit's own rule; the markers are what stops a figure
#: that merely happens to sit inside it from being read as a price per home.
PRICE_BAND_MIN = 50_000
PRICE_BAND_MAX = 600_000
PER_UNIT_MARKERS: tuple[str, ...] = ("per unit", "per home", "per door", "/unit", "a unit")


#: And the phrasings that make a figure inside the band a WHOLE-ASSET price.
#:
#: The middle band is symmetric: neither basis is the default there. A figure that says
#: what it is a price of is used as that; a figure that says nothing is ``unstated`` and
#: buys nothing. Without this, a genuine "$450,000 for the four-home package" was refused
#: alongside the bare "$450,000" it is nothing like.
WHOLE_ASSET_MARKERS: tuple[str, ...] = (
    "for the property", "for the portfolio", "for the community", "for the asset",
    "for the project", "for the entire", "total price", "aggregate price",
    "whole asset", "whole-asset", "entire property", "entire portfolio",
)

#: A package named by its own size — "the four-home package", "the 4-unit portfolio".
_PACKAGE_MARKER = re.compile(
    r"\b\S+[-\s](?:unit|home|door)s?\s+(?:package|portfolio|community|property|asset|"
    r"deal|project)\b"
)

#: "asking price of $X for the property" — the price word, the figure, and the thing it is
#: the price OF, in one phrase.
_PRICED_ASSET = re.compile(
    r"\b(?:asking|offering|purchase|list|guidance)\s+price\s+(?:of\s+)?\$?[\d,.]+\s+"
    r"(?:for\s+)?(?:the\s+)?(?:property|portfolio|community|asset|package|deal|project)\b"
)


def per_unit_marked(quote: str) -> bool:
    """Does this quote actually say the figure is per unit?"""
    haystack = _normalise(quote)
    return any(marker in haystack for marker in PER_UNIT_MARKERS)


def whole_asset_marked(quote: str) -> bool:
    """Does this quote actually say the figure covers the whole asset?"""
    haystack = _normalise(quote)
    return (
        any(marker in haystack for marker in WHOLE_ASSET_MARKERS)
        or bool(_PACKAGE_MARKER.search(haystack))
        or bool(_PRICED_ASSET.search(haystack))
    )


def decide_basis(value: float, claimed: str, quote: str) -> tuple[str, str | None]:
    """The basis a figure MAY carry, decided here rather than taken from the model.

    Three clauses, and none of them is a judgement call:

    * **Above the band** a figure can only be a total — nobody sells one home for more
      than $600,000 in this pipeline's world. A model that calls such a figure ``per_unit``
      has said something plainly wrong about the number, so the figure is refused outright
      rather than quietly reinterpreted: a source that wrong is not a source.
    * **Inside the band** neither basis is the default and both need saying. A figure is
      per-unit when the verified quote carries one of :data:`PER_UNIT_MARKERS`, and a
      total when it carries one of :data:`WHOLE_ASSET_MARKERS` — "for the property", "the
      four-home package", "total price". A figure whose quote says neither is ``unstated``
      and buys nothing: a whole-asset price that happens to land inside the per-unit band
      is exactly the shape that gets read as a price per home.
    * **Below the band** nothing is a price.

    Inside the band the MARKER decides, not the claim — the quote is the evidence, and the
    mock and the live model reach the same answer from the same sentence. The claim is
    still the tie-break where a quote carries both markers.

    Returns ``(basis, note)``; a note means the figure was refused and the caller must
    leave pricing MISSING.
    """
    if value < PRICE_BAND_MIN:
        return "unstated", TOO_SMALL_NOTE
    if value > PRICE_BAND_MAX:
        if claimed == "per_unit":
            return "unstated", REFUSED_PER_UNIT_NOTE
        return "total", None
    if claimed == "per_unit" and per_unit_marked(quote):
        return "per_unit", None
    if whole_asset_marked(quote):
        return "total", None
    if per_unit_marked(quote):
        return "per_unit", None
    return "unstated", UNSTATED_BASIS_NOTE


def blank_price(note: str | None = None) -> dict:
    """The shape a caller always gets back, whether or not a price survived."""
    return {"value": schema.MISSING, "basis": "unstated", "page": None,
            "quote": None, "note": note}


def verify_price(entry: dict | None, source: str) -> tuple[dict, list[str]]:
    """The asking price, its basis, and the quote that has to carry both.

    Same evidence bar as every other extracted value — the quote must appear verbatim in
    the document text and the number must appear in the quote — plus one more: a basis the
    document did not state is not a basis. ``unstated`` keeps the figure out of the
    pricing block entirely rather than guessing which of the two things it is.
    """
    if not isinstance(entry, dict):
        return blank_price(), []
    basis = str(entry.get("basis") or "unstated")
    if basis not in PRICING_BASES:
        # The returned basis is not echoed: it is a model-authored string, and a model
        # reading a stranger's document is not a source a log should quote.
        log.warning("extraction: the pricing basis returned is not one of %s",
                    PRICING_BASES)
        return blank_price(BAD_BASIS_NOTE), [PRICING_KEY]
    value = entry.get("value")
    page = entry.get("page")
    if value in (None, "", schema.MISSING) or numeric_value(value) is None:
        return blank_price(), []
    quote = entry.get("quote") or ""
    if not quote_appears(quote, source):
        return {"value": schema.MISSING, "basis": basis, "page": page, "quote": None,
                "note": UNVERIFIED_NOTE}, [PRICING_KEY]
    if not value_supported_by(value, quote):
        return {"value": schema.MISSING, "basis": basis, "page": page, "quote": None,
                "note": MISMATCHED_NOTE}, [PRICING_KEY]
    # The basis the MODEL claimed has been carried this far only so the rule below can
    # judge it. What a figure may be is decided from its magnitude and its own quote.
    #
    # A refusal here is NOT a `dropped` entry. `dropped` means the evidence did not hold up
    # — a quote that is not in the document, a number that is not in its quote — and that
    # list is read as an anti-hallucination signal. A figure the document simply did not
    # label, or one too small to be a price, is neither: an offering memorandum that says
    # "$2,412 per home" about RENT is an ordinary document, not a model failure. The note
    # and the log carry the refusal instead.
    basis, refusal = decide_basis(float(numeric_value(value)), basis, quote)
    if refusal is not None:
        # The FIELD and the reason, never the figure. The number here came out of the
        # visitor's own offering memorandum: writing it into a service log keeps client
        # financial data long past the hour the page promises. The full
        # refusal, with the number and its quote, still reaches the visitor on their own
        # token-protected run.
        if refusal is REFUSED_PER_UNIT_NOTE:
            log.warning("extraction: %s was refused — the figure is larger than any "
                        "per-unit sale price", PRICING_KEY)
        else:
            log.info("extraction: %s was not used as a price", PRICING_KEY)
        return {"value": schema.MISSING, "basis": basis, "page": page, "quote": quote,
                "note": refusal}, []
    return {"value": value, "basis": basis, "page": page, "quote": quote, "note": None}, []


#: The share of market value that is assessed, as a FRACTION of one.
#:
#: The prompt's general rule is that a percentage is the number a reader says aloud — 5.35
#: for 5.35% — and for the tax RATE that is right, because `usable_divisor` carries the
#: unit. For this field it is catastrophic: `compute.tax_underwriting` multiplies the
#: property's whole value by it, so a stated "100%" returned as 100 assesses the building
#: at a hundred times its worth and bills the tax on that. The prompt now asks for the
#: fraction, and this normaliser is what makes the answer safe whatever comes back.
ASSESSMENT_RATIO_KEY = "assessment_ratio"

#: A percentage spelled out in the quote, which is the only evidence that licenses
#: dividing a number greater than one.
_PERCENT_SPELLING = re.compile(r"%|\bper\s?cent\b|\bpercent(?:age)?\b", re.I)

#: The lowest share of value any market this toolkit covers actually assesses.
#:
#: Arizona is the floor in practice at 10%; the band below that is not a jurisdiction, it
#: is a misread. "1.5%" beside an assessment ratio is a TAX RATE that wandered into the
#: wrong field, and dividing it by a hundred produced 0.015 — a number inside (0, 1] that
#: would have passed every check and under-assessed the property by a factor of seven.
#: Half of Arizona's ratio, so a real one has room to be lower than any seen so far.
ASSESSMENT_RATIO_FLOOR = 0.05

RATIO_REFUSED_NOTE = (
    "Dropped: the assessment ratio did not resolve to a share of value between 0 and 1, "
    "and a wrong ratio multiplies the whole tax bill rather than shading it."
)

RATIO_NORMALISED_NOTE = (
    "The document states this as a percentage; it is carried as the fraction the "
    "pipeline multiplies by."
)


def normalise_assessment_ratio(value, quote: str = ""):
    """A stated assessment ratio as a fraction of one, or None when it is not one.

    Two ways in, and a floor under both. A value already inside
    ``[ASSESSMENT_RATIO_FLOOR, 1]`` is the fraction. A value above one is only rescued when
    the QUOTE spells a percentage — "100%", "100 percent" — and then only if dividing by a
    hundred lands back inside that band. Anything else is None, which the caller turns into
    MISSING: an assessment ratio is not a figure to guess at, because it is a multiplier on
    the entire tax line rather than one input among many.

    The FLOOR matters as much as the ceiling and is less obvious. A quote reading
    "assessed at 1.5% of market value" is a tax RATE in the wrong field, and 1.5 / 100 is
    0.015 — comfortably inside ``(0, 1]``, so every other check would have passed it, and
    the property would have been assessed at a seventh of Arizona's real ratio.
    """
    number = numeric_value(value)
    if number is None:
        return None
    if ASSESSMENT_RATIO_FLOOR <= number <= 1:
        return float(number)
    if number > 1 and _PERCENT_SPELLING.search(quote or ""):
        scaled = number / 100.0
        if ASSESSMENT_RATIO_FLOOR <= scaled <= 1:
            return float(scaled)
    return None


#: Summary keys the OM call asks for BEYOND the summary card's sections: the memorandum's own
#: property-tax page. A marketing package usually prints the parcel, the tax district, the
#: assessed value and the bill actually paid; they are the only parcel-specific tax facts an
#: upload can carry, and they travel as the memorandum's own statements — stamped to its
#: page and quote, labelled as its, never as the county's. Adding keys here widens only the ``key`` enum of the
#: array-of-entries schema (no new unions; ~90 bytes) — see ``test_live_schema_limits``.
ALSO_FROM_OM: tuple[str, ...] = (
    "tax_parcel_id", "tax_area_code", "tax_billed_amount", "tax_billed_year",
)


def om_field_keys() -> tuple[str, ...]:
    """The summary keys the OM call is asked for: the card's sections, then the tax page."""
    keys: list[str] = []
    for _section, section_keys in schema.summary_sections():
        for key in section_keys:
            if key not in NOT_FROM_OM and key not in keys:
                keys.append(key)
    for key in ALSO_FROM_OM:
        if key not in NOT_FROM_OM and key not in keys:
            keys.append(key)
    return tuple(keys)


#: Summary fields the schema types as prose, so the model may not answer them with a bare
#: number. ``pricing_guidance`` is the one that mattered: a number there both fails
#: validation and destroys the basis. The number has its own field.
#:
#: Since round 19 the types are GENERATED from the inventory rather than written here, so
#: this list is no longer what makes the field string-only — it is the assertion that the
#: inventory still says so, checked in :func:`om_output_schema`.
PROSE_ONLY_KEYS: frozenset = frozenset({"pricing_guidance"})

#: Python type -> its JSON Schema name, for generating the OM call's output types.
_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number"}

#: JSON type -> the word the task text uses for it. The schema cannot type a field
#: individually any more (see :func:`om_output_schema`), so the field list does, and
#: :func:`_normalise_field_types` holds the model to it.
TYPE_WORDS: dict[str, str] = {"string": "text", "integer": "whole number", "number": "number"}


def json_types_for(types: tuple) -> str:
    """The ONE JSON type a summary field is answered with.

    The OM schema used to say "string or number" for nearly every field while
    ``schema.validate`` and ``assemble`` are narrower — so a schema-VALID numeric ``zip``
    made the completed deal fail validation, and a schema-valid ``units_total`` of
    ``240.0`` was silently replaced with zero because it is not an ``int``. One inventory, one contract: what the model may return is what the field takes.

    One type, not a list, since the first live call (2026-09-03): the provider compiles a
    strict schema and refuses one with more than 16 union-typed parameters, and
    ``[type, "null"]`` on value, page and quote of 28 fields is 87 of them. A field that
    the inventory lets carry a number is asked for the number — ``integer`` when that is
    all it takes, ``number`` when a float is allowed — and "not stated" is a separate
    boolean (see :func:`om_output_schema`), never ``null``.
    """
    names = {_JSON_TYPES[t] for t in types if t in _JSON_TYPES}
    if "number" in names:
        return "number"
    if "integer" in names:
        return "integer"
    return "string"


def om_output_schema() -> dict:
    """A strict JSON schema the provider can COMPILE: an array of entries plus the price.

    Two limits fell on the first live call (2026-09-03). A schema with more than 16
    union-typed parameters is refused, and ``[type, "null"]`` on value, page and quote of
    28 fields was 87 of them. With the unions gone the same shape — one strict object per
    field — was refused as a grammar too large to compile; probed the same day, nested
    per-field objects fail from 14 fields up and an array with ONE item shape compiles at
    1.2 KB and costs half the input tokens. So:

    - ``fields`` is an array of ``{key, value, page, quote}`` where ``key`` is an enum of
      exactly the asked-for field names. A field the document does not state is ABSENT
      from the array; there is no ``null`` and no ``stated`` flag per field.
    - ``value`` is string or number — the one union kept. Which of the two a field takes
      is stated per field in :func:`om_task_text` and enforced afterwards by
      :func:`_normalise_field_types`; the schema no longer types fields individually.
    - ``asking_price`` keeps its own object: a ``stated`` boolean, the number, the basis
      enum, page and quote. One object, no unions.
    """
    keys = om_field_keys()
    value_types = {key: json_types_for(schema.SUMMARY_KEYS[key][1]) for key in keys}
    # Two lists that gate each other, asked the same question. `PROSE_ONLY_KEYS` is the
    # claim that these fields are prose; the inventory is what the generator reads. If the
    # inventory ever widens one of them, the number this list exists to keep out would be
    # allowed again silently — in the task text now, since that is where the type lives.
    for key in PROSE_ONLY_KEYS & set(keys):
        if value_types[key] != "string":
            raise RuntimeError(
                f"{key} is in PROSE_ONLY_KEYS but schema.SUMMARY_KEYS types it as "
                f"{value_types[key]}; a bare number in that field destroys the basis"
            )
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": list(keys)},
                        "value": {"type": ["string", "number"]},
                        "page": {"type": "integer"},
                        "quote": {"type": "string"},
                    },
                    "required": ["key", "value", "page", "quote"],
                    "additionalProperties": False,
                },
            },
            PRICING_KEY: {
                "type": "object",
                "properties": {
                    "stated": {"type": "boolean"},
                    "value": {"type": "number"},
                    "basis": {"type": "string", "enum": list(PRICING_BASES)},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["stated", "value", "basis", "page", "quote"],
                "additionalProperties": False,
            },
        },
        "required": ["fields", PRICING_KEY],
        "additionalProperties": False,
    }


def _placeholder_free(entry: dict) -> dict:
    """``page`` and ``quote`` placeholders to ``None``: pages are 1-based, "" is no quote."""
    page = entry.get("page")
    return {
        **entry,
        "page": page if isinstance(page, int) and not isinstance(page, bool) and page > 0
        else None,
        "quote": entry.get("quote") or None,
    }


def entries_to_fields(raw: dict) -> dict:
    """The model's array-of-entries answer back to ``{key: {value, page, quote}}``.

    Every asked-for key is present in the result; one the array does not carry is
    ``{"value": None, "page": None, "quote": None}``, which is what "not stated" has
    always looked like to :func:`verify_fields`. The first entry for a repeated key wins
    (the model was asked for one per field), a key outside :func:`om_field_keys` is
    dropped, and both are counted in the log — never quoted, they came out of a
    stranger's document. The priced field keeps its own object: ``stated: false`` empties
    it. A payload without a ``fields`` array (the mock path) passes through, placeholders
    cleaned, so both modes leave here in one shape.
    """
    raw = dict(raw or {})
    known = om_field_keys()
    out: dict = {}
    entries = raw.pop("fields", None)
    if isinstance(entries, list):
        repeated = 0
        unknown = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if key not in known:
                unknown += 1
                continue
            if key in out:
                repeated += 1
                continue
            out[key] = _placeholder_free(
                {"value": entry.get("value"), "page": entry.get("page"),
                 "quote": entry.get("quote")}
            )
        if repeated or unknown:
            log.warning("extraction: %d repeated and %d unknown entr%s ignored",
                        repeated, unknown, "y" if repeated + unknown == 1 else "ies")
        for key in known:
            out.setdefault(key, {"value": None, "page": None, "quote": None})
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            out[key] = entry
            continue
        cleaned = _placeholder_free({k: v for k, v in entry.items() if k != "stated"})
        if "stated" in entry and not entry.get("stated"):
            cleaned.update({"value": None, "page": None, "quote": None})
        out[key] = cleaned
    return out


def om_task_text() -> str:
    """The field list, generated from ``schema.summary_sections()`` (one source)."""
    lines = [
        "Extract these fields from the document as entries in the fields array, one "
        "entry per field, with the field's key, its value in the type shown beside it, "
        "the page it appears on, and quote — a sentence from the document that contains "
        "the value. Omit any field the document does not state: do not include an entry "
        "for it, and never invent a placeholder.",
        "",
    ]
    for section, section_keys in schema.summary_sections():
        wanted = [k for k in section_keys if k not in NOT_FROM_OM]
        if not wanted:
            continue
        lines.append(f"{section}:")
        for key in wanted:
            word = TYPE_WORDS[json_types_for(schema.SUMMARY_KEYS[key][1])]
            lines.append(f"  - {key} ({word})")
        lines.append("")
    lines.append("Property-tax page, if the memorandum has one:")
    for key in ALSO_FROM_OM:
        word = TYPE_WORDS[json_types_for(schema.SUMMARY_KEYS[key][1])]
        lines.append(f"  - {key} ({word})")
    lines.extend(
        [
            "",
            "tax_billed_amount is the property tax ACTUALLY BILLED or paid for the year in "
            "tax_billed_year, as the document states it — never a pro forma, projected, "
            "underwritten or 'estimated' tax, and never a transfer, sales, payroll or income "
            "tax. If the document shows only a projection, omit both. assessed_value_basis is "
            "the FULL value the assessor states BEFORE the assessment ratio — market, "
            "appraised, full cash, limited property, just or true value. Do not use a printed "
            "'assessed value' or 'taxable value': in most states those are post-ratio figures, "
            "and if the document gives only such a figure, omit the field. In Arizona the "
            "basis is the limited property value (LPV), never the full cash value: when both "
            "are printed, return the LPV and quote the line that labels it. tax_parcel_id is the "
            "assessor parcel number (APN) and tax_area_code the tax area, district or levy "
            "code: both are TEXT, copied exactly as printed, leading zeros included.",
            "",
            f"{ASSESSMENT_RATIO_KEY} is a FRACTION of one, never a percentage: 1.0 for "
            "\"assessed at 100% of market value\", 0.35 for \"assessed at 35%\". Quote the "
            "phrase the document uses, percent sign and all.",
            "",
            "Pricing, which is two answers and not one:",
            "",
            "  - pricing_guidance is PROSE. Give the sentence the document uses about "
            "price, in words. Never a bare number.",
            f"  - {PRICING_KEY} is the number, with the basis attached:",
            "      value : the dollar figure as a number; stated false when the "
            "document gives no figure",
            "      basis : per_unit when the document says the figure is per unit, per "
            "home or per door;",
            "              total when it is the price of the whole asset (a written "
            "magnitude such as",
            "              \"$12.4 million\" is always a total);",
            "              unstated when the document gives a figure but does not say "
            "which it is.",
            "      quote : the sentence that states BOTH the figure and its basis.",
            "",
            "    A stated total is never a per-unit price. Do not divide, and do not "
            "convert between the two — return what the document said and let the "
            "pipeline do the arithmetic.",
            "",
            "    The basis is then checked against the figure, deterministically, and a "
            "claim that does not survive costs the whole field:",
            f"      - above ${PRICE_BAND_MAX:,} a figure can only be a total. Calling one "
            "per_unit is refused outright and nothing is used.",
            f"      - between ${PRICE_BAND_MIN:,} and ${PRICE_BAND_MAX:,} the quote "
            "itself has to say which it is: per_unit needs "
            + ", ".join(repr(m) for m in PER_UNIT_MARKERS)
            + "; total needs a whole-asset phrase such as 'for the property', 'total "
            "price' or 'the four-home package'. A figure whose quote says neither is "
            "unstated and is not used.",
            f"      - below ${PRICE_BAND_MIN:,} nothing is a price.",
            "",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------


def _client(timeout: float = TIMEOUT_SECONDS):
    from anthropic import Anthropic  # live mode only; mock mode never imports the SDK

    # max_retries=0: see MAX_RETRIES. The retry this service wants is the one it manages
    # itself, inside the time the run has left. API key: ANTHROPIC_API_KEY from the
    # environment.
    return Anthropic(timeout=timeout, max_retries=MAX_RETRIES)


def _system(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _usage_from(response, call: str, model: str, seconds: float) -> UsageEntry:
    usage = getattr(response, "usage", None)
    return UsageEntry(
        call=call,
        model=model,
        mode=MODE_LIVE,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        seconds=seconds,
    )


def _json_from(response) -> dict:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise ValueError("the model returned no text block to parse")


def call_json(
    *,
    call: str,
    model: str,
    system: str,
    user: str,
    output_schema: dict,
    max_tokens: int,
    ledger: UsageLedger,
    effort: str | None = None,
    client=None,
) -> dict:
    """One structured-output call. No tools, no network, no filesystem in the request."""
    # The client is constructed BEFORE the price check rather than after it, because the
    # price check needs it: `count_tokens` is the provider's own count for this exact
    # request and is free. Constructing the client sends nothing; `messages.create` below
    # is still reached only by a call that has cleared the cap. an earlier review.
    # Before the preflight, not only after it: `count_tokens` is a network call of its
    # own, and a run with two seconds left should not spend them counting a call it can
    # never send.
    if ledger.out_of_time():
        raise DeadlineExceeded(
            f"{call}: the run has no time left, so nothing was sent"
        )
    client = client or _client(ledger.call_timeout())
    # The format is part of the request and part of its price, so it is built here and
    # counted here — the same object the request below carries.
    output_format = {"type": "json_schema", "schema": output_schema}
    counted = count_input_tokens(client, model, system, user,
                                 output_config={"format": output_format})
    upcoming = price_call(model, counted, system, user, max_tokens,
                          output_config={"format": output_format})
    if counted is None:
        # The bound, not a measurement. If it does not fit, the honest answer is that this
        # call could not be priced — never "send it and find out", which is how one call
        # crosses both the run cap and the day's reservation.
        try:
            ledger.reserve(upcoming)
        except BudgetExhausted as exhausted:
            raise PricingUnavailable(
                f"{call}: the token count could not be obtained and the worst case "
                f"({upcoming:.4f} dollars) does not fit what is left, so the call was "
                f"not sent ({exhausted})"
            ) from None
    else:
        # RESERVED, not merely checked: chunks of one statement are in flight together.
        ledger.reserve(upcoming)
    try:
        return _send_json(call=call, model=model, system=system, user=user,
                          output_format=output_format, max_tokens=max_tokens,
                          ledger=ledger, effort=effort, client=client)
    finally:
        ledger.release(upcoming)


def _send_json(
    *,
    call: str,
    model: str,
    system: str,
    user: str,
    output_format: dict,
    max_tokens: int,
    ledger: UsageLedger,
    effort: str | None,
    client,
) -> dict:
    """The send half of :func:`call_json`, inside its reservation."""
    if ledger.out_of_time():
        raise DeadlineExceeded(
            f"{call}: the run has no time left, so this call was not sent"
        )
    # The time left NOW. The client was built before counting, and counting can take
    # seconds of its own — reusing that client's timeout let a call start with the whole
    # remaining budget it had before the count and run past the hard stop holding a worker
    # slot. Passed per request, which is what the SDK's timeout argument is for.
    call_timeout = ledger.call_timeout()
    output_config: dict[str, Any] = {"format": output_format}
    if effort:
        output_config["effort"] = effort

    started = time.time()
    if model == EXTRACT_MODEL:
        # Refusal fallbacks are an Opus-tier feature and need the beta endpoint, which is
        # also where output_config lives for this shape.
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            timeout=call_timeout,
        )
    else:
        # Haiku 4.5 predates the effort parameter and rejects it; it also gets no
        # fallbacks, which are an Opus-tier feature.
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": output_config["format"]},
            timeout=call_timeout,
        )
    seconds = time.time() - started

    ledger.record(_usage_from(response, call, model, seconds))

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise ModelRefused(
            f"{call}: the request was declined "
            f"({getattr(details, 'category', 'no category')})"
        )
    if stop_reason == "max_tokens":
        # The ceiling and the call, never the text: what came back is a prefix of an
        # answer about the visitor's document.
        raise OutputTruncated(
            f"{call}: the answer was cut off at the {max_tokens}-token ceiling "
            f"(thinking counts against it); nothing was parsed"
        )
    return _json_from(response)


# --------------------------------------------------------------------------------------
# The five calls
# --------------------------------------------------------------------------------------

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["om", "t12", "rent_roll", "trade_out", "context"]},
        "reason": {"type": "string"},
    },
    "required": ["role", "reason"],
    "additionalProperties": False,
}


def classify(excerpt: str, ledger: UsageLedger, client=None) -> dict:
    """Role for one document. Only called when the rule layer were not confident."""
    # The cap is checked on every public call, not only inside call_json: mock mode never
    # reaches call_json, so a cap tested only there is a cap the default path never runs.
    ledger.check_cap()
    if mode() == MODE_MOCK:
        return _mock_classify(excerpt, ledger)
    return call_json(
        call="classify",
        model=CLASSIFY_MODEL,
        system=_system("system_classify.md"),
        user=excerpt_region(excerpt),
        output_schema=CLASSIFY_SCHEMA,
        max_tokens=MAX_TOKENS_CLASSIFY,
        ledger=ledger,
        client=client,
    )


#: The address-first call. The market report needs only the property's address and
#: product, and the full OM call takes ~45 s; this one reads those five fields off the
#: front of the memorandum in ~2 s on the classification model, so the report can run
#: BESIDE the extraction instead of after it. Every value is checked against the text;
#: a value the document does not state is MISSING, never a guess.
ADDRESS_MODEL = CLASSIFY_MODEL
MAX_TOKENS_ADDRESS = 400
ADDRESS_KEYS: tuple[str, ...] = ("address", "city", "state", "zip", "county", "product_type")
ADDRESS_REGION_CHARS = 20_000


#: The comparables the memorandum states. A second call on the same document —
#: NOT cache-shared with the summary call (its schema differs — see om_request_blocks) — asking only for the comp tables: rent comparables, and the whole-
#: community SALE comparables an institutional package cites. Retail for-sale comps are
#: out of scope by decision and the block says so.
MAX_TOKENS_COMPS = 4_000
OM_RENT_COMP_FIELDS: tuple[str, ...] = ("community", "distance_mi", "units", "year_built", "rent", "rent_basis", "page", "quote")
OM_SALE_COMP_FIELDS: tuple[str, ...] = ("property", "units", "year_built", "sale_date", "price", "price_per_unit",
                                        "cap_rate", "buyer", "seller", "page", "quote")
#: Textual fields that must appear in the row's own quote to be published. The name drops the row; the others read MISSING. ``rent_basis`` is
#: the model's characterisation of what the figure is, not a fact about the world, so it is
#: not held to the quote — the quote itself is shown beside it.
OM_COMPS_TEXT_IN_QUOTE = {"rent": (), "sale": ("sale_date", "buyer", "seller")}


def _in_quote(value: str, quote: str) -> bool:
    """Whitespace-normalised, case-insensitive containment."""
    norm = lambda t: " ".join(str(t or "").split()).lower()
    return bool(norm(value)) and norm(value) in norm(quote)


def om_comps_schema() -> dict:
    """Two arrays of plain-string entries — the shape the grammar ceiling accepts."""
    def entry(fields):
        return {
            "type": "object",
            "properties": {f: {"type": "string"} for f in fields},
            "required": list(fields),
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "rent": {"type": "array", "items": entry(OM_RENT_COMP_FIELDS)},
            "sale": {"type": "array", "items": entry(OM_SALE_COMP_FIELDS)},
        },
        "required": ["rent", "sale"],
        "additionalProperties": False,
    }


def om_comps_task_text() -> str:
    return (
        "List the COMPARABLES this marketing package states. RENT comparables: one entry per "
        "comparable community with community (name), distance_mi (miles from the subject, a "
        "number), units, year_built, rent (the monthly rent figure the document states, a "
        "number) and rent_basis (what that rent is — e.g. 'average in-place, 2BR', 'asking, "
        "all plans'). SALE comparables: one entry per whole-community trade the document "
        "cites with property, units, year_built, sale_date (as written), price (the total, a "
        "number), price_per_unit (a number), cap_rate (as written, e.g. 5.1%), buyer, seller. "
        "For every entry give page (the page number, as digits) and quote — the document's own "
        "words, copied exactly, that carry the figures. Every value must be copied from the "
        f"document; for anything an entry does not state, answer the literal string {schema.MISSING}. "
        "Do not include the subject property itself, and do not invent a comparable the "
        "document does not list. Answer empty arrays when the document states none."
    )


_MOCK_RENT_COMP = re.compile(
    r"Rent comparable:\s*(?P<community>[^|]+?)\s*\|\s*(?P<distance_mi>[\d.]+)\s*mi\s*\|\s*(?P<units>\d+)\s*units\s*\|"
    r"\s*built\s*(?P<year_built>\d{4})\s*\|\s*\$(?P<rent>[\d,]+)\s*"
    r"(?P<rent_basis>[^|\n]*?)(?=\s*(?:Rent comparable:|Sale comparable|\n|$|\.\s))"
)
_MOCK_SALE_COMP = re.compile(
    r"Sale comparable:\s*(?P<property>[^|]+?)\s*\|\s*(?P<units>\d+)\s*units\s*\|\s*built\s*(?P<year_built>\d{4})\s*\|"
    r"\s*sold\s*(?P<sale_date>[\w-]+)\s*\|\s*\$(?P<price>[\d,]+)\s*\|\s*\$(?P<price_per_unit>[\d,]+)\s*per unit\s*\|"
    r"\s*(?P<cap_rate>[\d.]+%)\s*cap\s*\|\s*buyer\s*(?P<buyer>[^|]+?)\s*\|\s*seller\s*(?P<seller>[^|.]+)"
)


def _mock_om_comps(text: str, ledger: UsageLedger) -> dict:
    """The mock reads one structured line per comparable — the contract, reachable without a key."""
    _mock_entry("om_comps", EXTRACT_MODEL, ledger)
    body = text or ""
    out: dict = {"rent": [], "sale": []}
    for m in _MOCK_RENT_COMP.finditer(body):
        entry = {k: (m.group(k) or "").strip() for k in ("community", "distance_mi", "units", "year_built", "rent", "rent_basis")}
        entry["rent"] = entry["rent"].replace(",", "")
        entry["page"] = schema.MISSING
        entry["quote"] = m.group(0).strip()
        out["rent"].append(entry)
    for m in _MOCK_SALE_COMP.finditer(body):
        entry = {k: (m.group(k) or "").strip() for k in
                 ("property", "units", "year_built", "sale_date", "price", "price_per_unit", "cap_rate", "buyer", "seller")}
        entry["price"] = entry["price"].replace(",", ""); entry["price_per_unit"] = entry["price_per_unit"].replace(",", "")
        entry["page"] = schema.MISSING
        entry["quote"] = m.group(0).strip()
        out["sale"].append(entry)
    return out


_PAGE_MARK = re.compile(r"\[\[page (\d+)\]\]")


def page_of_quote(text: str, quote: str) -> int | None:
    """The page whose marker sits above the quote's first occurrence; None without markers.

    The parser writes ``[[page n]]`` above every PDF page; a .docx carries no pagination.
    A page the model cites is never taken on trust — a number for a document that has no
    pages is invented, and a wrong one is a wrong provenance locator.
    """
    body = text or ""
    target = _normalise(quote or "")
    if not target:
        return None
    # Locate the quote in the normalised text and map back by counting markers that
    # precede that position in the ORIGINAL text; the normalisation keeps markers intact.
    norm = _normalise(body)
    at = norm.find(target)
    if at < 0:
        return None
    markers_before = list(_PAGE_MARK.finditer(norm[:at]))
    if not markers_before:
        return None
    return int(markers_before[-1].group(1))


def _comp_number(raw: str, field: str):
    """A stated figure as a number: '$52,000,000' → 52000000.0, '5.1%' → 0.051, '220' → 220."""
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text or text == schema.MISSING:
        return schema.MISSING
    percent = text.endswith("%")
    text = text.rstrip("%").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if field == "cap_rate":
        return round(value / 100.0, 6) if (percent or value > 1.0) else value
    if field in ("units", "year_built"):
        return int(value) if value == int(value) else None
    return value


def verify_comp_rows(raw: dict, source: str) -> tuple[dict, list[str]]:
    """Every row's quote must be in the document, its NAME and every stated number in its quote.

    A row that fails any of those is DROPPED and named; a field the model marked MISSING stays
    MISSING; a party or date the quote does not carry reads MISSING. Numbers are checked as the
    document writes them (``5.1``, not ``0.051``) and converted only after they pass.
    """
    rows: dict = {"rent": [], "sale": []}
    dropped: list[str] = []
    for kind, fields, name_key in (("rent", OM_RENT_COMP_FIELDS, "community"), ("sale", OM_SALE_COMP_FIELDS, "property")):
        for entry in (raw or {}).get(kind) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get(name_key) or "").strip()
            quote = str(entry.get("quote") or "").strip()
            if not name or name == schema.MISSING or not quote_appears(quote, source):
                dropped.append(f"{kind}: {name or '?'} (quote not in the document)")
                continue
            if not _in_quote(name, quote):
                dropped.append(f"{kind}: {name} (its name is not in its quote)")
                continue
            row: dict = {}
            bad = False
            for field in fields:  # noqa: F402
                value = str(entry.get(field) if entry.get(field) is not None else "").strip()
                if field == "quote":
                    row[field] = quote
                elif field == "page":
                    # Derived from where the quote sits, never the model's number.
                    row[field] = page_of_quote(source, quote)
                elif field in schema.OM_COMPS_NUMBER_FIELDS[kind]:
                    if not value or value == schema.MISSING:
                        row[field] = schema.MISSING
                        continue
                    written = value.replace("$", "").replace(",", "").rstrip("%").strip()
                    if not value_supported_by(written, quote):
                        bad = True
                        break
                    number = _comp_number(value, field)
                    if number is None:
                        bad = True
                        break
                    row[field] = number
                elif field in OM_COMPS_TEXT_IN_QUOTE[kind]:
                    # A party or date the quote does not carry is the model's word, not the
                    # document's: MISSING, never published.
                    row[field] = value if value and value != schema.MISSING and _in_quote(value, quote) else schema.MISSING
                else:
                    row[field] = value if value else schema.MISSING
            if bad:
                dropped.append(f"{kind}: {name} (a stated figure is not in its quote)")
                continue
            rows[kind].append(row)
    if dropped:
        log.warning("om_comps: %d row(s) dropped for unverifiable quotes or figures", len(dropped))
    return rows, dropped


def om_comps(text: str, ledger: UsageLedger, client=None) -> dict:
    """The comparables the memorandum states, verified; ``{"rent": [...], "sale": [...]}``."""
    ledger.check_cap()
    if mode() == MODE_MOCK:
        raw = _mock_om_comps(text, ledger)
    else:
        raw = call_json(
            call="om_comps",
            model=EXTRACT_MODEL,
            system=_system("system_extract.md"),
            user=om_request_blocks(text, om_comps_task_text()),
            output_schema=om_comps_schema(),
            max_tokens=MAX_TOKENS_COMPS,
            ledger=ledger,
            effort="medium",
            client=client,
        )
    rows, dropped = verify_comp_rows(dict(raw or {}), text)
    rows["dropped"] = dropped
    return rows


def om_address_schema() -> dict:
    """Six plain strings, no unions — the shape the live-leg limits accept."""
    return {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ADDRESS_KEYS},
        "required": list(ADDRESS_KEYS),
        "additionalProperties": False,
    }


def om_address_task_text() -> str:
    return (
        "Read the property's STREET ADDRESS from this marketing package: the number and "
        "street on one line (address), the city, the two-letter state, the five-digit ZIP, "
        "and the county name without the word County. Also the PRODUCT TYPE as one of "
        "SFD (detached rental homes / build-for-rent), TH (townhomes) or MF (apartments). "
        "Copy each value exactly as the document writes it. For anything the document does "
        f"not state, answer the literal string {schema.MISSING}. Never infer a city from a "
        "market name or a county from a city."
    )


def _mock_om_address(text: str, ledger: UsageLedger) -> dict:
    _mock_entry("om_address", ADDRESS_MODEL, ledger)
    body = text or ""
    out = {key: schema.MISSING for key in ADDRESS_KEYS}
    m = re.search(
        r"(\d{1,6} [A-Z][A-Za-z0-9 .'-]*?(?:Parkway|Pkwy|Street|St|Road|Rd|Avenue|Ave|Drive|Dr|"
        r"Boulevard|Blvd|Way|Lane|Ln|Court|Ct|Trail|Trl|Circle|Cir|Highway|Hwy))\.?,\s*"
        r"([A-Z][A-Za-z .'-]+?),\s*([A-Z]{2})\s+(\d{5})\b",
        body,
    )
    if m:
        out.update(address=m.group(1), city=m.group(2), state=m.group(3), zip=m.group(4))
    county = re.search(r"\b([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?) County\b", body)
    if county:
        out["county"] = county.group(1)
    product = re.search(r"\b(build-for-rent|build to rent|single-family rental|townhome|townhomes|multifamily|apartment)\b", body, re.I)
    if product:
        word = product.group(1).lower()
        out["product_type"] = "TH" if word.startswith("townhome") else "MF" if word in ("multifamily", "apartment") else "SFD"
    return out


PRODUCT_EVIDENCE = {
    "SFD": ("build-for-rent", "build to rent", "single-family", "single family", "detached", "bfr", "sfr"),
    "TH": ("townhome", "townhouse", "town home"),
    "MF": ("multifamily", "multi-family", "apartment"),
}


def _verify_address(raw: dict, text: str) -> dict:
    """The address as ONE grounded statement, not five loose substrings.

    ``"OR"`` occurs inside "offering" and ``"Sample"`` inside many words; checked one by one,
    a mixed or invented answer would launch the market report on the wrong market. So the
    street and city must appear together, the state as a bounded token just after them,
    the ZIP just after the state, the county as "<name> County", and the product only
    with a supporting phrase in the document. ``formatted`` is one line for a geocoder.
    """
    body = " ".join((text or "").split())
    lowered = body.lower()
    out: dict = {key: schema.MISSING for key in ADDRESS_KEYS}

    def clean(key: str) -> str:
        value = raw.get(key)
        value = " ".join(str(value).split()) if value is not None else ""
        return "" if (not value or value == schema.MISSING) else value

    address, city, state, zip_code, county, product = (clean(k) for k in ADDRESS_KEYS)
    anchor = -1
    if address and city:
        for form in (f"{address}, {city}", f"{address} {city}", f"{address}. {city}"):
            anchor = lowered.find(form.lower())
            if anchor >= 0:
                out["address"], out["city"] = address, city
                after = body[anchor + len(form): anchor + len(form) + 60]
                break
    if anchor >= 0 and state and re.search(rf"(?<![A-Za-z]){re.escape(state)}(?![A-Za-z])", after):
        out["state"] = state.upper()
        tail = after[after.find(state) + len(state):]
        if zip_code and re.search(rf"(?<!\d){re.escape(zip_code)}(?!\d)", tail[:20]):
            out["zip"] = zip_code
    if county and re.search(rf"\b{re.escape(county)} County\b", body, re.IGNORECASE):
        out["county"] = county
    if product and product.upper() in PRODUCT_EVIDENCE:
        if any(phrase in lowered for phrase in PRODUCT_EVIDENCE[product.upper()]):
            out["product_type"] = product.upper()
    if all(out[k] != schema.MISSING for k in ("address", "city", "state")):
        tail = f" {out['zip']}" if out["zip"] != schema.MISSING else ""
        out["formatted"] = f"{out['address']}, {out['city']}, {out['state']}{tail}"
    else:
        out["formatted"] = schema.MISSING
    return out


def om_address(text: str, ledger: UsageLedger, client=None) -> dict:
    """The property's address and product off the front of the memorandum, or MISSING."""
    ledger.check_cap()
    region = (text or "")[:ADDRESS_REGION_CHARS]
    if mode() == MODE_MOCK:
        raw = _mock_om_address(region, ledger)
    else:
        raw = call_json(
            call="om_address",
            model=ADDRESS_MODEL,
            system=_system("system_extract.md"),
            user=f"{om_address_task_text()}\n\n{document_region(region)}",
            output_schema=om_address_schema(),
            max_tokens=MAX_TOKENS_ADDRESS,
            ledger=ledger,
            client=client,
        )
    return _verify_address(dict(raw or {}), region)


def om_summary(text: str, ledger: UsageLedger, client=None) -> dict:
    """The summary inventory plus the priced field, every value checked against ``text``.

    ``pricing`` is returned separately from ``fields`` because it is a different shape —
    it carries a basis — and because assembly has to be able to tell "no price was
    stated" from "a price was stated and we could not tell what of".
    """
    ledger.check_cap()
    if mode() == MODE_MOCK:
        raw = _mock_om_summary(text, ledger)
    else:
        raw = call_json(
            call="om_summary",
            model=EXTRACT_MODEL,
            system=_system("system_extract.md"),
            user=om_request_blocks(text, om_task_text()),
            output_schema=om_output_schema(),
            max_tokens=MAX_TOKENS_OM,
            ledger=ledger,
            effort="high",
            client=client,
        )
    raw = entries_to_fields(dict(raw or {}))
    price_entry = raw.pop(PRICING_KEY, None)
    fields, dropped = verify_fields(raw, text)
    fields = couple_tax_bill(fields)
    # AFTER the quote check, on purpose. What the model returns is the number the document
    # says — "100" beside "assessed at 100% of market value" — and that is what has to be
    # found in its own quote. The fraction is what the field CARRIES, and it is derived
    # here, deterministically, with a note saying so.
    ratio_dropped = _normalise_ratio_field(fields)
    # And then the types, for the same reason and in the same place: the quote check is
    # about the EVIDENCE and this is about what the field can carry. Last, so the ratio
    # normaliser's own output goes through it too.
    type_dropped = _normalise_field_types(fields)
    pricing, price_dropped = verify_price(price_entry, text)
    return {
        "fields": fields,
        "dropped": dropped + ratio_dropped + type_dropped + price_dropped,
        "pricing": pricing,
    }


#: Summary fields whose value is a five-digit code rather than a number. A model that
#: answers `85001` is not wrong about the ZIP; it is wrong about the type, and
#: ``str(85001)`` would lose the leading zero every north-eastern ZIP has.
ZIP_KEYS: frozenset = frozenset({"zip"})

TYPE_NORMALISED_NOTE = (
    "The document states this as a number; it is carried in the type this field holds."
)

TYPE_REFUSED_NOTE = (
    "Dropped: the extracted value is not the type this field carries. A value the "
    "assembler cannot use is reported missing rather than silently replaced with zero."
)

#: Returned by :func:`_coerce_summary_value` when nothing usable can be made of a value.
_REFUSE = object()


def _as_text(key: str, value) -> str:
    """A number written out for a field that holds text."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if key in ZIP_KEYS and isinstance(value, int) and 0 <= value <= 99999:
        return f"{value:05d}"
    return str(value)


def _coerce_summary_value(key: str, value, types: tuple):
    """``(value, changed)`` in the type the field takes, or ``(_REFUSE, True)``.

    The rule is the inventory's, not this function's: a field that accepts ``float``
    accepts any number as it stands, a field that accepts only ``int`` takes an integral
    float or an integral numeral and refuses anything else, and a field that accepts only
    ``str`` gets the number written out. Refusing is deliberate — a fractional unit count
    is not a unit count, and the alternative is the zero that made this a finding.
    """
    if isinstance(value, bool):
        return _REFUSE, True
    accepts_float = float in types
    accepts_int = int in types
    accepts_str = str in types

    if isinstance(value, (int, float)):
        if accepts_float or (accepts_int and isinstance(value, int)):
            return value, False
        if accepts_int and isinstance(value, float):
            return (int(value), True) if value.is_integer() else (_REFUSE, True)
        if accepts_str:
            return _as_text(key, value), True
        return _REFUSE, True

    # A string. A field that takes a number gets the number FIRST when the string is a
    # clean numeral — "334" is a count, and `schema.usable_count` (round 20) refuses a
    # numeral in quotes on the promise that this function converted it. The schema no
    # longer types fields individually (test_live_schema_limits), so that promise is now
    # the only thing standing between a quoted count and a MISSING per-unit price. A
    # string that is not a numeral keeps being a string wherever the inventory allows one.
    number = numeric_value(value) if (accepts_float or accepts_int) else None
    if number is not None:
        if accepts_float:
            return number, True
        if float(number).is_integer():
            return int(number), True
        if accepts_str:
            return value, False
        return _REFUSE, True
    if accepts_str:
        return value, False
    return _REFUSE, True


def _normalise_field_types(fields: dict) -> list[str]:
    """Put every verified value in the type ``schema.SUMMARY_KEYS`` states. Returns drops.

    The model is now asked for the right type per field, so in the ordinary case this
    changes nothing. It runs anyway because a structured-output schema is a request and
    not a guarantee, and because the failure it prevents is silent: ``build_summary``
    replaces a non-``int`` ``units_total`` with zero, which then divides every per-unit
    figure in the output set.
    """
    dropped: list[str] = []
    for key, entry in list((fields or {}).items()):
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value in (None, "", schema.MISSING):
            continue
        spec = schema.SUMMARY_KEYS.get(key)
        if spec is None:
            continue
        fixed, changed = _coerce_summary_value(key, value, spec[1])
        if fixed is _REFUSE:
            # The KEY and the types, never the value: it came out of the visitor's own
            # document.
            log.warning("extraction: %s is not a %s and was dropped", key,
                        "/".join(t.__name__ for t in spec[1]))
            fields[key] = {"value": schema.MISSING, "page": entry.get("page"),
                           "quote": None, "note": TYPE_REFUSED_NOTE}
            dropped.append(key)
        elif changed:
            entry["value"] = fixed
            entry["note"] = entry.get("note") or TYPE_NORMALISED_NOTE
    return dropped


def _normalise_ratio_field(fields: dict) -> list[str]:
    """Rewrite ``assessment_ratio`` in place as a fraction, or MISSING. Returns drops."""
    entry = fields.get(ASSESSMENT_RATIO_KEY)
    if not isinstance(entry, dict):
        return []
    value = entry.get("value")
    if value in (None, "", schema.MISSING):
        return []
    fraction = normalise_assessment_ratio(value, entry.get("quote") or "")
    if fraction is None:
        log.warning("extraction: the %s returned is not a share of value",
                    ASSESSMENT_RATIO_KEY)
        fields[ASSESSMENT_RATIO_KEY] = {
            "value": schema.MISSING, "page": entry.get("page"), "quote": None,
            "note": RATIO_REFUSED_NOTE,
        }
        return [ASSESSMENT_RATIO_KEY]
    entry["value"] = fraction
    if numeric_value(value) != fraction:
        entry["note"] = RATIO_NORMALISED_NOTE
    return []


MAP_T12_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["label", "category"],
                "additionalProperties": False,
            },
        },
        "subtotals": {
            "type": "object",
            "properties": {
                "total_revenue": {"type": ["string", "null"]},
                "total_expenses": {"type": ["string", "null"]},
                "net_operating_income": {"type": ["string", "null"]},
            },
            "required": ["total_revenue", "total_expenses", "net_operating_income"],
            "additionalProperties": False,
        },
    },
    "required": ["mappings", "subtotals"],
    "additionalProperties": False,
}

EXCLUDE = "exclude"


#: Where a line sits in its own statement, read from the statement's subtotal rows.
SECTION_INCOME = "income"
SECTION_OPEX = "opex"
SECTION_BELOW = "below"
SECTION_STRUCTURAL = "structural"
SECTION_UNKNOWN = "unknown"

_STRUCTURAL_TO_KEY = {
    "Total Revenue": "total_revenue",
    "Total Operating Expenses": "total_expenses",
    "Net Operating Income": "net_operating_income",
}


def _label_sections(labels: list[str]) -> dict[str, set[str]]:
    """Every section each label is printed in, from the statement's own TOTAL / NOI rows, in row order.

    A T-12 says which of its lines are income and which are expenses: everything above
    its total-income row is income, everything between that and its total-operating-
    expenses row is an expense, and everything below its net-operating-income row is not
    operating at all. Nothing about a label's WORDS decides this — "PEST CONTROL FEES"
    above the income total is income, "RECOVERABLE ELEC/GAS/WATER" between the totals is
    an expense — which is why the first live run, bucketing labels one at a time, came
    out $1.3M of NOI away from the statement it had just parsed.

    Where a bounding row is missing the section is ``unknown`` and the model decides on
    the label alone, as before. The structural rows themselves are ``structural``.

    Keyed by label, like the mapping — and a statement prints the same description in
    more than one section as a matter of course (one real statement: ``MISCELLANEOUS`` at 5550 among
    the operating expenses and again at 7400 below its NOI). The OPERATING occurrence is
    the one the mapping serves, because the reader drops the below-NOI row by position on
    its own; a label printed in both income and opex is ``unknown``, since one category
    cannot be right for both rows and the tie-out will say which it was.
    """
    names = [schema.structural_row(label) if label else None for label in labels]
    # The LAST occurrence of each named row bounds the detail. A statement that prints a
    # summary block above its account schedule repeats the three rows at the top; taking
    # the first NOI there put the whole schedule "below NOI". A summary printed
    # after the capital lines would mislead this the other way, and the tie-out catches it.
    revenue_at = max((i for i, n in enumerate(names) if n == "Total Revenue"), default=None)
    opex_at = max((i for i, n in enumerate(names) if n == "Total Operating Expenses"),
                  default=None)
    noi_at = max((i for i, n in enumerate(names) if n == "Net Operating Income"), default=None)
    seen: dict[str, set[str]] = {}
    for index, label in enumerate(labels):
        if not label:
            continue
        if names[index]:
            section = SECTION_STRUCTURAL
        elif noi_at is not None and index > noi_at:
            section = SECTION_BELOW
        elif opex_at is not None and index > opex_at:
            # Between the operating-expense total and the NOI row: on every export seen
            # this is page furniture, not a line. Read as an expense, and the tie-out
            # says so if it was not.
            section = SECTION_OPEX if noi_at is not None else SECTION_UNKNOWN
        elif revenue_at is not None and index > revenue_at:
            # Past the revenue total. Only an expense total or an NOI row closes the
            # expense section; without either, nothing says where operating expenses
            # stop and capital or debt begins, and the tie-out could not tell.
            section = (SECTION_OPEX if (opex_at is not None or noi_at is not None)
                       else SECTION_UNKNOWN)
        elif revenue_at is not None:
            section = SECTION_INCOME
        else:
            section = SECTION_UNKNOWN
        seen.setdefault(label, set()).add(section)
    return seen


def statement_sections(labels: list[str]) -> dict[str, str]:
    """Each label's section, one per label: see :func:`_label_sections`.

    A label printed in both income and opex is ``unknown`` — one category cannot be right
    for both rows; :func:`two_sided_labels` names those so a withheld reason can too.
    """
    out: dict[str, str] = {}
    for label, sections in _label_sections(labels).items():
        if SECTION_INCOME in sections and SECTION_OPEX in sections:
            out[label] = SECTION_UNKNOWN
        elif SECTION_INCOME in sections:
            out[label] = SECTION_INCOME
        elif SECTION_OPEX in sections:
            out[label] = SECTION_OPEX
        elif SECTION_UNKNOWN in sections:
            out[label] = SECTION_UNKNOWN
        elif SECTION_STRUCTURAL in sections:
            out[label] = SECTION_STRUCTURAL
        else:
            out[label] = SECTION_BELOW
    return out


def two_sided_labels(labels: list[str]) -> list[str]:
    """Labels the statement prints under income AND under operating expenses, row order.

    The map is keyed by label, so both rows take one category and one of them is on the
    wrong side (one real statement prints ``Water/Sewer`` as utility income and as a utility expense).
     Named so a read that then fails its tie says why.
    """
    return [label for label, sections in _label_sections(labels).items()
            if SECTION_INCOME in sections and SECTION_OPEX in sections]


def _wrong_side(section: str, category: str) -> bool:
    """A vendored category from the other side of the statement."""
    if category == EXCLUDE:
        return False
    if section == SECTION_INCOME:
        return category in schema.T12_EXPENSE_CATEGORIES
    if section == SECTION_OPEX:
        return category in schema.T12_REVENUE_CATEGORIES
    return False


def map_t12(labels: list[str], headers: list[str], ledger: UsageLedger, client=None) -> dict:
    """Category per account label: the model's answer, inside the statement's own sections.

    Since 2026-09-03 the model is the categoriser for EVERY operating line — there is no
    keyword layer in front of it (that layer now lives only in the mock). What the code
    still decides, because the statement decides it: the subtotal rows are structural and
    excluded; lines below the net-operating-income row are not operating and excluded;
    and a category from the wrong side of a line's section is refused. The model never
    gets to invent a category either — anything outside the vendored lists is rejected.
    """
    ledger.check_cap()
    sections = statement_sections(labels)
    resolved: list[dict] = []
    below_noi: list[str] = []
    asked: list[tuple[str, str]] = []
    subtotals = _blank_subtotals()
    # The LAST row of each named subtotal, in ROW order, like `statement_sections`. A
    # statement printing "Controllable Net Operating Income" above its bottom-line "Net
    # Operating Income" had the controllable row handed on as its NOI, and the tie gate
    # compared the read against $2.79M where the statement says $1.75M. Read off
    # the row sequence, not the de-duplicated labels below: a summary block repeating the
    # bottom-line label at the top would otherwise pin its first position.
    for label in labels:
        key = _STRUCTURAL_TO_KEY.get(schema.structural_row(label) or "") if label else None
        if key:
            subtotals[key] = label
    # First-seen order, one entry per DISTINCT label: a statement repeats a description
    # across GL rows as a matter of course and the map is keyed by label.
    for label in dict.fromkeys(labels):
        if not label:
            continue
        section = sections.get(label, SECTION_UNKNOWN)
        if section == SECTION_STRUCTURAL:
            resolved.append({"label": label, "category": EXCLUDE})
            continue
        if section == SECTION_BELOW:
            below_noi.append(label)
            resolved.append({"label": label, "category": EXCLUDE})
            continue
        if schema.statistic_row(label):
            # Occupancy, averages, unit counts: a measure printed among the accounts, never
            # money. Not the model's call, like the subtotal rows above.
            resolved.append({"label": label, "category": EXCLUDE})
            continue
        asked.append((label, section))

    if not asked:
        return {
            "mappings": resolved,
            "subtotals": subtotals,
            "asked": 0,
            "calls": 0,
            "deterministic": len(resolved),
            "below_noi": below_noi,
            "sections": sections,
            "two_sided": two_sided_labels(labels),
        }

    chunks = [asked[start : start + T12_LABELS_PER_CALL]
              for start in range(0, len(asked), T12_LABELS_PER_CALL)]
    mappings: list[dict] = list(resolved)
    for part in _map_t12_chunks(chunks, headers, ledger, client):
        mappings.extend(part.get("mappings", []))
        for key, value in (part.get("subtotals") or {}).items():
            if value and not subtotals.get(key):
                subtotals[key] = value

    proposed = [m["category"] for m in mappings if m.get("category") != EXCLUDE]
    drift = schema.assert_categories(proposed)
    if drift:
        raise ValueError(
            "the model proposed categories that are not in the vendored list: "
            + ", ".join(sorted(set(drift)))
        )
    return {
        "mappings": mappings,
        "subtotals": subtotals,
        "asked": len(asked),
        "calls": len(chunks),
        "deterministic": len(resolved),
        "below_noi": below_noi,
        "sections": sections,
        "two_sided": two_sided_labels(labels),
    }


_SECTION_PREFIX = re.compile(r"^\[(income|opex|unknown)\]\s*")

#: Chunks of one statement in flight at once. Four is a 160-line statement in one wave;
#: with two runs admitted at a time that is eight model calls, which the service's
#: rate limit has room for.
T12_PARALLEL_CHUNKS = 4


def _map_t12_chunks(
    chunks: list[list[tuple[str, str]]],
    headers: list[str],
    ledger: UsageLedger,
    client=None,
) -> list[dict]:
    """Every chunk's answer, in chunk order, the chunks having run at the same time.

    Since every operating line goes to the model (2026-09-03) a 204-line statement is
    four chunks, and one after another they took 57 seconds on one real statement — with the OM
    call at 39–52 s, a three-document run was landing at 101–114 s against the 120-second
    hard stop. Nothing in one chunk depends on another: each prices, sends, checks and
    retries its own request against the shared ledger, whose ``record`` appends and whose
    ``check_cap`` reads. Run concurrently, four chunks cost what the slowest one costs.
    The first chunk to fail fails the mapping; the others finish their call and are
    discarded, since a call already sent is already billed.
    """
    if len(chunks) <= 1:
        return [_map_t12_chunk(chunk, headers, number, len(chunks), ledger, client)
                for number, chunk in enumerate(chunks, start=1)]
    from concurrent.futures import ThreadPoolExecutor

    total = len(chunks)
    with ThreadPoolExecutor(max_workers=min(T12_PARALLEL_CHUNKS, total)) as pool:
        futures = [
            pool.submit(_map_t12_chunk, chunk, headers, number, total, ledger, client)
            for number, chunk in enumerate(chunks, start=1)
        ]
        return [future.result() for future in futures]


def _map_t12_chunk(
    chunk: list[tuple[str, str]],
    headers: list[str],
    number: int,
    total: int,
    ledger: UsageLedger,
    client=None,
) -> dict:
    """One chunk of (label, section) pairs, checked for the label set AND the side.

    The category names were validated and the LABELS were not, so a mapping that came back
    a line short — or with a label reworded — was accepted and ``parse_t12`` then excluded
    the account it could no longer find. That understates revenue or expenses by a whole
    line and looks exactly like a statement that simply did not have it. Compared as a
    multiset, so a duplicate is as loud as an omission, and retried once before the run is
    failed: a single short answer is worth another attempt, two is a wrong tool.

    The SIDE is checked the same way: an income line answered with an
    expense category — or the reverse — is not a category choice, it is a line moved
    across the statement's own subtotal, and the P&L can no longer tie.
    """
    section_of = dict(chunk)
    wanted = sorted(section_of)
    last: str = ""
    problem: list[str] = []
    for attempt in (1, 2):
        if attempt == 2 and ledger.out_of_time():
            raise DeadlineExceeded(
                "the T-12 mapping came back short and there is no time left to retry it"
            )
        ledger.check_cap()
        part = _map_t12_call(chunk, headers, number, total, ledger, client)
        # A model that echoes the section tag it was shown is answering the right label.
        for entry in part.get("mappings", []):
            if isinstance(entry.get("label"), str):
                entry["label"] = _SECTION_PREFIX.sub("", entry["label"])
        got = sorted(str(m.get("label", "")) for m in part.get("mappings", []))
        missing = sorted(set(wanted) - set(got))
        extra = sorted(set(got) - set(wanted))
        crossed = sorted(
            str(m.get("label"))
            for m in part.get("mappings", [])
            if m.get("label") in section_of
            and _wrong_side(section_of[m["label"]], str(m.get("category")))
        )
        if got == wanted and not crossed:
            return part
        problem = missing + extra + crossed
        # COUNTS, never the labels themselves. An account label is a line out of the
        # visitor's own statement — "VALET TRASH REIMBURSEMENTS", and on some rolls a
        # property or a person — and this string is written to a service log and into an
        # exception whose traceback lands in the same place. The counts
        # are what an operator acts on: which chunk, how short, how many invented.
        last = (
            f"asked for {len(wanted)} label(s), got {len(got)}"
            + (f"; {len(missing)} missing" if missing else "")
            + (f"; {len(extra)} not asked for" if extra else "")
            + (f"; {len(crossed)} on the wrong side of its section" if crossed else "")
        )
        log.warning("map_t12[%d/%d] attempt %d was refused: %s", number, total, attempt, last)
    raise MappingIncomplete(
        f"the T-12 mapping for chunk {number} of {total} did not come back usable "
        f"({last}); refusing rather than reading the statement through it",
        labels=problem,
    )


def _map_t12_call(
    chunk: list[tuple[str, str]],
    headers: list[str],
    number: int,
    total: int,
    ledger: UsageLedger,
    client=None,
) -> dict:
    if mode() == MODE_MOCK:
        return _mock_map_t12(chunk, ledger)
    tagged = "\n".join(f"[{section}] {label}" for label, section in chunk)
    task = (
        "For each account label below, give the category it belongs to, from the list "
        "for its SECTION, or the literal string \"exclude\" if the line is a subtotal, "
        "a section header, or carries no operating activity.\n\n"
        "Each label is prefixed with its section in square brackets, read from the "
        "statement's own subtotal rows. [income] lines sit above the statement's total "
        "income row and take a revenue category whatever their wording — a fee, a "
        "reimbursement or a line that says \"expense\" there is still income. [opex] "
        "lines sit between total income and total operating expenses and take an expense "
        "category, even when the label mentions recovery or reimbursement. [unknown] "
        "lines could not be placed from the statement's layout; choose from either "
        "list.\n\n"
        "Return one entry for EVERY label below, using the label exactly as written and "
        "WITHOUT the bracketed section, and no entry for any label that is not below.\n\n"
        f"Revenue categories: {', '.join(schema.T12_REVENUE_CATEGORIES)}\n"
        f"Expense categories: {', '.join(schema.T12_EXPENSE_CATEGORIES)}\n\n"
        "Then name the labels of the stated Total Revenue, Total Operating "
        "Expenses and Net Operating Income rows that appear below, or null where "
        "none of them do.\n\n"
        f"Month columns: {', '.join(str(h) for h in headers[:14])}\n\n"
        + document_region(tagged)
    )
    return call_json(
        call=f"map_t12[{number}/{total}]",
        model=EXTRACT_MODEL,
        system=_system("system_extract.md"),
        user=task,
        output_schema=MAP_T12_SCHEMA,
        max_tokens=MAX_TOKENS_MAPPING,
        ledger=ledger,
        # A judgement call — which of the vendored categories a label belongs to — so
        # medium rather than the low the mechanical mappings get.
        effort="medium",
        client=client,
    )


MAP_RENT_ROLL_SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "object",
            "properties": {
                role: {"type": ["integer", "null"]}
                for role in (
                    "unit", "plan", "beds", "baths", "sqft", "status", "lease_status",
                    "current_rent", "market_rent", "lease_start", "lease_end", "move_in",
                )
            },
            "required": [
                "unit", "plan", "beds", "baths", "sqft", "status", "lease_status",
                "current_rent", "market_rent", "lease_start", "lease_end", "move_in",
            ],
            "additionalProperties": False,
        },
        "status_vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw": {"type": "string"},
                    "meaning": {
                        "type": "string",
                        "enum": ["occupied", "vacant", "notice", "model", "down", "other"],
                    },
                },
                "required": ["raw", "meaning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["columns", "status_vocabulary"],
    "additionalProperties": False,
}


def map_rent_roll(
    header_row: list[str], data_rows: list[list], ledger: UsageLedger, client=None
) -> dict:
    """Column roles and a status vocabulary. Numbers are read by code from these columns.

    ``data_rows`` is the WHOLE grid below the header, not a sample: the vocabulary has to
    be built from all of it (a roll sorted occupied-first hides its vacancy code past row
    eight). What reaches the model is a profile — see :func:`column_profiles` — so the
    grid getting bigger does not mean more of it travels.
    """
    profiles = column_profiles(header_row, data_rows)
    last = ""
    unmapped: set = set()
    for attempt in (1, 2):
        if attempt == 2 and ledger.out_of_time():
            raise DeadlineExceeded(
                "the status vocabulary came back short and there is no time left to "
                "retry it"
            )
        ledger.check_cap()
        result = _map_rent_roll_call(header_row, profiles, ledger, client)
        outside = out_of_range_columns(result, header_row)
        if outside:
            last = ", ".join(outside[:8])
            log.warning("map_rent_roll attempt %d pointed outside a %d-column header: %s",
                        attempt, len(header_row), last)
            continue
        roles = role_problems(result)
        if roles:
            last = "; ".join(roles[:4])
            log.warning("map_rent_roll attempt %d assigned roles that cannot stand: %s",
                        attempt, last)
            continue
        blind = blind_status_column(result, profiles)
        if blind is not None:
            # An empty vocabulary for a selected status column is never accepted. Where
            # the column's values may travel, they are added to the profile and the call
            # is made again — which is the answer for the ordinary case, a status column
            # whose header this file's allowlist does not happen to name. Where they may
            # not, the run fails plainly rather than reading the roll through an empty
            # status map. an earlier review.
            name = header_row[blind] if 0 <= blind < len(header_row) else ""
            if expose_status_vocabulary(profiles, blind, header_row, data_rows):
                last = f"status column {blind} came back with no vocabulary"
                continue
            raise ApiError(
                422,
                "status_column_not_readable",
                f"The column mapping chose {str(name) or 'a column'} as the unit status, "
                "but that column's values cannot be read as a status vocabulary — they "
                "are free text, dates or numbers, or the column is empty. Rather than "
                "guess at what each value means, this run stopped. Check the roll names "
                "its unit status in its own column and try again.",
            )
        gap = _vocabulary_gap(result, profiles, data_rows)
        if not gap:
            return result
        # Counts, not the values: a status column's vocabulary is the visitor's, and on
        # a badly built roll it holds prose about a resident. The values
        # themselves ride on the exception, to the run's own error detail.
        unmapped = set(gap)
        last = f"{len(gap)} status value(s) the mapping did not name"
        log.warning("map_rent_roll attempt %d left %d status value(s) unmapped",
                    attempt, len(gap))
    raise MappingIncomplete(
        "the rent-roll column mapping did not come back usable "
        f"({last}); refusing rather than reading the roll through it",
        labels=sorted(unmapped),
    )


def out_of_range_columns(result: dict, header_row: list[str]) -> list[str]:
    """Roles the mapper pointed at a column the sheet does not have.

    A valid integer past the end of the header used to be accepted: the profile lookup
    found nothing, the vocabulary check read that as "no vocabulary to check", and the
    mapping went through. An out-of-range status column then had occupancy inferred from
    other fields; an out-of-range unit or rent column turned a roll the visitor plainly
    provided into an absent section.
    """
    width = len(header_row)
    bad = []
    for role, index in (result.get("columns") or {}).items():
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if index < 0 or index >= width:
            bad.append(f"{role}={index}")
    return sorted(bad)


#: Roles the rent-roll parser cannot work without. `parse_rent_roll` raises on these too,
#: but by then the mapping has been accepted and the run has no retry left.
REQUIRED_ROLL_ROLES: tuple[str, ...] = ("unit", "current_rent")

#: The only pairs of roles that MAY name the same column, as sorted tuples.
#:
#: ``status``/``lease_status``: a roll with a single status column legitimately answers
#: both. ``plan``/``unit_type``: a roll that writes its floor plan under "Unit Type" does
#: the same (coordinator's ruling on round 14) — note that ``unit_type`` is not a role in
#: `MAP_RENT_ROLL_SCHEMA`, which is `additionalProperties: False`, so a live model cannot
#: return it today; the pair is here so that adding the role cannot reintroduce the
#: refusal. Every other pair sharing a column is a mapping that would read one kind of
#: value as another — unit IDs as rents, most damagingly.
ROLES_THAT_MAY_SHARE: frozenset = frozenset({
    ("lease_status", "status"),
    ("plan", "unit_type"),
})


def role_problems(result: dict) -> list[str]:
    """What is wrong with the ROLES this mapping assigns, in words for a log.

    Two questions the range check and the vocabulary check between them never asked
:

    * is every role the parser needs actually there? A mapping with no rent column was
      accepted and the parser then refused the roll, after the retry was spent;
    * does any pair of roles name the SAME column? ``unit`` and ``current_rent`` both
      pointing at column 0 produces a workbook where unit 101 pays $101 — schema-valid,
      materially wrong, and silent.
    """
    columns = result.get("columns") or {}
    problems = []
    for role in REQUIRED_ROLL_ROLES:
        index = columns.get(role)
        if isinstance(index, bool) or not isinstance(index, int):
            problems.append(f"{role} is missing")
    seen: dict[int, str] = {}
    for role in sorted(columns):
        index = columns.get(role)
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        other = seen.get(index)
        if other is not None and tuple(sorted((other, role))) not in ROLES_THAT_MAY_SHARE:
            problems.append(f"{other} and {role} name one column")
        else:
            seen.setdefault(index, role)
    return problems


def _vocabulary_gap(result: dict, profiles: list[dict],
                    data_rows: "list[list] | None" = None) -> set[str]:
    """Status values the column holds ON UNIT ROWS and the returned vocabulary does not name.

    The T-12 mapping's returned LABELS were checked and this one's returned VALUES were
    not, so a vocabulary that quietly omitted ``V`` sent every vacant unit to
    ``_status_of``'s fallback and out the other side as Non-revenue — occupancy, vacancy
    and the occupied-unit rents all wrong, and nothing said so.

    "Holds" means on a row the reader will read. The profile is built from the whole grid
    so a vacancy code past row eight is seen — and so is the export's own summary block
    under the roll, which on the first live run (2026-09-03) put ``Current Rent`` and
    eight rent totals into the status column's "vocabulary". The reader drops every row
    without a unit identifier before it reads a status (round 18), so a value found only
    on such rows can never reach ``_status_of``; the model that named the six real codes
    and none of the nine footer cells was right, and this check refused it twice. With
    ``data_rows`` and a mapped ``unit`` column the required set is the column's values on
    rows whose unit cell is non-empty; without either, the whole profile is required, as
    before. A duplicate, and a value the profile never showed, are still surplus.
    """
    columns = result.get("columns") or {}
    index = columns.get("status")
    if not isinstance(index, int):
        return set()
    profile = next((p for p in profiles if p["index"] == index), None)
    if not profile:
        return set()
    # Only a profiled VOCABULARY can be checked for completeness. A status column with
    # more distinct values than the profile enumerates was never fully sent, so demanding
    # coverage of it would fail every large roll for a reason the model cannot fix.
    known = {str(v) for v in (profile.get("values") or [])}
    if not known:
        return set()
    required = known
    unit_col = columns.get("unit")
    if data_rows is not None and isinstance(unit_col, int) and not isinstance(unit_col, bool):
        unit_rows = [
            row for row in data_rows
            if unit_col < len(row) and row[unit_col] is not None
            and str(row[unit_col]).strip()
        ]
        # The profile shows a status value as the KEY `bounded_status_values` gives it —
        # its first 40 characters, digest-suffixed where two values share them — and the
        # key a value gets depends on the whole column (a collision elsewhere changes it).
        # So the unit-row values are keyed through the same assignment over the same
        # column, or a status longer than the bound would never intersect `known` and
        # could be left unnamed without refusal. Never more than
        # the profile enumerated, for the reason above.
        _all_values, all_distinct = column_values(index, data_rows)
        keys = bounded_status_values(all_distinct) or all_distinct
        key_of = dict(zip(all_distinct, keys))
        _values, distinct = column_values(index, unit_rows)
        required = known & {key_of.get(v, v) for v in distinct}
    named = [
        str(entry.get("raw"))
        for entry in (result.get("status_vocabulary") or [])
        if entry.get("raw") is not None
    ]
    # A SET comparison let a duplicate through — the same raw value twice, with two
    # meanings — and `assemble` then let the last one win, reversing the occupancy of every
    # unit carrying it. Compared as a multiset, the way the T-12 mapping already compares
    # its labels: a duplicate and an invented value are both differences.
    surplus = sorted(_multiset_surplus(named, known))
    return (required - set(named)) | set(surplus)


def _multiset_surplus(named: list[str], known: set) -> set[str]:
    """Values the answer names more times than the column holds them, or not at all."""
    counts: dict[str, int] = {}
    for value in named:
        counts[value] = counts.get(value, 0) + 1
    return {value for value, count in counts.items() if value not in known or count > 1}


def _map_rent_roll_call(
    header_row: list[str], profiles: list[dict], ledger: UsageLedger, client=None
) -> dict:
    if mode() == MODE_MOCK:
        return _mock_map_rent_roll(header_row, profiles, ledger)

    task = (
        "Give the zero-based column index for each role below, or null where the export "
        "has no such column. Then list every distinct value in the status column with "
        "what it means.\n\n"
        "Roles: unit, plan, beds, baths, sqft, status, lease_status, current_rent, "
        "market_rent, lease_start, lease_end, move_in\n\n"
        "status is the unit's physical state (occupied, vacant, down). lease_status is "
        "the LEASE's state (current, new, notice) where the export carries both — a roll "
        "lists a turning-over unit twice, and lease_status is what says which of the two "
        "rows is the lease actually in place.\n\n"
        "Below is one line per column: index, header, inferred type, how many distinct "
        "values it holds, and either its full value set (short vocabularies) or up to "
        "three samples. Columns of free text contribute no values — they hold resident "
        "information and are not needed to identify a column's role.\n\n"
        + document_region(render_profiles(profiles))
    )
    return call_json(
        call="map_rent_roll",
        model=EXTRACT_MODEL,
        system=_system("system_extract.md"),
        user=task,
        output_schema=MAP_RENT_ROLL_SCHEMA,
        max_tokens=MAX_TOKENS_MAPPING,
        ledger=ledger,
        effort="low",
        client=client,
    )


#: The only things the free-text box may set. Everything else the visitor writes is read
#: and dropped — the box is a place to say "the rent roll is the second file", not a
#: channel into the schema.
NOTES_KEYS: tuple[str, ...] = (
    "units_total",
    "as_of_date",
    "price_per_unit",
    "product_type",
    "address",
    "lookback_months",
    "new_tenant_tolerance_days",
)

PARSE_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        **{
            key: {"type": ["string", "number", "null"]}
            for key in NOTES_KEYS
        },
        "role_hints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["om", "t12", "rent_roll", "trade_out", "context"],
                    },
                },
                "required": ["filename", "role"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*NOTES_KEYS, "role_hints"],
    "additionalProperties": False,
}


def parse_notes(notes: str, filenames: list[str], ledger: UsageLedger, client=None) -> dict:
    """Read the free-text box for the handful of knobs the schema already has."""
    if not (notes or "").strip():
        return {"values": {}, "role_hints": [], "dropped": []}

    ledger.check_cap()
    if mode() == MODE_MOCK:
        raw = _mock_parse_notes(notes, filenames, ledger)
    else:
        task = (
            "The visitor typed the note below. Pull out only these values if it states "
            f"them: {', '.join(NOTES_KEYS)}. Also note any statement of which uploaded "
            "file holds which role.\n\n"
            f"Uploaded files: {', '.join(filenames)}\n\n" + notes_region(notes)
        )
        raw = call_json(
            call="parse_notes",
            model=EXTRACT_MODEL,
            system=_system("system_extract.md"),
            user=task,
            output_schema=PARSE_NOTES_SCHEMA,
            max_tokens=MAX_TOKENS_MAPPING,
            ledger=ledger,
            effort="low",
            client=client,
        )

    # Belt and braces over the strict schema: whatever comes back, only the listed keys
    # survive, and a role hint naming a file nobody uploaded is discarded.
    values = {k: raw.get(k) for k in NOTES_KEYS if raw.get(k) not in (None, "")}
    dropped = sorted(set(raw) - set(NOTES_KEYS) - {"role_hints"})
    hints = [
        h for h in (raw.get("role_hints") or [])
        if isinstance(h, dict) and h.get("filename") in set(filenames)
    ]
    if dropped:
        # Counts only: these keys are model-authored, out of a box the visitor typed in.
        log.warning("notes: dropped %d key(s) the schema does not accept", len(dropped))
    return {"values": values, "role_hints": hints, "dropped": dropped}


# --------------------------------------------------------------------------------------
# Mock mode
# --------------------------------------------------------------------------------------


def _known() -> tuple[str, ...]:
    return schema.ASSIGNABLE_CATEGORIES


def _blank_subtotals() -> dict:
    return {"total_revenue": None, "total_expenses": None, "net_operating_income": None}


def _mock_entry(call: str, model: str, ledger: UsageLedger) -> None:
    ledger.record(UsageEntry(call=call, model=model, mode=MODE_MOCK))


def _mock_classify(excerpt: str, ledger: UsageLedger) -> dict:
    _mock_entry("classify", CLASSIFY_MODEL, ledger)
    lowered = (excerpt or "").lower()
    if "offering memorandum" in lowered or "executive summary" in lowered:
        return {"role": "om", "reason": "mock: marketing language in the excerpt"}
    if "prior rent" in lowered:
        return {"role": "trade_out", "reason": "mock: a prior-rent column"}
    if "unit" in lowered and "rent" in lowered:
        return {"role": "rent_roll", "reason": "mock: unit and rent columns"}
    return {"role": "context", "reason": "mock: nothing recognisable in the excerpt"}


#: Patterns the mock extractor uses. Each one captures a value AND the span it came from,
#: so the quote it returns is genuinely copied out of the text and the verification step
#: has something real to check.
_MOCK_OM_PATTERNS: tuple[tuple[str, str], ...] = (
    # The cover line and one "Address:" line, as a package prints them. The live model
    # reads these from anywhere in the document; the mock reads only these two shapes.
    ("deal_name", r"(?m)^([A-Z][A-Za-z' ]{2,40}?) - Offering Memorandum"),
    ("street_address", r"\bAddress:\s*(\d{1,6} [A-Za-z0-9 .]{2,40}?),"),
    ("city", r"\bAddress:\s*[^,\n]+,\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?),"),
    ("zip", r"\bAddress:[^\n]*\b[A-Z]{2}\s+(\d{5})\b"),
    ("units_total", r"(\d{1,4})[\s\-]?unit\b"),
    ("year_built", r"[Yy]ear built[:\s]+(\d{4})"),
    ("product_type", r"\b(build-for-rent|build to rent|single-family rental|multifamily)\b"),
    # Captured as the document writes it — the percentage — because that is what has to be
    # found in the quote. `normalise_assessment_ratio` turns it into the fraction.
    ("assessment_ratio", r"assessed at\s+([\d.]+)\s*(?:%|percent)"),
    # "Harris County, TX 77002" — the county as the package names it (without the word
    # County, which is not part of the name) and the two-letter state beside it. The live
    # model is asked for both; without a mock reading them the vendored tax table's
    # consumer could never be exercised without a key.
    ("county", r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?) County\b"),
    ("state", r"\b(?:County|[A-Z][a-z]+),\s*(AZ|TX|GA|FL|AL|CA|NV|NC|TN|ID)\b(?=\s*\d{5}|\b)"),
    # The memorandum's own tax page, as a package prints it. The bill needs the word
    # billed/paid/due beside "tax" so a pro forma tax line is not read as the bill.
    # The basis is the FULL value the assessor states BEFORE the ratio — market, appraised,
    # limited, just, true, full-cash value. A printed "assessed value" is usually the
    # post-ratio figure, and reading it as the basis applies Georgia's 40% twice.
    ("assessed_value_basis",
     r"\b(?i:(?:fair market|appraised(?: market)?|market|full cash|limited property|"
     r"just|true) value|LPV|FCV)[^$\n]{0,40}\$\s?([\d,]+(?:\.\d+)?)"),
    # The bill needs PROPERTY-tax context beside billed/paid/due: "transfer tax due" and
    # "payroll taxes paid" are taxes too, and neither is the property's annual bill.
    # Identifiers may carry / . _ - ("12345/678", "123.45"); the sentence's own full stop is
    # not part of them, so a trailing dot is not captured.
    ("tax_parcel_id", r"\b(?:APN|[Pp]arcel(?: (?:ID|No\.?|[Nn]umber))?)[:#\s]+([A-Z0-9](?:[A-Z0-9/_\-]|\.(?=[A-Z0-9])){3,})"),
    ("tax_area_code", r"\b[Tt]ax (?:area|district)(?: (?:code|no\.?|number))?[:#\s]+([A-Z0-9](?:[A-Z0-9/_\-]|\.(?=[A-Z0-9])){1,})"),
)


#: How a marketing package states a price, and what each phrasing licenses.
#:
#: Order matters twice over. "$185,000 per unit" also matches the bare-number pattern, so
#: the per-unit phrasing is tried first; and a bare figure is LAST, where it produces an
#: ``unstated`` basis and populates nothing — which is the honest mock answer, because a
#: document that writes "$400,000" and nothing else has not said what it is a price of.
#:
#: The mock exists to make the live contract reachable without a key. A knob the live
#: model can produce and the mock cannot is a knob whose consumer is never exercised —
#: which is exactly how the pricing basis went unnoticed until a review found it.
#: A dollar figure is a PRICE only when something beside it says so.
#:
#: An offering memorandum is full of large numbers — capital invested, renovation budgets,
#: reserves, loan balances, total operating expenses — and a pattern that took the biggest
#: one, or the first one, manufactures an asking price for a document that states none.
#: One real memorandum is the proof: it names no price at all, and an earlier
#: version of this table read "current ownership has invested more than $4.7 million" as
#: its asking price. The live model is asked for the asking price; requiring a price word
#: within a few dozen characters is the nearest a regex gets to asking the same question.
_PRICE_CONTEXT = (
    r"\b(?:asking|guidance|pricing|price|priced|offering|valuation|purchase|whisper)\b"
    r"[^$\n]{0,40}"
)

_MOCK_PRICE_PATTERNS: tuple[tuple[str, str, float], ...] = (
    # Exactly the phrasings PER_UNIT_MARKERS recognises, so the mock and the rule agree by
    # construction: a form the mock reads as per-unit is one `decide_basis` will confirm.
    ("per_unit",
     _PRICE_CONTEXT
     + r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:per\s+(?:unit|home|door)|/unit|a\s+unit)\b",
     1.0),
    # A written magnitude is the whole asset every time. Nobody quotes one home in
    # millions.
    ("total", _PRICE_CONTEXT + r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:million|mm)\b",
     1_000_000.0),
    # A price with nothing saying what it is a price OF. `decide_basis` settles it from the
    # magnitude and the quote.
    ("unstated", _PRICE_CONTEXT + r"\$\s?([\d,]+(?:\.\d+)?)", 1.0),
)


def _mock_quote(body: str, match) -> tuple[str, int | None]:
    """The window around a match, and the page marker above it."""
    start = max(0, match.start() - 40)
    end = min(len(body), match.end() + 40)
    quote = body[start:end].strip()
    page = None
    marker = body.rfind("[[page ", 0, match.start())
    if marker != -1:
        digits = re.match(r"\[\[page (\d+)\]\]", body[marker:])
        page = int(digits.group(1)) if digits else None
    return quote[:MAX_QUOTE_CHARS], page


def _mock_asking_price(body: str) -> dict:
    """The priced field, with the basis the phrasing implies.

    A candidate below the sale-price band is skipped rather than returned: the real
    West memorandum states no asking price at all, and its first "$6,500 per unit" is a
    CAPITAL BUDGET line. Returning that would put a capex sentence in the pricing-guidance
    prose under the heading "what this deal costs". `decide_basis` would refuse the number
    either way; skipping it here is what keeps the prose honest too.
    """
    for basis, pattern, scale in _MOCK_PRICE_PATTERNS:
        for match in re.finditer(pattern, body, re.I):
            try:
                number = float(match.group(1).replace(",", "")) * scale
            except ValueError:  # pragma: no cover - the patterns only capture digits
                continue
            if number < PRICE_BAND_MIN:
                continue
            quote, page = _mock_quote(body, match)
            return {
                "value": int(number) if number == int(number) else number,
                "basis": basis,
                "page": page,
                "quote": quote,
            }
    return {"value": None, "basis": "unstated", "page": None, "quote": None}


#: The bill and its year from ONE sentence — "2025 property taxes paid: $100,000" — so the
#: mock cannot pair an undated bill with a year from another line. An undated bill
#: alone is read with no year, and the assembly then withholds the tie-out.
_MOCK_TAX_BILL_DATED = (
    r"\b(20\d\d)\s+(?:[Pp]roperty|[Rr]eal [Ee]state|[Aa]d [Vv]alorem)\s+tax(?:es)?\s+"
    r"(?:bill(?:ed)?|paid|due)\b[^$\n]{0,30}\$\s?([\d,]+(?:\.\d+)?)")
_MOCK_TAX_BILL_UNDATED = (
    r"\b(?:[Pp]roperty|[Rr]eal [Ee]state|[Aa]d [Vv]alorem)\s+tax(?:es)?\s+(?:bill(?:ed)?|paid|due)\b"
    r"[^$\n]{0,30}\$\s?([\d,]+(?:\.\d+)?)")


def _mock_tax_bill(body: str, fields: dict) -> None:
    match = re.search(_MOCK_TAX_BILL_DATED, body)
    if match:
        quote, page = _mock_quote(body, match)
        fields["tax_billed_year"] = {"value": int(match.group(1)), "page": page, "quote": quote}
        fields["tax_billed_amount"] = {"value": match.group(2), "page": page, "quote": quote}
        return
    match = re.search(_MOCK_TAX_BILL_UNDATED, body)
    if match:
        quote, page = _mock_quote(body, match)
        fields["tax_billed_amount"] = {"value": match.group(1), "page": page, "quote": quote}


def _mock_om_summary(text: str, ledger: UsageLedger) -> dict:
    """Regex what is findable, MISSING for the rest, with quotes cut from the real text."""
    _mock_entry("om_summary", EXTRACT_MODEL, ledger)
    fields = {key: {"value": schema.MISSING, "page": None, "quote": None}
              for key in om_field_keys()}
    body = text or ""
    for key, pattern in _MOCK_OM_PATTERNS:
        match = re.search(pattern, body)
        if not match:
            continue
        if key == "assessed_value_basis":
            # Arizona prints a full cash value AND the limited property value it taxes; the
            # model is told to take the LPV, and so is the mock.
            match = next((mm for mm in re.finditer(pattern, body)
                          if re.match(r"(?:limited property|lpv)", mm.group(0), re.I)), match)
        quote, page = _mock_quote(body, match)
        value = match.group(1)
        # A digit string becomes a number only where the field TAKES a number. A parcel
        # id or tax-area code of "0311" is text, and int() would strip the zero.
        takes_number = json_types_for(schema.SUMMARY_KEYS[key][1]) != "string"
        fields[key] = {
            "value": int(value) if (takes_number and value.isdigit()) else value,
            "page": page,
            "quote": quote,
        }
    _mock_tax_bill(body, fields)
    price = _mock_asking_price(body)
    fields[PRICING_KEY] = price
    # The prose field says what the document says about price, in the document's own
    # words. The number is in `asking_price`; this is the sentence a reader would quote.
    if price.get("quote"):
        fields["pricing_guidance"] = {
            "value": price["quote"], "page": price.get("page"), "quote": price["quote"],
        }
    return fields


def _mock_map_t12(chunk: list[tuple[str, str]], ledger: UsageLedger) -> dict:
    """The keyword layer, standing in for the model, inside the statement's sections.

    Production sends every operating line to the model (2026-09-03); the mock has no
    model, so the keyword rules that used to run in front of it answer here instead —
    and, like the model, they are held to the section: a rule's answer from the wrong
    side is replaced by the section's generic category, and a label no rule knows gets
    that generic category too, so a mock run ties to its statement the way a live run
    must. Canned, and labelled as such by the mode.
    """
    _mock_entry("map_t12", EXTRACT_MODEL, ledger)
    generic = {SECTION_INCOME: "Other Income", SECTION_OPEX: "General and Administrative"}
    mappings = []
    for label, section in chunk:
        guess = schema.normalize_category(label)
        if guess not in _known():
            guess = schema.classify_account_label(label)
        if section in generic:
            if guess is None or _wrong_side(section, guess):
                guess = generic[section]
        elif guess is None:
            guess = EXCLUDE
        mappings.append({"label": label, "category": guess})
    return {"mappings": mappings, "subtotals": _blank_subtotals()}


_ROLE_HEADER_HINTS: dict[str, tuple[str, ...]] = {
    "unit": ("unit id", "unit no", "unit number", "unit", "apt"),
    "plan": ("floorplan", "floor plan", "plan", "unit type"),
    # No two-letter hints. "ba" matched "Balance" and put the balance column in the
    # bathrooms role — a loose hint does not fail loudly, it fills the slot with the wrong
    # column and everything downstream believes it.
    "beds": ("bedrooms", "beds", "bed"),
    "baths": ("bathrooms", "baths", "bath"),
    "sqft": ("sqft", "sq ft", "square feet", "area"),
    # "Unit Status" (physical) and "Status" (lease) are different columns in the same
    # export. Missing the second one deduped a turning-over unit by first-seen instead of
    # by which lease is in place, and moved the weighted rents — found by diffing the
    # upload path against the showcase set, not by reading the code.
    "status": ("unit status", "occupancy", "status"),
    "lease_status": ("lease status", "status"),
    "market_rent": ("market rent", "asking rent", "lro market", "market"),
    "current_rent": ("lease rent", "current rent", "actual rent", "rent"),
    "lease_start": ("lease start", "lease begin", "lease from"),
    "lease_end": ("expiration", "lease end", "expires"),
    # "Occupied" is what one common export calls the move-in date. Without it the
    # turnover figures are computed from nothing.
    "move_in": ("move in", "move-in", "movein", "occupied"),
}


def _may_share(role: str, owners: "set[str] | None") -> bool:
    """May ``role`` name a column these roles already hold?

    Only where EVERY holder forms a pair `ROLES_THAT_MAY_SHARE` names — the same rule
    `role_problems` applies to a live model's answer, asked of the mock's own assignment
    so the two cannot disagree about what a legal mapping is.
    """
    if not owners:
        return False
    return all(
        tuple(sorted((owner, role))) in ROLES_THAT_MAY_SHARE for owner in owners
    )


def _mock_map_rent_roll(header: list[str], profiles: list[dict], ledger: UsageLedger) -> dict:
    _mock_entry("map_rent_roll", EXTRACT_MODEL, ledger)
    lowered = [str(h or "").strip().lower() for h in header]
    columns: dict[str, int | None] = {}
    owners: dict[int, set[str]] = {}

    def _search(role: str, hints: tuple, shared: bool) -> "int | None":
        for hint in hints:
            for index, name in enumerate(lowered):
                if not name:
                    continue
                if index in owners and not (shared and _may_share(role, owners[index])):
                    continue
                if name == hint or hint in name:
                    return index
        return None

    for role, hints in _ROLE_HEADER_HINTS.items():
        # A free column first, ALWAYS. A roll carrying both "Unit Status" and "Status"
        # has one column for each role and they are not the same fact; only a roll with
        # nothing left for the second role falls through to sharing.
        found = _search(role, hints, shared=False)
        if found is None:
            # A roll with a single "Lease Status" column legitimately answers both status
            # roles, and `role_problems` says so — but `taken` refused the second one
            # regardless, so `lease_status` came back null and the duplicate rows of a
            # turning-over unit lost the only signal saying which lease was in place
            #. The pair rule is the mapper's; the mock now obeys it too.
            found = _search(role, hints, shared=True)
        if found is not None:
            owners.setdefault(found, set()).add(role)
        columns[role] = found

    vocabulary = []
    status_index = columns.get("status")
    if status_index is not None:
        # Every distinct value the profile found, across the WHOLE grid. Reading the
        # first eight rows instead is what let a "V" that first appears at row 17 fall
        # through to Non-revenue on a roll sorted occupied-first.
        by_index = {p["index"]: p for p in profiles}
        profile = by_index.get(status_index) or {}
        seen = list(profile.get("values") or profile.get("samples") or [])
        # Single-letter codes are as common as words in a real roll — O/V/N/D/M — and a
        # heuristic that only understood whole words sent every one of them to "other",
        # which the parser reads as Non-revenue. Exact codes are matched first so "O" does
        # not fall through on its way to a substring test.
        exact = {
            "o": "occupied", "occ": "occupied", "current": "occupied", "c": "occupied",
            "v": "vacant", "vac": "vacant",
            "n": "notice", "ntv": "notice", "notice": "notice",
            "d": "down", "down": "down",
            "m": "model", "model": "model",
        }
        for value in seen:
            low = value.strip().lower()
            meaning = exact.get(low)
            if meaning is None:
                meaning = (
                    "occupied" if "occup" in low
                    else "vacant" if "vacant" in low
                    else "notice" if "notice" in low
                    else "model" if "model" in low
                    else "down" if "down" in low or "unrentable" in low
                    else "other"
                )
            vocabulary.append({"raw": value, "meaning": meaning})
    return {"columns": columns, "status_vocabulary": vocabulary}


_MOCK_NOTE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("units_total", r"\b(\d{1,4})\s*(?:homes|units|doors)\b"),
    ("price_per_unit", r"\$\s?([\d,]{3,12})\s*(?:per|/)\s*(?:unit|home|door)"),
    ("as_of_date", r"\b(\d{4}-\d{2}-\d{2})\b"),
    ("lookback_months", r"\b(\d{1,2})[\s-]*month(?:s)?[\s-]*lookback\b"),
    # The tolerance knob had no mock pattern at all, so the mock could never produce the
    # value the live model can — and the assembly path that consumes it went untested.
    ("new_tenant_tolerance_days", r"\b(\d{1,3})[\s-]*day(?:s)?[\s-]*tolerance\b"),
)


def _mock_parse_notes(notes: str, filenames: list[str], ledger: UsageLedger) -> dict:
    _mock_entry("parse_notes", EXTRACT_MODEL, ledger)
    out: dict[str, Any] = {key: None for key in NOTES_KEYS}
    out["role_hints"] = []
    for key, pattern in _MOCK_NOTE_PATTERNS:
        match = re.search(pattern, notes or "", re.I)
        if match:
            raw = match.group(1).replace(",", "")
            out[key] = int(raw) if raw.isdigit() else raw
    for name in filenames:
        window = re.search(
            re.escape(name) + r"[^.]{0,60}?\b(rent roll|t-?12|offering memorandum)\b",
            notes or "",
            re.I,
        )
        if window:
            hint = window.group(1).lower()
            role = "rent_roll" if "rent" in hint else "t12" if "12" in hint else "om"
            out["role_hints"].append({"filename": name, "role": role})
    return out

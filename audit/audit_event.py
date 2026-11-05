"""Federation audit events: one hash-chained JSON line per event on stdout.

Canonical copy: ``nos-tromo/.github`` ``configs/audit/audit_event.py``. It is
vendored verbatim into every repo that emits or verifies audit events and
drift-checked in CI (``scripts/validate_audit_lib.py``), so never edit a
vendored copy. The field contract is ``configs/audit/audit-event.schema.json``.

Each emitter instance (one per process) writes one chain. Every event carries
its sequence number and the hash of the event before it, so a removed,
inserted, reordered or altered line breaks the chain. A heartbeat keeps a live
chain visibly moving and a shutdown event marks a clean end, so a chain that
stops without one points at lost or deleted lines. :func:`verify_chain` checks
all of this and is what the audit store's verifier runs. The written line
escapes the characters some readers take for a line break (NEL and the other
C1 controls, U+2028, U+2029), so one event is one line to every reader; the
parsed event and its hash are the same either way.

Hashes make alteration detectable, not authorship: whatever can write audit
lines can also write a whole, internally consistent fake chain. Two controls
outside this module close that gap. The log collector binds every line to the
container it came from, and the verifier rejects events whose ``service`` does
not match that container. Daily anchors, signed and kept off the host, fix
what existed when they were taken.

The module never decides *what* is worth recording. Callers pass references
(ids, counts), bounded search criteria and the caller's stated purpose, never
document text, prompts or answers. Strings are length-capped as a backstop,
not as a licence to pass content.

Stdlib only; Python 3.11+.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TextIO

SCHEMA_VERSION: Final = 1
#: Every audit line carries ``"stream": "audit"``; the log collector routes on it.
STREAM_MARKER: Final = "audit"
#: ``prev_hash`` of the first event of every chain.
GENESIS_HASH: Final = "0" * 64

#: The processing categories a data-protection log distinguishes (collection,
#: alteration, query, disclosure including transfer, combination, erasure), plus
#: authentication, administration, model calls and the chain's own lifecycle.
ACTIONS: Final = frozenset(
    {"collect", "modify", "query", "disclose", "combine", "delete", "auth", "admin", "ai_call", "lifecycle"}
)
OUTCOMES: Final = frozenset({"ok", "denied", "error"})
VIA: Final = frozenset({"edge", "service", "cli", "system"})

#: Backstop caps. A value over a cap is cut and marked, never dropped silently.
MAX_TEXT: Final = 256
MAX_IDS: Final = 200
MAX_CRITERIA_KEYS: Final = 32
TRUNCATION_MARK: Final = "…[truncated]"

_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_STRING_KEYS: Final = (
    "stream",
    "event_id",
    "ts",
    "service",
    "service_version",
    "instance",
    "prev_hash",
    "hash",
    "action",
    "operation",
    "outcome",
)
_REQUIRED_KEYS: Final = (
    "stream",
    "v",
    "event_id",
    "ts",
    "service",
    "service_version",
    "instance",
    "seq",
    "prev_hash",
    "action",
    "operation",
    "outcome",
    "actor",
    "hash",
)


class AuditWriteError(RuntimeError):
    """Raised when an audit event could not be written to the output stream."""


def canonical_json(value: object) -> str:
    """Serialize a value the one way every emitter and verifier hashes it.

    Args:
        value: A JSON-compatible value.

    Returns:
        Compact JSON with sorted keys and unescaped non-ASCII characters.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(event: Mapping[str, object]) -> str:
    """Hash an event over every field except its own ``hash``.

    Args:
        event: The event, with or without a ``hash`` field.

    Returns:
        The lowercase hex SHA-256 of the event's canonical JSON.
    """
    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


#: Characters JSON lets stand unescaped in a string but that some readers take
#: for a line break: NEL and the other C1 controls, and the Unicode line and
#: paragraph separators (Python's ``str.splitlines`` splits on all three kinds).
_LINE_BREAKERS: Final = re.compile("[\u0080-\u009f\u2028\u2029]")


def event_line(event: Mapping[str, object]) -> str:
    """Write an event as the one line every reader splits the same way.

    The canonical JSON with every character of :data:`_LINE_BREAKERS` written
    as its six-character JSON escape. Such a character can only stand inside
    a JSON string, so the escape decodes to the same value: the parsed event,
    and the hash :func:`verify_chain` checks over it, are those of
    :func:`canonical_json`.

    Args:
        event: The event, with its ``hash``.

    Returns:
        The line, without its newline.
    """
    return _LINE_BREAKERS.sub(lambda char: f"\\u{ord(char.group()):04x}", canonical_json(event))


def new_event_id() -> str:
    """Return a new time-ordered event id (UUID version 7 layout).

    Returns:
        A UUID string whose first 48 bits are the Unix time in milliseconds,
        so ids sort by creation time.
    """
    millis = time.time_ns() // 1_000_000
    rand = secrets.randbits(74)
    value = (millis & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= ((rand >> 62) & 0xFFF) << 64  # 12 random bits
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand & 0x3FFF_FFFF_FFFF_FFFF  # 62 random bits
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def utc_timestamp() -> str:
    """Return the current time as an ISO 8601 UTC timestamp with milliseconds.

    Returns:
        A timestamp such as ``2026-01-31T12:00:00.000Z``.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _cap_text(value: str) -> str:
    """Cut a string to :data:`MAX_TEXT` characters, marking the cut.

    Args:
        value: The string to bound.

    Returns:
        The string, or its first characters plus :data:`TRUNCATION_MARK`.
    """
    if len(value) <= MAX_TEXT:
        return value
    return value[: MAX_TEXT - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def _bounded(value: object) -> object:
    """Bound a criteria value: cap strings, flatten unknown types to strings.

    Args:
        value: A value from the caller's criteria mapping.

    Returns:
        A JSON-compatible, size-bounded copy of the value.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _cap_text(value)
    if isinstance(value, Mapping):
        items = list(value.items())[:MAX_CRITERIA_KEYS]
        return {_cap_text(str(key)): _bounded(item) for key, item in items}
    if isinstance(value, list) or isinstance(value, tuple) or isinstance(value, set) or isinstance(value, frozenset):
        return [_bounded(item) for item in list(value)[:MAX_IDS]]
    return _cap_text(str(value))


@dataclass(frozen=True)
class Actor:
    """Who caused the event.

    Attributes:
        user: The authenticated account, as the edge gateway asserted it, a
            service principal such as ``service:docint``, or ``None`` for the
            system itself.
        via: How the identity was established: ``edge`` (gateway-verified
            header), ``service`` (service token), ``cli`` (operator on the
            host) or ``system`` (no caller, e.g. a scheduled sweep).
        on_behalf_of: The owner an admin acted for, when that differs from
            ``user`` (e.g. a cross-owner read).
    """

    user: str | None
    via: str
    on_behalf_of: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form, omitting unset optional fields.

        Returns:
            The actor as a mapping.

        Raises:
            ValueError: If ``via`` is not one of :data:`VIA`.
        """
        if self.via not in VIA:
            raise ValueError(f"unknown actor via {self.via!r}")
        out: dict[str, object] = {"user": None if self.user is None else _cap_text(self.user), "via": self.via}
        if self.on_behalf_of is not None:
            out["on_behalf_of"] = _cap_text(self.on_behalf_of)
        return out


@dataclass(frozen=True)
class Purpose:
    """The caller's stated reason for a query or disclosure.

    Attributes:
        case_ref: A case or file reference (Aktenzeichen). Prefer references
            over names: the purpose is personal data in its own right.
        reason: A short free-text justification.
    """

    case_ref: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form, omitting unset fields.

        Returns:
            The purpose as a mapping.
        """
        out: dict[str, object] = {}
        if self.case_ref is not None:
            out["case_ref"] = _cap_text(self.case_ref)
        if self.reason is not None:
            out["reason"] = _cap_text(self.reason)
        return out


@dataclass(frozen=True)
class Target:
    """What the event touched, by reference only.

    Attributes:
        kind: The kind of object, e.g. ``collection``, ``document``,
            ``entity``, ``author``, ``job``, ``chat``.
        ids: Identifiers of the objects touched (capped at :data:`MAX_IDS`).
        count: How many objects were touched, also when ``ids`` is capped or
            empty.
        container: The enclosing object, e.g. the collection of a document.
    """

    kind: str
    ids: Sequence[str] = ()
    count: int | None = None
    container: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form, omitting unset optional fields.

        Returns:
            The target as a mapping; ``ids_truncated`` is set when ``ids``
            was capped.
        """
        out: dict[str, object] = {"kind": _cap_text(self.kind)}
        if self.ids:
            out["ids"] = [_cap_text(str(item)) for item in list(self.ids)[:MAX_IDS]]
            if len(self.ids) > MAX_IDS:
                out["ids_truncated"] = True
        if self.count is not None:
            out["count"] = self.count
        if self.container is not None:
            out["container"] = _cap_text(self.container)
        return out


@dataclass(frozen=True)
class AiCall:
    """Model provenance of an AI-produced result.

    Attributes:
        model_alias: The model name the caller requested.
        model_id: The model that actually served the request, if known.
        revision: The model revision (commit) served, if known.
        prompt_version: An identifier of the prompt template used.
        sources: References to the evidence the model was given.
        output_sha256: SHA-256 of the produced output, so a later copy can be
            matched without storing the output here.
        input_tokens: Prompt tokens, if reported.
        output_tokens: Completion tokens, if reported.
    """

    model_alias: str
    model_id: str | None = None
    revision: str | None = None
    prompt_version: str | None = None
    sources: Sequence[str] = ()
    output_sha256: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form, omitting unset optional fields.

        Returns:
            The model call as a mapping.
        """
        out: dict[str, object] = {"model_alias": _cap_text(self.model_alias)}
        for key, value in (
            ("model_id", self.model_id),
            ("revision", self.revision),
            ("prompt_version", self.prompt_version),
            ("output_sha256", self.output_sha256),
        ):
            if value is not None:
                out[key] = _cap_text(value)
        if self.sources:
            out["sources"] = [_cap_text(str(item)) for item in list(self.sources)[:MAX_IDS]]
        for key, number in (("input_tokens", self.input_tokens), ("output_tokens", self.output_tokens)):
            if number is not None:
                out[key] = number
        return out


def sha256_text(text: str) -> str:
    """Hash a text the way :attr:`AiCall.output_sha256` expects.

    Args:
        text: The text to hash, typically a model's output.

    Returns:
        The lowercase hex SHA-256 of the UTF-8 encoded text.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditEmitter:
    """Writes one hash-chained audit stream for one process.

    Create one per process, call :meth:`start` once, then :meth:`emit` for
    every audited operation. :meth:`start` also starts the heartbeat and
    registers :meth:`close` to run at interpreter exit. Thread-safe.
    """

    def __init__(
        self,
        service: str,
        service_version: str,
        *,
        stream: TextIO | None = None,
        heartbeat_interval_s: float = 60.0,
    ) -> None:
        """Prepare an emitter; nothing is written until :meth:`start`.

        Args:
            service: The emitting service, e.g. ``chorus``.
            service_version: Its release, e.g. ``0.7.0``.
            stream: Where lines go; defaults to ``sys.stdout``, which the
                container runtime ships to the log collector.
            heartbeat_interval_s: Seconds between heartbeat events; ``0``
                disables the heartbeat thread.
        """
        self.service = service
        self.service_version = service_version
        self.instance = secrets.token_hex(8)
        self._stream = stream
        self._heartbeat_interval_s = heartbeat_interval_s
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_hash: str = GENESIS_HASH
        self._started = False
        self._closing = False
        self._closed = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def start(self) -> dict[str, object]:
        """Open the chain: write its ``start`` event and start the heartbeat.

        Returns:
            The ``start`` event as written.

        Raises:
            RuntimeError: If called twice.
        """
        if self._started:
            raise RuntimeError("audit emitter already started")
        self._started = True
        event = self.emit("lifecycle", "start", Actor(user=None, via="system"))
        if self._heartbeat_interval_s > 0:
            self._heartbeat = threading.Thread(target=self._beat, name="audit-heartbeat", daemon=True)
            self._heartbeat.start()
        atexit.register(self.close)
        return event

    def close(self) -> None:
        """Close the chain with a ``shutdown`` event; later calls do nothing.

        The shutdown event is written and the chain ended in one locked step,
        so no other thread's event can follow it, and of concurrent calls (an
        explicit close racing the interpreter-exit hook) only one writes it.
        If the shutdown cannot be written, the chain still ends there.

        Raises:
            AuditWriteError: If the shutdown event could not be written.
        """
        with self._lock:
            if self._closing or not self._started:
                return
            self._closing = True
        self._stop.set()
        if self._heartbeat is not None and self._heartbeat is not threading.current_thread():
            self._heartbeat.join(timeout=5)
        self._append(self._body("lifecycle", "shutdown", Actor(user=None, via="system")), final=True)

    def _beat(self) -> None:
        """Heartbeat loop: one ``heartbeat`` event per interval until closed."""
        while not self._stop.wait(self._heartbeat_interval_s):
            try:
                self.emit("lifecycle", "heartbeat", Actor(user=None, via="system"))
            except (AuditWriteError, RuntimeError):
                return

    def emit(
        self,
        action: str,
        operation: str,
        actor: Actor,
        *,
        outcome: str = "ok",
        request_id: str | None = None,
        parent_event_id: str | None = None,
        purpose: Purpose | None = None,
        target: Target | None = None,
        criteria: Mapping[str, object] | None = None,
        recipient: str | None = None,
        ai: AiCall | None = None,
    ) -> dict[str, object]:
        """Write one audit event and advance the chain.

        Args:
            action: One of :data:`ACTIONS`.
            operation: What exactly happened, dotted, e.g.
                ``tool.posts_mentioning`` or ``export.csv``.
            actor: Who caused it.
            outcome: One of :data:`OUTCOMES`.
            request_id: The gateway's ``X-Request-ID``, for correlation.
            parent_event_id: The event this one belongs to, e.g. the agent
                turn a tool call ran in.
            purpose: The caller's stated purpose.
            target: What was touched, by reference.
            criteria: Search criteria or parameters, bounded by the caps.
            recipient: Where data went, for ``disclose``: e.g. ``download``,
                ``export:csv``, ``inference:chat``.
            ai: Model provenance, for ``ai_call`` and AI-produced results.

        Returns:
            The event as written, including ``seq``, ``prev_hash`` and
            ``hash``.

        Raises:
            ValueError: If ``action`` or ``outcome`` is unknown.
            RuntimeError: If the emitter is not started or already closed.
            AuditWriteError: If the line could not be written.
        """
        body = self._body(
            action,
            operation,
            actor,
            outcome=outcome,
            request_id=request_id,
            parent_event_id=parent_event_id,
            purpose=purpose,
            target=target,
            criteria=criteria,
            recipient=recipient,
            ai=ai,
        )
        return self._append(body, final=False)

    def _body(
        self,
        action: str,
        operation: str,
        actor: Actor,
        *,
        outcome: str = "ok",
        request_id: str | None = None,
        parent_event_id: str | None = None,
        purpose: Purpose | None = None,
        target: Target | None = None,
        criteria: Mapping[str, object] | None = None,
        recipient: str | None = None,
        ai: AiCall | None = None,
    ) -> dict[str, object]:
        """Build an event without its chain fields; the arguments are :meth:`emit`'s.

        Returns:
            The event, still without ``seq``, ``prev_hash`` and ``hash``.

        Raises:
            ValueError: If ``action`` or ``outcome`` is unknown.
        """
        if action not in ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown audit outcome {outcome!r}")
        body: dict[str, object] = {
            "stream": STREAM_MARKER,
            "v": SCHEMA_VERSION,
            "event_id": new_event_id(),
            "ts": utc_timestamp(),
            "service": self.service,
            "service_version": self.service_version,
            "instance": self.instance,
            "action": action,
            "operation": _cap_text(operation),
            "outcome": outcome,
            "actor": actor.to_dict(),
        }
        if request_id is not None:
            body["request_id"] = _cap_text(request_id)
        if parent_event_id is not None:
            body["parent_event_id"] = parent_event_id
        if purpose is not None and purpose.to_dict():
            body["purpose"] = purpose.to_dict()
        if target is not None:
            body["target"] = target.to_dict()
        if criteria:
            body["criteria"] = _bounded(criteria)
        if recipient is not None:
            body["recipient"] = _cap_text(recipient)
        if ai is not None:
            body["ai"] = ai.to_dict()
        return body

    def _append(self, body: dict[str, object], *, final: bool) -> dict[str, object]:
        """Chain an event and write it, all under the emitter's lock.

        Args:
            body: The event from :meth:`_body`.
            final: Whether this is the ``shutdown`` event. The chain then ends
                in the same locked step, whether the line was written or not.

        Returns:
            The event as written, including ``seq``, ``prev_hash`` and
            ``hash``.

        Raises:
            RuntimeError: If the emitter is not started or already closed.
            AuditWriteError: If the line could not be written.
        """
        with self._lock:
            if not self._started:
                raise RuntimeError("audit emitter not started")
            if self._closed:
                raise RuntimeError("audit emitter already closed")
            try:
                body["seq"] = self._seq
                body["prev_hash"] = self._prev_hash
                body["hash"] = event_hash(body)
                line = event_line(body)
                out = self._stream if self._stream is not None else sys.stdout
                try:
                    out.write(line + "\n")
                    out.flush()
                except (OSError, ValueError) as exc:
                    raise AuditWriteError("audit event could not be written") from exc
                self._seq += 1
                self._prev_hash = str(body["hash"])
            finally:
                if final:
                    self._closed = True
        return body


@dataclass(frozen=True)
class ChainProblem:
    """One defect found in an audit stream.

    Attributes:
        service: The emitting service, as far as known.
        instance: The chain (emitter instance) it belongs to.
        seq: The sequence number at which it was found, if any.
        kind: ``schema``, ``bad_hash``, ``broken_link``, ``seq_gap``,
            ``seq_repeat``, ``no_start``, ``service_mismatch`` (an event
            naming another service than its chain, left out of the chain),
            ``after_shutdown`` (an event following its chain's shutdown,
            left out of the chain) or ``unterminated`` (an open chain that
            went silent for longer than allowed).
        detail: A short human-readable description, never event content.
    """

    service: str
    instance: str
    seq: int | None
    kind: str
    detail: str


@dataclass(frozen=True)
class ChainHead:
    """The last verified position of one chain.

    Attributes:
        service: The emitting service.
        instance: The chain id.
        seq: The last verified sequence number.
        hash: That event's hash, which the next event must name as
            ``prev_hash``.
        ts: That event's timestamp.
        closed: Whether the chain is finished: it ended with its
            ``shutdown`` event, or it was reported ``unterminated`` (which
            is then not reported again; it may still resume).
        ended: Whether it ended with its ``shutdown`` event. A process
            writes nothing after that, so nothing may follow.
    """

    service: str
    instance: str
    seq: int
    hash: str
    ts: str
    closed: bool
    ended: bool = False


@dataclass
class ChainReport:
    """The result of :func:`verify_chain`.

    Attributes:
        events: How many events were checked.
        problems: Every defect found, in stream order.
        heads: The last verified position of each chain, by instance; pass
            them back as ``checkpoints`` to verify the next window.
    """

    events: int = 0
    problems: list[ChainProblem] = field(default_factory=list)
    heads: dict[str, ChainHead] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether no problem was found."""
        return not self.problems


def _parse_ts(value: str) -> datetime | None:
    """Parse an event timestamp.

    Args:
        value: An ISO 8601 timestamp, ``Z`` or offset suffixed.

    Returns:
        The timezone-aware time, or ``None`` when the value does not parse
        or carries no timezone.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _label(value: object) -> str:
    """Return a value for use as a problem label, or ``?`` if it is not one.

    Args:
        value: A field taken from an untrusted event.

    Returns:
        The value when it is a short string, else ``?``; never the content
        of anything else.
    """
    return value if isinstance(value, str) and len(value) <= 64 else "?"


def _schema_problem(event: object) -> str | None:
    """Describe the first structural defect of an event, if any.

    Every type is checked before any comparison, so a hostile line can only
    ever produce a problem, never an exception.

    Args:
        event: A parsed audit line, of any type.

    Returns:
        A description, or ``None`` when the event is well-formed.
    """
    if not isinstance(event, Mapping):
        return "not an object"
    for key in _REQUIRED_KEYS:
        if key not in event:
            return f"missing field {key!r}"
    for key in _STRING_KEYS:
        if not isinstance(event[key], str):
            return f"{key} is not a string"
    version = event["v"]
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        return "unsupported schema version"
    if event["stream"] != STREAM_MARKER:
        return "not an audit event"
    if event["action"] not in ACTIONS:
        return "unknown action"
    if event["outcome"] not in OUTCOMES:
        return "unknown outcome"
    seq = event["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return "seq is not a non-negative integer"
    for key in ("prev_hash", "hash"):
        if not _HEX64.fullmatch(event[key]):
            return f"{key} is not a SHA-256 hex digest"
    if _parse_ts(event["ts"]) is None:
        return "ts is not an ISO 8601 timestamp with a time zone"
    actor = event["actor"]
    if not isinstance(actor, Mapping) or not isinstance(actor.get("via"), str) or actor.get("via") not in VIA:
        return "actor is malformed"
    return None


def verify_chain(
    events: Iterable[object],
    checkpoints: Mapping[str, ChainHead] | None = None,
    *,
    now: datetime | None = None,
    silence: timedelta | None = None,
) -> ChainReport:
    """Check the integrity of audit events, chain by chain.

    Events may arrive interleaved across chains and out of order; they are
    grouped by ``instance`` and checked in ``seq`` order. Without a checkpoint,
    a chain must start at ``seq`` 0 with :data:`GENESIS_HASH`; with one, it
    must continue right after the checkpoint. A malformed line is reported as
    a ``schema`` problem and never stops the run.

    Args:
        events: Parsed audit lines.
        checkpoints: The heads of a previous report, to verify a later
            window of the same chains.
        now: The current time; with ``silence``, enables the check for
            chains that stopped without a ``shutdown`` event.
        silence: How long an open chain may go without an event before it is
            reported as ``unterminated`` (and from then on treated as
            closed, so it is reported once). Use a multiple of the
            emitters' heartbeat interval.

    Returns:
        A report with every problem found and the new chain heads.
    """
    report = ChainReport()
    by_instance: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        report.events += 1
        try:
            problem = _schema_problem(event)
        except (TypeError, ValueError, KeyError):
            problem = "malformed event"
        if problem is not None:
            source: Mapping[str, Any] = event if isinstance(event, Mapping) else {}
            raw_seq = source.get("seq")
            report.problems.append(
                ChainProblem(
                    service=_label(source.get("service")),
                    instance=_label(source.get("instance")),
                    seq=raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else None,
                    kind="schema",
                    detail=problem,
                )
            )
            continue
        if isinstance(event, Mapping):
            by_instance.setdefault(str(event["instance"]), []).append(event)

    known = dict(checkpoints or {})
    for instance, chain in by_instance.items():
        chain.sort(key=lambda item: int(item["seq"]))
        head = known.get(instance)
        # A chain belongs to one service: its checkpoint's, else its first event's.
        service = head.service if head is not None else str(chain[0]["service"])
        expected_seq = 0 if head is None else head.seq + 1
        expected_prev = GENESIS_HASH if head is None else head.hash
        closed = False if head is None else head.closed
        ended = False if head is None else head.ended
        last: Mapping[str, Any] | None = None
        for event in chain:
            seq = int(event["seq"])
            if str(event["service"]) != service:
                detail = f"event of service {_label(event['service'])} in a chain of {_label(service)}"
                report.problems.append(ChainProblem(service, instance, seq, "service_mismatch", detail))
                continue
            if last is not None and seq == int(last["seq"]):
                report.problems.append(ChainProblem(service, instance, seq, "seq_repeat", "sequence number repeats"))
                continue
            if ended and seq >= expected_seq:
                report.problems.append(
                    ChainProblem(service, instance, seq, "after_shutdown", "event after the chain's shutdown")
                )
                continue
            if seq != expected_seq:
                kind = "no_start" if head is None and last is None else "seq_gap"
                detail = f"expected seq {expected_seq}, got {seq}"
                report.problems.append(ChainProblem(service, instance, seq, kind, detail))
            elif event["prev_hash"] != expected_prev:
                report.problems.append(
                    ChainProblem(service, instance, seq, "broken_link", "prev_hash does not match the previous event")
                )
            try:
                intact = event_hash(event) == event["hash"]
            except (TypeError, ValueError):
                intact = False
            if not intact:
                report.problems.append(ChainProblem(service, instance, seq, "bad_hash", "event hash does not match"))
            expected_seq = seq + 1
            expected_prev = str(event["hash"])
            closed = ended = event["action"] == "lifecycle" and event["operation"] == "shutdown"
            last = event
        if last is not None:
            report.heads[instance] = ChainHead(
                service=service,
                instance=instance,
                seq=int(last["seq"]),
                hash=str(last["hash"]),
                ts=str(last["ts"]),
                closed=closed,
                ended=ended,
            )
    for instance, head in known.items():
        report.heads.setdefault(instance, head)

    if now is not None and silence is not None:
        for instance, head in list(report.heads.items()):
            last_seen = _parse_ts(head.ts)
            if head.closed or last_seen is None or now - last_seen <= silence:
                continue
            report.problems.append(
                ChainProblem(
                    head.service, instance, head.seq, "unterminated", "chain went silent without a shutdown event"
                )
            )
            report.heads[instance] = ChainHead(
                service=head.service, instance=instance, seq=head.seq, hash=head.hash, ts=head.ts, closed=True
            )
    return report

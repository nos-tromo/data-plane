"""data-plane's data-protection audit events: direct database access, backups, restores.

The apps record what their users do with the data in Neo4j and Qdrant; access
that bypasses them leaves no trace there. Neo4j Community keeps no query log and
Qdrant no access log, so the operator targets that open the stores directly
(``make db-shell``, ``make qdrant-shell``), copy their data out (``make backup``)
or write it back (``make restore-neo4j``, ``make restore-qdrant``) run through
``scripts/audit-admin.sh``, which records them with this module; the admin
overlay's loopback ports (``make up-admin``, closed by ``make up`` or ``make
down``) through ``scripts/admin-ports.sh``. Operator guide: the README's "Audit
trail" section.

Every record is a chain of its own, written by a short ``docker compose exec``
run in the ``admin-audit`` container (``AUDIT_OUTPUT=container``). It therefore
lands in that container's log, the source obs-plane binds data-plane's chains to.

Backups are encrypted with age on their way out of the stores
(``scripts/backup_age.sh``): a backup's record says so, to how many public keys,
and which set of them by its fingerprint, never the keys.

Subcommands:

``serve``
    The long-running ``admin-audit`` service. Its chain records the service's
    configuration, then only beats until the container stops.
``check``
    Exit 0 when a record could be written right now (the healthcheck).
``begin KIND [STORE] [KEY=VALUE ...]``
    Admit an operation before it starts. Exit 2 when it needs a stated purpose
    and has none (recorded as ``denied``) or the purpose is malformed, 1 when
    nothing can be recorded. A shell's opening, and the admin ports' (kind
    ``ports``, no store), are recorded here and the event's id printed, for the
    record of the close.
``end KIND [STORE] --status N [--duration-s N] [--opened EVENT_ID] [KEY=VALUE ...]``
    Record an operation that has run: ``ok`` when it exited 0, else ``error``.
``verify``
    Check the hash chains in audit lines read from stdin.

Stdlib only; Python 3.12.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Final, TextIO

from audit_event import ACTIONS, STREAM_MARKER, Actor, AuditEmitter, AuditWriteError, Purpose, Target, verify_chain
from audit_runtime import PurposeError, id_chunks, open_audit_stream, parse_required_actions, purpose_from_env

#: The compose project: every chain this module writes names it as its service.
SERVICE: Final = "data-plane"
DEFAULT_VERSION_FILE: Final = "/etc/data-plane/VERSION"
STORES: Final = ("neo4j", "qdrant")
#: Each kind of operation and the action its record has.
KIND_ACTIONS: Final = {"shell": "admin", "backup": "disclose", "restore": "modify", "ports": "admin", "prune": "delete"}
#: The kinds recorded when they open and again when they close, linked: the
#: operations of the two events. They always need a stated purpose.
OPENED_KINDS: Final = {
    "shell": ("db.shell.open", "db.shell.close"),
    "ports": ("db.ports.open", "db.ports.close"),
}
#: The loopback bindings docker/compose.admin.yaml publishes (make up-admin).
ADMIN_PORTS: Final = ("127.0.0.1:7474", "127.0.0.1:7687", "127.0.0.1:6333")
#: What ended an admin-ports window: make up, make down, or the failed opening.
PORT_CLOSERS: Final = ("up", "down", "up-admin")
#: Where a backup goes, as its record names the recipient.
BACKUP_RECIPIENT: Final = "file:backup"
#: The actions ``AUDIT_REQUIRE_PURPOSE`` may list.
REQUIRABLE: Final = ACTIONS - {"lifecycle"}
#: How backup files are encrypted (scripts/backup_age.sh): age, to public keys.
ENCRYPTIONS: Final = ("age",)

_PAIR: Final = re.compile(r"([a-z][a-z0-9_]{0,31})=(.*)", re.DOTALL)
_VERSION: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_EVENT_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A base name only: never a directory, so a path naming a user cannot be recorded.
_FILE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_UTC: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

Parser = Callable[[str], object]

LOG = logging.getLogger("admin-audit")


def whole_number(text: str) -> int:
    """Parse a size, count or duration.

    Args:
        text: Decimal digits.

    Returns:
        The number.

    Raises:
        ValueError: If it is not a whole number from 0 to 18 digits.
    """
    if not text.isascii() or not text.isdigit() or len(text) > 18:
        raise ValueError("not a whole number")
    return int(text)


def positive_number(text: str) -> int:
    """Parse a count that is never zero, e.g. of a backup's recipients.

    Args:
        text: Decimal digits.

    Returns:
        The number.

    Raises:
        ValueError: If it is not a whole number of at least 1.
    """
    value = whole_number(text)
    if value < 1:
        raise ValueError("not a positive number")
    return value


def sha256_hex(text: str) -> str:
    """Accept a SHA-256 digest, e.g. the fingerprint of a backup's recipients.

    Args:
        text: 64 lowercase hex digits.

    Returns:
        The digest.

    Raises:
        ValueError: If it is not one.
    """
    if _SHA256.fullmatch(text) is None:
        raise ValueError("not a SHA-256 digest")
    return text


def encryption(text: str) -> str:
    """Accept how a backup file is encrypted.

    Args:
        text: ``age``.

    Returns:
        The encryption.

    Raises:
        ValueError: If the scripts do not encrypt that way.
    """
    if text not in ENCRYPTIONS:
        raise ValueError("not an encryption the backups use")
    return text


def file_name(text: str) -> str:
    """Accept the base name of a dump or snapshot file.

    Args:
        text: E.g. ``neo4j-20260131T020000Z.dump``.

    Returns:
        The name.

    Raises:
        ValueError: If it is not a plain file name of letters, digits and ``._-``.
    """
    if _FILE_NAME.fullmatch(text) is None:
        raise ValueError("not a plain file name")
    return text


def utc_time(text: str) -> str:
    """Accept a moment in UTC, e.g. the cutoff a pruning applied.

    Args:
        text: ``YYYY-MM-DDTHH:MM:SSZ``.

    Returns:
        The moment, as given.

    Raises:
        ValueError: If it is not in that form.
    """
    if _UTC.fullmatch(text) is None:
        raise ValueError("not a UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    return text


def store_name(text: str) -> str:
    """Accept a name a store gave an object, e.g. a Qdrant collection.

    Args:
        text: The name.

    Returns:
        The name.

    Raises:
        ValueError: If it is empty, longer than 255 characters or contains
            a character that is not printable (controls, line separators).
    """
    if not text or len(text) > 255 or not text.isprintable():
        raise ValueError("not a name")
    return text


def port_closer(text: str) -> str:
    """Accept what ended an admin-ports window.

    Args:
        text: ``up`` (make up recreated the stores without the ports),
            ``down`` (make down removed them) or ``up-admin`` (the opening
            failed and published nothing).

    Returns:
        The closer.

    Raises:
        ValueError: If it is none of them.
    """
    if text not in PORT_CLOSERS:
        raise ValueError("not a closer of the admin ports")
    return text


#: An operation, by kind and store (none for the admin ports).
OperationKey = tuple[str, str | None]

#: The parameters of an operation: given to ``begin`` and again to ``end``,
#: and recorded as criteria on each of its events.
PARAMETERS: Final[Mapping[OperationKey, Mapping[str, Parser]]] = {
    ("restore", "neo4j"): {"file": file_name},
    ("restore", "qdrant"): {"collection": store_name, "snapshot": file_name},
}
#: What a backup reports about its encryption: age, to how many public keys
#: (the recipients file's distinct entries), and the SHA-256 of that set, so a
#: record shows which keys can read the file without naming one.
ENCRYPTION_FACTS: Final[Mapping[str, Parser]] = {
    "encryption": encryption,
    "recipients": positive_number,
    "recipients_sha256": sha256_hex,
}
#: The facts an operation reports for its record (``end`` only).
FACTS: Final[Mapping[OperationKey, Mapping[str, Parser]]] = {
    ("backup", "neo4j"): {"file": file_name, "size_bytes": whole_number, **ENCRYPTION_FACTS},
    ("backup", "qdrant"): {
        "collection": store_name,
        "snapshots": whole_number,
        "size_bytes": whole_number,
        "failed": whole_number,
        **ENCRYPTION_FACTS,
    },
    ("restore", "neo4j"): {"size_bytes": whole_number, "encryption": encryption},
    ("restore", "qdrant"): {"encryption": encryption},
    ("ports", None): {"by": port_closer},
    **{
        ("prune", store): {
            "file": file_name,
            "period_days": positive_number,
            "cutoff": utc_time,
            "deleted": whole_number,
            "not_deleted": whole_number,
            "grace_until": utc_time,
            "in_force_days": positive_number,
        }
        for store in STORES
    },
}
#: The fact that may repeat, once per object of the record's target: the
#: collections a Qdrant backup covers, the files a pruning deleted.
TARGET_FACTS: Final[Mapping[OperationKey, str]] = {
    ("backup", "qdrant"): "collection",
    ("prune", "neo4j"): "file",
    ("prune", "qdrant"): "file",
}
#: The facts an operation that succeeded must report: a backup how it was
#: encrypted and to which keys, a restore that it read an encrypted file. No
#: ``ok`` record can therefore show an unencrypted backup.
REQUIRED_FACTS: Final[Mapping[OperationKey, tuple[str, ...]]] = {
    ("backup", "neo4j"): tuple(ENCRYPTION_FACTS),
    ("backup", "qdrant"): tuple(ENCRYPTION_FACTS),
    ("restore", "neo4j"): ("encryption",),
    ("restore", "qdrant"): ("encryption",),
    ("prune", "neo4j"): ("period_days", "cutoff", "deleted"),
    ("prune", "qdrant"): ("period_days", "cutoff", "deleted"),
}


def parse_pairs(
    pairs: Sequence[str],
    fields: Mapping[str, Parser],
    *,
    repeatable: str | None = None,
) -> tuple[dict[str, object], list[str]]:
    """Parse ``KEY=VALUE`` arguments against the fields an operation records.

    Args:
        pairs: E.g. ``["file=neo4j-20260131T020000Z.dump", "size_bytes=1024"]``.
        fields: The keys accepted and how each value is checked.
        repeatable: A key that may be given more than once.

    Returns:
        The values by key, and the values of ``repeatable`` in order.

    Raises:
        ValueError: If a pair is malformed, its key is not accepted or given
            twice, or its value is not valid for it.
    """
    values: dict[str, object] = {}
    repeated: list[str] = []
    for pair in pairs:
        match = _PAIR.fullmatch(pair)
        if match is None:
            raise ValueError(f"not a key=value pair: {pair!r}")
        key, raw = match.group(1), match.group(2)
        parse = fields.get(key)
        if parse is None:
            raise ValueError(f"{key!r} is not recorded for this operation")
        try:
            value = parse(raw)
        except ValueError:
            raise ValueError(f"{key!r} has an invalid value") from None
        if key == repeatable:
            repeated.append(str(value))
        elif key in values:
            raise ValueError(f"{key!r} given twice")
        else:
            values[key] = value
    return values, repeated


def read_version(path: Path) -> str:
    """Read the release this checkout is (the repo's ``VERSION`` file).

    Args:
        path: The mounted version file.

    Returns:
        The version, e.g. ``0.4.0``.

    Raises:
        ValueError: If the file is missing or holds no version.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read the data-plane version from {path}") from exc
    if _VERSION.fullmatch(text) is None:
        raise ValueError(f"{path} holds no version")
    return text


def version_file(environ: Mapping[str, str]) -> Path:
    """Locate the version file.

    Args:
        environ: The process environment.

    Returns:
        ``AUDIT_VERSION_FILE``, else the path the container mounts it at.
    """
    return Path(environ.get("AUDIT_VERSION_FILE") or DEFAULT_VERSION_FILE)


def required_actions(environ: Mapping[str, str]) -> frozenset[str]:
    """Read ``AUDIT_REQUIRE_PURPOSE``.

    Args:
        environ: The process environment.

    Returns:
        The actions that need a stated purpose; none when unset or blank.

    Raises:
        ValueError: If it names an unknown action.
    """
    return parse_required_actions(environ.get("AUDIT_REQUIRE_PURPOSE"), known=REQUIRABLE)


def purpose_required(kind: str, required: frozenset[str]) -> bool:
    """Decide whether an operation needs a stated purpose.

    Args:
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        required: The actions ``AUDIT_REQUIRE_PURPOSE`` lists.

    Returns:
        Always ``True`` for a shell and the admin ports: both open the stores
        to anything. Otherwise whether the action of the kind's record is listed.
    """
    return kind in OPENED_KINDS or KIND_ACTIONS[kind] in required


def operation_of(kind: str, store: str | None) -> str:
    """Name the operation an operation's main record has.

    Args:
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports.

    Returns:
        The opening's operation for a shell or the admin ports
        (``db.shell.open``, ``db.ports.open``; :data:`OPENED_KINDS` names their
        closes), ``backup.prune.<store>`` for a pruning of a store's backups,
        else ``backup.<store>`` or ``restore.<store>``.
    """
    if kind in OPENED_KINDS:
        return OPENED_KINDS[kind][0]
    if kind == "prune":
        return f"backup.prune.{store}"
    return f"{kind}.{store}"


def target_of(
    kind: str,
    store: str | None,
    parameters: Mapping[str, object],
    ids: Sequence[str] = (),
    total: int | None = None,
) -> Target:
    """Name what an operation touched, by reference.

    Args:
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports.
        parameters: The operation's parameters.
        ids: The objects it reported: the collections a Qdrant backup covers,
            at most one event's worth.
        total: How many objects there are in all, when ``ids`` is the first
            chunk of a longer list.

    Returns:
        The store for a shell, both stores for the admin ports; Neo4j's
        database ``neo4j`` for a dump or load; the collections snapshotted,
        or the one recovered, for Qdrant.
    """
    if kind == "ports":
        return Target(kind="store", ids=list(STORES), count=len(STORES))
    if kind == "prune":
        return Target(kind="file", ids=list(ids), count=len(ids) if total is None else total)
    if kind == "shell":
        return Target(kind="store", ids=[str(store)], count=1)
    if store == "neo4j":
        return Target(kind="database", ids=["neo4j"], count=1)
    if kind == "backup":
        return Target(kind="collection", ids=list(ids), count=len(ids) if total is None else total)
    return Target(kind="collection", ids=[str(parameters["collection"])], count=1)


def check_operation(kind: str, store: str | None, parameters: Mapping[str, object]) -> None:
    """Check that an operation is known and has all its parameters.

    Args:
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports, which
            open both.
        parameters: The parsed parameters.

    Raises:
        ValueError: If the operation is unknown or a parameter is missing.
    """
    if kind not in KIND_ACTIONS or (store is not None if kind == "ports" else store not in STORES):
        raise ValueError(f"unknown operation {kind} {store or ''}".rstrip())
    missing = sorted(set(PARAMETERS.get((kind, store), {})) - set(parameters))
    if missing:
        raise ValueError(f"missing parameter(s): {', '.join(missing)}")


class Invocation:
    """What every record of one run shares: who acts, why, and on what."""

    def __init__(
        self, environ: Mapping[str, str], kind: str, store: str | None, parameters: Mapping[str, object]
    ) -> None:
        """Collect the shared parts of a run's records; nothing is written.

        Args:
            environ: The process environment: ``AUDIT_CLI_USER`` (the operator),
                ``AUDIT_CASE_REF``/``AUDIT_REASON`` (the purpose),
                ``AUDIT_REQUIRE_PURPOSE``, ``AUDIT_OUTPUT``.
            kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
            store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports.
            parameters: The operation's parameters, already parsed.

        Raises:
            ValueError: If the operation is unknown or a parameter is missing.
            PurposeError: If the stated purpose is malformed.
        """
        check_operation(kind, store, parameters)
        self.environ = environ
        self.kind = kind
        self.store = store
        self.action = KIND_ACTIONS[kind]
        self.operation = operation_of(kind, store)
        self.parameters = dict(parameters)
        case_ref, reason = purpose_from_env(environ)
        self.purpose = Purpose(case_ref=case_ref, reason=reason) if case_ref or reason else None
        self.actor = Actor(environ.get("AUDIT_CLI_USER") or None, "cli")

    def criteria(self, **extra: object) -> dict[str, object]:
        """Collect an event's criteria: the store for a shell, the bindings for the admin ports, then the parameters.

        Args:
            **extra: More criteria.

        Returns:
            The criteria.
        """
        base: dict[str, object] = {}
        if self.kind == "shell":
            base["store"] = self.store
        elif self.kind == "ports":
            base["ports"] = list(ADMIN_PORTS)
        return {**base, **self.parameters, **extra}

    def open_stream(self) -> tuple[str, TextIO | None]:
        """Read the version and open the audit stream; nothing is written.

        Returns:
            The version for the chain, and the stream (``None`` for stdout).

        Raises:
            ValueError: If the version file or ``AUDIT_OUTPUT`` is not valid.
            OSError: If the audit stream cannot be opened.
        """
        version = read_version(version_file(self.environ))
        return version, open_audit_stream(self.environ.get("AUDIT_OUTPUT"))


def _refuse(message: str, code: int) -> int:
    """Print a one-line message for the operator and return the exit code.

    Args:
        message: What is wrong. It reaches the operator's terminal, never the log.
        code: The exit code.

    Returns:
        ``code``.
    """
    print(f"admin-audit: {message}", file=sys.stderr)
    return code


def begin(environ: Mapping[str, str], kind: str, store: str | None, pairs: Sequence[str], out: TextIO) -> int:
    """Admit an operation before it runs.

    A shell and the admin ports need a stated purpose always; a backup or
    restore when ``AUDIT_REQUIRE_PURPOSE`` lists the action of its record. A
    refusal is recorded as a ``denied`` event with the operation's criteria
    and no target. A shell's opening is recorded as ``admin`` /
    ``db.shell.open``, the admin ports' as ``admin`` / ``db.ports.open``, and
    the event id printed to ``out`` for :func:`end`. A backup or restore writes
    nothing here: its record is written once it has run.

    Args:
        environ: The process environment.
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports.
        pairs: The operation's ``KEY=VALUE`` parameters.
        out: Where the opening's event id goes.

    Returns:
        0 when the operation may run; 2 when it is refused (or its arguments or
        purpose are malformed); 1 when it cannot be recorded.
    """
    try:
        parameters, _ = parse_pairs(pairs, PARAMETERS.get((kind, store), {}))
        call = Invocation(environ, kind, store, parameters)
    except PurposeError as exc:
        return _refuse(f"the stated purpose is not accepted ({exc})", 2)
    except ValueError as exc:
        return _refuse(str(exc), 2)
    try:
        required = required_actions(environ)
        version, stream = call.open_stream()
    except (OSError, ValueError) as exc:
        return _refuse(f"cannot record events ({exc})", 1)
    refused = purpose_required(kind, required) and call.purpose is None
    if kind not in OPENED_KINDS and not refused:
        if stream is not None:
            stream.close()
        return 0
    emitter = AuditEmitter(SERVICE, version, stream=stream, heartbeat_interval_s=0)
    try:
        emitter.start()
        if refused:
            event = emitter.emit(call.action, call.operation, call.actor, outcome="denied", criteria=call.criteria())
        else:
            event = emitter.emit(
                call.action,
                call.operation,
                call.actor,
                purpose=call.purpose,
                target=target_of(kind, store, call.parameters),
                criteria=call.criteria(),
            )
        emitter.close()
    except (AuditWriteError, RuntimeError) as exc:
        return _refuse(f"could not write the event ({exc})", 1)
    if refused:
        return _refuse(f"{call.operation} needs a stated purpose: set CASE_REF=<reference> and/or REASON=<text>", 2)
    print(f"admin-audit: recorded {call.action} {call.operation} (ok)", file=sys.stderr)
    print(event["event_id"], file=out)
    return 0


def end(
    environ: Mapping[str, str],
    kind: str,
    store: str | None,
    pairs: Sequence[str],
    *,
    status: int,
    duration_s: int | None = None,
    opened: str | None = None,
) -> int:
    """Record an operation that has run, as a chain of its own.

    Args:
        environ: The process environment.
        kind: ``shell``, ``backup``, ``restore``, ``ports`` or ``prune``.
        store: ``neo4j`` or ``qdrant``; ``None`` for the admin ports.
        pairs: The operation's parameters (as given to :func:`begin`) and the
            facts it reported, as ``KEY=VALUE``.
        status: The operation's exit status; 0 is ``ok``, anything else ``error``
            (it may have run in part).
        duration_s: How long it ran.
        opened: For a shell or the admin ports, the id of the opening's event.

    Returns:
        0 when the record was written, 1 when it was not, 2 on bad arguments,
        among them an operation that succeeded without its required facts
        (:data:`REQUIRED_FACTS`).
    """
    names = PARAMETERS.get((kind, store), {})
    try:
        if status < 0 or (duration_s is not None and duration_s < 0):
            raise ValueError("the status and the duration must not be negative")
        if opened is not None and _EVENT_ID.fullmatch(opened) is None:
            raise ValueError("--opened is not an event id")
        if kind in OPENED_KINDS and opened is None:
            raise ValueError("a close needs the event id of its opening (--opened)")
        values, ids = parse_pairs(
            pairs, {**names, **FACTS.get((kind, store), {})}, repeatable=TARGET_FACTS.get((kind, store))
        )
        call = Invocation(environ, kind, store, {key: value for key, value in values.items() if key in names})
        if kind == "ports" and "by" not in values:
            raise ValueError(f"the admin ports' close needs by=<{'|'.join(PORT_CLOSERS)}>")
        missing = [key for key in REQUIRED_FACTS.get((kind, store), ()) if key not in values]
        if status == 0 and missing:
            raise ValueError(f"{call.operation} succeeded without reporting {', '.join(missing)}")
    except PurposeError as exc:
        return _refuse(f"the stated purpose is not accepted ({exc})", 2)
    except ValueError as exc:
        return _refuse(str(exc), 2)
    facts = {key: value for key, value in values.items() if key not in names}
    facts["exit_status"] = status
    if duration_s is not None:
        facts["duration_s"] = duration_s
    operation = OPENED_KINDS[kind][1] if kind in OPENED_KINDS else call.operation
    chunks = id_chunks(ids) or [[]]
    target = target_of(kind, store, call.parameters, chunks[0], len(ids))
    outcome = "ok" if status == 0 else "error"
    recipient = BACKUP_RECIPIENT if kind == "backup" else None
    try:
        version, stream = call.open_stream()
    except (OSError, ValueError) as exc:
        return _refuse(f"cannot record events ({exc})", 1)
    emitter = AuditEmitter(SERVICE, version, stream=stream, heartbeat_interval_s=0)
    try:
        emitter.start()
        primary = emitter.emit(
            call.action,
            operation,
            call.actor,
            outcome=outcome,
            parent_event_id=opened,
            purpose=call.purpose,
            target=target,
            criteria=call.criteria(**facts),
            recipient=recipient,
        )
        for chunk in chunks[1:]:
            emitter.emit(
                call.action,
                f"{operation}.ids",
                call.actor,
                outcome=outcome,
                parent_event_id=str(primary["event_id"]),
                purpose=call.purpose,
                target=Target(kind=target.kind, ids=chunk, count=len(chunk)),
                recipient=recipient,
            )
        emitter.close()
    except (AuditWriteError, RuntimeError) as exc:
        return _refuse(f"could not write the record ({exc})", 1)
    print(f"admin-audit: recorded {call.action} {operation} ({outcome})", file=sys.stderr)
    return 0


def serve(environ: Mapping[str, str], stop: threading.Event | None = None) -> int:
    """Run the service until SIGTERM/SIGINT (or ``stop``).

    Its chain records the configuration once (``admin`` / ``config.audit``)
    and then beats every minute; the records themselves are written by the
    ``begin``/``end`` runs, each in a chain of its own.

    Args:
        environ: The process environment.
        stop: Ends the service when set; signal handlers set it in the container.

    Returns:
        The exit status: 0 after a clean stop, 1 when an event could not be
        written, 2 on bad configuration.
    """
    try:
        version = read_version(version_file(environ))
        required = required_actions(environ)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2
    try:
        stream = open_audit_stream(environ.get("AUDIT_OUTPUT"))
    except (OSError, ValueError) as exc:
        LOG.error("cannot open the audit stream: %s", exc)
        return 1
    stop = stop or threading.Event()
    emitter = AuditEmitter(SERVICE, version, stream=stream)
    status = 0
    try:
        emitter.start()
        emitter.emit(
            "admin",
            "config.audit",
            Actor(None, "system"),
            criteria={"require_purpose": sorted(required), "shells_require_purpose": True},
        )
        LOG.info("ready to record direct database access, backups and restores")
        while not stop.wait(5):
            pass
    except (AuditWriteError, RuntimeError) as exc:
        LOG.error("an audit event could not be written (%s); stopping", exc)
        status = 1
    try:
        emitter.close()
    except (AuditWriteError, RuntimeError):
        LOG.error("the shutdown event could not be written")
        status = 1
    return status


def check(environ: Mapping[str, str]) -> int:
    """Check that a record could be written right now; write nothing.

    Args:
        environ: The process environment.

    Returns:
        0 when the version, ``AUDIT_REQUIRE_PURPOSE`` and the audit stream are
        usable, else 1.
    """
    try:
        read_version(version_file(environ))
        required_actions(environ)
        stream = open_audit_stream(environ.get("AUDIT_OUTPUT"))
    except (OSError, ValueError) as exc:
        return _refuse(f"cannot record events ({exc})", 1)
    if stream is not None:
        stream.close()
    return 0


def verify(lines: Iterable[str], out: TextIO) -> int:
    """Check the hash chains of the audit lines among ``lines``.

    Args:
        lines: Log lines; only those carrying the audit marker are read.
        out: Where the summary goes.

    Returns:
        0 when there were events and every chain verified, else 1.
    """
    marker = f'"stream":"{STREAM_MARKER}"'
    events: list[object] = []
    for line in lines:
        if marker not in line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append(line)  # reported as a schema problem
    report = verify_chain(events)
    print(f"{report.events} audit events in {len(report.heads)} chains, {len(report.problems)} problems", file=out)
    for problem in report.problems:
        print(f"  {problem.kind}: {problem.service} {problem.instance} seq {problem.seq}: {problem.detail}", file=out)
    return 0 if report.events and report.ok else 1


def _install_stop_handlers(stop: threading.Event) -> None:
    """End ``serve`` cleanly on SIGTERM (``docker stop``) and SIGINT.

    As PID 1, the process would otherwise ignore SIGTERM.

    Args:
        stop: The event ``serve`` waits on.
    """

    def handler(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a subcommand.

    Args:
        argv: The arguments, without the program name.

    Returns:
        The exit status.
    """
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="admin_audit.py", description="data-plane audit records (README: Audit trail)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="keep the service up until stopped")
    commands.add_parser("check", help="exit 0 when a record could be written now")
    commands.add_parser("verify", help="check the hash chains of the audit lines on stdin")
    for name, help_text in (("begin", "admit an operation before it runs"), ("end", "record an operation that ran")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("kind", choices=sorted(KIND_ACTIONS))
        sub.add_argument("store", nargs="?", help=f"{', '.join(STORES)}; none for ports")
        sub.add_argument("pairs", nargs="*", metavar="KEY=VALUE", help="the operation's parameters and facts")
        if name == "end":
            sub.add_argument("--status", type=int, required=True, help="the operation's exit status")
            sub.add_argument("--duration-s", type=int, help="how long it ran, in seconds")
            sub.add_argument("--opened", help="the opening's event id, for a shell or the admin ports")
    args = parser.parse_args(argv)
    command: str = args.command
    if command == "serve":
        stop = threading.Event()
        _install_stop_handlers(stop)
        return serve(os.environ, stop)
    if command == "check":
        return check(os.environ)
    if command == "verify":
        return verify(sys.stdin, sys.stdout)
    kind: str = args.kind
    store: str | None = args.store
    pairs: list[str] = args.pairs
    if kind == "ports" and store is not None:
        store, pairs = None, [store, *pairs]  # the admin ports have no store: it is a pair
    if command == "begin":
        return begin(os.environ, kind, store, pairs, sys.stdout)
    status: int = args.status
    duration_s: int | None = args.duration_s
    opened: str | None = args.opened
    return end(os.environ, kind, store, pairs, status=status, duration_s=duration_s, opened=opened)


if __name__ == "__main__":
    sys.exit(main())

"""Runtime glue between an emitting app and the federation audit library.

Canonical copy: ``nos-tromo/.github`` ``configs/audit/audit_runtime.py``. It is
vendored verbatim next to ``audit_event.py`` and drift-checked in CI
(``scripts/validate_audit_lib.py``), so never edit a vendored copy.

``audit_event.py`` defines what an audit line is. This module holds what every
emitting app must do the same way around it:

* where the lines go (:func:`open_audit_stream`). In a container, every
  process writes its chain to the container's main stdout under one
  container-wide lock, so a command started with ``docker compose exec`` lands
  in the long-running container's log, and lines from concurrent writers never
  interleave, however long they are;
* how a request states its purpose (:func:`parse_purpose_header`): the wire
  format the shared frontend component sends, and its inverse
  :func:`encode_purpose`;
* how a command-line run states it (:func:`purpose_from_env`);
* which actions need a stated purpose (:func:`parse_required_actions`);
* which correlation ids are kept (:func:`clean_request_id`);
* how a long id list is split instead of truncated (:func:`id_chunks`).

It imports nothing from ``audit_event.py``, so it works under any package
layout; callers combine the two. Stdlib only; Python 3.11+; the container
stream needs a POSIX system.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import time
import urllib.parse
from collections.abc import Collection, Mapping, Sequence
from typing import Final, TextIO, cast

#: Request header carrying the caller's stated purpose.
PURPOSE_HEADER: Final = "X-Audit-Purpose"
#: Request header carrying the gateway's correlation id.
REQUEST_ID_HEADER: Final = "X-Request-ID"
#: Response status for "this action needs a stated purpose" (Precondition Required).
PURPOSE_REQUIRED_STATUS: Final = 428
#: Longest accepted purpose header value, in characters.
MAX_PURPOSE_HEADER: Final = 4096
#: Longest accepted purpose field once decoded. Equal to ``audit_event.MAX_TEXT``,
#: so an accepted value is never cut when it is recorded.
MAX_PURPOSE_FIELD: Final = 256
#: Ids per event. Equal to ``audit_event.MAX_IDS``.
MAX_IDS_PER_EVENT: Final = 200
#: The purpose fields, in wire order. Other keys are ignored, so the format can grow.
PURPOSE_FIELDS: Final = ("case_ref", "reason")
#: The environment variables a command-line run reads its purpose from.
PURPOSE_ENV: Final = {"case_ref": "AUDIT_CASE_REF", "reason": "AUDIT_REASON"}

#: The container's main stdout, which the container runtime ships to the log collector.
CONTAINER_STDOUT: Final = "/proc/1/fd/1"
#: The lock every audit writer in a container takes around each write. It lives
#: on the container's own private ``/tmp``, shared by its processes on purpose.
CONTAINER_LOCK: Final = "/tmp/audit-stream.lock"
#: How long a write waits for the lock before it fails, in seconds.
LOCK_TIMEOUT_S: Final = 10.0

_MAX_HEADER_FIELDS: Final = 16
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# C0 and C1 controls, plus the two Unicode line separators line-based readers split on.
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class PurposeError(ValueError):
    """Raised for a stated purpose that is malformed, oversized or ambiguous."""


def _clean_field(name: str, value: str | None) -> str | None:
    """Validate one decoded purpose field.

    Args:
        name: The field name, for the error message.
        value: The decoded value, or ``None`` when absent.

    Returns:
        The value without surrounding whitespace, or ``None`` when it is
        absent or blank.

    Raises:
        PurposeError: If the value contains control characters or is longer
            than :data:`MAX_PURPOSE_FIELD`.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _CONTROL.search(text):
        raise PurposeError(f"purpose field {name!r} contains control characters")
    if len(text) > MAX_PURPOSE_FIELD:
        raise PurposeError(f"purpose field {name!r} is longer than {MAX_PURPOSE_FIELD} characters")
    return text


def parse_purpose_header(value: str | None) -> tuple[str | None, str | None]:
    """Decode an ``X-Audit-Purpose`` header value.

    The value is the ``application/x-www-form-urlencoded`` serialization of up
    to two fields, ``case_ref`` and ``reason``: what a browser's
    ``new URLSearchParams({case_ref, reason}).toString()`` produces, e.g.
    ``case_ref=AZ-12&reason=Pr%C3%BCfung+des+Hinweises``. Fields are UTF-8 and
    trimmed; a blank field counts as absent; other keys are ignored.

    Args:
        value: The raw header value, or ``None`` when the header is absent.

    Returns:
        ``(case_ref, reason)``, each ``None`` when not given. A purpose is
        stated when at least one of them is set.

    Raises:
        PurposeError: If the value is longer than :data:`MAX_PURPOSE_HEADER`,
            is not ASCII, is not valid form encoding of UTF-8, has more than
            16 fields or repeats a purpose field, or a field contains control
            characters or exceeds :data:`MAX_PURPOSE_FIELD`.
    """
    if value is None:
        return None, None
    if len(value) > MAX_PURPOSE_HEADER:
        raise PurposeError(f"purpose header is longer than {MAX_PURPOSE_HEADER} characters")
    if not value.isascii():
        raise PurposeError("purpose header must be ASCII; percent-encode its fields")
    if not value.strip():
        return None, None
    try:
        pairs = urllib.parse.parse_qsl(
            value.strip(),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_HEADER_FIELDS,
        )
    except ValueError as exc:  # also UnicodeDecodeError, a ValueError subclass
        raise PurposeError("purpose header is not form-encoded UTF-8") from exc
    found: dict[str, str] = {}
    for key, item in pairs:
        if key not in PURPOSE_FIELDS:
            continue
        if key in found:
            raise PurposeError(f"purpose field {key!r} is repeated")
        found[key] = item
    return _clean_field("case_ref", found.get("case_ref")), _clean_field("reason", found.get("reason"))


def encode_purpose(case_ref: str | None, reason: str | None) -> str:
    """Encode a purpose the way :func:`parse_purpose_header` reads it.

    Args:
        case_ref: A case or file reference, or ``None``.
        reason: A short justification, or ``None``.

    Returns:
        The header value; empty when neither field is set.
    """
    pairs = [(name, text) for name, text in zip(PURPOSE_FIELDS, (case_ref, reason), strict=True) if text]
    return urllib.parse.urlencode(pairs)


def purpose_from_env(environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Read a command-line run's purpose from :data:`PURPOSE_ENV`.

    Args:
        environ: The process environment, usually ``os.environ``.

    Returns:
        ``(case_ref, reason)``, each ``None`` when unset or blank.

    Raises:
        PurposeError: If a value contains control characters or exceeds
            :data:`MAX_PURPOSE_FIELD`.
    """
    return (
        _clean_field("case_ref", environ.get(PURPOSE_ENV["case_ref"])),
        _clean_field("reason", environ.get(PURPOSE_ENV["reason"])),
    )


def parse_required_actions(value: str | None, *, known: Collection[str]) -> frozenset[str]:
    """Parse ``AUDIT_REQUIRE_PURPOSE``: the actions that need a stated purpose.

    Args:
        value: A comma-separated list such as ``"query, disclose"``; ``None``
            or blank for none.
        known: The action names a caller may list, usually
            ``audit_event.ACTIONS - {"lifecycle"}``.

    Returns:
        The listed actions, lowercased.

    Raises:
        ValueError: If an entry is not in ``known``. Call this at startup, so
            a misspelt action stops the service instead of switching a
            requirement off.
    """
    if value is None:
        return frozenset()
    names = {part.strip().lower() for part in value.split(",")} - {""}
    unknown = sorted(names - set(known))
    if unknown:
        raise ValueError(f"unknown audit action(s) in AUDIT_REQUIRE_PURPOSE: {', '.join(unknown)}")
    return frozenset(names)


def clean_request_id(value: str | None) -> str | None:
    """Keep a correlation id only if it looks like one.

    Args:
        value: The raw ``X-Request-ID`` header value, or ``None``.

    Returns:
        The value when it is 1 to 128 characters of letters, digits and
        ``._:-``, starting with a letter or digit; otherwise ``None``.
    """
    if value is None or _REQUEST_ID.fullmatch(value) is None:
        return None
    return value


def id_chunks(ids: Sequence[str], size: int = MAX_IDS_PER_EVENT) -> list[list[str]]:
    """Split an id list into chunks that fit one event each.

    Record the first chunk on the operation's event and each further chunk on
    a continuation event pointing at it, instead of letting the library cut
    the list.

    Args:
        ids: The identifiers to record.
        size: The most ids per chunk.

    Returns:
        The chunks in order; empty when ``ids`` is empty.

    Raises:
        ValueError: If ``size`` is less than 1.
    """
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    return [list(ids[start : start + size]) for start in range(0, len(ids), size)]


class ContainerAuditStream:
    """Writes to the container's main stdout, one whole write at a time.

    The audit emitter writes each event with one ``write()`` followed by one
    ``flush()``, which is all it needs of a stream. This one collects the text
    until ``flush()`` and then writes all of it under an exclusive ``flock`` on
    :data:`CONTAINER_LOCK`, which every audit writer in the container takes. A
    process started with ``docker compose exec`` thereby writes into the
    long-running container's log, and a line longer than a pipe's atomic write
    size cannot interleave with another writer's.

    The lock file must be a regular file of this user (or the process must run
    as root), not a symlink and writable by nobody else; a write that cannot
    get the lock within ``lock_timeout_s`` fails instead of blocking for good.

    Open it after any ``fork``: a child shares its parent's lock and would not
    be excluded by it.
    """

    def __init__(
        self,
        path: str = CONTAINER_STDOUT,
        lock_path: str = CONTAINER_LOCK,
        *,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
    ) -> None:
        """Open the target and the lock file.

        Args:
            path: Where lines go; the container's main stdout by default.
            lock_path: The lock file shared by every writer to ``path``.
            lock_timeout_s: How long :meth:`flush` waits for the lock.

        Raises:
            OSError: If either cannot be opened, e.g. because PID 1 belongs to
                another user, or ``lock_path`` is a symlink. Audited work must
                not start then.
            PermissionError: If ``lock_path`` is not a regular file owned by
                this user (or root runs this), or others can write to it.
        """
        self._pending = bytearray()
        self._closed = False
        self._lock_timeout_s = lock_timeout_s
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
        try:
            self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError:
            os.close(self._fd)
            raise
        info = os.fstat(self._lock_fd)
        euid = os.geteuid()
        if not stat.S_ISREG(info.st_mode) or (info.st_uid != euid and euid != 0) or info.st_mode & 0o022:
            os.close(self._fd)
            os.close(self._lock_fd)
            raise PermissionError(f"refusing audit stream lock {lock_path!r}: not a private regular file")

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run.

        Returns:
            ``True`` once the stream is closed.
        """
        return self._closed

    def write(self, s: str) -> int:
        """Collect text until the next :meth:`flush`.

        Args:
            s: The text to write.

        Returns:
            The number of characters accepted.

        Raises:
            ValueError: If the stream is closed.
        """
        if self._closed:
            raise ValueError("I/O operation on a closed audit stream")
        self._pending += s.encode("utf-8")
        return len(s)

    def flush(self) -> None:
        """Write the collected text under the container-wide lock.

        Raises:
            TimeoutError: If the lock was not free within ``lock_timeout_s``.
            OSError: If the write fails. In both cases the collected text is
                dropped, so a later write never repeats part of a line.
        """
        if self._closed or not self._pending:
            return
        data = bytes(self._pending)
        self._pending.clear()
        self._acquire()
        try:
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _acquire(self) -> None:
        """Take the container-wide lock, waiting at most ``lock_timeout_s``.

        Raises:
            TimeoutError: If another writer held the lock for that long.
        """
        deadline = time.monotonic() + self._lock_timeout_s
        delay = 0.001
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"audit stream lock not acquired within {self._lock_timeout_s} s") from None
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
            else:
                return

    def close(self) -> None:
        """Flush, then release both file descriptors; later calls do nothing."""
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True
            os.close(self._fd)
            os.close(self._lock_fd)


def open_audit_stream(
    output: str | None,
    *,
    path: str = CONTAINER_STDOUT,
    lock_path: str = CONTAINER_LOCK,
) -> TextIO | None:
    """Return the stream an emitter writes to, as ``AUDIT_OUTPUT`` selects it.

    Args:
        output: ``None``, blank or ``"stdout"`` for the process's own stdout;
            ``"container"`` for the container's main stdout under the
            container-wide lock (:class:`ContainerAuditStream`).
        path: The container stream's target, for tests.
        lock_path: The container stream's lock file, for tests.

    Returns:
        ``None`` for stdout (the emitter's default), else the container stream.

    Raises:
        ValueError: If ``output`` names neither mode.
        OSError: If the container stream cannot be opened.
    """
    mode = (output or "").strip().lower() or "stdout"
    if mode == "stdout":
        return None
    if mode == "container":
        return cast("TextIO", ContainerAuditStream(path, lock_path))
    raise ValueError(f"unknown AUDIT_OUTPUT {output!r}: expected 'stdout' or 'container'")

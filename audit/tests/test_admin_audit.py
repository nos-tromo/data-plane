"""Tests for admin_audit.py.

Run from the repo root: ``python3 -m unittest discover -s audit/tests -t audit``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any, override
from unittest import mock

import admin_audit
from audit_event import verify_chain

VERSION = "0.4.0"
ENVELOPE = frozenset(
    {"stream", "v", "event_id", "ts", "service", "service_version", "instance", "seq", "prev_hash", "hash"}
    | {"action", "operation", "outcome", "actor"}
)
OPERATOR = {"user": "ops.example", "via": "cli"}
PURPOSE = {"case_ref": "case-0001", "reason": "verify a reported duplicate"}
DUMP = "neo4j-20260131T020000Z.dump"


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse everything an emitter wrote.

    Args:
        stream: The captured stream.

    Returns:
        The events.
    """
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the events that are not the chains' own lifecycle.

    Args:
        events: Parsed events.

    Returns:
        The records, in order.
    """
    return [event for event in events if event["action"] != "lifecycle"]


class Workspace(unittest.TestCase):
    """A version file, captured streams and operator messages per test."""

    @override
    def setUp(self) -> None:
        """Create the temporary paths."""
        self.tmp = Path(tempfile.mkdtemp(prefix="admin-audit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.version_file = self.tmp / "VERSION"
        self.version_file.write_text(f"{VERSION}\n", encoding="utf-8")
        self.streams: list[io.StringIO] = []
        self.stderr = io.StringIO()

    def environ(self, **extra: str) -> dict[str, str]:
        """Build the environment of a run by the operator, without a purpose.

        Args:
            **extra: More variables.

        Returns:
            The environment.
        """
        return {"AUDIT_VERSION_FILE": str(self.version_file), "AUDIT_CLI_USER": "ops.example", **extra}

    def purposed(self, **extra: str) -> dict[str, str]:
        """Build the environment of a run with a stated purpose.

        Args:
            **extra: More variables.

        Returns:
            The environment.
        """
        return self.environ(AUDIT_CASE_REF=PURPOSE["case_ref"], AUDIT_REASON=PURPOSE["reason"], **extra)

    @contextlib.contextmanager
    def captured(self) -> Generator[io.StringIO]:
        """Capture what the emitter writes to stdout, and the operator messages.

        Yields:
            The captured stream.
        """
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(self.stderr):
            yield stream
        self.streams.append(stream)

    def begin(
        self, environ: dict[str, str], kind: str, store: str, *pairs: str
    ) -> tuple[int, str, list[dict[str, Any]]]:
        """Run ``begin``.

        Args:
            environ: The environment.
            kind: The operation's kind.
            store: Its store.
            *pairs: Its parameters.

        Returns:
            The exit status, what it printed for the wrapper, and the events.
        """
        out = io.StringIO()
        with self.captured() as stream:
            status = admin_audit.begin(environ, kind, store, list(pairs), out)
        return status, out.getvalue().strip(), lines(stream)

    def end(
        self,
        environ: dict[str, str],
        kind: str,
        store: str,
        *pairs: str,
        status: int = 0,
        duration_s: int | None = 7,
        opened: str | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Run ``end``.

        Args:
            environ: The environment.
            kind: The operation's kind.
            store: Its store.
            *pairs: Its parameters and facts.
            status: The operation's exit status.
            duration_s: How long it ran.
            opened: A shell's opening event id.

        Returns:
            The exit status and the events.
        """
        with self.captured() as stream:
            code = admin_audit.end(
                environ, kind, store, list(pairs), status=status, duration_s=duration_s, opened=opened
            )
        return code, lines(stream)

    def assert_chains_intact(self) -> None:
        """Assert every captured chain verifies and ended with its shutdown."""
        events = [event for stream in self.streams for event in lines(stream)]
        report = verify_chain(events)
        self.assertEqual(report.problems, [])
        for head in report.heads.values():
            self.assertTrue(head.ended, head)


class HelperTest(unittest.TestCase):
    """The pure helpers."""

    def test_whole_number(self) -> None:
        """Sizes, counts and durations are plain decimal numbers."""
        self.assertEqual(admin_audit.whole_number("0"), 0)
        self.assertEqual(admin_audit.whole_number("2048"), 2048)
        for bad in ("", "-1", "1.5", "1e3", " 1", chr(0x0661), "1" * 19):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.whole_number(bad)

    def test_file_name(self) -> None:
        """Only a base name is accepted: a path could name a user."""
        for good in (DUMP, "docs-1714953600-2026-01-31-02-00-00.snapshot", "a"):
            self.assertEqual(admin_audit.file_name(good), good)
        for bad in ("", ".hidden", "../neo4j.dump", "backup/neo4j.dump", "a b", "a:b", "x" * 256):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.file_name(bad)

    def test_store_name(self) -> None:
        """A store's own names are kept as given, unless unprintable."""
        for good in ("case files 2026", "Akten-" + chr(0xE4), "a=b"):
            self.assertEqual(admin_audit.store_name(good), good)
        for bad in ("", "a\nb", "a\x1fb", f"a{chr(0x2028)}b", "x" * 256):
            with self.assertRaises(ValueError, msg=repr(bad)):
                admin_audit.store_name(bad)

    def test_parse_pairs(self) -> None:
        """Each key is checked by its own parser; only the repeatable key repeats."""
        fields = admin_audit.FACTS[("backup", "qdrant")]
        values, ids = admin_audit.parse_pairs(
            ["collection=a", "snapshots=2", "collection=b=c", "size_bytes=10"], fields, repeatable="collection"
        )
        self.assertEqual(values, {"snapshots": 2, "size_bytes": 10})
        self.assertEqual(ids, ["a", "b=c"])
        for bad in (["snapshots"], ["Snapshots=1"], ["size_bytes=x"], ["file=a.dump"], ["failed=1", "failed=2"]):
            with self.assertRaises(ValueError, msg=str(bad)):
                admin_audit.parse_pairs(bad, fields, repeatable="collection")

    def test_read_version(self) -> None:
        """The VERSION file's content, trimmed; anything else is refused."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("0.4.0\n", encoding="utf-8")
            self.assertEqual(admin_audit.read_version(path), "0.4.0")
            path.write_text("\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                admin_audit.read_version(path)
            with self.assertRaises(ValueError):
                admin_audit.read_version(Path(tmp) / "missing")

    def test_required_actions(self) -> None:
        """AUDIT_REQUIRE_PURPOSE lists actions; an unknown one is an error, never ignored."""
        self.assertEqual(admin_audit.required_actions({}), frozenset())
        self.assertEqual(admin_audit.required_actions({"AUDIT_REQUIRE_PURPOSE": ""}), frozenset())
        self.assertEqual(
            admin_audit.required_actions({"AUDIT_REQUIRE_PURPOSE": "Query, disclose"}), {"query", "disclose"}
        )
        for bad in ("lifecycle", "disclosure", "query,bogus"):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.required_actions({"AUDIT_REQUIRE_PURPOSE": bad})

    def test_purpose_rules(self) -> None:
        """A shell always needs a purpose; a backup as disclose, a restore as modify."""
        none: frozenset[str] = frozenset()
        self.assertTrue(admin_audit.purpose_required("shell", none))
        self.assertFalse(admin_audit.purpose_required("backup", none))
        self.assertTrue(admin_audit.purpose_required("backup", frozenset({"disclose"})))
        self.assertFalse(admin_audit.purpose_required("restore", frozenset({"query", "disclose"})))
        self.assertTrue(admin_audit.purpose_required("restore", frozenset({"modify"})))

    def test_operation_names(self) -> None:
        """The operations the records carry."""
        self.assertEqual(admin_audit.operation_of("shell", "neo4j"), "db.shell.open")
        self.assertEqual(admin_audit.operation_of("backup", "qdrant"), "backup.qdrant")
        self.assertEqual(admin_audit.operation_of("restore", "neo4j"), "restore.neo4j")


class BeginTest(Workspace):
    """Admission before an operation runs."""

    def test_shell_opening_is_recorded(self) -> None:
        """One admin event before the session, by the operator, with the purpose."""
        status, opened, events = self.begin(self.purposed(), "shell", "neo4j")
        self.assertEqual(status, 0)
        self.assertEqual([event["operation"] for event in events], ["start", "db.shell.open", "shutdown"])
        (event,) = records(events)
        self.assertEqual(event["event_id"], opened)
        self.assertEqual(event["service"], "data-plane")
        self.assertEqual(event["service_version"], VERSION)
        self.assertEqual((event["action"], event["outcome"]), ("admin", "ok"))
        self.assertEqual(event["actor"], OPERATOR)
        self.assertEqual(event["purpose"], PURPOSE)
        self.assertEqual(event["target"], {"kind": "store", "ids": ["neo4j"], "count": 1})
        self.assertEqual(event["criteria"], {"store": "neo4j"})
        self.assertEqual(set(event), ENVELOPE | {"purpose", "target", "criteria"})
        self.assertIn("recorded admin db.shell.open", self.stderr.getvalue())
        self.assert_chains_intact()

    def test_either_purpose_field_is_enough(self) -> None:
        """A case reference alone, or a reason alone, states a purpose."""
        for extra in ({"AUDIT_CASE_REF": "case-0002"}, {"AUDIT_REASON": "monthly review"}):
            status, _, events = self.begin(self.environ(**extra), "shell", "qdrant")
            self.assertEqual(status, 0, extra)
            (event,) = records(events)
            self.assertEqual(event["outcome"], "ok")
            self.assertEqual(
                event["purpose"], {key.removeprefix("AUDIT_").lower(): value for key, value in extra.items()}
            )
        self.assert_chains_intact()

    def test_shell_without_purpose_is_refused_and_recorded(self) -> None:
        """A shell needs a purpose even when AUDIT_REQUIRE_PURPOSE lists nothing."""
        status, opened, events = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE=""), "shell", "qdrant")
        self.assertEqual(status, 2)
        self.assertEqual(opened, "")
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("admin", "db.shell.open", "denied"))
        self.assertEqual(event["actor"], OPERATOR)
        self.assertEqual(event["criteria"], {"store": "qdrant"})
        self.assertEqual(set(event), ENVELOPE | {"criteria"})
        self.assertIn("needs a stated purpose", self.stderr.getvalue())
        self.assert_chains_intact()

    def test_backup_follows_require_purpose(self) -> None:
        """Under query,disclose a backup without a purpose is refused; without the knob it may run."""
        status, _, events = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE="query,disclose"), "backup", "neo4j")
        self.assertEqual(status, 2)
        (event,) = records(events)
        self.assertEqual(
            (event["action"], event["operation"], event["outcome"]), ("disclose", "backup.neo4j", "denied")
        )
        self.assertEqual(set(event), ENVELOPE)
        for environ in (self.environ(AUDIT_REQUIRE_PURPOSE=""), self.purposed(AUDIT_REQUIRE_PURPOSE="query,disclose")):
            status, printed, events = self.begin(environ, "backup", "qdrant")
            self.assertEqual((status, printed, events), (0, "", []))
        self.assert_chains_intact()

    def test_restore_follows_require_purpose(self) -> None:
        """A restore is a modify: refused only when AUDIT_REQUIRE_PURPOSE lists it."""
        status, _, events = self.begin(
            self.environ(AUDIT_REQUIRE_PURPOSE="query,disclose"), "restore", "neo4j", f"file={DUMP}"
        )
        self.assertEqual((status, events), (0, []))
        status, _, events = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE="modify"), "restore", "neo4j", f"file={DUMP}")
        self.assertEqual(status, 2)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("modify", "restore.neo4j", "denied"))
        self.assertEqual(event["criteria"], {"file": DUMP})
        self.assertNotIn("target", event)
        self.assert_chains_intact()

    def test_malformed_purpose_writes_nothing(self) -> None:
        """A purpose with control characters or over 256 characters is refused unrecorded."""
        for extra in ({"AUDIT_REASON": "line\nbreak"}, {"AUDIT_CASE_REF": "x" * 257}):
            status, _, events = self.begin(self.environ(**extra), "shell", "neo4j")
            self.assertEqual((status, events), (2, []), extra)
        self.assertIn("not accepted", self.stderr.getvalue())

    def test_bad_arguments_write_nothing(self) -> None:
        """Unknown operations, missing or malformed parameters: exit 2, no event."""
        bad = [
            ("vacuum", "neo4j"),
            ("shell", "postgres"),
            ("restore", "neo4j"),
            ("restore", "neo4j", "file=../neo4j.dump"),
            ("restore", "qdrant", "collection=docs"),
            ("backup", "neo4j", f"file={DUMP}"),
            ("shell", "neo4j", "store=qdrant"),
        ]
        for args in bad:
            status, _, events = self.begin(self.purposed(), *args)
            self.assertEqual((status, events), (2, []), args)

    def test_unrecordable_operation_is_refused(self) -> None:
        """No stream, no version or a bad AUDIT_REQUIRE_PURPOSE: exit 1, nothing runs."""
        environs = [
            self.purposed(AUDIT_OUTPUT="nowhere"),
            self.purposed(AUDIT_REQUIRE_PURPOSE="query,bogus"),
            {**self.purposed(), "AUDIT_VERSION_FILE": str(self.tmp / "missing")},
        ]
        for environ in environs:
            for kind in ("shell", "backup"):
                status, _, events = self.begin(environ, kind, "neo4j")
                self.assertEqual((status, events), (1, []), (environ, kind))


class EndTest(Workspace):
    """The record of an operation that ran."""

    def test_shell_close_points_at_its_opening(self) -> None:
        """The close carries the duration and exit status, linked to the opening."""
        _, opened, _ = self.begin(self.purposed(), "shell", "neo4j")
        status, events = self.end(self.purposed(), "shell", "neo4j", duration_s=42, opened=opened)
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("admin", "db.shell.close", "ok"))
        self.assertEqual(event["parent_event_id"], opened)
        self.assertEqual(event["actor"], OPERATOR)
        self.assertEqual(event["purpose"], PURPOSE)
        self.assertEqual(event["target"], {"kind": "store", "ids": ["neo4j"], "count": 1})
        self.assertEqual(event["criteria"], {"store": "neo4j", "exit_status": 0, "duration_s": 42})
        self.assertEqual(set(event), ENVELOPE | {"parent_event_id", "purpose", "target", "criteria"})
        self.assert_chains_intact()

    def test_abnormal_session_end_is_an_error(self) -> None:
        """An interrupted or failed session is recorded as error with its status."""
        _, opened, _ = self.begin(self.purposed(), "shell", "qdrant")
        _, events = self.end(self.purposed(), "shell", "qdrant", status=130, opened=opened)
        (event,) = records(events)
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["criteria"]["exit_status"], 130)
        self.assert_chains_intact()

    def test_shell_close_needs_its_opening(self) -> None:
        """No or a malformed opening id: exit 2, nothing written."""
        for opened in (None, "not-an-event-id"):
            status, events = self.end(self.purposed(), "shell", "neo4j", opened=opened)
            self.assertEqual((status, events), (2, []), opened)

    def test_neo4j_backup_is_a_disclosure_to_file_backup(self) -> None:
        """The dump's name and size, the database, the recipient."""
        status, events = self.end(self.purposed(), "backup", "neo4j", f"file={DUMP}", "size_bytes=22430")
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("disclose", "backup.neo4j", "ok"))
        self.assertEqual(event["recipient"], "file:backup")
        self.assertEqual(event["target"], {"kind": "database", "ids": ["neo4j"], "count": 1})
        self.assertEqual(event["criteria"], {"file": DUMP, "size_bytes": 22430, "exit_status": 0, "duration_s": 7})
        self.assertEqual(event["purpose"], PURPOSE)
        self.assertNotIn("parent_event_id", event)
        self.assert_chains_intact()

    def test_failed_backup_is_an_error(self) -> None:
        """A backup that failed is recorded with what is known."""
        _, events = self.end(self.environ(), "backup", "neo4j", status=1, duration_s=None)
        (event,) = records(events)
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["criteria"], {"exit_status": 1})
        self.assertNotIn("purpose", event)
        self.assert_chains_intact()

    def test_qdrant_backup_names_its_collections(self) -> None:
        """Every collection snapshotted is a target id; the totals are criteria."""
        facts = ["collection=docs", "collection=case files 2026", "snapshots=2", "size_bytes=4096", "failed=1"]
        _, events = self.end(self.purposed(), "backup", "qdrant", *facts, status=1)
        (event,) = records(events)
        self.assertEqual(event["target"], {"kind": "collection", "ids": ["docs", "case files 2026"], "count": 2})
        self.assertEqual(
            event["criteria"], {"snapshots": 2, "size_bytes": 4096, "failed": 1, "exit_status": 1, "duration_s": 7}
        )
        self.assertEqual((event["outcome"], event["recipient"]), ("error", "file:backup"))
        self.assert_chains_intact()

    def test_long_collection_list_continues_in_id_events(self) -> None:
        """More than 200 collections are not cut: continuation events carry the rest."""
        names = [f"collection-{n:03d}" for n in range(450)]
        _, events = self.end(self.purposed(), "backup", "qdrant", *[f"collection={name}" for name in names])
        primary, *more = records(events)
        self.assertEqual(primary["operation"], "backup.qdrant")
        self.assertEqual(primary["target"]["count"], 450)
        self.assertEqual(len(primary["target"]["ids"]), 200)
        self.assertNotIn("ids_truncated", primary["target"])
        self.assertEqual([event["operation"] for event in more], ["backup.qdrant.ids", "backup.qdrant.ids"])
        self.assertEqual({event["parent_event_id"] for event in more}, {primary["event_id"]})
        self.assertEqual([event["target"]["count"] for event in more], [200, 50])
        ids = [name for event in (primary, *more) for name in event["target"]["ids"]]
        self.assertEqual(ids, names)
        self.assert_chains_intact()

    def test_neo4j_restore_is_a_modification(self) -> None:
        """The dump loaded, by name and size; no recipient."""
        _, events = self.end(self.environ(), "restore", "neo4j", f"file={DUMP}", "size_bytes=22430")
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("modify", "restore.neo4j", "ok"))
        self.assertEqual(event["target"], {"kind": "database", "ids": ["neo4j"], "count": 1})
        self.assertEqual(event["criteria"], {"file": DUMP, "size_bytes": 22430, "exit_status": 0, "duration_s": 7})
        self.assertNotIn("recipient", event)
        self.assert_chains_intact()

    def test_qdrant_restore_names_its_collection(self) -> None:
        """The collection replaced is the target; the snapshot and its source are criteria."""
        pairs = ["collection=docs", "snapshot=docs-1-2026-01-31-02-00-00.snapshot", "source=host"]
        _, events = self.end(self.purposed(), "restore", "qdrant", *pairs)
        (event,) = records(events)
        self.assertEqual(event["target"], {"kind": "collection", "ids": ["docs"], "count": 1})
        self.assertEqual(
            event["criteria"],
            {
                "collection": "docs",
                "snapshot": "docs-1-2026-01-31-02-00-00.snapshot",
                "source": "host",
                "exit_status": 0,
                "duration_s": 7,
            },
        )
        self.assert_chains_intact()

    def test_bad_arguments_write_nothing(self) -> None:
        """Unknown or malformed facts, missing parameters: exit 2, no event."""
        bad = [
            ("restore", "neo4j", "size_bytes=10"),
            ("backup", "neo4j", "size_bytes=ten"),
            ("backup", "neo4j", "path=/srv/backup/neo4j.dump"),
            ("backup", "neo4j", f"file={DUMP}", f"file={DUMP}"),
            ("restore", "qdrant", "collection=docs", "snapshot=x.snapshot", "source=usb"),
        ]
        for args in bad:
            status, events = self.end(self.purposed(), *args)
            self.assertEqual((status, events), (2, []), args)
        for status, duration_s in ((-1, 1), (0, -1)):
            code, events = self.end(self.purposed(), "backup", "neo4j", status=status, duration_s=duration_s)
            self.assertEqual((code, events), (2, []))

    def test_unwritable_stream_fails_the_record(self) -> None:
        """Fail closed: no stream, or a write that fails, is exit 1."""

        class Broken(io.StringIO):
            """A stream whose writes fail like a full disk."""

            @override
            def write(self, s: str) -> int:
                raise OSError("disk full")

        code, events = self.end(self.purposed(AUDIT_OUTPUT="nowhere"), "backup", "neo4j")
        self.assertEqual((code, events), (1, []))
        with contextlib.redirect_stdout(Broken()), contextlib.redirect_stderr(self.stderr):
            code = admin_audit.end(self.purposed(), "backup", "neo4j", [], status=0)
        self.assertEqual(code, 1)
        self.assertIn("could not write the record", self.stderr.getvalue())


class ServeTest(Workspace):
    """The long-running service, check and verify."""

    def test_serve_records_its_configuration_until_stopped(self) -> None:
        """start, config.audit, shutdown: one chain, ended cleanly."""
        stop = threading.Event()
        result: list[int] = []
        environ = self.environ(AUDIT_REQUIRE_PURPOSE="query, disclose")
        with self.captured() as stream:
            worker = threading.Thread(target=lambda: result.append(admin_audit.serve(environ, stop)))
            worker.start()
            deadline = time.monotonic() + 10
            while len(stream.getvalue().splitlines()) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            stop.set()
            worker.join(10)
        self.assertEqual(result, [0])
        events = lines(stream)
        self.assertEqual([event["operation"] for event in events], ["start", "config.audit", "shutdown"])
        self.assertEqual(events[1]["actor"], {"user": None, "via": "system"})
        self.assertEqual(
            events[1]["criteria"], {"require_purpose": ["disclose", "query"], "shells_require_purpose": True}
        )
        self.assert_chains_intact()

    def test_serve_refuses_bad_configuration(self) -> None:
        """An unknown action or a missing version stops startup (exit 2) unrecorded."""
        with self.captured() as stream, self.assertLogs("admin-audit", level="ERROR"):
            self.assertEqual(admin_audit.serve(self.environ(AUDIT_REQUIRE_PURPOSE="bogus"), threading.Event()), 2)
            self.version_file.unlink()
            self.assertEqual(admin_audit.serve(self.environ(), threading.Event()), 2)
        self.assertEqual(stream.getvalue(), "")

    def test_serve_without_a_stream_fails(self) -> None:
        """No audit stream, no service (exit 1)."""
        with self.captured() as stream, self.assertLogs("admin-audit", level="ERROR"):
            self.assertEqual(admin_audit.serve(self.environ(AUDIT_OUTPUT="nowhere"), threading.Event()), 1)
        self.assertEqual(stream.getvalue(), "")

    def test_check(self) -> None:
        """The check passes when a record could be written, and writes nothing."""
        with self.captured() as stream:
            self.assertEqual(admin_audit.check(self.environ()), 0)
            self.assertEqual(admin_audit.check(self.environ(AUDIT_OUTPUT="nowhere")), 1)
            self.assertEqual(admin_audit.check(self.environ(AUDIT_REQUIRE_PURPOSE="bogus")), 1)
            self.version_file.unlink()
            self.assertEqual(admin_audit.check(self.environ()), 1)
        self.assertEqual(stream.getvalue(), "")

    def test_verify(self) -> None:
        """Intact chains verify; an altered or removed line, or no events, fails."""
        _, _, _ = self.begin(self.purposed(), "shell", "neo4j")
        log = ["2026-01-31 02:00:00,000 INFO admin-audit: ready", *self.streams[0].getvalue().splitlines()]
        out = io.StringIO()
        self.assertEqual(admin_audit.verify(log, out), 0)
        self.assertEqual(out.getvalue(), "3 audit events in 1 chains, 0 problems\n")
        altered = [
            line.replace('"outcome":"ok"', '"outcome":"denied"', 1) if "db.shell.open" in line else line for line in log
        ]
        self.assertEqual(admin_audit.verify(altered, io.StringIO()), 1)
        self.assertEqual(admin_audit.verify([log[0], log[1], log[3]], io.StringIO()), 1)
        self.assertEqual(admin_audit.verify(["plain ops line"], io.StringIO()), 1)


class CommandLineTest(Workspace):
    """The argument order the wrapper script uses parses."""

    def run_main(self, environ: dict[str, str], argv: list[str]) -> tuple[int, list[dict[str, Any]], list[str]]:
        """Run ``main`` in a patched environment.

        Args:
            environ: The process environment.
            argv: The arguments.

        Returns:
            The exit status, the events, and the other lines on stdout.
        """
        stream = io.StringIO()
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(admin_audit.logging, "basicConfig"),
            contextlib.redirect_stdout(stream),
            contextlib.redirect_stderr(self.stderr),
        ):
            status = admin_audit.main(argv)
        audit = [line for line in stream.getvalue().splitlines() if line.startswith("{")]
        self.streams.append(io.StringIO("".join(f"{line}\n" for line in audit)))
        other = [line for line in stream.getvalue().splitlines() if not line.startswith("{")]
        return status, [json.loads(line) for line in audit], other

    def test_begin_and_end(self) -> None:
        """The opening's id printed by begin is taken back by end with --opened."""
        status, events, other = self.run_main(self.purposed(), ["begin", "shell", "neo4j"])
        self.assertEqual(status, 0)
        self.assertEqual(other, [records(events)[0]["event_id"]])
        argv = ["end", "shell", "neo4j", "--status", "0", "--duration-s", "5", "--opened", other[0]]
        status, events, _ = self.run_main(self.purposed(), argv)
        self.assertEqual(status, 0)
        self.assertEqual(records(events)[0]["parent_event_id"], other[0])
        argv = ["end", "backup", "neo4j", "--status", "0", "--duration-s", "3", f"file={DUMP}", "size_bytes=10"]
        status, events, _ = self.run_main(self.purposed(), argv)
        self.assertEqual((status, records(events)[0]["criteria"]["size_bytes"]), (0, 10))
        self.assert_chains_intact()

    def test_usage_errors_exit_2(self) -> None:
        """Unknown kinds and a missing status are usage errors."""
        for argv in (["begin", "vacuum", "neo4j"], ["end", "backup", "neo4j"]):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(self.purposed(), argv)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

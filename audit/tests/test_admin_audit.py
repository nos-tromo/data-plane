"""Tests for admin_audit.py and the scripts that run it.

Run from the repo root: ``python3 -m unittest discover -s audit/tests -t audit``.
The script tests need bash and the repo's ``scripts/``; they are skipped where
either is missing, e.g. in the pinned python image, which mounts only ``audit/``.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
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
AUDIT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = AUDIT_DIR.parent / "scripts"
ADMIN_OVERLAY = AUDIT_DIR.parent / "docker" / "compose.admin.yaml"
HAS_SCRIPTS = shutil.which("bash") is not None and (SCRIPTS / "audit-admin.sh").is_file()
ENVELOPE = frozenset(
    {"stream", "v", "event_id", "ts", "service", "service_version", "instance", "seq", "prev_hash", "hash"}
    | {"action", "operation", "outcome", "actor"}
)
OPERATOR = {"user": "ops.example", "via": "cli"}
PURPOSE = {"case_ref": "case-0001", "reason": "verify a reported duplicate"}
DUMP = "neo4j-20260131T020000Z.dump"
LOOPBACK = ["127.0.0.1:7474", "127.0.0.1:7687", "127.0.0.1:6333"]
BOTH_STORES = {"kind": "store", "ids": ["neo4j", "qdrant"], "count": 2}


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
        self, environ: dict[str, str], kind: str, store: str | None, *pairs: str
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
        store: str | None,
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
        self.assertTrue(admin_audit.purpose_required("ports", none))

    def test_operation_names(self) -> None:
        """The operations the records carry."""
        self.assertEqual(admin_audit.operation_of("shell", "neo4j"), "db.shell.open")
        self.assertEqual(admin_audit.operation_of("backup", "qdrant"), "backup.qdrant")
        self.assertEqual(admin_audit.operation_of("restore", "neo4j"), "restore.neo4j")
        self.assertEqual(admin_audit.operation_of("ports", None), "db.ports.open")
        self.assertEqual(admin_audit.OPENED_KINDS["ports"][1], "db.ports.close")


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

    def test_admin_ports_opening_is_recorded(self) -> None:
        """The opening of make up-admin: both stores, the loopback bindings, the purpose."""
        status, opened, events = self.begin(self.purposed(), "ports", None)
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual(event["event_id"], opened)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("admin", "db.ports.open", "ok"))
        self.assertEqual(event["actor"], OPERATOR)
        self.assertEqual(event["purpose"], PURPOSE)
        self.assertEqual(event["target"], BOTH_STORES)
        self.assertEqual(event["criteria"], {"ports": LOOPBACK})
        self.assert_chains_intact()

    def test_admin_ports_always_need_a_purpose(self) -> None:
        """Like a shell, whatever AUDIT_REQUIRE_PURPOSE says; refused with a denied event."""
        status, opened, events = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE=""), "ports", None)
        self.assertEqual((status, opened), (2, ""))
        (event,) = records(events)
        self.assertEqual((event["operation"], event["outcome"]), ("db.ports.open", "denied"))
        self.assertEqual(event["criteria"], {"ports": LOOPBACK})
        self.assertNotIn("target", event)
        status, _, events = self.begin(self.purposed(), "ports", "neo4j")
        self.assertEqual((status, events), (2, []))
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

    def test_admin_ports_close_points_at_the_opening(self) -> None:
        """The close names what ended the window and how long it was open."""
        _, opened, _ = self.begin(self.purposed(), "ports", None)
        status, events = self.end(self.environ(), "ports", None, "by=up", duration_s=600, opened=opened)
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("admin", "db.ports.close", "ok"))
        self.assertEqual(event["parent_event_id"], opened)
        self.assertEqual(event["target"], BOTH_STORES)
        self.assertEqual(event["criteria"], {"ports": LOOPBACK, "by": "up", "exit_status": 0, "duration_s": 600})
        self.assertNotIn("purpose", event)
        self.assert_chains_intact()

    def test_admin_ports_close_needs_its_opening_and_closer(self) -> None:
        """No opening id, no or an unknown closer: exit 2, nothing written."""
        _, opened, _ = self.begin(self.purposed(), "ports", None)
        for pairs, opening in ((("by=up",), None), ((), opened), (("by=reboot",), opened)):
            status, events = self.end(self.environ(), "ports", None, *pairs, opened=opening)
            self.assertEqual((status, events), (2, []), pairs)

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

    def test_admin_ports_take_no_store(self) -> None:
        """`begin ports` and `end ports ... by=up`: the first pair is not taken for a store."""
        status, events, other = self.run_main(self.purposed(), ["begin", "ports"])
        self.assertEqual(status, 0)
        argv = ["end", "ports", "--status", "0", "--duration-s", "9", "--opened", other[0], "by=down"]
        status, events, _ = self.run_main(self.environ(), argv)
        self.assertEqual(status, 0)
        self.assertEqual(records(events)[0]["criteria"]["by"], "down")
        self.assert_chains_intact()

    def test_usage_errors_exit_2(self) -> None:
        """Unknown kinds and a missing status are usage errors."""
        for argv in (["begin", "vacuum", "neo4j"], ["end", "backup", "neo4j"]):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(self.purposed(), argv)
            self.assertEqual(raised.exception.code, 2)


# A stand-in for `docker compose`, run by scripts/audit-admin.sh. For
# `exec ... admin-audit python /app/admin_audit.py ...` it runs the real module
# with the container's environment (FAKE_REQUIRE_PURPOSE) plus the -e
# variables, taken from its own environment like compose does. Audit lines go to
# the "container log" (as AUDIT_OUTPUT=container would put them); anything else
# on stdout to the caller. Like the docker CLI, it reads all of its stdin.
FAKE_COMPOSE = r"""#!/usr/bin/env python3
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write("compose " + " ".join(args) + "\n")
sys.stdin.read()
if "exec" not in args:
    if "ps" in args:
        sys.stdout.write(os.environ.get("FAKE_PUBLISHED", ""))
    sys.exit(0)
rest = args[args.index("exec") + 1 :]
env = {
    "PATH": os.environ["PATH"],
    "PYTHONDONTWRITEBYTECODE": "1",
    "AUDIT_VERSION_FILE": os.environ["FAKE_VERSION_FILE"],
    "AUDIT_REQUIRE_PURPOSE": os.environ.get("FAKE_REQUIRE_PURPOSE", "query,disclose"),
}
while rest[0].startswith("-"):
    if rest.pop(0) == "-e":
        name = rest.pop(0)
        if "=" in name:
            sys.exit("the purpose must not be passed on the command line: " + name)
        if name in os.environ:
            env[name] = os.environ[name]
service, program, script, *command = rest
if service != "admin-audit" or (program, script) != ("python", "/app/admin_audit.py"):
    sys.exit("unexpected exec: " + " ".join(rest))
if os.environ.get("FAKE_DOWN") or command[0] == os.environ.get("FAKE_FAIL"):
    sys.exit(1)
done = subprocess.run(
    [sys.executable, os.environ["FAKE_ADMIN_AUDIT"], *command], env=env, capture_output=True, text=True, check=False
)
with open(os.environ["FAKE_CONTAINER_LOG"], "a", encoding="utf-8") as container:
    for line in done.stdout.splitlines():
        if '"stream":"audit"' in line:
            container.write(line + "\n")
        else:
            print(line)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""


class ComposeStandIn(Workspace):
    """A workspace whose `docker compose` is FAKE_COMPOSE, logging every call."""

    @override
    def setUp(self) -> None:
        """Create the stand-in, its logs and the base environment."""
        super().setUp()
        self.log = self.tmp / "calls.log"
        self.container_log = self.tmp / "container.log"
        compose = self.tmp / "fake-compose"
        compose.write_text(FAKE_COMPOSE, encoding="utf-8")
        compose.chmod(0o755)
        self.base = {
            "PATH": os.environ["PATH"],
            "TMPDIR": str(self.tmp),
            "COMPOSE": f"{compose} --env-file .env -f docker/compose.yaml",
            "FAKE_LOG": str(self.log),
            "FAKE_CONTAINER_LOG": str(self.container_log),
            "FAKE_VERSION_FILE": str(self.version_file),
            "FAKE_ADMIN_AUDIT": str(AUDIT_DIR / "admin_audit.py"),
            "SUDO_USER": "ops.example",
        }

    def calls_and_events(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Read the stand-in's call log and the container log.

        Returns:
            The calls, in order, and the events in the container log.
        """
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        text = self.container_log.read_text(encoding="utf-8") if self.container_log.exists() else ""
        self.streams = [io.StringIO(text)]
        return calls, lines(self.streams[0])


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class WrapperScriptTest(ComposeStandIn):
    """scripts/audit-admin.sh around a command, with admin_audit.py behind a stand-in compose."""

    def run_wrapper(self, *args: str, stdin: str = "", **env: str) -> tuple[int, list[str], list[dict[str, Any]], str]:
        """Run the wrapper.

        Args:
            *args: Its arguments, ``--`` and the command included.
            stdin: What the wrapper reads on stdin.
            **env: More environment, e.g. ``REASON``, ``FAKE_DOWN``.

        Returns:
            The exit status, the calls in order, the events in the container
            log, and what the wrapper wrote to stderr.
        """
        result = subprocess.run(
            ["bash", str(SCRIPTS / "audit-admin.sh"), *args],
            env={**self.base, **env},
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        text = self.container_log.read_text(encoding="utf-8") if self.container_log.exists() else ""
        self.streams = [io.StringIO(text)]
        return result.returncode, calls, lines(self.streams[0]), result.stderr

    def test_shell_session_is_recorded_open_and_close(self) -> None:
        """begin, the session, end: the close points at the opening; both by the sudo user."""
        status, calls, events, _ = self.run_wrapper(
            "shell", "neo4j", "--", "sh", "-c", 'echo session >> "$FAKE_LOG"', CASE_REF="case-0001", REASON="review"
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn(" exec -T -e AUDIT_CLI_USER -e AUDIT_CASE_REF -e AUDIT_REASON admin-audit ", calls[0])
        self.assertTrue(calls[0].endswith("admin_audit.py begin shell neo4j"), calls[0])
        self.assertEqual(calls[1], "session")
        self.assertIn(" end shell neo4j --status 0 --duration-s ", calls[2])
        opening, close = records(events)
        self.assertEqual(opening["operation"], "db.shell.open")
        self.assertEqual(close["operation"], "db.shell.close")
        self.assertEqual(close["parent_event_id"], opening["event_id"])
        self.assertEqual(opening["actor"], OPERATOR)
        self.assertEqual(close["purpose"], {"case_ref": "case-0001", "reason": "review"})
        self.assertEqual(close["criteria"]["exit_status"], 0)
        self.assert_chains_intact()

    def test_refused_operation_runs_nothing(self) -> None:
        """No purpose for a shell: exit 2, the refusal recorded, the command never run."""
        status, calls, events, stderr = self.run_wrapper(
            "shell", "qdrant", "--", "sh", "-c", 'echo session >> "$FAKE_LOG"'
        )
        self.assertEqual(status, 2)
        self.assertNotIn("session", calls)
        (event,) = records(events)
        self.assertEqual(event["outcome"], "denied")
        self.assertIn("needs a stated purpose", stderr)
        self.assert_chains_intact()

    def test_nothing_runs_while_admin_audit_cannot_record(self) -> None:
        """admin-audit down: exit 1 before the command."""
        status, calls, events, stderr = self.run_wrapper(
            "backup", "neo4j", "--", "sh", "-c", 'echo dump >> "$FAKE_LOG"', REASON="nightly backup", FAKE_DOWN="1"
        )
        self.assertEqual((status, len(calls), events), (1, 1, []))
        self.assertIn("Nothing was done", stderr)

    def test_reported_facts_reach_the_record(self) -> None:
        """What the command writes to AUDIT_FACTS is recorded; its status is passed on."""
        command = f'printf "file={DUMP}\\nsize_bytes=2048\\n" >> "$AUDIT_FACTS"; exit 3'
        status, _, events, _ = self.run_wrapper("backup", "neo4j", "--", "sh", "-c", command, REASON="nightly backup")
        self.assertEqual(status, 3)
        (event,) = records(events)
        self.assertEqual((event["operation"], event["outcome"]), ("backup.neo4j", "error"))
        self.assertEqual(event["criteria"]["file"], DUMP)
        self.assertEqual(event["criteria"]["size_bytes"], 2048)
        self.assertEqual(event["criteria"]["exit_status"], 3)
        self.assertEqual(event["purpose"], {"reason": "nightly backup"})
        self.assertFalse(list(self.tmp.glob("data-plane-audit.*")), "the facts file is removed")
        self.assert_chains_intact()

    def test_parameters_reach_both_calls(self) -> None:
        """A restore's parameters are checked at begin and recorded at end."""
        status, calls, events, _ = self.run_wrapper(
            "restore", "neo4j", f"file={DUMP}", "--", "sh", "-c", 'echo "size_bytes=10" >> "$AUDIT_FACTS"'
        )
        self.assertEqual(status, 0)
        self.assertTrue(calls[0].endswith(f"begin restore neo4j file={DUMP}"), calls[0])
        (event,) = records(events)
        self.assertEqual(event["criteria"], {"file": DUMP, "size_bytes": 10, "exit_status": 0, "duration_s": 0})
        self.assert_chains_intact()

    def test_piped_input_reaches_the_command(self) -> None:
        """The records read no stdin, so a piped session gets all of it."""
        status, calls, _, _ = self.run_wrapper(
            "shell",
            "neo4j",
            "--",
            "sh",
            "-c",
            'cat >> "$FAKE_LOG"',
            stdin="MATCH (n) RETURN count(n);\n",
            REASON="count",
        )
        self.assertEqual(status, 0)
        self.assertIn("MATCH (n) RETURN count(n);", calls)

    def test_interrupted_operation_is_recorded(self) -> None:
        """An interrupt during the command is recorded as error, status 130."""
        status, _, events, _ = self.run_wrapper("shell", "neo4j", "--", "sh", "-c", "kill -INT $PPID", REASON="review")
        self.assertEqual(status, 130)
        close = records(events)[-1]
        self.assertEqual((close["operation"], close["outcome"]), ("db.shell.close", "error"))
        self.assertEqual(close["criteria"]["exit_status"], 130)
        self.assert_chains_intact()

    def test_unrecorded_operation_fails_loudly(self) -> None:
        """When the final record cannot be written, the wrapper fails after a good command."""
        status, calls, _, stderr = self.run_wrapper(
            "backup", "qdrant", "--", "sh", "-c", 'echo snapshot >> "$FAKE_LOG"', REASON="nightly", FAKE_FAIL="end"
        )
        self.assertEqual(status, 1)
        self.assertEqual(calls[1], "snapshot")
        self.assertIn("could not be written", stderr)

    def test_usage(self) -> None:
        """Too few arguments or no command: exit 2, nothing called."""
        for args in (("shell",), ("shell", "neo4j", "--"), ("shell", "neo4j", "true")):
            status, calls, _, _ = self.run_wrapper(*args)
            self.assertEqual((status, calls), (2, []), args)


# A stand-in for `docker compose` for the store scripts: logs every call and
# plays the part of neo4j-admin and of the Qdrant container.
FAKE_STORE_COMPOSE = r"""#!/usr/bin/env bash
call="$*"
printf 'compose %s\n' "${call//$'\n'/\\n}" >> "$FAKE_LOG"
args=("$@")
case " $* " in
  *" stop neo4j "*) exit "${FAKE_STOP:-0}" ;;
  *" start neo4j "*) exit "${FAKE_START:-0}" ;;
  *" run --rm "*)
    for ((i = 0; i < ${#args[@]}; i++)); do
      [[ ${args[i]} == -v ]] && mount=${args[i + 1]}
      [[ ${args[i]} == -c ]] && command=${args[i + 1]}
    done
    if [[ ${FAKE_RUN:-0} == 0 && $command == *"database dump"* ]]; then
      head -c 1234 /dev/zero > "${mount%%:*}/${command##*/backup/}"
    fi
    exit "${FAKE_RUN:-0}" ;;
  *" sh -c command -v bash"*) exit "${FAKE_NO_BASH:-0}" ;;
  *" bash -s "*) cat > /dev/null; printf 'collection=docs\nsnapshots=1\nsize_bytes=2048\nfailed=0\n' ;;
  *" sh -c "*"cat >"*) cat > "${FAKE_LOG%/*}/streamed" ;;
esac
"""


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class StoreScriptTest(unittest.TestCase):
    """scripts/neo4j_admin.sh and scripts/qdrant_admin.sh with a stand-in compose."""

    @override
    def setUp(self) -> None:
        """Create the stand-in, a backup directory and the facts file."""
        self.tmp = Path(tempfile.mkdtemp(prefix="admin-audit-store-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"
        self.facts = self.tmp / "facts"
        self.backup_dir = self.tmp / "snapshots"
        compose = self.tmp / "fake-compose"
        compose.write_text(FAKE_STORE_COMPOSE, encoding="utf-8")
        compose.chmod(0o755)
        self.compose = f"{compose} --env-file .env -f docker/compose.yaml"

    def run_script(self, script: str, *args: str, **env: str) -> tuple[int, list[str], list[str]]:
        """Run a store script.

        Args:
            script: ``neo4j_admin.sh`` or ``qdrant_admin.sh``.
            *args: Its arguments.
            **env: More environment.

        Returns:
            The exit status, the compose calls in order, and the facts reported.
        """
        result = subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            env={
                "PATH": os.environ["PATH"],
                "COMPOSE": self.compose,
                "BACKUP_DIR": str(self.backup_dir),
                "AUDIT_FACTS": str(self.facts),
                "FAKE_LOG": str(self.log),
                **env,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [line.split(" -f docker/compose.yaml ", 1)[-1] for line in self.read(self.log)]
        return result.returncode, calls, self.read(self.facts)

    @staticmethod
    def read(path: Path) -> list[str]:
        """Read a file's lines, if it exists.

        Args:
            path: The file.

        Returns:
            Its lines; none when it does not exist.
        """
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_dump_stops_dumps_starts_and_reports_the_file(self) -> None:
        """Neo4j is stopped for the dump and started again; the file is reported."""
        status, calls, facts = self.run_script("neo4j_admin.sh", "dump")
        self.assertEqual(status, 0)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertIn(f"-v {self.backup_dir}:/backup --entrypoint /bin/bash neo4j -c", calls[1])
        self.assertRegex(facts[0], r"^file=neo4j-\d{8}T\d{6}Z\.dump$")
        self.assertEqual(facts[1], "size_bytes=1234")
        self.assertTrue((self.backup_dir / facts[0].removeprefix("file=")).is_file())

    def test_failed_dump_still_starts_neo4j(self) -> None:
        """A failing neo4j-admin never leaves Neo4j down; nothing is reported."""
        status, calls, facts = self.run_script("neo4j_admin.sh", "dump", FAKE_RUN="1")
        self.assertEqual(status, 1)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertEqual(facts, [])

    def test_nothing_runs_when_neo4j_cannot_be_stopped(self) -> None:
        """No dump against a running database."""
        status, calls, _ = self.run_script("neo4j_admin.sh", "dump", FAKE_STOP="1")
        self.assertNotEqual(status, 0)
        self.assertEqual([call.split()[0] for call in calls], ["stop"])

    def test_load_mounts_the_dump_as_neo4j_dump(self) -> None:
        """The chosen dump is loaded read-only under the name neo4j-admin expects."""
        self.backup_dir.mkdir()
        (self.backup_dir / DUMP).write_bytes(b"x" * 99)
        status, calls, facts = self.run_script("neo4j_admin.sh", "load", DUMP)
        self.assertEqual(status, 0)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertIn(f"-v {self.backup_dir}/{DUMP}:/backup/neo4j.dump:ro", calls[1])
        self.assertIn("neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true", calls[1])
        self.assertEqual(facts, ["size_bytes=99"])

    def test_load_refuses_a_missing_or_odd_dump(self) -> None:
        """No such dump, or a name with a path in it: exit 2, Neo4j untouched."""
        for name in (DUMP, "../neo4j.dump", "a:b.dump"):
            status, calls, _ = self.run_script("neo4j_admin.sh", "load", name)
            self.assertEqual((status, calls), (2, []), name)

    def test_snapshot_reports_what_the_container_script_wrote(self) -> None:
        """The snapshot script's stdout becomes the facts."""
        status, calls, facts = self.run_script("qdrant_admin.sh", "snapshot", PROFILE="cpu")
        self.assertEqual(status, 0)
        self.assertEqual(calls[-1], "exec -T qdrant-cpu bash -s")
        self.assertEqual(facts, ["collection=docs", "snapshots=1", "size_bytes=2048", "failed=0"])

    def test_snapshot_needs_bash_in_the_image(self) -> None:
        """Without bash in the image no snapshot is attempted."""
        status, calls, facts = self.run_script("qdrant_admin.sh", "snapshot", FAKE_NO_BASH="1")
        self.assertEqual((status, len(calls), facts), (1, 1, []))

    def test_recover_copies_a_host_snapshot_in_first(self) -> None:
        """A snapshot in the backup directory is streamed into the container, then recovered."""
        self.backup_dir.mkdir()
        snapshot = "docs-1-2026-01-31-02-00-00.snapshot"
        (self.backup_dir / snapshot).write_bytes(b"snapshot bytes")
        status, calls, facts = self.run_script("qdrant_admin.sh", "recover", "docs", snapshot, PROFILE="cuda")
        self.assertEqual(status, 0)
        path = f"/qdrant/snapshots/docs/{snapshot}"
        self.assertTrue(calls[0].startswith("exec -T qdrant-cuda sh -c "), calls[0])
        self.assertTrue(calls[0].endswith(f" _ {path}"), calls[0])
        self.assertEqual((self.tmp / "streamed").read_bytes(), b"snapshot bytes")
        self.assertTrue(calls[1].startswith("exec -T qdrant-cuda bash -c "), calls[1])
        self.assertTrue(calls[1].endswith(f"qdrant_api recover docs {path}"), calls[1])
        self.assertEqual(facts, ["source=host"])

    def test_recover_from_the_volume(self) -> None:
        """Without a host copy, the snapshot is taken from Qdrant's volume."""
        status, calls, facts = self.run_script("qdrant_admin.sh", "recover", "docs", "docs-1.snapshot")
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(facts, ["source=volume"])

    def test_recover_refuses_odd_names(self) -> None:
        """Names that could leave the snapshot directory are refused before anything runs."""
        for args in (("../docs", "x.snapshot"), ("docs", "../x.snapshot"), ("docs", "a..b"), ("docs files", "x")):
            status, calls, _ = self.run_script("qdrant_admin.sh", "recover", *args)
            self.assertEqual((status, calls), (2, []), args)


@unittest.skipUnless(ADMIN_OVERLAY.is_file(), "needs the repo's docker/")
class AdminOverlayTest(unittest.TestCase):
    """The bindings db.ports.open records are the admin overlay's."""

    def test_recorded_bindings_match_the_overlay(self) -> None:
        """admin_audit.ADMIN_PORTS lists exactly what docker/compose.admin.yaml publishes."""
        published = set(re.findall(r'"(127\.0\.0\.1:\d+):\d+"', ADMIN_OVERLAY.read_text(encoding="utf-8")))
        self.assertEqual(published, set(admin_audit.ADMIN_PORTS))


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class AdminPortsTest(ComposeStandIn):
    """scripts/admin-ports.sh: the admin ports' window, recorded at both ends."""

    @override
    def setUp(self) -> None:
        """Point the state file into the workspace."""
        super().setUp()
        self.state = self.tmp / "admin-ports-state"

    def run_ports(self, *args: str, **env: str) -> tuple[int, list[str], list[dict[str, Any]], str, str]:
        """Run the script.

        Args:
            *args: Its arguments.
            **env: More environment, e.g. ``REASON``, ``FAKE_PUBLISHED``.

        Returns:
            The exit status, the calls in order, the events in the container
            log, and what the script wrote to stderr and stdout.
        """
        result = subprocess.run(
            ["bash", str(SCRIPTS / "admin-ports.sh"), *args],
            env={**self.base, "ADMIN_PORTS_STATE": str(self.state), **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        calls, events = self.calls_and_events()
        return result.returncode, calls, events, result.stderr, result.stdout

    def open_window(self, **env: str) -> str:
        """Open the ports through the script, publishing nothing for real.

        Args:
            **env: More environment.

        Returns:
            The open event's id.
        """
        status, _, events, stderr, _ = self.run_ports(
            "open", "--", "sh", "-c", 'echo publish >> "$FAKE_LOG"', REASON="inspect the graph schema", **env
        )
        self.assertEqual(status, 0, stderr)
        return str(records(events)[-1]["event_id"])

    def write_state(self, opened: str, since: int) -> None:
        """Leave an open window behind, as an earlier make up-admin would.

        Args:
            opened: The open event's id.
            since: When it opened, in Unix seconds.
        """
        self.state.write_text(f"opened={opened}\nopened_at={since}\n", encoding="utf-8")

    def test_opening_is_recorded_before_anything_is_published(self) -> None:
        """First the opening's record, then the command; the window is kept, private to the operator."""
        opened = self.open_window()
        calls, events = self.calls_and_events()
        self.assertTrue(calls[0].endswith("admin_audit.py begin ports"), calls[0])
        self.assertEqual(calls[1], "publish")
        (event,) = records(events)
        self.assertEqual((event["operation"], event["outcome"], event["event_id"]), ("db.ports.open", "ok", opened))
        self.assertEqual(event["purpose"], {"reason": "inspect the graph schema"})
        self.assertEqual(event["actor"], OPERATOR)
        self.assertRegex(self.state.read_text(encoding="utf-8"), rf"\Aopened={opened}\nopened_at=\d+\n\Z")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assert_chains_intact()

    def test_nothing_is_published_without_a_purpose(self) -> None:
        """No CASE_REF/REASON: refused, recorded as denied, no window."""
        status, calls, events, stderr, _ = self.run_ports("open", "--", "sh", "-c", 'echo publish >> "$FAKE_LOG"')
        self.assertEqual(status, 2)
        self.assertNotIn("publish", calls)
        self.assertFalse(self.state.exists())
        (event,) = records(events)
        self.assertEqual((event["operation"], event["outcome"]), ("db.ports.open", "denied"))
        self.assertIn("needs a stated purpose", stderr)
        self.assert_chains_intact()

    def test_nothing_is_published_while_admin_audit_cannot_record(self) -> None:
        """admin-audit down: exit 1, no window."""
        status, calls, _, stderr, _ = self.run_ports(
            "open", "--", "sh", "-c", 'echo publish >> "$FAKE_LOG"', REASON="inspect", FAKE_DOWN="1"
        )
        self.assertEqual(status, 1)
        self.assertNotIn("publish", calls)
        self.assertFalse(self.state.exists())
        self.assertIn("No port was published", stderr)

    def test_a_second_window_is_refused(self) -> None:
        """While one window is open, make up-admin does nothing."""
        self.write_state("01a1a557-d62a-71a3-b278-0d559f15c636", 1_800_000_000)
        status, calls, _, stderr, _ = self.run_ports("open", "--", "true", REASON="inspect")
        self.assertEqual((status, calls), (2, []))
        self.assertIn("already open", stderr)

    def test_a_failed_opening_that_published_nothing_is_closed_at_once(self) -> None:
        """The command failed and no port is published: the close follows, as error."""
        status, _, events, _, _ = self.run_ports("open", "--", "sh", "-c", "exit 3", REASON="inspect")
        self.assertEqual(status, 3)
        opening, close = records(events)
        self.assertEqual((close["operation"], close["outcome"]), ("db.ports.close", "error"))
        self.assertEqual(close["parent_event_id"], opening["event_id"])
        self.assertEqual((close["criteria"]["by"], close["criteria"]["exit_status"]), ("up-admin", 3))
        self.assertFalse(self.state.exists())
        self.assert_chains_intact()

    def test_a_failed_opening_with_ports_up_stays_open(self) -> None:
        """Some port is published after all: the window stays for make up to close."""
        status, _, events, _, _ = self.run_ports(
            "open", "--", "sh", "-c", "exit 3", REASON="inspect", FAKE_PUBLISHED="127.0.0.1:7474 \n"
        )
        self.assertEqual(status, 3)
        self.assertEqual([event["operation"] for event in records(events)], ["db.ports.open"])
        self.assertTrue(self.state.exists())

    def test_make_up_closes_the_window(self) -> None:
        """Closing by make up: linked to the opening, with the window's length; the state goes."""
        opened = self.open_window()
        since = int(time.time()) - 90
        self.write_state(opened, since)
        status, calls, events, _, _ = self.run_ports("close", "up", SUDO_USER="ops2.example")
        self.assertEqual(status, 0)
        self.assertIn(" end ports --status 0 --duration-s ", calls[-1])
        close = records(events)[-1]
        self.assertEqual(
            (close["operation"], close["outcome"], close["parent_event_id"]), ("db.ports.close", "ok", opened)
        )
        self.assertEqual(close["actor"], {"user": "ops2.example", "via": "cli"})
        self.assertEqual(close["criteria"]["by"], "up")
        self.assertGreaterEqual(close["criteria"]["duration_s"], 90)
        self.assertLess(close["criteria"]["duration_s"], 120)
        self.assertFalse(self.state.exists())
        self.assert_chains_intact()

    def test_make_down_closes_it_too(self) -> None:
        """Closing by make down records `down` as what ended the window."""
        self.open_window()
        status, _, events, _, _ = self.run_ports("close", "down")
        self.assertEqual(status, 0)
        self.assertEqual(records(events)[-1]["criteria"]["by"], "down")
        self.assertFalse(self.state.exists())
        self.assert_chains_intact()

    def test_no_window_nothing_to_close(self) -> None:
        """Without a state file close does nothing at all."""
        status, calls, _, _, _ = self.run_ports("close", "up")
        self.assertEqual((status, calls), (0, []))

    def test_an_unrecorded_close_keeps_the_window(self) -> None:
        """The close could not be written: exit 1, the state stays for the next make up."""
        self.open_window()
        status, _, _, stderr, _ = self.run_ports("close", "up", FAKE_FAIL="end")
        self.assertEqual(status, 1)
        self.assertTrue(self.state.exists())
        self.assertIn("could not be recorded", stderr)

    def test_status(self) -> None:
        """The line make health prints: closed, open, open without a record, or a close still owed."""
        published = "127.0.0.1:7474 127.0.0.1:7687 \n127.0.0.1:6333 \n"
        self.assertEqual(self.run_ports("status")[4], "admin ports: closed\n")
        self.assertIn("without an open record", self.run_ports("status", FAKE_PUBLISHED=published)[4])
        self.write_state("01a1a557-d62a-71a3-b278-0d559f15c636", 1_800_000_000)
        out = self.run_ports("status", FAKE_PUBLISHED=published)[4]
        self.assertEqual(
            out,
            "admin ports: OPEN since 2027-01-15T08:00:00Z (127.0.0.1:6333 127.0.0.1:7474 127.0.0.1:7687); "
            "make up closes them\n",
        )
        self.assertIn("no close record yet", self.run_ports("status")[4])

    def test_a_damaged_state_is_refused(self) -> None:
        """A state file that is not one: neither open nor close guesses."""
        self.state.write_text("opened=somewhere\n", encoding="utf-8")
        for args in (("open", "--", "true"), ("close", "up")):
            status, calls, _, stderr, _ = self.run_ports(*args, REASON="inspect")
            self.assertEqual((status, calls), (2, []), args)
            self.assertIn("damaged", stderr)

    def test_usage(self) -> None:
        """Wrong arguments: exit 2, nothing called."""
        for args in (("open",), ("open", "--"), ("open", "true"), ("close",), ("close", "sideways"), ("bogus",)):
            status, calls, _, _, _ = self.run_ports(*args)
            self.assertEqual((status, calls), (2, []), args)


class FakeQdrant:
    """A stand-in for Qdrant's HTTP API: answers with a set status, keeps each request."""

    def __init__(self) -> None:
        """Start serving on a free local port."""
        self.status = 200
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Records the request and answers like Qdrant, with a JSON body."""

            def answer(self) -> None:
                """Record the request and send the set status."""
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append((self.command, self.path, dict(self.headers.items()), body))
                payload = b'{"status":"ok","time":0.0}'
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                """Answer a GET."""
                self.answer()

            def do_PUT(self) -> None:
                """Answer a PUT."""
                self.answer()

            @override
            def log_message(self, format: str, *args: Any) -> None:
                """Keep the test output clean."""
                del format, args

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Stop serving."""
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class QdrantApiTest(unittest.TestCase):
    """scripts/qdrant_api.sh, run as qdrant_admin.sh runs it, against a stand-in Qdrant."""

    @override
    def setUp(self) -> None:
        """Start the stand-in."""
        self.qdrant = FakeQdrant()
        self.addCleanup(self.qdrant.close)

    def shell(self, commands: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Pipe commands into the helper's shell mode.

        Args:
            commands: The shell input.
            **env: The container's environment, e.g. ``QDRANT__SERVICE__API_KEY``.

        Returns:
            The finished process.
        """
        script = (SCRIPTS / "qdrant_api.sh").read_text(encoding="utf-8")
        return subprocess.run(
            ["bash", "-c", script, "qdrant_api", "shell"],
            env={"PATH": os.environ["PATH"], "QDRANT_PORT": str(self.qdrant.port), **env},
            input=commands,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_the_servers_key_is_sent(self) -> None:
        """With QDRANT_API_KEY set, every request carries the container's key."""
        result = self.shell("qdrant GET /collections\n", QDRANT__SERVICE__API_KEY="zz-test-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "HTTP/1.0 200 OK")
        ((method, path, headers, _),) = self.qdrant.requests
        self.assertEqual((method, path), ("GET", "/collections"))
        self.assertEqual(headers.get("api-key"), "zz-test-key")
        self.assertNotIn("NOTE", result.stderr)

    def test_without_a_key_none_is_sent(self) -> None:
        """No key in the container: no header, nothing to explain."""
        result = self.shell("qdrant GET /collections\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        ((_, _, headers, _),) = self.qdrant.requests
        self.assertNotIn("api-key", {name.lower() for name in headers})
        self.assertEqual(result.stderr, "")

    def test_an_empty_key_is_explained(self) -> None:
        """A Qdrant with an empty key refuses the request: the 401 comes with how to fix it."""
        self.qdrant.status = 401
        result = self.shell("qdrant GET /collections\n", QDRANT__SERVICE__API_KEY="")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines()[0], "HTTP/1.0 401 Unauthorized")
        ((_, _, headers, _),) = self.qdrant.requests
        self.assertNotIn("api-key", {name.lower() for name in headers})
        note = "NOTE: 401: this Qdrant has no API key to send. Set QDRANT_API_KEY in .env and run make up again.\n"
        self.assertEqual(result.stderr, note)

    def test_a_refusal_with_a_key_has_no_note(self) -> None:
        """A 401 although the key was sent is not about the key's absence."""
        self.qdrant.status = 401
        result = self.shell("qdrant GET /collections\n", QDRANT__SERVICE__API_KEY="zz-test-key")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")

    def test_the_body_length_is_counted_in_bytes(self) -> None:
        """A JSON body with non-ASCII text is sent whole."""
        body = '{"payload":{"title":"Akten-' + chr(0xE4) + '"}}'
        result = self.shell(f"qdrant PUT /collections/docs '{body}'\n", LC_ALL="C.UTF-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        ((method, _, headers, sent),) = self.qdrant.requests
        self.assertEqual(method, "PUT")
        self.assertEqual(sent, body.encode("utf-8"))
        self.assertEqual(headers.get("Content-Length"), str(len(body.encode("utf-8"))))


if __name__ == "__main__":
    unittest.main()

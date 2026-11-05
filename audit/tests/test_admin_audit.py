"""Tests for admin_audit.py and the scripts that run it.

Run from the repo root: ``python3 -m unittest discover -s audit/tests -t audit``.
The script tests need bash and the repo's ``scripts/``; they are skipped where
either is missing, e.g. in the pinned python image, which mounts only ``audit/``.
"""

from __future__ import annotations

import contextlib
import hashlib
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
HAS_MAKE = HAS_SCRIPTS and shutil.which("make") is not None and (AUDIT_DIR.parent / "Makefile").is_file()
ENVELOPE = frozenset(
    {"stream", "v", "event_id", "ts", "service", "service_version", "instance", "seq", "prev_hash", "hash"}
    | {"action", "operation", "outcome", "actor"}
)
OPERATOR = {"user": "ops.example", "via": "cli"}
PURPOSE = {"case_ref": "case-0001", "reason": "verify a reported duplicate"}
DUMP = "neo4j-20260131T020000Z.dump"
LOOPBACK = ["127.0.0.1:7474", "127.0.0.1:7687", "127.0.0.1:6333"]
BOTH_STORES = {"kind": "store", "ids": ["neo4j", "qdrant"], "count": 2}
FINGERPRINT = hashlib.sha256(b"age1synthetic0recipient\n").hexdigest()
# What a backup reports about its encryption, and how its record shows it.
ENCRYPTED = ("encryption=age", "recipients=2", f"recipients_sha256={FINGERPRINT}")
ENCRYPTED_CRITERIA = {"encryption": "age", "recipients": 2, "recipients_sha256": FINGERPRINT}


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

    def test_positive_number(self) -> None:
        """A backup has at least one recipient: zero is refused like a non-number."""
        self.assertEqual(admin_audit.positive_number("1"), 1)
        self.assertEqual(admin_audit.positive_number("12"), 12)
        for bad in ("0", "", "-2", "two"):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.positive_number(bad)

    def test_sha256_hex(self) -> None:
        """A fingerprint is 64 lowercase hex digits, nothing else."""
        self.assertEqual(admin_audit.sha256_hex(FINGERPRINT), FINGERPRINT)
        for bad in ("", FINGERPRINT[:-1], FINGERPRINT.upper(), FINGERPRINT + "0", "g" * 64):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.sha256_hex(bad)

    def test_encryption(self) -> None:
        """The only encryption a backup can report is age."""
        self.assertEqual(admin_audit.encryption("age"), "age")
        for bad in ("", "none", "gpg", "AGE"):
            with self.assertRaises(ValueError, msg=bad):
                admin_audit.encryption(bad)

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
        """The dump's name and size, its encryption, the database, the recipient."""
        status, events = self.end(self.purposed(), "backup", "neo4j", f"file={DUMP}", "size_bytes=22430", *ENCRYPTED)
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("disclose", "backup.neo4j", "ok"))
        self.assertEqual(event["recipient"], "file:backup")
        self.assertEqual(event["target"], {"kind": "database", "ids": ["neo4j"], "count": 1})
        self.assertEqual(
            event["criteria"],
            {"file": DUMP, "size_bytes": 22430, **ENCRYPTED_CRITERIA, "exit_status": 0, "duration_s": 7},
        )
        self.assertEqual(event["purpose"], PURPOSE)
        self.assertNotIn("parent_event_id", event)
        self.assert_chains_intact()

    def test_backup_records_its_encryption(self) -> None:
        """The record names the encryption, how many recipients and their set's fingerprint; never a key."""
        pairs = (f"file={DUMP}.age", "size_bytes=22512", *ENCRYPTED)
        status, events = self.end(self.purposed(), "backup", "neo4j", *pairs)
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual(
            event["criteria"],
            {"file": f"{DUMP}.age", "size_bytes": 22512, **ENCRYPTED_CRITERIA, "exit_status": 0, "duration_s": 7},
        )
        facts = ["collection=docs", "snapshots=1", "size_bytes=4096", "failed=0", *ENCRYPTED]
        _, events = self.end(self.purposed(), "backup", "qdrant", *facts)
        (event,) = records(events)
        self.assertEqual(event["criteria"]["recipients"], 2)
        self.assertEqual(event["criteria"]["recipients_sha256"], FINGERPRINT)
        self.assertNotIn("age1", json.dumps(event))
        self.assert_chains_intact()

    def test_success_must_state_the_encryption(self) -> None:
        """An ok backup without its encryption facts, or an ok restore without its: exit 2, nothing written."""
        restore_qdrant = ("collection=docs", "snapshot=qdrant-docs-1.snapshot.age")
        for kind, store, pairs in (
            ("backup", "neo4j", (f"file={DUMP}.age", "size_bytes=1")),
            ("backup", "neo4j", (f"file={DUMP}.age", "encryption=age", "recipients=2")),
            ("backup", "qdrant", ("collection=docs", "snapshots=1", "size_bytes=1", "failed=0")),
            ("restore", "neo4j", (f"file={DUMP}.age", "size_bytes=1")),
            ("restore", "qdrant", restore_qdrant),
        ):
            status, events = self.end(self.purposed(), kind, store, *pairs)
            self.assertEqual((status, events), (2, []), (kind, store, pairs))
        self.assertIn("succeeded without reporting", self.stderr.getvalue())
        status, events = self.end(self.purposed(), "restore", "qdrant", *restore_qdrant, status=1)
        self.assertEqual((status, records(events)[0]["outcome"]), (0, "error"))
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
        _, events = self.end(self.purposed(), "backup", "qdrant", *[f"collection={name}" for name in names], *ENCRYPTED)
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
        pairs = (f"file={DUMP}.age", "size_bytes=22512", "encryption=age")
        _, events = self.end(self.environ(), "restore", "neo4j", *pairs)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("modify", "restore.neo4j", "ok"))
        self.assertEqual(event["target"], {"kind": "database", "ids": ["neo4j"], "count": 1})
        self.assertEqual(
            event["criteria"],
            {"file": f"{DUMP}.age", "size_bytes": 22512, "encryption": "age", "exit_status": 0, "duration_s": 7},
        )
        self.assertNotIn("recipient", event)
        self.assert_chains_intact()

    def test_qdrant_restore_names_its_collection(self) -> None:
        """The collection replaced is the target; the snapshot and its encryption are criteria."""
        pairs = ["collection=docs", "snapshot=qdrant-docs-20260131T020000Z.snapshot.age", "encryption=age"]
        _, events = self.end(self.purposed(), "restore", "qdrant", *pairs)
        (event,) = records(events)
        self.assertEqual(event["target"], {"kind": "collection", "ids": ["docs"], "count": 1})
        self.assertEqual(
            event["criteria"],
            {
                "collection": "docs",
                "snapshot": "qdrant-docs-20260131T020000Z.snapshot.age",
                "encryption": "age",
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
            ("restore", "qdrant", "collection=docs", "snapshot=x.snapshot.age", "source=host"),
            ("backup", "neo4j", "encryption=none"),
            ("backup", "neo4j", "recipients=0"),
            ("backup", "qdrant", "recipients_sha256=0123"),
            ("backup", "neo4j", "recipient=age1synthetic0recipient"),
            ("restore", "neo4j", f"file={DUMP}", "encryption=gpg"),
            ("restore", "neo4j", f"file={DUMP}", "recipients=2"),
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

        code, events = self.end(self.purposed(AUDIT_OUTPUT="nowhere"), "backup", "neo4j", *ENCRYPTED)
        self.assertEqual((code, events), (1, []))
        with contextlib.redirect_stdout(Broken()), contextlib.redirect_stderr(self.stderr):
            code = admin_audit.end(self.purposed(), "backup", "neo4j", list(ENCRYPTED), status=0)
        self.assertEqual(code, 1)
        self.assertIn("could not write the record", self.stderr.getvalue())


PRUNE_FACTS = ("period_days=30", "cutoff=2026-01-01T02:00:00Z", "deleted=2", "not_deleted=0")


class PruneRecordTest(Workspace):
    """The record of a backup pruning: a deletion of files."""

    def test_prune_is_a_deletion_of_files(self) -> None:
        """The deleted files are the target, by name; the rule and the counts are criteria."""
        names = [f"{DUMP}.age", "neo4j-20260130T020000Z.dump"]
        status, events = self.end(self.environ(), "prune", "neo4j", *PRUNE_FACTS, *[f"file={n}" for n in names])
        self.assertEqual(status, 0)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("delete", "backup.prune.neo4j", "ok"))
        self.assertEqual(event["target"], {"kind": "file", "ids": names, "count": 2})
        self.assertEqual(
            event["criteria"],
            {
                "period_days": 30,
                "cutoff": "2026-01-01T02:00:00Z",
                "deleted": 2,
                "not_deleted": 0,
                "exit_status": 0,
                "duration_s": 7,
            },
        )
        self.assertIsNone(event.get("recipient"))
        self.assert_chains_intact()

    def test_nothing_pruned_still_records_an_empty_target(self) -> None:
        """A pruning that found nothing due is recorded too, with no ids."""
        facts = ("period_days=30", "cutoff=2026-01-01T02:00:00Z", "deleted=0", "not_deleted=0")
        _, events = self.end(self.environ(), "prune", "qdrant", *facts)
        (event,) = records(events)
        self.assertEqual(event["operation"], "backup.prune.qdrant")
        self.assertEqual(event["target"], {"kind": "file", "count": 0})

    def test_long_file_list_continues_in_id_events(self) -> None:
        """More than 200 files are not cut: continuation events carry the rest."""
        names = [f"qdrant-docs-20260101T0000{n:02d}Z-{n}.snapshot.age" for n in range(250)]
        _, events = self.end(self.environ(), "prune", "qdrant", *PRUNE_FACTS, *[f"file={n}" for n in names])
        primary, *more = records(events)
        self.assertEqual(primary["target"]["count"], 250)
        self.assertEqual([event["operation"] for event in more], ["backup.prune.qdrant.ids"])
        self.assertEqual([n for event in (primary, *more) for n in event["target"]["ids"]], names)
        self.assert_chains_intact()

    def test_success_must_state_the_rule_and_counts(self) -> None:
        """An ok pruning without its period, cutoff or count is refused and writes nothing."""
        for missing in ("period_days", "cutoff", "deleted"):
            facts = [fact for fact in PRUNE_FACTS if not fact.startswith(f"{missing}=")]
            status, events = self.end(self.environ(), "prune", "neo4j", *facts)
            self.assertEqual((status, events), (2, []), missing)

    def test_malformed_facts_are_refused(self) -> None:
        """A path instead of a file name, or a cutoff that is not UTC, is never recorded."""
        for bad in ("file=../neo4j.dump", "file=dir/neo4j.dump", "cutoff=2026-01-01", "cutoff=2026-01-01T02:00:00+01:00"):
            status, events = self.end(self.environ(), "prune", "neo4j", *PRUNE_FACTS, bad)
            self.assertEqual((status, events), (2, []), bad)

    def test_prune_follows_require_purpose(self) -> None:
        """Without delete in AUDIT_REQUIRE_PURPOSE a pruning needs no purpose; with it, it does."""
        status, _, _ = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE="query,disclose"), "prune", "neo4j")
        self.assertEqual(status, 0)
        status, _, events = self.begin(self.environ(AUDIT_REQUIRE_PURPOSE="delete"), "prune", "neo4j")
        self.assertEqual(status, 2)
        (event,) = records(events)
        self.assertEqual((event["action"], event["operation"], event["outcome"]), ("delete", "backup.prune.neo4j", "denied"))


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
        argv = [
            "end",
            "backup",
            "neo4j",
            "--status",
            "0",
            "--duration-s",
            "3",
            f"file={DUMP}",
            "size_bytes=10",
            *ENCRYPTED,
        ]
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
            "restore",
            "neo4j",
            f"file={DUMP}",
            "--",
            "sh",
            "-c",
            'printf "size_bytes=10\\nencryption=age\\n" >> "$AUDIT_FACTS"',
        )
        self.assertEqual(status, 0)
        self.assertTrue(calls[0].endswith(f"begin restore neo4j file={DUMP}"), calls[0])
        (event,) = records(events)
        self.assertEqual(
            event["criteria"],
            {"file": DUMP, "size_bytes": 10, "encryption": "age", "exit_status": 0, "duration_s": 0},
        )
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


# A stand-in for age with its command line (-R/-i/-o/-d, one input file or
# stdin) and a readable "ciphertext": its header, then the plaintext. It
# refuses recipients that are not age1... keys and identities without the
# stand-in's secret, and logs every call. FAKE_AGE_FAIL=encrypt|decrypt fails
# that direction once it has written (a decryption after FAKE_AGE_PASS_BYTES
# bytes, and only from its FAKE_AGE_FAIL_FROM-th call on), except into
# /dev/null; FAKE_AGE_WARN warns like age about a key it skips. Each
# encryption appends the recipients file it read to FAKE_AGE_SEEN, as one JSON
# string per line.
FAKE_AGE = r"""#!/usr/bin/env python3
import json
import os
import sys

HEADER = b"age-encryption.org/v1\n-> X25519 stand-in\n--- stand-in\n"
SECRET = "AGE-SECRET-KEY-1ZZSTANDIN"
args = sys.argv[1:]
with open(os.environ["FAKE_AGE_LOG"], "a", encoding="utf-8") as log:
    log.write("age " + " ".join(args) + "\n")


def fail(message):
    sys.stderr.write(f"age: error: {message}\n")
    sys.exit(1)


options, inputs, decrypt, i = {}, [], False, 0
while i < len(args):
    if args[i] in ("-R", "-i", "-o"):
        options[args[i]] = args[i + 1]
        i += 2
    elif args[i] == "-d":
        decrypt, i = True, i + 1
    else:
        inputs.append(args[i])
        i += 1
data = open(inputs[0], "rb").read() if inputs else sys.stdin.buffer.read()
if decrypt:
    try:
        identity = open(options["-i"], encoding="utf-8").read()
    except OSError:
        fail("failed to open the identity file")
    if SECRET not in identity:
        fail("no identity matched any of the recipients")
    if not data.startswith(HEADER):
        fail("failed to read header")
    out = data[len(HEADER):]
    with open(os.environ["FAKE_AGE_LOG"], encoding="utf-8") as log:
        decryptions = sum(line.startswith("age -d ") for line in log)
    if decryptions < int(os.environ.get("FAKE_AGE_FAIL_FROM", "1")):
        os.environ.pop("FAKE_AGE_FAIL", None)
    if os.environ.get("FAKE_AGE_FAIL") == "decrypt":
        out = out[: int(os.environ.get("FAKE_AGE_PASS_BYTES", "0"))]
else:
    try:
        text = open(options["-R"], encoding="utf-8").read()
    except OSError:
        fail("failed to open recipient file")
    with open(os.environ["FAKE_AGE_SEEN"], "a", encoding="utf-8") as seen:
        seen.write(json.dumps(text) + "\n")
    lines = text.replace("\r", "").split("\n")
    keys = [line for line in lines if line and not line.startswith("#")]
    for number, line in enumerate(lines, 1):
        if line in keys and not line.startswith("age1"):
            fail(f'failed to parse recipient file "{options["-R"]}": malformed recipient at line {number}')
    if not keys:
        fail("no recipients found")
    if os.environ.get("FAKE_AGE_WARN"):
        sys.stderr.write("age: warning: ignoring unsupported SSH key\n")
    out = HEADER + data
if out:
    if "-o" in options:
        with open(options["-o"], "wb") as sink:
            sink.write(out)
    else:
        sys.stdout.buffer.write(out)
elif "-o" in options and not decrypt:
    open(options["-o"], "wb").close()
if os.environ.get("FAKE_AGE_FAIL") == ("decrypt" if decrypt else "encrypt") and options.get("-o") != "/dev/null":
    fail("stand-in failure")
"""
AGE_HEADER = b"age-encryption.org/v1\n-> X25519 stand-in\n--- stand-in\n"
AGE_SECRET = "AGE-SECRET-KEY-1ZZSTANDIN"
# Two synthetic public keys, as a recipients file lists them.
RECIPIENT_KEYS = ("age1zzsyntheticrecipient0aaaa", "age1zzsyntheticrecipient0bbbb")


def recipients_fingerprint(keys: tuple[str, ...]) -> str:
    """Compute the fingerprint a backup records for a set of public keys.

    Args:
        keys: The keys, in any order, repeats allowed.

    Returns:
        The SHA-256 of the distinct keys, sorted, one per line.
    """
    return hashlib.sha256("".join(f"{key}\n" for key in sorted(set(keys))).encode()).hexdigest()


class AgeStandIn(unittest.TestCase):
    """A temporary directory with the age stand-in first on PATH and a recipients file.

    ``TMPDIR`` is a directory of its own, so that the private copy of the
    recipients the scripts make there can be found, and found removed.
    """

    @override
    def setUp(self) -> None:
        """Create the stand-in, its logs, TMPDIR and a recipients file with two keys."""
        self.tmp = Path(tempfile.mkdtemp(prefix="admin-audit-age-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.private = self.tmp / "private"
        self.private.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        age = self.bin / "age"
        age.write_text(FAKE_AGE, encoding="utf-8")
        age.chmod(0o755)
        self.age_log = self.tmp / "age.log"
        self.age_seen = self.tmp / "age-seen.log"
        self.recipients = self.tmp / "recipients.txt"
        self.recipients.write_text(f"# keyholder one\n{RECIPIENT_KEYS[0]}\n{RECIPIENT_KEYS[1]}\n", encoding="utf-8")
        self.recipients.chmod(0o644)
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AGE_LOG": str(self.age_log),
            "FAKE_AGE_SEEN": str(self.age_seen),
            "BACKUP_RECIPIENTS": str(self.recipients),
            "TMPDIR": str(self.private),
        }

    def age_calls(self) -> list[str]:
        """Read the stand-in's log, a private copy of the recipients named ``<copy>``.

        Returns:
            Its calls, in order.
        """
        if not self.age_log.exists():
            return []
        private = os.path.realpath(self.private)
        copy = re.compile(re.escape(f"{private}{os.sep}data-plane-recipients.") + r"[A-Za-z0-9]{6}\b")
        return [copy.sub("<copy>", line) for line in self.age_log.read_text(encoding="utf-8").splitlines()]

    def age_read(self) -> list[str]:
        """Read what each encryption took as its recipients file.

        Returns:
            The contents, in order.
        """
        if not self.age_seen.exists():
            return []
        return [json.loads(line) for line in self.age_seen.read_text(encoding="utf-8").splitlines()]

    def left_private(self) -> list[str]:
        """List what is left in TMPDIR.

        Returns:
            The names, sorted.
        """
        return sorted(path.name for path in self.private.iterdir())


# A stand-in for `docker compose` for the store scripts: logs every call and
# plays the part of neo4j-admin and of the Qdrant container. FAKE_INTERRUPT
# names a call that is interrupted like ^C: SIGINT to the whole process group.
# FAKE_SWAP replaces the recipients file's content as Neo4j is stopped.
FAKE_STORE_COMPOSE = r"""#!/usr/bin/env bash
call="$*"
printf 'compose %s\n' "${call//$'\n'/\\n}" >> "$FAKE_LOG"
args=("$@")
api=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ ${args[i]} == qdrant_api ]] && api=${args[i + 1]}
done
last=${args[${#args[@]} - 1]}
interrupt() { [[ ${FAKE_INTERRUPT:-} != "$1" ]] || kill -INT 0; }
case " $* " in
  *" stop neo4j "*)
    [[ -z ${FAKE_SWAP:-} ]] || printf '%s\n' "$FAKE_SWAP" > "$BACKUP_RECIPIENTS"
    exit "${FAKE_STOP:-0}" ;;
  *" start neo4j "*) exit "${FAKE_START:-0}" ;;
  *" run --rm -T --entrypoint neo4j-admin neo4j database dump neo4j --to-stdout "*)
    printf 'DZV1 synthetic dump of Alice Example'
    interrupt dump
    exit "${FAKE_RUN:-0}" ;;
  *" run --rm -T --entrypoint neo4j-admin neo4j database load "*)
    cat > "${FAKE_LOG%/*}/loaded"
    exit "${FAKE_LOAD:-0}" ;;
  *" run --rm "*) exit "${FAKE_RUN:-0}" ;;
  *" sh -c command -v bash"*) exit "${FAKE_NO_BASH:-0}" ;;
  *"shopt -s globstar"*) echo "${FAKE_LEFTOVERS:-0}" ;;
  *" cat /qdrant/snapshots/"*)
    printf 'snapshot bytes of %s' "$last"
    interrupt cat
    [[ $last != */"${FAKE_CAT_FAIL:-none}"-* ]] || exit 1 ;;
  *" sh -c "*"cat >"*) cat > "${FAKE_LOG%/*}/streamed" ;;
esac
case $api in
  collections) printf '%b' "${FAKE_COLLECTIONS-docs\n}" ;;
  snapshot) [[ $last != "${FAKE_SNAPSHOT_FAIL:-}" ]] || exit 1; printf '%s-1-2026-11-04-02-00-00.snapshot\n' "$last" ;;
  drop-snapshot) exit "${FAKE_DROP:-0}" ;;
  recover) exit "${FAKE_RECOVER:-0}" ;;
esac
exit 0
"""


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class StoreScriptTest(AgeStandIn):
    """scripts/neo4j_admin.sh and scripts/qdrant_admin.sh with a stand-in compose and age."""

    @override
    def setUp(self) -> None:
        """Create the stand-ins, a backup directory and the facts file."""
        super().setUp()
        self.log = self.tmp / "calls.log"
        self.facts = self.tmp / "facts"
        self.backup_dir = self.tmp / "snapshots"
        compose = self.tmp / "fake-compose"
        compose.write_text(FAKE_STORE_COMPOSE, encoding="utf-8")
        compose.chmod(0o755)
        self.compose = f"{compose} --env-file .env -f docker/compose.yaml"
        self.stderr = ""

    def run_script(self, script: str, *args: str, **env: str) -> tuple[int, list[str], list[str]]:
        """Run a store script, in a process group of its own (as a terminal would).

        Args:
            script: ``neo4j_admin.sh`` or ``qdrant_admin.sh``.
            *args: Its arguments.
            **env: More environment.

        Returns:
            The exit status, the compose calls in order, and the facts reported.
            What it wrote to stderr is kept in ``self.stderr``.
        """
        result = subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            env={
                **self.env,
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
            start_new_session=True,
        )
        self.stderr = result.stderr
        calls = [line.split(" -f docker/compose.yaml ", 1)[-1] for line in self.read(self.log)]
        return result.returncode, calls, self.read(self.facts)

    def backups(self) -> dict[str, bytes]:
        """Read every file the backup directory holds.

        Returns:
            Their contents by name.
        """
        if not self.backup_dir.is_dir():
            return {}
        return {path.name: path.read_bytes() for path in sorted(self.backup_dir.iterdir())}

    def encryption_facts(self) -> list[str]:
        """Build the encryption facts a backup to the two stand-in keys reports.

        Returns:
            The key=value lines.
        """
        return ["encryption=age", "recipients=2", f"recipients_sha256={recipients_fingerprint(RECIPIENT_KEYS)}"]

    @staticmethod
    def read(path: Path) -> list[str]:
        """Read a file's lines, if it exists.

        Args:
            path: The file.

        Returns:
            Its lines; none when it does not exist.
        """
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    @staticmethod
    def qdrant_steps(calls: list[str]) -> list[str]:
        """Shorten the calls into the Qdrant container to what they do.

        Args:
            calls: The compose calls.

        Returns:
            ``api <command>`` for scripts/qdrant_api.sh, ``leftovers`` for the
            count of files in the snapshot volume, other calls as they are.
        """
        steps = []
        for call in calls:
            if " qdrant_api " in call:
                steps.append("api " + call.rsplit(" qdrant_api ", 1)[1])
            else:
                steps.append("leftovers" if "shopt -s globstar" in call else call)
        return steps

    def test_dump_streams_through_age_into_the_backup_directory(self) -> None:
        """Stopped, dumped to stdout, encrypted on the way, started again: one encrypted file, nothing else."""
        status, calls, facts = self.run_script("neo4j_admin.sh", "dump")
        self.assertEqual(status, 0, self.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertEqual(calls[1], "run --rm -T --entrypoint neo4j-admin neo4j database dump neo4j --to-stdout")
        ((name, content),) = self.backups().items()
        self.assertRegex(name, r"^neo4j-\d{8}T\d{6}Z\.dump\.age$")
        self.assertEqual(content, AGE_HEADER + b"DZV1 synthetic dump of Alice Example")
        self.assertEqual(
            self.age_calls(),
            ["age -R <copy> -o /dev/null", f"age -R <copy> -o {self.backup_dir}/{name}.part"],
        )
        self.assertEqual(self.left_private(), [])
        self.assertEqual(facts, [*self.encryption_facts(), f"file={name}", f"size_bytes={len(content)}"])
        self.assertEqual((self.backup_dir / name).stat().st_mode & 0o777, 0o600)

    def test_dump_refuses_without_recipients(self) -> None:
        """No recipients file: refused before Neo4j is touched, saying how to make a key pair."""
        for recipients in ("", str(self.tmp / "missing.txt")):
            status, calls, facts = self.run_script("neo4j_admin.sh", "dump", BACKUP_RECIPIENTS=recipients)
            self.assertEqual((status, calls, facts), (2, [], []), recipients)
            self.assertIn("age-keygen -o", self.stderr)
        self.assertEqual(self.backups(), {})

    def test_failed_dump_still_starts_neo4j(self) -> None:
        """A failing neo4j-admin never leaves Neo4j down; no file is kept or reported."""
        status, calls, facts = self.run_script("neo4j_admin.sh", "dump", FAKE_RUN="1")
        self.assertEqual(status, 1)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertEqual(facts, self.encryption_facts())
        self.assertEqual(self.backups(), {})
        self.assertEqual(self.left_private(), [])

    def test_failed_encryption_keeps_nothing(self) -> None:
        """Age failing after it wrote part of the file leaves no file, not even the part."""
        status, calls, facts = self.run_script("neo4j_admin.sh", "dump", FAKE_AGE_FAIL="encrypt")
        self.assertEqual(status, 1)
        self.assertEqual(calls[-1], "start neo4j")
        self.assertEqual(self.backups(), {})
        self.assertEqual(facts, self.encryption_facts())

    def test_interrupted_dump_starts_neo4j_again(self) -> None:
        """^C during the dump: the unfinished file goes, Neo4j starts again, exit 130."""
        status, calls, _ = self.run_script("neo4j_admin.sh", "dump", FAKE_INTERRUPT="dump")
        self.assertEqual(status, 130)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])
        self.assertEqual(self.backups(), {})
        self.assertEqual(self.left_private(), [])

    def test_the_keys_checked_are_the_keys_encrypted_to(self) -> None:
        """A recipients file changed after the check changes neither whom the dump is encrypted to nor its record."""
        original = self.recipients.read_text(encoding="utf-8")
        status, _, facts = self.run_script("neo4j_admin.sh", "dump", FAKE_SWAP=RECIPIENT_KEYS[0])
        self.assertEqual(status, 0, self.stderr)
        self.assertEqual(self.recipients.read_text(encoding="utf-8"), f"{RECIPIENT_KEYS[0]}\n")
        self.assertEqual(self.age_read(), [original, original])
        self.assertEqual(facts[:3], self.encryption_facts())
        self.assertEqual(self.left_private(), [])

    def test_nothing_runs_when_neo4j_cannot_be_stopped(self) -> None:
        """No dump against a running database."""
        status, calls, _ = self.run_script("neo4j_admin.sh", "dump", FAKE_STOP="1")
        self.assertNotEqual(status, 0)
        self.assertEqual([call.split()[0] for call in calls], ["stop"])

    def encrypted(self, name: str, plaintext: bytes) -> Path:
        """Put a backup encrypted by the stand-in into the backup directory.

        Args:
            name: Its file name.
            plaintext: What it decrypts to.

        Returns:
            Its path.
        """
        self.backup_dir.mkdir(exist_ok=True)
        path = self.backup_dir / name
        path.write_bytes(AGE_HEADER + plaintext)
        return path

    def identity(self, secret: str = AGE_SECRET) -> str:
        """Write an identity file, as an operator brings one for a restore.

        Args:
            secret: Its key; the stand-in's own unless another is wanted.

        Returns:
            Its path.
        """
        path = self.tmp / "offline" / "backup-identity.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# created offline\n{secret}\n", encoding="utf-8")
        return str(path)

    def test_load_checks_the_whole_file_then_streams_it_in(self) -> None:
        """Decrypted whole while Neo4j runs, then stopped, decrypted into neo4j-admin, started again."""
        dump = self.encrypted(f"{DUMP}.age", b"DZV1 synthetic dump of Alice Example")
        identity = self.identity()
        status, calls, facts = self.run_script("neo4j_admin.sh", "load", dump.name, IDENTITY=identity)
        self.assertEqual(status, 0, self.stderr)
        self.assertEqual(self.age_calls(), [f"age -d -i {identity} {dump}"] * 2)
        self.assertEqual(
            calls,
            [
                "stop neo4j",
                "run --rm -T --entrypoint neo4j-admin neo4j"
                " database load neo4j --from-stdin --overwrite-destination=true",
                "start neo4j",
            ],
        )
        self.assertEqual((self.tmp / "loaded").read_bytes(), b"DZV1 synthetic dump of Alice Example")
        self.assertEqual(facts, [f"size_bytes={dump.stat().st_size}", "encryption=age"])
        self.assertEqual(sorted(path.name for path in self.backup_dir.iterdir()), [dump.name])

    def test_load_refuses_before_anything_without_a_usable_identity_or_file(self) -> None:
        """No identity, an unreadable one, a plaintext dump, a missing or odd name: exit 2, nothing run."""
        dump = self.encrypted(f"{DUMP}.age", b"DZV1 synthetic dump")
        (self.backup_dir / DUMP).write_bytes(b"DZV1 an earlier, unencrypted dump")
        cases: list[tuple[str, dict[str, str]]] = [
            (dump.name, {}),
            (dump.name, {"IDENTITY": str(self.tmp / "missing.key")}),
            (DUMP, {"IDENTITY": self.identity()}),
            ("neo4j-20260101T000000Z.dump.age", {"IDENTITY": self.identity()}),
            ("../neo4j.dump", {"IDENTITY": self.identity()}),
            ("a:b.dump", {"IDENTITY": self.identity()}),
        ]
        for name, env in cases:
            status, calls, _ = self.run_script("neo4j_admin.sh", "load", name, **env)
            self.assertEqual((status, calls), (2, []), (name, env))
        self.assertEqual(self.age_calls(), [])

    def test_another_identity_never_stops_neo4j(self) -> None:
        """The whole-file check fails: Neo4j keeps running untouched."""
        dump = self.encrypted(f"{DUMP}.age", b"DZV1 synthetic dump")
        status, calls, facts = self.run_script(
            "neo4j_admin.sh", "load", dump.name, IDENTITY=self.identity("AGE-SECRET-KEY-1ZZOTHER")
        )
        self.assertEqual((status, calls), (1, []))
        self.assertIn("does not decrypt with this identity", self.stderr)
        self.assertIn("encryption=age", facts)

    def test_a_file_that_holds_no_dump_never_stops_neo4j(self) -> None:
        """An encrypted Qdrant snapshot given as a dump decrypts, but is refused before the stop."""
        snapshot = self.encrypted("qdrant-docs-20260131T020000Z.snapshot.age", b"a tar archive, not a dump")
        status, calls, _ = self.run_script("neo4j_admin.sh", "load", snapshot.name, IDENTITY=self.identity())
        self.assertEqual((status, calls), (1, []))
        self.assertIn("holds no Neo4j dump", self.stderr)

    def test_a_decryption_that_fails_at_once_never_starts_the_load(self) -> None:
        """The second decryption fails before any byte (a mistyped passphrase): neo4j-admin never runs."""
        dump = self.encrypted(f"{DUMP}.age", b"DZV1 synthetic dump")
        second_fails = {"FAKE_AGE_FAIL": "decrypt", "FAKE_AGE_FAIL_FROM": "2"}
        status, calls, _ = self.run_script(
            "neo4j_admin.sh", "load", dump.name, IDENTITY=self.identity(), **second_fails
        )
        self.assertEqual(status, 1)
        self.assertEqual(calls, ["stop neo4j", "start neo4j"])
        self.assertFalse((self.tmp / "loaded").exists())
        self.assertIn("the database was not touched", self.stderr)

    def test_a_failed_load_still_starts_neo4j(self) -> None:
        """neo4j-admin failing never leaves Neo4j down."""
        dump = self.encrypted(f"{DUMP}.age", b"DZV1 synthetic dump")
        status, calls, _ = self.run_script("neo4j_admin.sh", "load", dump.name, IDENTITY=self.identity(), FAKE_LOAD="1")
        self.assertEqual(status, 1)
        self.assertEqual([call.split()[0] for call in calls], ["stop", "run", "start"])

    def test_snapshots_leave_the_volume_encrypted(self) -> None:
        """Per collection: snapshot, copy out through age, delete in the volume; a file and a fact each."""
        docs = "docs-1-2026-11-04-02-00-00.snapshot"
        cases = "case files 2026-1-2026-11-04-02-00-00.snapshot"
        status, calls, facts = self.run_script(
            "qdrant_admin.sh", "snapshot", PROFILE="cpu", FAKE_COLLECTIONS="docs\ncase files 2026\n"
        )
        self.assertEqual(status, 0, self.stderr)
        self.assertEqual(
            self.qdrant_steps(calls),
            [
                "exec -T qdrant-cpu sh -c command -v bash >/dev/null 2>&1",
                "api collections",
                "api snapshot docs",
                f"exec -T qdrant-cpu cat /qdrant/snapshots/docs/{docs}",
                f"api drop-snapshot docs {docs}",
                "api snapshot case files 2026",
                f"exec -T qdrant-cpu cat /qdrant/snapshots/case files 2026/{cases}",
                f"api drop-snapshot case files 2026 {cases}",
                "leftovers",
            ],
        )
        files = self.backups()
        cases_file, docs_file = sorted(files)
        self.assertRegex(cases_file, r"^qdrant-case_files_2026-\d{8}T\d{6}Z\.snapshot\.age$")
        self.assertRegex(docs_file, r"^qdrant-docs-\d{8}T\d{6}Z\.snapshot\.age$")
        self.assertEqual(files[docs_file], AGE_HEADER + f"snapshot bytes of /qdrant/snapshots/docs/{docs}".encode())
        total = sum(len(content) for content in files.values())
        self.assertEqual(
            facts,
            [
                *self.encryption_facts(),
                "collection=docs",
                "collection=case files 2026",
                "snapshots=2",
                f"size_bytes={total}",
                "failed=0",
            ],
        )

    def test_a_failed_copy_still_leaves_the_volume(self) -> None:
        """One copy fails: its snapshot is deleted all the same and counted; the others go on."""
        status, calls, facts = self.run_script(
            "qdrant_admin.sh", "snapshot", FAKE_COLLECTIONS="docs\nnotes\n", FAKE_CAT_FAIL="docs"
        )
        self.assertEqual(status, 1)
        self.assertIn("api drop-snapshot docs docs-1-2026-11-04-02-00-00.snapshot", self.qdrant_steps(calls))
        (name,) = self.backups()
        self.assertTrue(name.startswith("qdrant-notes-"), name)
        size = len(self.backups()[name])
        self.assertEqual(facts[3:], ["collection=notes", "snapshots=1", f"size_bytes={size}", "failed=1"])

    def test_a_snapshot_left_in_the_volume_fails_the_backup(self) -> None:
        """A snapshot that cannot be deleted is an error, though its encrypted copy is kept."""
        status, _, facts = self.run_script("qdrant_admin.sh", "snapshot", FAKE_DROP="1")
        self.assertEqual(status, 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("collection=docs", facts)
        self.assertIn("still in the qdrant-snapshots volume", self.stderr)

    def test_an_interrupted_copy_still_leaves_the_volume(self) -> None:
        """^C while a snapshot is copied out: it is deleted in the volume, the unfinished file goes, exit 130."""
        status, calls, _ = self.run_script("qdrant_admin.sh", "snapshot", FAKE_INTERRUPT="cat")
        self.assertEqual(status, 130)
        self.assertEqual(self.qdrant_steps(calls)[-1], "api drop-snapshot docs docs-1-2026-11-04-02-00-00.snapshot")
        self.assertEqual(self.backups(), {})

    def test_snapshot_refuses_without_recipients(self) -> None:
        """No recipients: nothing is asked of Qdrant at all."""
        status, calls, facts = self.run_script("qdrant_admin.sh", "snapshot", BACKUP_RECIPIENTS="")
        self.assertEqual((status, calls, facts), (2, [], []))

    def test_snapshot_needs_bash_in_the_image(self) -> None:
        """Without bash in the image no snapshot is attempted."""
        status, calls, facts = self.run_script("qdrant_admin.sh", "snapshot", FAKE_NO_BASH="1")
        self.assertEqual((status, len(calls), facts), (1, 1, []))

    def test_unencrypted_leftovers_are_reported(self) -> None:
        """Plaintext an earlier version left, in the volume or the backup directory, is named, never touched."""
        self.backup_dir.mkdir()
        (self.backup_dir / DUMP).write_bytes(b"an earlier, unencrypted dump")
        status, _, _ = self.run_script("qdrant_admin.sh", "snapshot", FAKE_LEFTOVERS="3")
        self.assertEqual(status, 0, self.stderr)
        self.assertIn("1 unencrypted backup file(s)", self.stderr)
        self.assertIn("3 unencrypted file(s) remain in the qdrant-snapshots volume", self.stderr)
        self.assertTrue((self.backup_dir / DUMP).is_file())

    def test_recover_decrypts_into_the_volume_and_cleans_up(self) -> None:
        """Decrypted into a private .part, renamed, recovered from, then deleted from the volume."""
        snapshot = self.encrypted("qdrant-docs-20260131T020000Z.snapshot.age", b"synthetic snapshot tar")
        identity = self.identity()
        status, calls, facts = self.run_script(
            "qdrant_admin.sh", "recover", "docs", snapshot.name, PROFILE="cuda", IDENTITY=identity
        )
        self.assertEqual(status, 0, self.stderr)
        path = "/qdrant/snapshots/docs/qdrant-docs-20260131T020000Z.snapshot"
        steps = self.qdrant_steps(calls)
        self.assertTrue(steps[0].startswith("exec -T qdrant-cuda sh -c umask 077 && "), steps[0])
        self.assertTrue(steps[0].endswith(f' && cat >"$1.part" _ {path}'), steps[0])
        self.assertEqual(
            steps[1:],
            [
                f"exec -T qdrant-cuda mv {path}.part {path}",
                f"api recover docs {path}",
                f"exec -T qdrant-cuda rm -f {path} {path}.part",
            ],
        )
        self.assertEqual((self.tmp / "streamed").read_bytes(), b"synthetic snapshot tar")
        self.assertEqual(self.age_calls(), [f"age -d -i {identity} {snapshot}"])
        self.assertEqual(facts, ["encryption=age"])

    def test_a_failed_decryption_recovers_nothing(self) -> None:
        """Age fails part-way: the part is never renamed or recovered from, and is deleted."""
        snapshot = self.encrypted("qdrant-docs-20260131T020000Z.snapshot.age", b"synthetic snapshot tar")
        status, calls, _ = self.run_script(
            "qdrant_admin.sh",
            "recover",
            "docs",
            snapshot.name,
            IDENTITY=self.identity(),
            FAKE_AGE_FAIL="decrypt",
            FAKE_AGE_PASS_BYTES="9",
        )
        self.assertEqual(status, 1)
        steps = self.qdrant_steps(calls)
        self.assertEqual(len(steps), 2, steps)
        self.assertTrue(steps[1].startswith("exec -T qdrant-cpu rm -f "), steps[1])
        self.assertEqual((self.tmp / "streamed").read_bytes(), b"synthetic")
        self.assertIn("The collection was not touched", self.stderr)

    def test_a_failed_recovery_still_cleans_up(self) -> None:
        """Qdrant refusing the snapshot: exit 1, the decrypted copy is deleted all the same."""
        snapshot = self.encrypted("qdrant-docs-20260131T020000Z.snapshot.age", b"synthetic snapshot tar")
        status, calls, _ = self.run_script(
            "qdrant_admin.sh", "recover", "docs", snapshot.name, IDENTITY=self.identity(), FAKE_RECOVER="1"
        )
        self.assertEqual(status, 1)
        self.assertTrue(self.qdrant_steps(calls)[-1].startswith("exec -T qdrant-cpu rm -f "))

    def test_recover_refuses_before_anything(self) -> None:
        """Odd names, a missing or plaintext snapshot, no identity: exit 2, nothing run."""
        snapshot = self.encrypted("qdrant-docs-20260131T020000Z.snapshot.age", b"synthetic snapshot tar")
        (self.backup_dir / "docs-1.snapshot").write_bytes(b"an earlier, unencrypted snapshot")
        identity = {"IDENTITY": self.identity()}
        cases: list[tuple[tuple[str, str], dict[str, str]]] = [
            (("../docs", snapshot.name), identity),
            (("docs", "../x.snapshot"), identity),
            (("docs", "a..b"), identity),
            (("docs files", snapshot.name), identity),
            (("docs", "missing.snapshot.age"), identity),
            (("docs", "docs-1.snapshot"), identity),
            (("docs", snapshot.name), {}),
        ]
        for args, env in cases:
            status, calls, _ = self.run_script("qdrant_admin.sh", "recover", *args, **env)
            self.assertEqual((status, calls), (2, []), (args, env))


# A stand-in for stat(1), for the recipients checks: with FAKE_STAT_STYLE=gnu it
# answers GNU's -c '%a %u', with bsd only BSD's -f '%p %u' (the GNU form then
# fails, as on BSD), with none neither. FAKE_STAT_AS, a JSON object, gives the
# answer for a path instead of the path's own mode and owner. Every other call
# goes to the real stat, FAKE_STAT_REAL.
FAKE_STAT = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
style = os.environ.get("FAKE_STAT_STYLE", "gnu")
forms = {("-c", "%a %u"): "gnu", ("-f", "%p %u"): "bsd"}
if len(args) == 3 and tuple(args[:2]) in forms:
    if forms[tuple(args[:2])] != style:
        sys.stderr.write("stat: illegal option\n")
        sys.exit(1)
    answer = json.loads(os.environ.get("FAKE_STAT_AS", "{}")).get(args[2])
    if answer is None:
        info = os.lstat(args[2])
        mode = info.st_mode if style == "bsd" else info.st_mode & 0o7777
        answer = f"{mode:o} {info.st_uid}"
    print(answer)
    sys.exit(0)
os.execv(os.environ["FAKE_STAT_REAL"], ["stat", *args])
"""
# A uid that is neither root nor the account running the tests.
OTHER_UID = os.getuid() + 1


@unittest.skipUnless(HAS_SCRIPTS, "needs bash and the repo's scripts/")
class BackupAgeTest(AgeStandIn):
    """scripts/backup_age.sh: whether a backup can be encrypted, and what its record says about it."""

    def stat_env(self, style: str = "gnu", answers: dict[str, str] | None = None) -> dict[str, str]:
        """Put the stat stand-in first on PATH.

        Args:
            style: ``gnu``, ``bsd`` or ``none``: the form it answers.
            answers: What it answers for a path (physical, as the checks name
                it) instead of the path's own mode and owner.

        Returns:
            The environment to run the check with.
        """
        real = shutil.which("stat", path=self.env["PATH"])
        if real is None:
            self.skipTest("needs stat")
        stand_in = self.tmp / "stat-bin"
        stand_in.mkdir(exist_ok=True)
        (stand_in / "stat").write_text(FAKE_STAT, encoding="utf-8")
        (stand_in / "stat").chmod(0o755)
        return {
            "PATH": f"{stand_in}{os.pathsep}{self.env['PATH']}",
            "FAKE_STAT_REAL": real,
            "FAKE_STAT_STYLE": style,
            "FAKE_STAT_AS": json.dumps(answers or {}),
        }

    def check(self, **env: str) -> subprocess.CompletedProcess[str]:
        """Run ``backup_age.sh recipients``, as make does before a backup.

        Args:
            **env: More environment, e.g. ``BACKUP_RECIPIENTS``.

        Returns:
            The finished process.
        """
        return subprocess.run(
            ["bash", str(SCRIPTS / "backup_age.sh"), "recipients"],
            env={**self.env, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def sourced(self, commands: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run shell commands with the file sourced, as the store scripts use it.

        Args:
            commands: The commands.
            *args: Their positional parameters.

        Returns:
            The finished process.
        """
        script = f'set -euo pipefail; . "{SCRIPTS / "backup_age.sh"}"; {commands}'
        return subprocess.run(
            ["bash", "-c", script, "backup_age", *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_public_keys_are_accepted(self) -> None:
        """Age installed, every line a public key: exit 0, silent, nothing encrypted for real."""
        result = self.check()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.age_calls(), ["age -R <copy> -o /dev/null"])
        self.assertEqual(self.age_read(), [self.recipients.read_text(encoding="utf-8")])
        self.assertEqual(self.left_private(), [])

    def test_no_recipients_file_is_refused_with_how_to_make_one(self) -> None:
        """Unset, missing or unreadable: exit 2, with the offline age-keygen steps."""
        for recipients in ("", str(self.tmp / "missing.txt"), str(self.tmp)):
            result = self.check(BACKUP_RECIPIENTS=recipients)
            self.assertEqual(result.returncode, 2, recipients)
            self.assertIn("age-keygen -o data-plane-backup.key", result.stderr)
            self.assertIn("stays offline", result.stderr)
            self.assertIn("Nothing was done", result.stderr)
        self.assertEqual(self.age_calls(), [])

    def test_a_private_key_is_refused(self) -> None:
        """An identity or an SSH private key in the file: refused, it must not be on the host."""
        for line in (AGE_SECRET, "AGE-PLUGIN-YUBIKEY-1ZZSTANDIN", "-----BEGIN OPENSSH PRIVATE KEY-----"):
            self.recipients.write_text(f"{RECIPIENT_KEYS[0]}\n{line}\n", encoding="utf-8")
            result = self.check()
            self.assertEqual(result.returncode, 2, line)
            self.assertIn("holds a private key", result.stderr)
            self.assertNotIn("data-plane-recipients.", result.stderr)
        self.assertEqual(self.age_calls(), [])
        self.assertEqual(self.left_private(), [])

    def test_a_file_others_can_change_is_refused(self) -> None:
        """Recipients the group or anyone can write would let them choose who reads the next backup."""
        for mode in (0o664, 0o646, 0o666):
            self.recipients.chmod(mode)
            result = self.check()
            self.assertEqual(result.returncode, 2, oct(mode))
            self.assertIn("chmod go-w", result.stderr)
        self.assertEqual(self.age_calls(), [])

    def test_a_directory_others_can_change_is_refused(self) -> None:
        """Whoever can change a directory above the file can put another file in its place."""
        keys = self.tmp / "keys"
        keys.mkdir()
        recipients = keys / "recipients.txt"
        recipients.write_text(f"{RECIPIENT_KEYS[0]}\n", encoding="utf-8")
        recipients.chmod(0o644)
        keys.chmod(0o775)
        result = self.check(BACKUP_RECIPIENTS=str(recipients))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"the directory {os.path.realpath(keys)} (above the recipients file)", result.stderr)
        self.assertIn("chmod go-w", result.stderr)
        self.assertEqual(self.age_calls(), [])
        keys.chmod(0o755)
        result = self.check(BACKUP_RECIPIENTS=str(recipients))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_symbolic_link_is_refused(self) -> None:
        """The file itself is named, never a link to it."""
        link = self.tmp / "link.txt"
        link.symlink_to(self.recipients)
        result = self.check(BACKUP_RECIPIENTS=str(link))
        self.assertEqual(result.returncode, 2)
        self.assertIn("is a symbolic link", result.stderr)
        self.assertEqual(self.age_calls(), [])

    def test_only_root_or_the_backup_account_may_own_the_file_and_its_directories(self) -> None:
        """Their owner chooses who can read the backups."""
        file = os.path.realpath(self.recipients)
        directory = os.path.dirname(file)
        for path in (file, directory):
            result = self.check(**self.stat_env(answers={path: f"644 {OTHER_UID}"}))
            self.assertEqual(result.returncode, 2, path)
            self.assertIn(f"belongs to uid {OTHER_UID}, not to root", result.stderr)
        self.assertEqual(self.age_calls(), [])
        result = self.check(**self.stat_env(answers={file: "644 0", directory: "755 0"}))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_mode_or_owner_that_cannot_be_read_is_refused(self) -> None:
        """Every check fails closed: no answer, or one not in the shape asked for."""
        file = os.path.realpath(self.recipients)
        cases = [self.stat_env("none")]
        cases += [self.stat_env(answers={file: answer}) for answer in ("", "rw-r--r-- 0", "644", "12345 0", "644 x")]
        cases += [self.stat_env("bsd", {file: answer}) for answer in ("644 0", "-rw-r--r-- 0")]
        for env in cases:
            result = self.check(**env)
            self.assertEqual(result.returncode, 2, env["FAKE_STAT_AS"])
            self.assertIn("cannot read the mode and owner", result.stderr)
        self.assertEqual(self.age_calls(), [])

    def test_bsd_stat_answers_the_same(self) -> None:
        """BSD's -f '%p %u' carries the file type in front of the mode; the checks see the same mode."""
        env = self.stat_env("bsd")
        result = self.check(**env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.recipients.chmod(0o664)
        result = self.check(**env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("chmod go-w", result.stderr)

    def test_a_sticky_directory_others_may_write_is_safe(self) -> None:
        """Like /tmp: nobody can rename or remove an entry there they do not own. Without the bit, refused."""
        file = os.path.realpath(self.recipients)
        directory = os.path.dirname(file)
        for style, answer in (("gnu", "1777 0"), ("bsd", "41777 0"), ("gnu", "1775 0")):
            result = self.check(**self.stat_env(style, {directory: answer}))
            self.assertEqual(result.returncode, 0, (answer, result.stderr))
        refused = (("gnu", "777 0", directory), ("bsd", "40777 0", directory), ("gnu", "1666 0", file))
        for style, answer, path in refused:
            result = self.check(**self.stat_env(style, {path: answer}))
            self.assertEqual(result.returncode, 2, answer)
            self.assertIn("chmod go-w", result.stderr)

    def test_the_private_copy_is_what_age_reads_and_is_removed(self) -> None:
        """The file is read once, into a copy only its owner can read; age_cleanup removes it."""
        result = self.sourced(
            'age_recipients; echo "$AGE_RECIPIENTS"; _age_stat "$AGE_RECIPIENTS"; age_cleanup; '
            '[[ -e $AGE_RECIPIENTS ]] || echo removed'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        copy, info, removed = result.stdout.splitlines()
        private = os.path.realpath(self.private)
        self.assertRegex(copy, "^" + re.escape(f"{private}{os.sep}data-plane-recipients.") + "[A-Za-z0-9]{6}$")
        mode, owner = info.split()
        self.assertEqual((int(mode, 8), int(owner)), (0o600, os.getuid()))
        self.assertEqual(removed, "removed")
        self.assertEqual(self.age_read(), [self.recipients.read_text(encoding="utf-8")])
        self.assertEqual(self.left_private(), [])

    def test_age_must_be_installed(self) -> None:
        """Without age on PATH: exit 2, pointing at how to install it."""
        path = os.pathsep.join(("/usr/bin", "/bin"))
        if shutil.which("age", path=path):
            self.skipTest("age is installed system-wide")
        result = self.check(PATH=path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("age is not installed", result.stderr)

    def test_a_line_age_does_not_take_is_refused(self) -> None:
        """Age's own parse decides; its message is shown."""
        self.recipients.write_text(f"{RECIPIENT_KEYS[0]}\nnot-a-public-key\n", encoding="utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn(f'recipient file "{self.recipients}": malformed recipient at line 2', result.stderr)
        self.assertNotIn("data-plane-recipients.", result.stderr)
        self.assertEqual(self.left_private(), [])

    def test_a_path_with_an_ampersand_is_named_as_it_is(self) -> None:
        """Age's message names the recipients file, not the copy, whatever characters its path holds."""
        keys = self.tmp / "a&b"
        keys.mkdir(mode=0o755)
        recipients = keys / "recipients.txt"
        recipients.write_text("not-a-public-key\n", encoding="utf-8")
        recipients.chmod(0o644)
        result = self.check(BACKUP_RECIPIENTS=str(recipients))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f'recipient file "{recipients}": malformed recipient at line 1', result.stderr)
        self.assertNotIn("data-plane-recipients.", result.stderr)

    def test_a_temporary_directory_others_can_change_is_refused(self) -> None:
        """Whoever can change TMPDIR could swap the private copy before age reads it."""
        self.private.chmod(0o777)
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("(above the private copy of the recipients (TMPDIR))", result.stderr)
        self.assertEqual(self.age_calls(), [])
        self.private.chmod(0o1777)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.check(TMPDIR=str(self.tmp / "missing")).returncode, 2)

    def test_a_warning_from_age_is_refused(self) -> None:
        """A key age skips with a warning would make the recipient count wrong."""
        result = self.check(FAKE_AGE_WARN="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ignoring unsupported SSH key", result.stderr)

    def test_the_count_and_fingerprint_follow_ages_reading(self) -> None:
        """Comments, empty lines, CRs and repeats do not count; the order does not matter."""
        keys = (RECIPIENT_KEYS[1], RECIPIENT_KEYS[0])
        self.recipients.write_bytes(
            f"# created offline\r\n{keys[0]}\r\n\r\n{keys[1]}\n{keys[0]}\n#{RECIPIENT_KEYS[1]}x\n".encode()
        )
        result = self.sourced("age_recipients; age_facts /dev/stdout; age_cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["encryption=age", "recipients=2", f"recipients_sha256={recipients_fingerprint(RECIPIENT_KEYS)}"],
        )

    def test_unencrypted_backups_are_warned_about(self) -> None:
        """Dumps and snapshots without .age in the backup directory: counted, left alone."""
        backups = self.tmp / "snapshots"
        backups.mkdir()
        for name in (DUMP, "docs-1.snapshot", f"{DUMP}.age", "qdrant-docs-1.snapshot.age.part"):
            (backups / name).write_bytes(b"x")
        result = self.sourced('warn_plaintext "$1"', str(backups))
        self.assertEqual(result.returncode, 0)
        self.assertIn("2 unencrypted backup file(s)", result.stderr)
        self.assertEqual(len(list(backups.iterdir())), 4)
        self.assertEqual(self.sourced('warn_plaintext "$1"', str(self.tmp / "bin")).stderr, "")

    def test_a_restore_needs_an_identity_and_an_encrypted_file(self) -> None:
        """IDENTITY unset or unreadable, the file missing or unencrypted: exit 2. Binary and armored age files pass."""
        identity = self.tmp / "backup-identity.txt"
        identity.write_text(f"{AGE_SECRET}\n", encoding="utf-8")
        files = {
            "binary": AGE_HEADER + b"DZV1",
            "armored": b"-----BEGIN AGE ENCRYPTED FILE-----\r\nYWdl\r\n-----END AGE ENCRYPTED FILE-----\r\n",
            "plaintext": b"DZV1 an earlier, unencrypted dump",
        }
        for name, content in files.items():
            (self.tmp / name).write_bytes(content)

        def identity_check(name: str, **env: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(SCRIPTS / "backup_age.sh"), "identity", str(self.tmp / name)],
                env={**self.env, **env},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )

        for name in ("binary", "armored"):
            result = identity_check(name, IDENTITY=str(identity))
            self.assertEqual((result.returncode, result.stderr), (0, ""), name)
        result = identity_check("binary")
        self.assertEqual(result.returncode, 2)
        self.assertIn("IDENTITY=<file>", result.stderr)
        self.assertEqual(identity_check("binary", IDENTITY=str(self.tmp / "missing.key")).returncode, 2)
        result = identity_check("plaintext", IDENTITY=str(identity))
        self.assertEqual(result.returncode, 2)
        self.assertIn("plaintext is not age-encrypted", result.stderr)
        self.assertEqual(identity_check("missing", IDENTITY=str(identity)).returncode, 2)
        self.assertEqual(self.age_calls(), [])

    def test_usage(self) -> None:
        """Anything but `recipients` or `identity <file>`: exit 2."""
        for args in ((), ("identity",), ("identity", "a", "b"), ("recipients", "extra"), ("encrypt",)):
            result = subprocess.run(
                ["bash", str(SCRIPTS / "backup_age.sh"), *args],
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, args)


@unittest.skipUnless(HAS_MAKE, "needs make, bash and the repo's Makefile")
class MakefileTest(AgeStandIn):
    """The backup and restore targets, end to end through the scripts, with stand-in compose and age."""

    @override
    def setUp(self) -> None:
        """Create the compose stand-in and its log."""
        super().setUp()
        self.log = self.tmp / "calls.log"
        self.backup_dir = self.tmp / "snapshots"
        compose = self.tmp / "fake-compose"
        compose.write_text(FAKE_STORE_COMPOSE, encoding="utf-8")
        compose.chmod(0o755)
        self.compose = f"{compose} --env-file .env -f docker/compose.yaml"

    def make(self, *args: str, stdin: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Run make in the repo with the stand-ins.

        Args:
            *args: Targets and variables.
            stdin: What make reads, e.g. a restore's confirmation.

        Returns:
            The finished process, and the compose calls in order.
        """
        command = ["make", "-s", "-C", str(AUDIT_DIR.parent), *args, f"COMPOSE={self.compose}"]
        result = subprocess.run(
            [*command, f"BACKUP_DIR={self.backup_dir}"],
            env={**self.env, "FAKE_LOG": str(self.log), "SUDO_USER": "ops.example"},
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        lines: list[str] = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return result, [line.split(" -f docker/compose.yaml ", 1)[-1] for line in lines]

    def test_a_backup_that_cannot_be_encrypted_does_nothing(self) -> None:
        """No recipients file: every backup target refuses before admin-audit or a store is asked."""
        for target in ("backup", "backup-neo4j", "backup-qdrant"):
            result, calls = self.make(target, f"BACKUP_RECIPIENTS={self.tmp / 'missing.txt'}", "REASON=zz backup")
            self.assertNotEqual(result.returncode, 0, target)
            self.assertEqual(calls, [], target)
            self.assertIn("age-keygen -o", result.stderr)
        self.assertEqual(self.age_calls(), [])
        self.assertFalse(self.backup_dir.exists())

    def test_a_backup_carries_its_recipients_to_the_record(self) -> None:
        """BACKUP_RECIPIENTS reaches the dump, and the encryption facts reach admin-audit's record."""
        result, calls = self.make("backup-neo4j", f"BACKUP_RECIPIENTS={self.recipients}", "REASON=zz backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(calls[0].endswith("admin_audit.py begin backup neo4j"), calls[0])
        self.assertEqual([call.split()[0] for call in calls[1:4]], ["stop", "run", "start"])
        (name,) = os.listdir(self.backup_dir)
        record = f"{calls[4]} "
        self.assertIn(" end backup neo4j --status 0 ", record)
        fingerprint = recipients_fingerprint(RECIPIENT_KEYS)
        self.assertIn(f" encryption=age recipients=2 recipients_sha256={fingerprint} file={name} ", record)

    def restorable(self) -> str:
        """Put an encrypted dump, an earlier plaintext one and an encrypted snapshot in place, and an identity.

        Returns:
            The identity file's path.
        """
        self.backup_dir.mkdir()
        (self.backup_dir / f"{DUMP}.age").write_bytes(AGE_HEADER + b"DZV1 synthetic dump")
        (self.backup_dir / DUMP).write_bytes(b"DZV1 an earlier, unencrypted dump")
        (self.backup_dir / "qdrant-docs-1.snapshot.age").write_bytes(AGE_HEADER + b"synthetic snapshot tar")
        identity = self.tmp / "offline" / "backup-identity.txt"
        identity.parent.mkdir()
        identity.write_text(f"{AGE_SECRET}\n", encoding="utf-8")
        return str(identity)

    def test_a_restore_without_a_usable_identity_asks_nothing(self) -> None:
        """No IDENTITY, or a plaintext dump: refused before the confirmation; nothing is called."""
        identity = self.restorable()
        cases = [
            ("restore-neo4j", f"DUMP={DUMP}.age"),
            ("restore-neo4j", f"DUMP={DUMP}", f"IDENTITY={identity}"),
            ("restore-qdrant", "COLLECTION=docs", "SNAPSHOT=qdrant-docs-1.snapshot.age"),
        ]
        for target, *args in cases:
            result, calls = self.make(target, *args, stdin="restore\n")
            self.assertNotEqual(result.returncode, 0, args)
            self.assertEqual(calls, [], args)
            self.assertNotIn("REPLACES", result.stdout, args)
        self.assertEqual(self.age_calls(), [])

    def test_a_restore_decrypts_with_the_identity_of_the_run(self) -> None:
        """Confirmed, then recorded at both ends; the identity's path reaches no record."""
        identity = self.restorable()
        result, calls = self.make("restore-neo4j", f"DUMP={DUMP}.age", f"IDENTITY={identity}", stdin="restore\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(calls[0].endswith(f"admin_audit.py begin restore neo4j file={DUMP}.age"), calls[0])
        self.assertEqual([call.split()[0] for call in calls[1:4]], ["stop", "run", "start"])
        self.assertIn(" end restore neo4j --status 0 ", calls[4])
        self.assertTrue(calls[4].endswith(" encryption=age"), calls[4])
        self.assertNotIn("offline", "".join(calls))
        self.assertEqual(self.age_calls(), [f"age -d -i {identity} {self.backup_dir}/{DUMP}.age"] * 2)


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
        self.bodies: dict[tuple[str, str], bytes] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """Records the request and answers like Qdrant, with a JSON body."""

            def answer(self) -> None:
                """Record the request and send the set status, with the body set for it."""
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append((self.command, self.path, dict(self.headers.items()), body))
                payload = fake.bodies.get((self.command, self.path), b'{"status":"ok","time":0.0}')
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

            def do_POST(self) -> None:
                """Answer a POST."""
                self.answer()

            def do_DELETE(self) -> None:
                """Answer a DELETE."""
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

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one of the helper's commands, as scripts/qdrant_admin.sh does.

        Args:
            *args: The command and its arguments.

        Returns:
            The finished process.
        """
        script = (SCRIPTS / "qdrant_api.sh").read_text(encoding="utf-8")
        return subprocess.run(
            ["bash", "-c", script, "qdrant_api", *args],
            env={"PATH": os.environ["PATH"], "QDRANT_PORT": str(self.qdrant.port), "QDRANT__SERVICE__API_KEY": "zz-k"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_the_collections_are_listed(self) -> None:
        """One name per line, as Qdrant spells them."""
        listing = '{"result":{"collections":[{"name":"docs"},{"name":"case files 2026"}]},"status":"ok"}'
        self.qdrant.bodies[("GET", "/collections")] = listing.encode()
        result = self.command("collections")
        self.assertEqual((result.returncode, result.stdout), (0, "docs\ncase files 2026\n"), result.stderr)
        self.qdrant.bodies[("GET", "/collections")] = b'{"result":{"collections":[]},"status":"ok"}'
        self.assertEqual((self.command("collections").returncode, self.command("collections").stdout), (0, ""))
        self.qdrant.status = 500
        self.assertEqual(self.command("collections").returncode, 1)

    def test_a_snapshot_is_made_and_named(self) -> None:
        """POST to the collection, its name percent-encoded; the snapshot's file name is printed."""
        path = "/collections/case%20files%202026-%C3%A4/snapshots"
        name = "case files 2026-" + chr(0xE4) + "-1-2026-11-04-02-00-00.snapshot"
        # Qdrant answers in UTF-8, not with \u escapes.
        answer = {"result": {"name": name, "size": 1}}
        self.qdrant.bodies[("POST", path)] = json.dumps(answer, ensure_ascii=False).encode()
        result = self.command("snapshot", "case files 2026-" + chr(0xE4))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{name}\n")
        ((method, sent_path, headers, _),) = self.qdrant.requests
        self.assertEqual((method, sent_path, headers.get("api-key")), ("POST", path, "zz-k"))

    def test_a_failed_snapshot_names_nothing(self) -> None:
        """An error answer, or no file name in it: exit 1, nothing printed."""
        self.qdrant.status = 500
        result = self.command("snapshot", "docs")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("the snapshot of docs failed", result.stderr)
        self.qdrant.status = 200
        self.qdrant.bodies[("POST", "/collections/docs/snapshots")] = b'{"result":{"name":"../x"},"status":"ok"}'
        self.assertEqual(self.command("snapshot", "docs").returncode, 1)

    def test_a_snapshot_is_dropped(self) -> None:
        """DELETE names the collection and the snapshot, both percent-encoded; an error answer is exit 1."""
        result = self.command("drop-snapshot", "case files", "case files-1.snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        ((method, path, _, _),) = self.qdrant.requests
        self.assertEqual((method, path), ("DELETE", "/collections/case%20files/snapshots/case%20files-1.snapshot"))
        self.qdrant.status = 404
        self.assertEqual(self.command("drop-snapshot", "docs", "docs-1.snapshot").returncode, 1)

    def test_usage(self) -> None:
        """Unknown commands and missing arguments: exit 2, no request."""
        for args in (("vacuum",), ("snapshot",), ("drop-snapshot", "docs"), ("collections", "docs")):
            self.assertEqual(self.command(*args).returncode, 2, args)
        self.assertEqual(self.qdrant.requests, [])


if __name__ == "__main__":
    unittest.main()

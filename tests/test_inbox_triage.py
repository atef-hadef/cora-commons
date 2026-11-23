from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from cora_mcp.inbox_triage import is_noise, score, triage


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inbox_triage"
NOW = "2026-11-21T09:00:00Z"
PROFILE = {"vip": ["prism-bot"], "owner_aliases": ["@orbit"], "pressing_topics": ["seed vault"]}
TOP_FIVE = ["sim-004", "sim-057", "sim-050", "sim-043", "sim-036"]


def message(**changes):
    return {
        "id": "note-a", "channel": "telegram", "chat_id": "tile-room", "chat_type": "group",
        "sender_id": "tile-bot", "sender_name": "Tile Bot", "from_me": False, "kind": "text",
        "body": "The tray is ready.", "ts": "2026-11-21T08:00:00Z", **changes,
    }


def fixture_inputs():
    rows = (FIXTURES / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(row) for row in rows], json.loads((FIXTURES / "profile.json").read_text(encoding="utf-8"))


class NoiseTests(unittest.TestCase):
    def test_membership_notice_requires_a_participant_subject(self):
        for body in ("Nobody left the group.", "Everyone joined the group.", "No One left the group."):
            with self.subTest(body=body):
                self.assertEqual(is_noise(message(body=body)), (False, ""))
        for body in ("Tile Bot left.", "Orbit Bot left the group.", "Spindle joined the group.",
                     "@nobody left the group."):
            with self.subTest(body=body):
                self.assertEqual(is_noise(message(body=body)), (True, "system"))
        self.assertEqual(is_noise(message(kind="system", body="Nobody left the group.")), (True, "system"))

    def test_text_membership_notices_require_context(self):
        for body in ("Left.", "Joined!", "I left the group.", "Tile Bot left the call."):
            with self.subTest(body=body):
                self.assertEqual(is_noise(message(body=body)), (False, ""))
        for body in ("Tile Bot left.", "Orbit Bot left the group.", "Joined the group."):
            with self.subTest(body=body):
                self.assertEqual(is_noise(message(body=body)), (True, "system"))
        self.assertEqual(is_noise(message(body="Left.", kind="system")), (True, "system"))

    def test_individual_noise_rules_and_precedence(self):
        cases = [
            ({"body": ""}, "empty_body"), ({"kind": "media", "body": " \t "}, "empty_body"),
            ({"kind": "reaction", "body": ""}, "reaction"), ({"kind": "edit"}, "edit"),
            ({"kind": "delete"}, "delete"), ({"kind": "system"}, "system"),
            ({"chat_id": "status@broadcast", "body": ""}, "status_broadcast"),
            ({"from_me": True, "kind": "edit"}, "from_me"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(is_noise(message(**changes)), (True, expected))

    def test_text_system_notices(self):
        for body in ("Tile Bot joined the group.", "Tile Bot left", "Tile Bot changed the group subject to Tiles.",
                     "Messages and calls are end-to-end encrypted."):
            with self.subTest(body=body):
                self.assertEqual(is_noise(message(body=body)), (True, "system"))

    def test_kept_controls(self):
        for changes in ({"body": "I left my tray behind."}, {"body": "Tile Bot joined the call."},
                        {"body": "Are messages end-to-end encrypted?"}, {"kind": "media"},
                        {"chat_type": "broadcast"}, {"sender_name": "Orbit Desk"}):
            with self.subTest(changes=changes):
                self.assertEqual(is_noise(message(**changes)), (False, ""))

    def test_duplicate_window_is_inclusive_and_anchored_at_last_kept(self):
        for instant, duplicate in [("08:09:59", True), ("08:10:00", True), ("08:10:01", False)]:
            later = message(id="later", ts=f"2026-11-21T{instant}Z")
            with self.subTest(instant=instant):
                result = triage([later, message()], {}, NOW)
                self.assertEqual(result["dropped_by_reason"], {"duplicate": 1} if duplicate else {})
                expected = ["note-a"] if duplicate else ["later", "note-a"]
                self.assertEqual([row["id"] for row in result["messages"]], expected)
        rows = [message(id=minute, ts=f"2026-11-21T08:{minute}:00Z") for minute in ("18", "09", "00")]
        self.assertEqual([r["id"] for r in triage(rows, {}, NOW)["messages"]], ["18", "00"])

    def test_exact_body_and_conversation_identity(self):
        for changes in ({"sender_id": "reed-bot"}, {"chat_id": "reed-room"}, {"channel": "email"},
                        {"kind": "media"}, {"body": "The tray is ready. "}, {"body": "THE TRAY IS READY."}):
            with self.subTest(changes=changes):
                result = triage([message(), message(id="note-b", **changes)], {}, NOW)
                self.assertEqual(result["dropped_by_reason"], {})
        result = triage([message(), message(id="new-id", sender_name="New label")], {}, NOW)
        self.assertEqual([r["id"] for r in result["messages"]], ["note-a"])


class ScoringTests(unittest.TestCase):
    def test_question_bonus_follows_the_addressee_of_the_question(self):
        profile = {"owner_aliases": ["@orbit-desk"]}
        for body, expected in [
            ("@orbit-desk is away today, @tile, can you cover?", (0.10, ["Money, date or deadline"])),
            ("@orbit-desk can help, @tile can you cover?", (0, [])),
            ("@tile, did you ask @orbit-desk?", (0, [])),
            ("@orbit-desk, thanks, @tile, can you cover?", (0, [])),
            ("@orbit-desk-helper, can you cover?", (0, [])),
            ("@orbit-desk, can you cover?", (0.20, ["Question addressed to owner"])),
            ("@tile is away, @orbit-desk, can you cover?", (0.20, ["Question addressed to owner"])),
            ("@orbit-desk, can you ask @tile?", (0.20, ["Question addressed to owner"])),
        ]:
            with self.subTest(body=body):
                self.assertEqual(score(message(body=body), profile), expected)

    def test_group_question_requires_a_complete_alias_as_addressee(self):
        profile = {"owner_aliases": ["@orbit-desk", "Orbit Desk"]}
        for body in ("@ORBIT-DESK, can you confirm?", "Orbit Desk: ready?", "Hey @orbit-desk can you check?",
                     "Can you check, @orbit-desk?", "Tray loaded. @orbit-desk, ready?"):
            with self.subTest(body=body):
                self.assertEqual(score(message(body=body), profile), (0.20, ["Question addressed to owner"]))
        for body in ("@orbit-desk-helper, ready?", "@orbit-desk.helper, ready?", "Orbit Desk Two, ready?",
                     "See @orbit-desk. @tile, can you confirm?", "@orbit-desk sent a note, is @tile ready?",
                     "Did anyone ask @orbit-desk?", "Are you ready? Thanks, @orbit-desk."):
            with self.subTest(body=body):
                self.assertEqual(score(message(body=body), profile), (0, []))

    def test_supplied_unanswered_duration_respects_elapsed_time(self):
        for sent, duration, status, hours in [
            (NOW, 172800, {}, 0),
            ("2026-11-20T21:00:00Z", 172800, {}, 12),
            ("2026-11-20T21:00:00Z", 3600, {"unanswered": True}, 1),
            ("2026-11-20T21:00:00Z", 172800, {"unanswered": False}, 0),
            ("2026-11-17T09:00:00Z", 259200, {}, 48),
            ("2026-11-22T09:00:00Z", 172800, {}, 0),
        ]:
            with self.subTest(sent=sent, duration=duration, status=status):
                msg = message(ts=sent, unanswered_age_seconds=duration, **status)
                row = triage([msg], {}, NOW)["messages"][0]
                self.assertAlmostEqual(row["score"], 0.15 * hours / 48)
                self.assertEqual(bool(row["reasons"]), hours > 0)

    def test_individual_signals(self):
        cases = [
            ({}, (0, [])),
            ({"chat_type": "direct"}, (0.15, ["Direct chat"])),
            ({"sender_id": "prism-bot"}, (0.25, ["VIP sender"])),
            ({"body": "@Orbit, can you check?"}, (0.20, ["Question addressed to owner"])),
            ({"chat_type": "direct", "body": "Ready?"}, (0.35, ["Direct chat", "Question addressed to owner"])),
            ({"body": "Seed vault and SEED VAULT"}, (0.15, ["Pressing topic"])),
            ({"unanswered_age_seconds": 86400}, (0.075, ["Explicitly unanswered (age capped at 48h)"])),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(score(message(**changes), PROFILE), expected)
        for body in ("Quote: $12.", "Meet on 2026-12-12.", "The deadline moved.", "Payment due tomorrow."):
            with self.subTest(body=body):
                self.assertEqual(score(message(body=body), PROFILE), (0.10, ["Money, date or deadline"]))

    def test_signals_require_a_match(self):
        for changes in ({"body": "Anyone ready?"}, {"body": "@orbit, thanks."}, {"body": "@orbital, ready?"},
                        {"body": "seed vaults"}, {"sender_name": "prism-bot"}, {"body": "Overdueish label"},
                        {"unanswered": True}, {"unanswered_age_seconds": 0}):
            with self.subTest(changes=changes):
                self.assertEqual(score(message(**changes), PROFILE), (0, []))

    def test_explicit_status_age_cap_and_clock(self):
        msg = message(ts="2026-11-19T09:00:00Z", unanswered=True)
        for now, expected in (("2026-11-19T08:59:59Z", 0), ("2026-11-19T09:00:00Z", 0),
                              ("2026-11-20T09:00:00Z", 0.075), (NOW, 0.15), ("2026-12-01T09:00:00Z", 0.15)):
            with self.subTest(now=now):
                row = triage([msg], {}, now)["messages"][0]
                self.assertEqual(row["score"], expected)
                self.assertEqual(bool(row["reasons"]), expected > 0)

    def test_missing_history_does_not_imply_unanswered(self):
        for status in ({}, {"unanswered": False}, {"unanswered": "true"},
                       {"unanswered": False, "unanswered_age_seconds": 86400}):
            msg = message(ts="2026-01-01T00:00:00Z", **status)
            for history in ([], [message(from_me=True)]):
                with self.subTest(status=status, history=history):
                    self.assertEqual(triage([msg, *history], {}, NOW)["messages"][0]["score"], 0)

    def test_full_score_and_reason_order(self):
        msg = message(chat_type="direct", sender_id="prism-bot", body="Seed vault deadline?",
                      unanswered_age_seconds=300000)
        self.assertEqual(score(msg, PROFILE), (1.0, [
            "Direct chat", "VIP sender", "Question addressed to owner", "Pressing topic",
            "Money, date or deadline", "Explicitly unanswered (age capped at 48h)",
        ]))

    def test_duration_requires_numeric_evidence_and_respects_clock(self):
        for invalid in ("86400", True, -1, float("inf"), float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                score(message(unanswered_age_seconds=invalid), PROFILE)
        msg = message(ts="2026-11-22T09:00:00Z", unanswered_age_seconds=86400)
        self.assertEqual(triage([msg], PROFILE, NOW)["messages"][0]["score"], 0)


class TriageTests(unittest.TestCase):
    def test_sort_by_score_then_instant_then_input_order(self):
        rows = [message(id="older", body="old"), message(id="first", body="a", ts="2026-11-21T10:30:00+02:00"),
                message(id="second", body="b", ts="2026-11-21T08:30:00Z"),
                message(id="highest", body="urgent", sender_id="prism-bot", ts="2026-11-20T00:00:00Z")]
        self.assertEqual([r["id"] for r in triage(rows, PROFILE, NOW)["messages"]],
                         ["highest", "first", "second", "older"])

    def test_empty_and_all_noise(self):
        self.assertEqual(triage([], {}, NOW), {"messages": [], "dropped_by_reason": {}})
        self.assertEqual(triage([message(from_me=True)], {}, NOW),
                         {"messages": [], "dropped_by_reason": {"from_me": 1}})

    def test_fixture_counts_rank_and_repeatability(self):
        messages, profile = fixture_inputs()
        original = copy.deepcopy(messages)
        result = triage(messages, profile, NOW)
        self.assertEqual(result["dropped_by_reason"], {
            "delete": 2, "duplicate": 3, "edit": 2, "empty_body": 2, "from_me": 2,
            "reaction": 2, "status_broadcast": 2, "system": 5,
        })
        self.assertEqual(len(messages), 60)
        self.assertEqual(len(result["messages"]), 40)
        self.assertEqual([r["id"] for r in result["messages"][:5]], TOP_FIVE)
        self.assertEqual(triage(messages, profile, NOW), result)
        self.assertEqual(messages, original)

    def test_cli_top_twenty_matches_core_and_repeats_byte_for_byte(self):
        command = [sys.executable, "-m", "cora_mcp.inbox_triage", "--input", str(FIXTURES / "messages.jsonl"),
                   "--profile", str(FIXTURES / "profile.json"), "--now", NOW]
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        first = subprocess.run(command, env=env, capture_output=True, check=True)
        second = subprocess.run(command, env=env, capture_output=True, check=True)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, b"")
        heading, blank, *lines = first.stdout.decode().splitlines()
        self.assertEqual((heading, blank), ("# Inbox triage — top 20", ""))
        self.assertEqual(len(lines), 20)
        messages, profile = fixture_inputs()
        for i, (line, row) in enumerate(zip(lines, triage(messages, profile, NOW)["messages"]), 1):
            self.assertTrue(line.startswith(f'{i}. **{row["id"]}**'))
            self.assertIn(row["body"], line)


class InboxTriageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_process_tool_returns_complete_structured_result(self):
        from mcp import Client
        from cora_mcp.server import mcp

        messages, profile = fixture_inputs()
        async with Client(mcp) as client:
            result = await client.call_tool("inbox_triage", {"messages": messages, "profile": profile, "now": NOW})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, triage(messages, profile, NOW))

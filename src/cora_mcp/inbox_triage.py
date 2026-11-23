"""Deterministic triage of caller-supplied inbox records.

`from_me` identifies the owner. Profile lists are optional: `vip` contains
exact sender IDs, `pressing_topics` contains literal words or phrases, and
`owner_aliases` supplies names or handles for recognizing group questions.
Question marks, English event notices and money/date/deadline expressions are
heuristics. Other broadcasts and groups remain eligible.

The issue's base schema contains no reply status. As an optional convention,
callers with explicit evidence may set `unanswered` to the JSON boolean true
as of the supplied clock. Alternatively, standalone score callers can supply
an explicit nonnegative `unanswered_age_seconds` measurement. An explicit
false status overrides a duration. In triage, elapsed time since the message
also bounds the credit. A missing reply in the batch supplies no such evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DUPLICATE_WINDOW = timedelta(minutes=10)
AGE_CAP = timedelta(hours=48)
ENCRYPTION_NOTICE = re.compile(
    r"messages (?:and calls )?(?:in this chat )?are end-to-end encrypted"
    r"(?:[.!] (?:no one outside of this chat|only people in this chat).*)?[.!]?",
    re.IGNORECASE,
)
GROUP_EVENT = re.compile(
    r"(?:joined(?: (?:the |this )?group)?(?: (?:via|using) (?:an? |this )?invite link)?"
    r"|left(?: (?:the |this )?group)?"
    r"|changed (?:the )?(?:group )?subject(?: (?:from .+ to|to) .+)?)"
    r"[.!]?", re.IGNORECASE,
)
# Exporters can report another participant's membership change as text.
# Require a handle or name and an explicit group reference. A capitalized
# pronoun or quantifier at the start of a sentence isn't evidence of a name.
NAMED_MEMBERSHIP_NOTICE = re.compile(
    r"(?!(?i:I|we|they|you|he|she|it|no|nobody|none|all|both|few|many|most|several|"
    r"(?:any|every|some)(?:one|body)?)\b)(?:@[\w.-]+|[A-Z][\w'-]*(?: [A-Z][\w'-]*)*) "
    r"(?i:(?:joined|left) (?:the |this )?group)[.!]?"
)
MONEY_DATE_DEADLINE = re.compile(
    r"[$€£¥]\s*\d|\b\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|dollars?|euros?|pounds?)\b"
    r"|\b(?:USD|EUR|GBP)\s*\d|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b"
    r"|\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|deadline|due|overdue|invoice|payment|budget)\b", re.IGNORECASE,
)


def is_noise(msg: dict[str, Any]) -> tuple[bool, str]:
    """Return the first matching drop reason, or an empty reason for a candidate."""
    if msg["from_me"] is True:
        return True, "from_me"
    if msg["chat_id"] == "status@broadcast":
        return True, "status_broadcast"
    kind = msg["kind"]
    if kind in ("reaction", "edit", "delete", "system"):
        return True, kind
    body = msg["body"].strip()
    if not body:
        return True, "empty_body"
    if kind == "text":
        # Some exporters put the sender's name and the event in a text row.
        if ENCRYPTION_NOTICE.fullmatch(body):
            return True, "system"
        if msg["chat_type"] == "group":
            event = body.removeprefix(msg["sender_name"] + " ")
            # Bare "left" or "joined" also occur as ordinary replies.
            if GROUP_EVENT.fullmatch(event) and (event != body or " " in event):
                return True, "system"
            if NAMED_MEMBERSHIP_NOTICE.fullmatch(body):
                return True, "system"
    return False, ""


def _mentions(body: str, terms: list[str]) -> bool:
    return any(
        re.search(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", body, re.IGNORECASE)
        for term in terms if term.strip()
    )


def _group_question_to_owner(body: str, aliases: list[str]) -> bool:
    """Resolve direct address within the clause that asks the question."""
    owners = {alias.strip().casefold() for alias in aliases if alias.strip()}
    if not owners:
        return False
    names = "|".join(re.escape(name) for name in sorted(owners))
    opener = re.compile(
        rf"(?:(?P<addressee>{names}|@[\w.-]+)\s+)?(?:can|could|will|would|do|did|"
        r"does|is|are|have|has|what|when|where|which|who|why|how)\b"
    )
    for sentence in re.split(r"(?<=[.!?;])\s+|[\r\n]+", body):
        question, mark, _ = sentence.partition("?")
        if not mark:
            continue
        clauses = [
            re.sub(r"^(?:hey|hi|hello)\s+", "", part.strip().casefold())
            for part in re.split(r"[:,]", question)
        ]
        if clauses[-1] in owners:  # "Can you cover, @desk?"
            return True
        for index in range(len(clauses) - 1, -1, -1):
            match = opener.match(clauses[index])
            if match and match["addressee"]:
                # Before a comma, "@desk is away" can introduce context.
                # Require second-person address to treat it as the question.
                remainder = clauses[index][match.end():].lstrip()
                if match["addressee"] in owners and (
                    index == len(clauses) - 1 or re.match(r"(?:you|your)\b", remainder)
                ):
                    return True
                break
            if match or index == len(clauses) - 1:
                preceding = clauses[index - 1] if index else ""
                if preceding in owners:
                    return True
                # An explicit question or another addressee takes precedence
                # over contextual mentions in earlier clauses.
                if match or re.fullmatch(r"@[\w.-]+", preceding):
                    break
    return False


def score(msg: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    """Award each signal once, with linear age credit up to 15 points at 48h."""
    return _score(msg, profile, elapsed_seconds=None)


def _score(msg: dict[str, Any], profile: dict[str, Any], elapsed_seconds: float | None) -> tuple[float, list[str]]:
    points = 0
    reasons = []
    body = msg["body"]
    direct = msg["chat_type"] == "direct"
    if direct:
        points += 15
        reasons.append("Direct chat")
    if msg["sender_id"] in profile.get("vip", []):
        points += 25
        reasons.append("VIP sender")
    group_question = msg["chat_type"] == "group" and "?" in body and _group_question_to_owner(
        body, profile.get("owner_aliases", [])
    )
    if (direct and "?" in body) or group_question:
        points += 20
        reasons.append("Question addressed to owner")
    if _mentions(body, profile.get("pressing_topics", [])):
        points += 15
        reasons.append("Pressing topic")
    if MONEY_DATE_DEADLINE.search(body):
        points += 10
        reasons.append("Money, date or deadline")
    age = msg.get("unanswered_age_seconds")
    if age is None and msg.get("unanswered") is True:
        age = elapsed_seconds
    if age is not None and msg.get("unanswered", True) is True:
        if type(age) not in (int, float) or not 0 <= age < math.inf:
            raise ValueError("unanswered_age_seconds must be a finite, nonnegative number")
        if elapsed_seconds is not None:
            age = min(age, elapsed_seconds)
        if age > 0:
            points += 15 * min(age, AGE_CAP.total_seconds()) / AGE_CAP.total_seconds()
            reasons.append("Explicitly unanswered (age capped at 48h)")
    return points / 100, reasons


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset or Z")
    return instant


def triage(messages: list[dict[str, Any]], profile: dict[str, Any], now: str) -> dict[str, Any]:
    """Return all ranked candidates and one drop reason per discarded message.

    Deduplicate exact bodies within a sender, channel, chat and kind. The
    inclusive window starts at the last kept copy. Equal instants retain
    input order throughout. Future messages receive zero age points.
    """
    as_of = _instant(now)
    dated = [(_instant(msg["ts"]), msg) for msg in messages]
    dated.sort(key=lambda item: item[0])
    dropped: Counter[str] = Counter()
    last_kept = {}
    ranked = []
    for sent, msg in dated:
        noise, reason = is_noise(msg)
        if noise:
            dropped[reason] += 1
            continue
        key = (msg["channel"], msg["chat_id"], msg["sender_id"], msg["kind"], msg["body"])
        if key in last_kept and sent - last_kept[key] <= DUPLICATE_WINDOW:
            dropped["duplicate"] += 1
            continue
        last_kept[key] = sent
        elapsed_seconds = max(0, (as_of - sent).total_seconds())
        value, reasons = _score(msg, profile, elapsed_seconds)
        ranked.append((sent, {**msg, "score": value, "reasons": reasons}))

    ranked.sort(key=lambda item: (item[1]["score"], item[0]), reverse=True)
    return {"messages": [msg for _, msg in ranked], "dropped_by_reason": dict(sorted(dropped.items()))}


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter and rank synthetic inbox messages")
    parser.add_argument("--input", required=True, type=Path, help="JSONL message file")
    parser.add_argument("--profile", required=True, type=Path, help="JSON profile file")
    parser.add_argument("--now", required=True, help="ISO 8601 timestamp with UTC offset")
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8") as source:
            messages = [json.loads(line) for line in source if line.strip()]
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        result = triage(messages, profile, args.now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))

    print("# Inbox triage — top 20\n")
    for index, msg in enumerate(result["messages"][:20], 1):
        body = " ".join(msg["body"].split())
        reasons = ", ".join(msg["reasons"]) or "No matching signals"
        print(f'{index}. **{msg["id"]}** ({msg["score"]:.3f}) — {body} ({reasons})')
    if not result["messages"]:
        print("_No kept messages._")


if __name__ == "__main__":
    main()

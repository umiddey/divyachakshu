"""Deterministic explicit-trigger gate — decides what deserves attention.

Rules (first match wins):
1. Our own ``<@id>``/``<@!id>`` markup written in the content -> RESPOND.
2. ``oi:`` command-style prefix -> RESPOND.
3. ``#oi``, ``#audit`` or ``#bugreport`` marker -> RESPOND.
4. Our name used as a direct call -> RESPOND.
5. Our id present only as a reply ping -> RESPOND, unless the content clearly
   addresses another member.
6. Everything else -> IGNORE.

A Discord reply adds the replied-to author to ``mentions`` without any
markup in the content, so an id alone does not prove we were addressed.

Author identity and ordinary claim vocabulary are context, not permission to
interrupt a conversation with an autonomous reply.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache


class Action(str, Enum):
    """What the pipeline should do with a message."""

    IGNORE = "ignore"
    RESPOND = "respond"


@dataclass
class GateDecision:
    """Result of gating one incoming message.

    Attributes:
        action: Pipeline action to take.
        reason: Short human-readable explanation for logs.
    """

    action: Action
    reason: str


@dataclass(frozen=True)
class MentionedUser:
    """One other user Discord resolved inside a message.

    Attributes:
        id: Discord user id.
        name: Best display name available for the user (may be empty).
        bot: Whether the mentioned user is itself a bot.
    """

    id: int
    name: str = ""
    bot: bool = False


_COMMAND_RE = re.compile(r"^\s*oi\s*:", re.IGNORECASE)
_MARKER_RE = re.compile(
    r"(?<!\w)#(?:oi|audit|bugreport)\b", re.IGNORECASE
)
_ANY_MENTION_RE = re.compile(r"<@!?(\d+)>")
_GREETINGS = r"(?:hey|hi|yo|ok)"


@lru_cache(maxsize=128)
def _call_pattern(name: str) -> re.Pattern[str]:
    """Build the direct-call pattern for one display name.

    A call is the name at the very start followed by punctuation
    ("ultron, ...", "ultron: ..."), right after a greeting word
    ("hey ultron ..."), or at the end after a comma ("..., ultron?").
    A name followed by ordinary prose ("Ultron said X earlier") is
    narration about the bot, never a call.
    """
    escaped = re.escape(name.strip())
    return re.compile(
        rf"^[^\w]*{escaped}\s*[,;:!?]"
        rf"|^{_GREETINGS}[\s,]+{escaped}\b"
        rf"|,\s*{escaped}\b[\s?!.…]*$",
        re.IGNORECASE,
    )


def _called_by_name(content: str, name: str) -> bool:
    """Return True when the content uses the name as a direct call."""
    cleaned = name.strip()
    if not cleaned:
        return False
    return _call_pattern(cleaned).search(content) is not None


def _own_markup(content: str, bot_user_id: int) -> bool:
    """Return True when the content itself writes our mention markup."""
    if not bot_user_id:
        return False
    return re.search(rf"<@!?{bot_user_id}>", content) is not None


def _addresses_other(
    content: str,
    bot_user_id: int,
    mentioned_users: Sequence[MentionedUser],
) -> bool:
    """Return True when the content clearly addresses another member."""
    for match in _ANY_MENTION_RE.finditer(content):
        if int(match.group(1)) != bot_user_id:
            return True
    for user in mentioned_users:
        if user.id != bot_user_id and _called_by_name(content, user.name):
            return True
    return False


def evaluate(
    content: str,
    mention_ids: list[int],
    bot_user_id: int,
    *,
    bot_names: Sequence[str] = (),
    mentioned_users: Sequence[MentionedUser] = (),
    reply_to_user_id: int | None = None,
) -> GateDecision:
    """Admit only messages that explicitly request OI's attention.

    Args:
        content: Message text to inspect for mention markup, command and
            hashtag markers, and direct name calls.
        mention_ids: Ids of users mentioned in the message (includes reply
            pings, which say nothing about who was addressed).
        bot_user_id: Our bot application user id.
        bot_names: Names we can be called by (account name, display name,
            guild nickname).
        mentioned_users: The other users Discord resolved in the message.
        reply_to_user_id: Author id of the replied-to message when
            resolvable, else None.

    Returns:
        GateDecision with the action and its justification.
    """
    if _own_markup(content, bot_user_id):
        return GateDecision(Action.RESPOND, "bot mentioned")
    if _COMMAND_RE.search(content):
        return GateDecision(Action.RESPOND, "oi command")
    marker = _MARKER_RE.search(content)
    if marker:
        return GateDecision(
            Action.RESPOND, f"explicit marker '{marker.group(0).lower()}'"
        )
    if any(_called_by_name(content, name) for name in bot_names):
        return GateDecision(Action.RESPOND, "bot named")
    if bot_user_id and bot_user_id in mention_ids:
        # The id alone is a reply ping: Discord adds the replied-to author
        # to `mentions` without any markup, so check who was really
        # addressed before answering.
        if (reply_to_user_id is not None and reply_to_user_id != bot_user_id) or _addresses_other(
            content, bot_user_id, mentioned_users
        ):
            return GateDecision(Action.IGNORE, "addressed to another member")
        return GateDecision(Action.RESPOND, "reply to bot")
    return GateDecision(Action.IGNORE, "no explicit trigger")

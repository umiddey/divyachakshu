"""Tests for the deterministic explicit-trigger gate."""

from oi_agent.watch.gate import Action, MentionedUser, evaluate

BOT_ID = 99
OWN_NAMES = ("OI", "Ultron")
DWIGHT = MentionedUser(id=77, name="Dwight", bot=False)
HELPER_BOT = MentionedUser(id=55, name="Helper", bot=True)


# --- rule a: our own mention markup in the content ---------------------------


def test_own_mention_markup_responds():
    """Written <@id> markup is an explicit address."""
    d = evaluate("<@99> check this", [], BOT_ID)
    assert d.action == Action.RESPOND
    assert d.reason == "bot mentioned"


def test_own_nickname_mention_markup_responds():
    """The <@!id> nickname form is an explicit address too."""
    d = evaluate("<@!99> run the audit", [42], BOT_ID)
    assert d.action == Action.RESPOND
    assert d.reason == "bot mentioned"


def test_both_bots_written_responds():
    """A message written to two bots still addresses this one."""
    d = evaluate("<@99> <@55> coordinate the deploy", [99, 55], BOT_ID)
    assert d.action == Action.RESPOND
    assert d.reason == "bot mentioned"


def test_markup_wins_over_reply_to_another_member():
    """An explicit @us inside a reply to someone else is still an address."""
    d = evaluate(
        "<@99> do it", [99], BOT_ID,
        mentioned_users=[DWIGHT], reply_to_user_id=DWIGHT.id,
    )
    assert d.action == Action.RESPOND
    assert d.reason == "bot mentioned"


# --- rule b: command prefix and markers --------------------------------------


def test_oi_command_responds():
    """The command-style prefix admits a message without a mention."""
    d = evaluate("  OI: inspect the migration", [], BOT_ID)
    assert d.action == Action.RESPOND
    assert d.reason == "oi command"


def test_explicit_markers_respond():
    """Supported hashtags admit a message regardless of author."""
    for marker in ("#oi", "#audit", "#bugreport"):
        d = evaluate(f"please inspect this {marker}", [], BOT_ID)
        assert d.action == Action.RESPOND
        assert marker in d.reason


# --- rule c: our name used as a direct call ----------------------------------


def test_name_call_at_start_responds():
    """A leading name call admits the message without any @."""
    d = evaluate("Ultron, check this", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.RESPOND
    assert d.reason == "bot named"


def test_name_call_with_colon_responds():
    d = evaluate("ultron: status of the release", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.RESPOND


def test_greeting_name_call_responds():
    d = evaluate("hey ultron can you see this", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.RESPOND
    assert d.reason == "bot named"


def test_name_call_at_end_after_comma_responds():
    d = evaluate("what do you think, ultron?", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.RESPOND
    assert d.reason == "bot named"


def test_name_narration_is_ignored():
    """Talking ABOUT the bot is not calling it."""
    d = evaluate("Ultron said X earlier", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.IGNORE
    assert d.reason == "no explicit trigger"


def test_name_in_prose_is_ignored():
    d = evaluate("the ultron service depends on it", [], BOT_ID, bot_names=OWN_NAMES)
    assert d.action == Action.IGNORE


# --- rule d: bare reply ping, unless someone else is addressed ---------------


def test_reply_ping_to_us_responds():
    """The 13:11 UTC shape: a reply ping with a neutral question is for us."""
    d = evaluate(
        "Use OpenStreetMap and find businesses that match our ICP",
        [BOT_ID], BOT_ID,
        mentioned_users=[],
        reply_to_user_id=BOT_ID,
    )
    assert d.action == Action.RESPOND
    assert d.reason == "reply to bot"


def test_reply_ping_with_markup_for_another_member_is_ignored():
    d = evaluate(
        "<@77> do X", [BOT_ID, DWIGHT.id], BOT_ID,
        mentioned_users=[DWIGHT],
        reply_to_user_id=BOT_ID,
    )
    assert d.action == Action.IGNORE
    assert d.reason == "addressed to another member"


def test_reply_ping_with_name_call_for_another_member_is_ignored():
    d = evaluate(
        "Dwight, what do you think", [BOT_ID], BOT_ID,
        mentioned_users=[DWIGHT],
        reply_to_user_id=BOT_ID,
    )
    assert d.action == Action.IGNORE
    assert d.reason == "addressed to another member"


def test_reply_to_another_member_is_ignored_even_when_pinged():
    """A reply aimed at another member is addressed to that member."""
    d = evaluate(
        "what do you think", [BOT_ID], BOT_ID,
        reply_to_user_id=DWIGHT.id,
    )
    assert d.action == Action.IGNORE
    assert d.reason == "addressed to another member"


def test_reply_ping_naming_another_bot_is_ignored():
    d = evaluate(
        "<@55> take this one", [BOT_ID, HELPER_BOT.id], BOT_ID,
        mentioned_users=[HELPER_BOT],
        reply_to_user_id=BOT_ID,
    )
    assert d.action == Action.IGNORE
    assert d.reason == "addressed to another member"


# --- rule e: everything else -------------------------------------------------


def test_founder_message_without_trigger_is_ignored():
    """Author identity no longer turns ordinary chatter into a trigger."""
    d = evaluate("hello there", [], BOT_ID)
    assert d.action == Action.IGNORE
    assert d.reason == "no explicit trigger"


def test_claim_keyword_without_trigger_is_ignored():
    """Domain words alone do not trigger an autonomous reply."""
    d = evaluate("the migration is deployed right?", [], BOT_ID)
    assert d.action == Action.IGNORE


def test_chatter_ignored():
    """Unmarked casual chat stays silent."""
    d = evaluate("lol nice", [], BOT_ID)
    assert d.action == Action.IGNORE

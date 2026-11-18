"""Post-delivery summary worker (P5b): the serialized queue that folds
scrolled-out messages into the rolling summary after a reply is posted.

Covered here: reply-before-summary ordering, failure keeping the old summary,
bounded cold start (30 days / 300 messages / <=60-message batches), the
short-window skip, serialized updates per conversation, open-channel run-cap
accounting for summary runs, and clean shutdown cancellation.
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from conftest import FakeAuthor, FakeChannel, FakeMention, FakeMessage
from conftest import make_watcher as _watcher
from conftest import process_messages as _process_messages

from oi_agent.agent.reply import Reply
from oi_agent.opencode.prompt import FINAL_OUTPUT_MARKER, OPEN_CHANNEL_SILENT
from oi_agent.poster import DeliveryResult
from oi_agent.summary import SUMMARY_SYSTEM
from oi_agent.watch import discord_client

# Tasks started by a test; the autouse fixture cancels them afterwards so no
# worker loop leaks into the next test's event loop.
_started_workers: list[asyncio.Task] = []


@pytest_asyncio.fixture(autouse=True)
async def _cancel_workers():
    """Cancel every summary worker a test started before the next test runs."""
    yield
    pending = [t for t in _started_workers if not t.done()]
    _started_workers.clear()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _start_worker(watcher) -> asyncio.Task:
    """Start the summary worker loop the way on_ready does."""
    task = asyncio.create_task(watcher._summary_worker_loop())
    watcher._summary_worker_task = task
    _started_workers.append(task)
    return task


async def _drain(watcher, timeout: float = 5.0) -> None:
    """Wait until the worker has fully processed every queued update."""
    await asyncio.wait_for(watcher._summary_queue.join(), timeout=timeout)


def _valid_output() -> str:
    """One schema-valid summary proposal with a current touched date."""
    return json.dumps([
        {
            "section": "now",
            "topic": "deploy",
            "text": "green target",
            "touched": datetime.now(UTC).date().isoformat(),
            "durable": False,
            "status": "active",
        },
    ])


_UNSET = object()


def _script_summary_model(watcher, monkeypatch, prompts, output=_UNSET) -> None:
    """Replace the runner's raw model call with a scripted JSON answerer.

    ``output=None`` scripts a runner error (run_raw_prompt's failure shape).
    """
    answer = _valid_output() if output is _UNSET else output

    async def fake_run_raw_prompt(prompt, *, system_prompt="", timeout=60, steps=1, model=""):
        prompts.append((prompt, model, system_prompt))
        return answer

    monkeypatch.setattr(watcher._runner, "run_raw_prompt", fake_run_raw_prompt)


def _script_reply_and_poster(watcher, monkeypatch, posted) -> None:
    """A successful reply run plus an accepting Poster boundary."""

    async def fake_run(*args, **kwargs):
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        posted.append(kwargs.get("chunk"))
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)


@pytest.mark.asyncio
async def test_reply_posted_before_summary_run_starts(tmp_path, monkeypatch):
    """The reply is posted first; the summary run happens strictly afterwards."""
    watcher = _watcher(tmp_path)
    watcher._cfg.context_messages = 2
    watcher._cfg.summary_model = "openai/mini"
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "old question one", [])
    m2 = FakeMessage(2, channel, FakeAuthor(7), "old question two", [])
    m3 = FakeMessage(3, channel, FakeAuthor(7), "<@99> what broke?", [FakeMention(99)])
    # Newest first: FakeChannel.history returns the first N entries, so
    # this order emulates discord.py's newest-first page.
    channel._history = [m3, m2, m1]
    events: list[str] = []
    prompts = []
    allow_summary = asyncio.Event()

    async def fake_run(*args, **kwargs):
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        events.append("posted")
        return DeliveryResult(ok=True, discord_message_id=999)

    async def fake_run_raw_prompt(prompt, *, system_prompt="", timeout=60, steps=1, model=""):
        events.append("summary")
        prompts.append((prompt, model, system_prompt))
        await allow_summary.wait()
        return _valid_output()

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    monkeypatch.setattr(watcher._runner, "run_raw_prompt", fake_run_raw_prompt)
    _start_worker(watcher)

    await _process_messages(watcher, channel, m3)

    # The reply path never waits for the summary run.
    assert events == ["posted"]
    allow_summary.set()
    await _drain(watcher)
    assert events == ["posted", "summary"]

    prompt, model, system_prompt = prompts[0]
    assert model == "openai/mini"
    assert system_prompt == SUMMARY_SYSTEM
    # Only the message that scrolled out of the window (id 1) is folded in.
    assert "[1]" in prompt
    assert "[2]" not in prompt and "[3]" not in prompt
    doc = json.loads(watcher._store.get_summary(111))
    assert doc["v"] == 1
    assert watcher._store.list_summaries(111)[0]["last_summarized_message_id"] == 1


@pytest.mark.parametrize(
    "bad_output",
    [
        None,  # runner error / timeout surface as None
        "not json at all",
        json.dumps([
            {"section": "system", "topic": "t", "text": "x",
             "touched": "2026-01-01", "durable": False, "status": "active"},
        ]),  # schema reject: unknown section
    ],
)
@pytest.mark.asyncio
async def test_summary_failure_keeps_old_summary_and_reply(
    tmp_path, monkeypatch, bad_output
):
    """A failed summary run keeps the stored summary; the posted reply stands."""
    watcher = _watcher(tmp_path)
    watcher._cfg.context_messages = 2
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "old one", [])
    m2 = FakeMessage(2, channel, FakeAuthor(7), "old two", [])
    m3 = FakeMessage(3, channel, FakeAuthor(7), "<@99> help", [FakeMention(99)])
    channel._history = [m3, m2, m1]  # newest first (see the ordering test)
    old_doc = json.dumps({"v": 1, "items": [
        {"section": "now", "topic": "keep", "text": "kept item",
         "touched": "2026-01-01", "durable": False},
    ]})
    watcher._store.set_summary(111, old_doc, 0, max_chars=1500)
    posted = []
    _script_reply_and_poster(watcher, monkeypatch, posted)
    _script_summary_model(watcher, monkeypatch, prompts=[], output=bad_output)
    _start_worker(watcher)

    await _process_messages(watcher, channel, m3)
    await _drain(watcher)

    assert posted == ["answer"]
    assert watcher._store.get_summary(111) == old_doc


@pytest.mark.asyncio
async def test_cold_start_reads_only_last_30_days_bounded_batches(tmp_path, monkeypatch):
    """1,000 messages over ~2 years: the model sees only the newest 30 days,
    at most 300 messages, in chronological batches of at most 60."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    now = datetime.now(UTC)
    messages = []
    mid = 0
    for i in range(100):  # ancient: 700 down to 106 days ago, outside the window
        mid += 1
        msg = FakeMessage(mid, channel, FakeAuthor(7), f"ancient {mid}", [])
        msg.created_at = now - timedelta(days=700 - i * 6)
        messages.append(msg)
    for i in range(900):  # recent: all inside the last 30 days
        mid += 1
        msg = FakeMessage(mid, channel, FakeAuthor(7), f"recent {mid}", [])
        msg.created_at = now - timedelta(hours=29 * 24) + timedelta(minutes=45 * i)
        messages.append(msg)
    channel._history = list(reversed(messages))  # newest first
    prompts = []
    _script_summary_model(watcher, monkeypatch, prompts)
    posted = []
    _script_reply_and_poster(watcher, monkeypatch, posted)
    _start_worker(watcher)

    # The newest 40 messages fill the reply window (context_messages default);
    # everything before id 961 is summary input.
    await _process_messages(watcher, channel, messages[-1])
    await _drain(watcher)

    assert len(prompts) == 5  # ceil(300 / 60)
    batches = []
    for prompt, _model, system_prompt in prompts:
        assert system_prompt == SUMMARY_SYSTEM
        batch_ids = [int(m) for m in re.findall(r"^\[(\d+)\]", prompt, re.M)]
        assert len(batch_ids) <= 60
        assert batch_ids == sorted(batch_ids)  # chronological within the batch
        assert all(i >= 101 for i in batch_ids)  # nothing older than 30 days
        batches.append(batch_ids)
    flat = [i for batch in batches for i in batch]
    assert len(flat) == 300  # at most summary_bootstrap_messages folded in
    assert flat == sorted(flat)  # oldest batch first across batches
    assert max(flat) < 961  # the reply window itself is never folded in


@pytest.mark.asyncio
async def test_short_window_skips_summary_update(tmp_path, monkeypatch):
    """A reply window smaller than context_messages never queues an update."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "<@99> hi", [FakeMention(99)])
    channel._history = [m1]
    prompts = []
    _script_summary_model(watcher, monkeypatch, prompts)
    posted = []
    _script_reply_and_poster(watcher, monkeypatch, posted)
    _start_worker(watcher)

    await _process_messages(watcher, channel, m1)
    await asyncio.sleep(0.05)

    assert posted == ["answer"]
    assert watcher._summary_queue.empty()
    assert prompts == []


@pytest.mark.asyncio
async def test_two_updates_processed_serially(tmp_path, monkeypatch):
    """Two quick completions in one conversation run one after the other."""
    watcher = _watcher(tmp_path)
    watcher._cfg.context_messages = 2
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "first", [])
    m2 = FakeMessage(2, channel, FakeAuthor(7), "second", [])
    m3 = FakeMessage(3, channel, FakeAuthor(7), "<@99> third", [FakeMention(99)])
    m4 = FakeMessage(4, channel, FakeAuthor(7), "<@99> fourth", [FakeMention(99)])
    channel._history = [m3, m2, m1]  # newest first (see the ordering test)
    order: list[str] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(*args, **kwargs):
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        return DeliveryResult(ok=True, discord_message_id=999)

    async def fake_run_raw_prompt(prompt, *, system_prompt="", timeout=60, steps=1, model=""):
        order.append("start")
        if not release.is_set():
            first_started.set()
            await release.wait()
        order.append("end")
        return _valid_output()

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    monkeypatch.setattr(watcher._runner, "run_raw_prompt", fake_run_raw_prompt)
    _start_worker(watcher)

    await _process_messages(watcher, channel, m3)
    await asyncio.wait_for(first_started.wait(), timeout=2)
    # The conversation grows while the first update is still running.
    channel._history.insert(0, m4)
    await _process_messages(watcher, channel, m4)
    assert order == ["start"]  # the second update never runs concurrently
    release.set()
    await _drain(watcher)
    await asyncio.sleep(0.05)
    assert order == ["start", "end", "start", "end"]
    assert watcher._store.list_summaries(111)[0]["last_summarized_message_id"] == 2


@pytest.mark.asyncio
async def test_open_channel_summary_run_counts_against_hourly_cap(tmp_path, monkeypatch):
    """A summary run after a silent completion counts against the run cap."""
    watcher = _watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    watcher._cfg.context_messages = 2
    watcher._cfg.open_channel_runs_per_hour = 1
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "hello all", [])
    m2 = FakeMessage(2, channel, FakeAuthor(7), "any news?", [])
    m3 = FakeMessage(3, channel, FakeAuthor(7), "thanks everyone!", [])
    channel._history = [m3, m2, m1]  # newest first
    reply_runs: list[str | None] = []
    prompts = []

    async def fake_run(*args, **kwargs):
        reply_runs.append(kwargs.get("open_channel_prompt"))
        return Reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}", "sha")

    async def fake_run_raw_prompt(prompt, *, system_prompt="", timeout=60, steps=1, model=""):
        prompts.append(prompt)
        return _valid_output()

    async def fake_deliver(self, **kwargs):
        raise AssertionError("silent completion must not post")

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(watcher._runner, "run_raw_prompt", fake_run_raw_prompt)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    _start_worker(watcher)

    await _process_messages(watcher, channel, m3)
    await _drain(watcher)

    # One reply run + one summary run against open_channel_runs_per_hour,
    # and the silent completion still updated the summary.
    assert watcher._open_runs_in_last_hour(111) == 2
    assert reply_runs == ["none"]
    assert len(prompts) == 1

    await _process_messages(watcher, channel, m2)
    await asyncio.sleep(0.05)

    # The next trigger is deferred by the open-run cap, not run again.
    assert reply_runs == ["none"]
    assert len(prompts) == 1
    assert watcher._store._conn.execute(
        "SELECT status, last_error_class FROM mention_jobs WHERE source_message_id = 2"
    ).fetchone() == ("pending", "open_run_cap")


@pytest.mark.asyncio
async def test_worker_cancelled_cleanly_on_shutdown(tmp_path, monkeypatch):
    """close() stops a mid-run worker: the task always ends, cleanly."""
    watcher = _watcher(tmp_path)
    watcher._cfg.context_messages = 2
    channel = FakeChannel(111)
    m1 = FakeMessage(1, channel, FakeAuthor(7), "old one", [])
    m2 = FakeMessage(2, channel, FakeAuthor(7), "old two", [])
    m3 = FakeMessage(3, channel, FakeAuthor(7), "<@99> help", [FakeMention(99)])
    channel._history = [m3, m2, m1]  # newest first
    posted = []
    _script_reply_and_poster(watcher, monkeypatch, posted)
    release = asyncio.Event()
    started = asyncio.Event()

    async def fake_run_raw_prompt(prompt, *, system_prompt="", timeout=60, steps=1, model=""):
        started.set()
        await release.wait()
        return _valid_output()

    monkeypatch.setattr(watcher._runner, "run_raw_prompt", fake_run_raw_prompt)
    task = _start_worker(watcher)

    await _process_messages(watcher, channel, m3)
    await asyncio.wait_for(started.wait(), timeout=2)
    assert not task.done()
    release.set()

    await asyncio.wait_for(watcher.close(), timeout=10)
    assert watcher._summary_worker_task.done()
    assert watcher._store.list_summaries(111)[0]["last_summarized_message_id"] == 1

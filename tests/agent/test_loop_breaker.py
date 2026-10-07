"""Unit tests for the v2 loop circuit breaker.

Covers the two defences that matter to the user:

* no-new-information loop -> nudge, nudge, halt (with reset on user input or on
  a mutating tool call / new file);
* session token ladder -> warn at 50k (never stops) and a two-staged soft stop
  at 150k (ask for a summary first, pause on the next pass).

Pure logic tests: no agent runtime, no network, no I/O.
"""

import time
from types import SimpleNamespace

try:  # pytest is optional here: the venv may not ship it.
    import pytest  # noqa: F401
except ImportError:  # pragma: no cover
    pytest = None

from agent.loop_breaker import LoopBreaker, LoopBreakerConfig

SAME = (
    "step one: read the configuration file and inspect the guardrail settings. "
    "step two: run the existing test suite to confirm the baseline behaviour. "
    "step three: report the findings back to the user in a short summary."
)
DIFFERENT = (
    "actually a completely different plan: pandas eat bamboo in the afternoon "
    "while the config file sits untouched and nobody reads it twice."
)


class _FakeAgent:
    """Minimal stand-in for the bits of the runtime the breaker reads."""

    def __init__(self, total_tokens: int = 0):
        self.session_total_tokens = total_tokens


def _lb(**overrides) -> LoopBreaker:
    cfg = dict(
        enabled=True,
        warnings_enabled=True,
        thinking_repeat_limit=5,
        thinking_repeat_similarity=0.90,
        nudges_before_halt=2,
        token_warn=50000,
        token_soft_stop=150000,
        token_soft_stop_retrigger_delta=50000,
        max_same_tool=5,
        max_steps=0,
        max_time=0,
    )
    cfg.update(overrides)
    return LoopBreaker(LoopBreakerConfig.from_mapping(cfg))


# --------------------------------------------------------------------------
# L1 — no-new-information loop
# --------------------------------------------------------------------------
def test_no_new_info_loop_escalates_nudge_nudge_halt():
    lb = _lb()
    agent = _FakeAgent()
    lb.reset_turn(agent)
    # Step 1 establishes the baseline; each later identical thought is a repeat,
    # so 5 consecutive steps WITHOUT new information == stalls reaching 5.
    for _ in range(5):
        lb.observe_step(agent, SAME, ["read_file"])
    assert lb.state.stalls == 4
    assert lb.check(agent).action == "ok"

    lb.observe_step(agent, SAME, ["read_file"])
    assert lb.state.stalls == 5

    d1 = lb.check(agent)
    assert (d1.action, d1.code) == ("nudge", "thinking_loop")
    assert lb.state.nudges == 1

    d2 = lb.check(agent)
    assert d2.action == "nudge"
    assert lb.state.nudges == 2

    d3 = lb.check(agent)
    assert d3.action == "halt"
    assert d3.should_halt and d3.ask_user
    assert d3.message  # a written conclusion must be produced for the user


def test_mutating_tool_resets_stalls():
    lb = _lb()
    agent = _FakeAgent()
    lb.reset_turn(agent)
    for _ in range(6):
        lb.observe_step(agent, SAME, ["read_file"])
    assert lb.state.stalls == 5

    lb.observe_step(agent, SAME, ["write_file"])  # new file => new information
    assert lb.state.stalls == 0
    assert lb.check(agent).action == "ok"


def test_user_input_resets_stalls():
    lb = _lb()
    agent = _FakeAgent()
    lb.reset_turn(agent)
    for _ in range(5):
        lb.observe_step(agent, SAME, ["read_file"])
    lb.check(agent)  # nudged once
    lb.note_user_input()
    assert lb.state.stalls == 0
    assert lb.state.nudges == 0
    assert lb.state.last_thinking == ""


def test_new_reasoning_resets_stalls():
    lb = _lb()
    agent = _FakeAgent()
    lb.reset_turn(agent)
    lb.observe_step(agent, SAME, ["read_file"])
    lb.observe_step(agent, SAME, ["read_file"])
    assert lb.state.stalls == 1
    lb.observe_step(agent, DIFFERENT, ["read_file"])
    assert lb.state.stalls == 0


def test_pure_idling_counts_as_stall():
    lb = _lb()
    agent = _FakeAgent()
    lb.reset_turn(agent)
    for _ in range(5):
        lb.observe_step(agent, "", [])
    assert lb.state.stalls == 5
    assert lb.check(agent).action == "nudge"


# --------------------------------------------------------------------------
# L2 — token warn: never stops, announced once
# --------------------------------------------------------------------------
def test_token_warn_is_single_shot_and_non_blocking():
    lb = _lb()
    agent = _FakeAgent(total_tokens=50001)
    lb.reset_turn(agent)

    d = lb.check(agent)
    assert (d.action, d.code) == ("warn", "token_warn")
    assert not d.should_halt

    # Never repeats, never halts.
    assert lb.check(_FakeAgent(total_tokens=90000)).action == "ok"


def test_token_warn_can_be_disabled():
    lb = _lb(token_warn=0)
    agent = _FakeAgent(total_tokens=90000)
    lb.reset_turn(agent)
    assert lb.check(agent).action == "ok"


# --------------------------------------------------------------------------
# L3 — token soft stop: two staged (summary first, pause next pass)
# --------------------------------------------------------------------------
def test_token_soft_stop_is_two_staged():
    lb = _lb()
    lb.reset_turn(_FakeAgent())
    agent = _FakeAgent(total_tokens=150001)

    d1 = lb.check(agent)
    assert d1.action == "nudge"
    assert d1.code == "token_soft_stop_summary"
    assert lb.token_soft_summary_requested is True

    d2 = lb.check(agent)
    assert d2.action == "halt"
    assert d2.code == "token_soft_stop"
    assert d2.ask_user
    assert "继续" in d2.message
    assert lb.token_ack_at == 150001


def test_soft_stop_does_not_immediately_retrigger():
    lb = _lb()  # ladder a1 = 150k, a2 = 400k (defaults)
    lb.reset_turn(_FakeAgent())
    lb.check(_FakeAgent(total_tokens=150001))  # stage 1
    lb.check(_FakeAgent(total_tokens=150001))  # stage 2 -> halt, ladder slides

    # After a halt the ladder waits for the NEXT point (400k): +10k and even
    # +50k must NOT pause again (one-shot 50k warn may still fire, harmless).
    assert lb.confirm_point == 400000
    d = lb.check(_FakeAgent(total_tokens=200002))
    assert d.action in {"ok", "warn"}
    assert not d.should_halt


def test_confirm_point_ladder_is_fibonacci():
    """15万 -> 40万 -> 55万 -> 95万 -> 150万 ... 每个点 = 前两个之和；
    每点都走 总结(nudge) -> 暂停(halt) 两步，且暂停后阶梯前移。"""
    lb = _lb(token_soft_stop=150000, token_soft_stop_second=400000)
    agent = _FakeAgent()
    lb.reset_turn(agent)
    for p in (150000, 400000, 550000, 950000, 1500000):
        agent.session_total_tokens = p + 1
        d1 = lb.check(agent)
        assert (d1.action, d1.code) == ("nudge", "token_soft_stop_summary"), f"stage1 @ {p}"
        d2 = lb.check(agent)
        assert (d2.action, d2.code) == ("halt", "token_soft_stop"), f"stage2 @ {p}"
        assert d2.ask_user and "继续" in d2.message
        assert lb.confirm_point > p, f"ladder did not advance past {p}"


def test_ladder_skips_points_already_passed():
    """若一轮里 token 猛涨越过多个点，只暂停一次并一步跳到未到达的下一点。"""
    lb = _lb(token_soft_stop=150000, token_soft_stop_second=400000)
    agent = _FakeAgent(total_tokens=600001)  # 已越过 150k/400k/550k
    lb.reset_turn(agent)
    assert lb.check(agent).action == "nudge"
    d = lb.check(agent)
    assert d.action == "halt"
    assert lb.confirm_point > 600000  # 直接跳到 950k


def test_soft_stop_below_threshold_is_silent():
    lb = _lb()
    lb.reset_turn(_FakeAgent())
    # Under the 50k warn line: completely silent.
    assert lb.check(_FakeAgent(total_tokens=49999)).action == "ok"
    # Past the warn line but under the soft stop: warns, never pauses.
    d = lb.check(_FakeAgent(total_tokens=149999))
    assert d.action == "warn" and not d.should_halt


# --------------------------------------------------------------------------
# max_time — warn only, never halts
# --------------------------------------------------------------------------
def test_max_time_warns_only_and_never_halts():
    lb = _lb(max_time=5.0)
    agent = _FakeAgent()
    lb.reset_turn(agent)

    # Move the turn anchor into the past instead of sleeping: deterministic on
    # Windows, where time.monotonic() only ticks every ~15.6 ms.
    lb.state.turn_started_at -= 10.0

    d = lb.check(agent)
    assert (d.action, d.code) == ("warn", "turn_time")
    assert not d.should_halt

    # Once only; never escalates to a halt, however long the turn runs.
    lb.state.turn_started_at -= 10.0
    assert lb.check(agent).action == "ok"
    lb.state.turn_started_at -= 10.0
    assert lb.check(agent).action == "ok"


# --------------------------------------------------------------------------
# disabled / config plumbing
# --------------------------------------------------------------------------
def test_disabled_breaker_is_silent():
    lb = _lb(enabled=False)
    agent = _FakeAgent(total_tokens=999_999)
    lb.reset_turn(agent)
    for _ in range(10):
        lb.observe_step(agent, SAME, ["read_file"])
    assert lb.check(agent).action == "ok"


def test_config_defaults_match_agreed_values():
    cfg = LoopBreakerConfig.from_mapping(None)
    assert cfg.thinking_repeat_limit == 5
    assert cfg.thinking_repeat_similarity == 0.90
    assert cfg.nudges_before_halt == 2
    assert cfg.token_warn == 50000
    assert cfg.token_soft_stop == 150000
    assert cfg.token_soft_stop_retrigger_delta == 50000
    assert cfg.token_soft_stop_second == 400000
    assert cfg.max_steps == 0  # no hard iteration cap
    assert cfg.max_time == 180.0  # single-turn wall clock, warn only


def test_reset_turn_clears_per_turn_state_only():
    lb = _lb()
    agent = _FakeAgent(total_tokens=60000)
    lb.reset_turn(agent)
    lb.check(agent)  # consume the 50k warn
    for _ in range(6):
        lb.observe_step(agent, SAME, ["read_file"])
    assert lb.state.stalls == 5

    lb.reset_turn(agent)
    assert lb.state.stalls == 0
    assert lb.state.nudges == 0
    assert "token_warn" in lb.warned  # session scope survives the reset


# --- v3.1: one-click sentinel switch (hot reload, no restart) ---

def _sentinel_case():
    """Context manager: redirect the breaker to a temp sentinel dir."""
    import tempfile, shutil, pathlib
    import agent.loop_breaker as _lbmod

    class _Ctx:
        def __enter__(self):
            self.dir = pathlib.Path(tempfile.mkdtemp(prefix="lb_switch_"))
            self.old = _lbmod._SENTINEL_DIR_OVERRIDE
            _lbmod._SENTINEL_DIR_OVERRIDE = self.dir
            return self.dir

        def __exit__(self, *exc):
            _lbmod._SENTINEL_DIR_OVERRIDE = self.old
            shutil.rmtree(self.dir, ignore_errors=True)

    return _Ctx()


def test_switch_off_beats_loop_and_token_thresholds():
    with _sentinel_case() as d:
        (d / "loop_breaker.off").write_text("", encoding="utf-8")
        lb = LoopBreaker(LoopBreakerConfig(token_soft_stop=1000))
        lb.state.stalls = 99
        agent = SimpleNamespace(session_total_tokens=10_000_000)
        first = lb.check(agent)      # first hit only emits one status line
        second = lb.check(agent)
        assert not first.should_halt and not second.should_halt
        assert second.code != "token_soft_stop"


def test_switch_token_off_disables_ladder_only():
    with _sentinel_case() as d:
        (d / "loop_breaker.token_off").write_text("", encoding="utf-8")
        lb = LoopBreaker(LoopBreakerConfig(token_soft_stop=1000))
        agent = SimpleNamespace(session_total_tokens=10_000_000)
        assert not lb.check(agent).should_halt   # token ladder is off
        lb.state.stalls = 99
        lb.state.nudges = lb.config.nudges_before_halt  # already past nudges
        assert lb.check(agent).should_halt       # anti-repeat guard still on


def test_switch_on_overrides_disabled_config():
    with _sentinel_case() as d:
        (d / "loop_breaker.on").write_text("", encoding="utf-8")
        lb = LoopBreaker(LoopBreakerConfig(enabled=False, token_soft_stop=1000))
        lb.state.stalls = 99
        lb.state.nudges = lb.config.nudges_before_halt  # already past nudges
        assert lb.check(SimpleNamespace(session_total_tokens=0)).should_halt


def test_switch_absent_follows_config():
    with _sentinel_case() as d:      # no sentinel files at all
        lb = LoopBreaker(LoopBreakerConfig(enabled=False))
        lb.state.stalls = 99
        assert not lb.check(SimpleNamespace(session_total_tokens=0)).should_halt

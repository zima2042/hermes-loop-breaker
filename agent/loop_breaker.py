"""Loop circuit breaker for pure-reasoning loops and session token spend.

Two defences, both tuned for a *visible* outcome rather than a silent stall:

* **No-new-information loop** — when the main agent keeps working without
  producing anything new (same reasoning reworded, read-only tools that add no
  information), the breaker escalates: visible nudge -> second nudge -> forced
  stop with a written conclusion.  User input or a mutating tool call (file
  written, code patched, command run) resets the counter, so genuine work never
  trips it.
* **Session token confirm points** — a running total of session tokens is
  compared against an ascending ladder of confirm points: 150k, 400k, 550k,
  950k, ... (each point is the sum of the previous two).  Every point is
  two-staged *by design*: the first strike asks the model to write its
  best-current-conclusion summary, and the turn is paused on the following
  iteration, so the user always gets a written summary to read — and then
  decides whether to keep going.

Pure logic: no side effects, no I/O, no message construction.  The runtime
decides whether a decision becomes a status-bar line, a synthetic nudge riding
a tool result, or a controlled turn halt.  Scope is the main agent only;
subagents keep their own counters.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:  # Tool names that always change state => definitionally "new information".
    from agent.tool_guardrails import MUTATING_TOOL_NAMES
except Exception:  # pragma: no cover - defensive: breaker must never break import
    MUTATING_TOOL_NAMES = frozenset()

_WS_RE = re.compile(r"\s+")

DEFAULT_THINKING_REPEAT_LIMIT = 5
DEFAULT_THINKING_REPEAT_SIMILARITY = 0.90
DEFAULT_NUDGES_BEFORE_HALT = 2
DEFAULT_TOKEN_WARN = 50_000
DEFAULT_TOKEN_SOFT_STOP = 150_000
DEFAULT_TOKEN_SOFT_STOP_RETRIGGER_DELTA = 50_000
DEFAULT_TOKEN_SOFT_STOP_SECOND = 400_000   # 2nd confirm point (v3 ladder)
DEFAULT_MAX_TIME = 180.0          # single-turn wall clock (seconds), warn only

# --- v3.1: one-click sentinel switch -------------------------------------
# Empty files next to config.yaml that flip the breaker for *running*
# sessions without a restart.  Precedence: off > token_off > on > config.
SENTINEL_OFF = "loop_breaker.off"
SENTINEL_TOKEN_OFF = "loop_breaker.token_off"
SENTINEL_ON = "loop_breaker.on"

# Test hook / explicit override (None => resolve next to config.yaml).
_SENTINEL_DIR_OVERRIDE: Optional[Path] = None


def sentinel_dir() -> Path:
    """Directory holding the sentinel files (same dir as config.yaml)."""
    if _SENTINEL_DIR_OVERRIDE is not None:
        return Path(_SENTINEL_DIR_OVERRIDE)
    try:
        from hermes_cli.config import get_config_path

        return Path(get_config_path()).parent
    except Exception:
        home = os.environ.get("HERMES_HOME")
        return Path(home) if home else Path.home() / ".hermes"


def read_switch() -> str:
    """Return 'off' | 'token_off' | 'on' | 'config'."""
    base = sentinel_dir()
    for name, value in (
        (SENTINEL_OFF, "off"),
        (SENTINEL_TOKEN_OFF, "token_off"),
        (SENTINEL_ON, "on"),
    ):
        try:
            if (base / name).exists():
                return value
        except OSError:
            continue
    return "config"


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def _shingles(text: str, n: int = 4) -> set:
    """Word n-gram shingles; short texts degrade to a single shingle."""
    tokens = [tok for tok in _normalize(text).split(" ") if tok]
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity over word shingles, 0.0 - 1.0."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _as_int(raw: Any, default: int) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def _as_float(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


@dataclass
class LoopBreakerConfig:
    """Thresholds.  All values are read from ``agent.loop_breaker`` config."""

    enabled: bool = True
    warnings_enabled: bool = True

    # No-new-information loop (the main defence).
    thinking_repeat_limit: int = DEFAULT_THINKING_REPEAT_LIMIT
    thinking_repeat_similarity: float = DEFAULT_THINKING_REPEAT_SIMILARITY
    nudges_before_halt: int = DEFAULT_NUDGES_BEFORE_HALT

    # Session token ladder.
    token_warn: int = DEFAULT_TOKEN_WARN
    token_soft_stop: int = DEFAULT_TOKEN_SOFT_STOP
    token_soft_stop_retrigger_delta: int = DEFAULT_TOKEN_SOFT_STOP_RETRIGGER_DELTA
    # v3 confirm-point ladder: second point; later points follow a(n)=a(n-1)+a(n-2).
    token_soft_stop_second: int = DEFAULT_TOKEN_SOFT_STOP_SECOND

    # Out of scope for this patch (kept for config compatibility / phase 2).
    max_same_tool: int = 5
    max_steps: int = 0        # 0 disables: no hard iteration cap here.
    max_time: float = 180.0   # single-turn wall clock; WARN ONLY, never halts.
                              # 0 disables the check entirely.

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "LoopBreakerConfig":
        raw = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            warnings_enabled=bool(raw.get("warnings_enabled", True)),
            thinking_repeat_limit=_as_int(
                raw.get("thinking_repeat_limit"), DEFAULT_THINKING_REPEAT_LIMIT
            ),
            thinking_repeat_similarity=_as_float(
                raw.get("thinking_repeat_similarity"),
                DEFAULT_THINKING_REPEAT_SIMILARITY,
            ),
            nudges_before_halt=_as_int(
                raw.get("nudges_before_halt"), DEFAULT_NUDGES_BEFORE_HALT
            ),
            token_warn=_as_int(raw.get("token_warn"), DEFAULT_TOKEN_WARN),
            token_soft_stop=_as_int(
                raw.get("token_soft_stop"), DEFAULT_TOKEN_SOFT_STOP
            ),
            token_soft_stop_retrigger_delta=_as_int(
                raw.get("token_soft_stop_retrigger_delta"),
                DEFAULT_TOKEN_SOFT_STOP_RETRIGGER_DELTA,
            ),
            token_soft_stop_second=_as_int(
                raw.get("token_soft_stop_second"), DEFAULT_TOKEN_SOFT_STOP_SECOND
            ),
            max_same_tool=_as_int(raw.get("max_same_tool"), 5),
            max_steps=_as_int(raw.get("max_steps"), 0),
            max_time=(
                _as_float(raw.get("max_time"), DEFAULT_MAX_TIME)
                if raw.get("max_time") is not None
                else DEFAULT_MAX_TIME
            ),
        )


@dataclass
class LoopBreakerDecision:
    """``action``: ok | warn | nudge | halt."""

    action: str = "ok"
    code: str = ""
    message: str = ""
    ask_user: bool = False

    @property
    def should_halt(self) -> bool:
        return self.action == "halt"


@dataclass
class LoopBreakerState:
    """Per-turn state; rebuilt by :meth:`LoopBreaker.reset_turn`."""

    turn_started_at: float = 0.0
    last_thinking: str = ""
    stalls: int = 0
    nudges: int = 0


class LoopBreaker:
    """Main-agent loop breaker: per-turn stall counter + session token ladder."""

    def __init__(self, config: Optional[LoopBreakerConfig] = None) -> None:
        self.config = config or LoopBreakerConfig()
        self.state = LoopBreakerState()
        # Session-scoped (survive turn resets).
        self.token_ack_at = 0
        self.warned: set = set()
        self.token_soft_summary_requested = False
        # v3 confirm-point ladder: a1 = token_soft_stop, a2 = token_soft_stop_second,
        # then a(n) = a(n-1) + a(n-2)  =>  150k, 400k, 550k, 950k, 1.5M, ...
        _a1 = int(self.config.token_soft_stop)
        _a2 = int(self.config.token_soft_stop_second)
        if _a2 <= _a1:  # defensive: bad/missing config value
            _a2 = _a1 + max(int(self.config.token_soft_stop_retrigger_delta), 1)
        self.confirm_point = _a1
        self.confirm_prev = _a1
        self.confirm_next = _a2

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def reset_turn(self, agent: Any = None) -> None:
        """Fresh stall counter for a new user turn; token ladder persists."""
        self.state = LoopBreakerState(turn_started_at=time.monotonic())
        self.token_soft_summary_requested = False

    def note_user_input(self) -> None:
        """The user spoke mid-turn: new instruction == fresh start."""
        self.state.stalls = 0
        self.state.nudges = 0
        self.state.last_thinking = ""

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    def observe_step(
        self,
        agent: Any,
        reasoning_text: Optional[str],
        tool_names: Optional[Iterable[str]] = None,
    ) -> None:
        """Record one completed loop iteration (after tool execution)."""
        names = {str(name) for name in (tool_names or ()) if name}

        if names & MUTATING_TOOL_NAMES:
            # A file was written, code patched, command run ... that is new
            # information by construction, so the stall counter resets.
            self.state.stalls = 0
            self.state.last_thinking = ""
            return

        text = (reasoning_text or "").strip()
        if not text:
            if not names:
                self.state.stalls += 1  # neither thought nor action: idling
            return

        if (
            self.state.last_thinking
            and _similarity(text, self.state.last_thinking)
            >= self.config.thinking_repeat_similarity
        ):
            self.state.stalls += 1  # same reasoning, reworded
        else:
            self.state.stalls = 0  # genuinely new reasoning
        self.state.last_thinking = text

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def check(self, agent: Any) -> LoopBreakerDecision:
        cfg = self.config
        # --- v3.1: sentinel switch, evaluated every pass (hot, no restart) ---
        switch = read_switch()
        token_muted = switch == "token_off"

        if switch == "off":
            return self._switch_notice(
                "off",
                "Loop breaker is OFF (loop_breaker.off) — anti-repeat and "
                "token confirm points are both disabled this session.",
            )
        # 'on' deliberately falls through: it overrides a disabled config.
        if switch == "config" and not cfg.enabled:
            return LoopBreakerDecision()
        if token_muted:
            _notice = self._switch_notice(
                "token_off",
                "Token confirm points are OFF (loop_breaker.token_off) — "
                "anti-repeat guard still active.",
            )
            if _notice.action != "ok":
                return _notice

        total = int(getattr(agent, "session_total_tokens", 0) or 0)

        # --- Session token confirm points (two-staged: summarise, then pause) ---
        # Ladder 150k -> 400k -> 550k -> 950k -> ...  Each point first asks for a
        # written summary, then pauses on the next pass and asks to continue.
        if (
            not token_muted
            and cfg.token_soft_stop > 0
            and total >= self.confirm_point
        ):
            if self.token_soft_summary_requested:
                # Stage 2: the summary was requested last iteration -> pause now.
                self.token_soft_summary_requested = False
                self.token_ack_at = total
                _point = self.confirm_point
                self._advance_confirm_point(total)
                return LoopBreakerDecision(
                    action="halt",
                    code="token_soft_stop",
                    ask_user=True,
                    message=(
                        f"⏸️ 会话 Token 已达 {total:,}，触发确认点 "
                        f"{_point:,}，本轮已暂停。\n\n"
                        "以上一条消息就是当前的进度总结与最佳结论。\n"
                        f"是否继续？（回复「继续」即可。下一个确认点是 "
                        f"{self.confirm_point:,} tokens）"
                    ),
                )
            # Stage 1: ask for a written summary; pause on the next pass.
            self.token_soft_summary_requested = True
            return LoopBreakerDecision(
                action="nudge",
                code="token_soft_stop_summary",
                ask_user=True,
                message=(
                    f"会话 Token 已达 {total:,}，触发确认点 "
                    f"{self.confirm_point:,}。请立即用 3-6 句话写出"
                    "「当前进度 + 最佳结论 + 剩余工作」，写完就停止，"
                    "不要再开新的探索步骤。"
                ),
            )

        # --- Warn threshold: status bar only, never stops anything ---
        if (
            not token_muted
            and cfg.token_warn > 0
            and total >= cfg.token_warn
            and "token_warn" not in self.warned
        ):
            self.warned.add("token_warn")
            return LoopBreakerDecision(
                action="warn",
                code="token_warn",
                message=(
                    f"session tokens at {total:,} "
                    f"(warn threshold {cfg.token_warn:,}) — continuing"
                ),
            )

        # --- No-new-information loop: the main defence ---
        if self.state.stalls >= cfg.thinking_repeat_limit:
            return self._escalate(
                "thinking_loop",
                f"{self.state.stalls} consecutive steps without new information",
            )

        # --- Single-turn wall clock: warn only, never halts ---
        if cfg.max_time > 0 and "turn_time" not in self.warned:
            elapsed = time.monotonic() - self.state.turn_started_at
            if elapsed > cfg.max_time:
                self.warned.add("turn_time")
                return LoopBreakerDecision(
                    action="warn",
                    code="turn_time",
                    message=(
                        f"single turn running {elapsed:.0f}s "
                        f"(soft check {cfg.max_time:.0f}s) — continuing"
                    ),
                )

        return LoopBreakerDecision()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _advance_confirm_point(self, total: int = 0) -> None:
        """Slide to the next confirm point: a(n) = a(n-1) + a(n-2).

        150k -> 400k -> 550k -> 950k -> 1.5M ...  Any point at or below
        ``total`` is skipped, so a pause can never re-trigger immediately.
        """
        while True:
            self.confirm_point = self.confirm_next
            self.confirm_prev, self.confirm_next = (
                self.confirm_next,
                self.confirm_prev + self.confirm_next,
            )
            if not total or self.confirm_point > total:
                return

    def _switch_notice(self, key: str, message: str) -> LoopBreakerDecision:
        """Emit one visible status line per session while a switch is active."""
        marker = "switch:" + key
        if self.config.warnings_enabled and marker not in self.warned:
            self.warned.add(marker)
            return LoopBreakerDecision(
                action="warn", code="switch_" + key, message=message
            )
        return LoopBreakerDecision()

    def _escalate(self, code: str, detail: str) -> LoopBreakerDecision:
        if self.state.nudges < self.config.nudges_before_halt:
            self.state.nudges += 1
            return LoopBreakerDecision(
                action="nudge",
                code=code,
                message=(
                    f"检测到疑似无新信息的循环（{code}）：{detail}。"
                    f"这是第 {self.state.nudges}/{self.config.nudges_before_halt} 次提醒。"
                    "不要重复已有推理。要么调用一个能带来新进展的工具，"
                    "要么现在就给出当前最佳结论并停止。"
                ),
            )
        return LoopBreakerDecision(
            action="halt",
            code=code,
            ask_user=True,
            message=(
                f"🛑 已强制停止本轮循环（{code}）：{detail}。\n\n"
                "当前最佳结论：仅基于上方已完成的工作与最新结果。\n"
                "阻塞原因：继续下去只是在重复既有步骤，不会产生新信息。\n"
                "建议：换一种策略、缩小问题范围，或直接告诉我下一步该做什么。"
            ),
        )

"""Typed configuration.

* ``Settings`` — secrets, URLs and mode from the environment / ``.env``.
* ``Config``   — every tunable parameter from ``configs/*.yaml`` (+ ``--set`` overrides,
  + an optional runtime overlay written by the Reviewer). ``config_hash`` identifies it.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

import orjson
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- settings (env)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore")

    wallex_api_key: SecretStr = SecretStr("")
    wallex_base_url: str = "https://api.wallex.ir"
    wallex_ws_url: str = "wss://api.wallex.ir/connection/websocket"
    wallex_mcp_url: str = "https://api.wallex.ir/mcp"
    wallex_mcp_auth_header: str = "X-API-Key"
    wallex_sub_account_client_id: str = ""
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model_fast: str = ""
    openrouter_model_strong: str = ""
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    wta_mode: Literal["shadow", "live", "backtest", "replay"] = "shadow"
    wta_live: int = 0
    challenge_start_utc: str = ""
    challenge_end_utc: str = ""
    initial_equity_tmn: int = 5_000_000

    def secret_values(self) -> list[str]:
        """Every non-empty secret, for log scrubbing and the secrets-scan test."""
        vals = [
            self.wallex_api_key.get_secret_value(),
            self.openrouter_api_key.get_secret_value(),
            self.gemini_api_key.get_secret_value(),
            self.telegram_bot_token.get_secret_value(),
        ]
        return [v for v in vals if v]

    # not a secret the engine scrubs from its logs, but an account identifier that must never reach
    # git or a deliverable (the challenge sub-account; SUBMISSION_CHECKLIST §2 treats it like a key)
    SCANNED_IDENTIFIERS: ClassVar[tuple[str, ...]] = ("wallex_sub_account_client_id",)

    def scan_values(self) -> dict[str, str]:
        """``{field name: value}`` for the secrets scan (``wta logs check --secrets``,
        ``scripts/make_deliverables.py``): every ``SecretStr`` field plus ``SCANNED_IDENTIFIERS``.
        Empty values are kept (the scanner skips them); the values are never printed."""
        out: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = value.get_secret_value()
            elif name in self.SCANNED_IDENTIFIERS:
                out[name] = str(value or "")
        return out

    def masked(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if isinstance(getattr(self, name), SecretStr):
                out[name] = "<set>" if getattr(self, name).get_secret_value() else "<empty>"
            else:
                out[name] = value
        return out


def mask_secrets(text: str, secrets: list[str]) -> str:
    """Replace any secret occurrence in ``text`` with ``***``."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text


# ---------------------------------------------------------------- config (yaml)


class EngineCfg(_Cfg):
    fast_loop_interval_s: float = 2
    slow_loop_interval_s: float = 300
    reviewer_interval_s: float = 3600
    reconcile_interval_s: float = 300
    heartbeat_interval_s: float = 60
    equity_row_interval_s: float = 10
    stale_data_s: float = 30
    initial_equity_tmn: Decimal = Decimal("5000000")
    challenge_start_utc: str | None = None
    challenge_end_utc: str | None = None
    flatten_start_before_end_s: float = 7200
    flatten_final_before_end_s: float = 1800
    final_state_before_end_s: float = 300
    rng_seed: int = 42
    symbol_cooldown_s: float = 30
    exception_storm_count: int = 3
    exception_storm_window_s: float = 600


class DataCfg(_Cfg):
    sqlite_path: str = "data/wta.sqlite"  # market data (recorder)
    state_sqlite_path: str = "data/state_{mode}.sqlite"  # engine state per mode (shadow/live never mix)
    depth_levels_stored: int = 10
    rest_poll_interval_s: float = 2
    rest_poll_use_depth_all: bool = True
    candle_poll_interval_s: float = 60
    ws_enabled: bool = True
    ws_reconnect_max_s: float = 60
    rate_limit_rps: float = 6
    http_timeout_s: float = 8
    max_retries: int = 4
    store_flush_interval_s: float = 1
    store_flush_rows: int = 200


class RiskCfg(_Cfg):
    max_order_pct_equity: Decimal = Decimal("0.25")
    max_order_pct_of_best_level_qty: Decimal = Decimal("0.8")
    max_coin_exposure_pct: Decimal = Decimal("0.40")
    max_concurrent_positions: int = 3
    max_positions_per_strategy: dict[str, int] = Field(default_factory=lambda: {"S1": 2, "S3": 1})
    max_open_orders: int = 8
    max_orders_per_hour: int = 10
    max_orders_per_day: int = 60
    daily_loss_limit_pct: Decimal = Decimal("0.025")
    max_drawdown_kill_pct: Decimal = Decimal("0.05")
    fee_budget_pct: Decimal = Decimal("0.02")
    price_disagreement_pause_pct: Decimal = Decimal("0.05")
    price_sanity_pct: Decimal = Decimal("0.03")
    consecutive_api_errors_pause: int = 5
    anomaly_clear_s: float = 60
    balance_buffer_pct: Decimal = Decimal("0.005")
    reconcile_drift_pct: Decimal = Decimal("0.005")
    duplicate_cooldown_s: float = 60
    min_notional_tmn: Decimal = Decimal("50000")
    min_notional_usdt: Decimal = Decimal("1")


class ExecutionCfg(_Cfg):
    poll_interval_s: float = 2
    limit_reprice_after_s: float = 15
    limit_max_reprices: int = 2
    limit_cancel_after_s: float = 45
    flatten_max_reprices: int = 3
    maker_offset_ticks: int = 1
    use_server_side_stops: bool = True
    stop_limit_slippage_pct: Decimal = Decimal("0.004")
    flatten_final_cross_pct: Decimal = Decimal("0.005")


class S1Cfg(_Cfg):
    enabled: bool = True
    coins: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP", "DOGE"])
    margin_min_pct: Decimal = Decimal("0.0015")
    leg1_taker: bool = True
    leg2_max_loss_pct: Decimal = Decimal("0.003")
    lockin_enabled: bool = True
    revert_enabled: bool = True
    revert_max_hold_s: float = 1200
    revert_stop_pct: Decimal = Decimal("0.010")
    revert_target_fraction: Decimal = Decimal("0.6")
    revert_max_concurrent: int = 1
    z_min: Decimal = Decimal("2.0")
    dev_window_s: float = 3600
    cooldown_s: float = 60
    max_concurrent: int = 2
    size_pct_equity: Decimal = Decimal("0.20")


class S2Cfg(_Cfg):
    enabled: bool = True
    target_share_by_regime: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "range": Decimal("0.50"),
            "trend_up": Decimal("0.65"),
            "trend_down": Decimal("0.30"),
            "chaos": Decimal("0.40"),
        }
    )
    bounds: tuple[Decimal, Decimal] = (Decimal("0.25"), Decimal("0.70"))
    rebalance_threshold_pp: Decimal = Decimal("0.15")
    min_rebalance_interval_s: float = 1800
    ema_fast: int = 20
    ema_slow: int = 60
    candle_resolution: str = "15"
    trend_ret_24h_pct: Decimal = Decimal("0.008")
    trend_flag_min_gap_pct: Decimal = Decimal("0.0015")
    chaos_atr_mult: Decimal = Decimal("2.0")


class S3Cfg(_Cfg):
    enabled: bool = False
    allow_rules_only: bool = False
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    candle_resolution: str = "15"
    breakout_lookback: int = 20
    volume_mult: Decimal = Decimal("1.5")
    atr_period: int = 14
    atr_min_pct: Decimal = Decimal("0.003")
    atr_max_pct: Decimal = Decimal("0.03")
    stop_atr_mult: Decimal = Decimal("1.5")
    target_atr_mult: Decimal = Decimal("2.5")
    max_hold_s: float = 14400
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_size_pct_equity: Decimal = Decimal("0.20")
    min_regime_confidence: Decimal = Decimal("0.6")


class StrategiesCfg(_Cfg):
    s1_crossbook: S1Cfg = Field(default_factory=S1Cfg)
    s2_usdtalloc: S2Cfg = Field(default_factory=S2Cfg)
    s3_momentum: S3Cfg = Field(default_factory=S3Cfg)


class AICfg(_Cfg):
    enabled: bool = True
    provider: Literal["openrouter", "gemini"] = "openrouter"
    temperature: float = 0.0
    analyst_tools_enabled: bool = True
    analyst_max_tool_rounds: int = 6
    analyst_timeout_s: float = 40
    reviewer_timeout_s: float = 90
    plan_stale_s: float = 1200
    max_analyst_calls_per_day: int = 400
    max_reviewer_calls_per_day: int = 30
    tool_result_max_bytes: int = 4096
    cache_dir: str = "logs/llm_cache"
    replay_strict: bool = True
    # After the one 'fix the JSON' retry, lift top-level plan keys the model wrote inside `regimes` back to the
    # top level (logged as `repaired` in the plan record; the result is still validated and clamped). The default
    # is False so configs recorded before the flag existed replay exactly; configs/default.yaml turns it on.
    repair_plan_nesting: bool = False


class AlertsCfg(_Cfg):
    telegram_enabled: bool = False
    # inbound /status /pause /resume /kill: exactly ONE engine per bot may poll getUpdates (Telegram
    # gives each update to whichever poller asks), so a second engine (the optional shadow next to
    # live, flatten_now) keeps its alerts but turns this off
    telegram_commands_enabled: bool = True
    max_msgs_per_10min: int = 20


class MarketsCfg(_Cfg):
    quote_assets: list[str] = Field(default_factory=lambda: ["TMN", "USDT"])
    watchlist: list[str] = Field(default_factory=list)
    banned: list[str] = Field(default_factory=list)

    def is_banned(self, symbol: str) -> bool:
        return symbol.upper().replace("/", "") in self._banned_set()

    def _banned_set(self) -> frozenset[str]:
        return frozenset(s.upper() for s in self.banned)


class LLMRoleCfg(_Cfg):
    provider: Literal["openrouter", "gemini"] = "openrouter"
    model: str | None = None
    max_tokens: int = 1500


class ReviewerCfg(LLMRoleCfg):
    editable: dict[str, list[Any]] = Field(default_factory=dict)
    max_changes_per_review: int = 2
    cooldown_s: float = 3600


class FallbackCfg(_Cfg):
    provider: Literal["openrouter", "gemini"] = "gemini"
    model: str | None = None
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"


class OpenRouterCfg(_Cfg):
    base_url: str = "https://openrouter.ai/api/v1"


class LLMCfg(_Cfg):
    openrouter: OpenRouterCfg = Field(default_factory=OpenRouterCfg)
    analyst: LLMRoleCfg = Field(default_factory=LLMRoleCfg)
    reviewer: ReviewerCfg = Field(default_factory=ReviewerCfg)
    fallback: FallbackCfg = Field(default_factory=FallbackCfg)


class Config(_Cfg):
    engine: EngineCfg = Field(default_factory=EngineCfg)
    data: DataCfg = Field(default_factory=DataCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    strategies: StrategiesCfg = Field(default_factory=StrategiesCfg)
    ai: AICfg = Field(default_factory=AICfg)
    alerts: AlertsCfg = Field(default_factory=AlertsCfg)
    markets: MarketsCfg = Field(default_factory=MarketsCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)

    @property
    def config_hash(self) -> str:
        return config_hash(self)


# ---------------------------------------------------------------- loading


_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _resolve_env(node: Any, env: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {k: _resolve_env(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env(v, env) for v in node]
    if isinstance(node, str):
        full = _ENV_RE.fullmatch(node.strip())
        if full:
            val = env.get(full.group(1), "")
            return val if val != "" else None
        return _ENV_RE.sub(lambda m: env.get(m.group(1), ""), node)
    return node


def _set_path(tree: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = tree
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise ValueError(f"cannot set {dotted}: {p} is not a mapping")
    node[parts[-1]] = value


def get_path(tree: dict[str, Any], dotted: str) -> Any:
    node: Any = tree
    for p in dotted.split("."):
        node = node[p]
    return node


def parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    """``["risk.max_order_pct_equity=0.2"]`` -> ``{"risk.max_order_pct_equity": 0.2}`` (yaml-typed)."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"override must be key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        out[key.strip()] = yaml.safe_load(raw)
    return out


def _env_for_interpolation() -> dict[str, str]:
    env = dict(os.environ)
    # values from .env (without exporting them into os.environ)
    dotenv = ROOT / ".env"
    if dotenv.exists():
        from dotenv import dotenv_values

        for k, v in dotenv_values(dotenv).items():
            if v is not None and k not in env:
                env[k] = v
    return env


def load_raw(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    default = yaml.safe_load((config_dir / "default.yaml").read_text()) or {}
    raw.update(default)
    markets_file = config_dir / "markets.yaml"
    if markets_file.exists():
        raw["markets"] = yaml.safe_load(markets_file.read_text()) or {}
    llm_file = config_dir / "llm.yaml"
    if llm_file.exists():
        raw["llm"] = yaml.safe_load(llm_file.read_text()) or {}
    return raw


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(
    overrides: dict[str, Any] | list[str] | None = None,
    config_dir: Path = CONFIG_DIR,
    runtime_overlay: Path | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    raw = load_raw(config_dir)
    if runtime_overlay is not None and runtime_overlay.exists():
        raw = _deep_merge(raw, yaml.safe_load(runtime_overlay.read_text()) or {})
    if isinstance(overrides, list):
        overrides = parse_overrides(overrides)
    for key, value in (overrides or {}).items():
        _set_path(raw, key, value)
    raw = _resolve_env(raw, env if env is not None else _env_for_interpolation())
    return Config.model_validate(raw)


def apply_changes(cfg: Config, changes: dict[str, Any]) -> Config:
    """Return a new Config with dotted-path changes applied (validated)."""
    tree = cfg.model_dump(mode="json")
    for key, value in changes.items():
        _set_path(tree, key, value)
    return Config.model_validate(tree)


def canonical_json(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON serialisable: {type(o)}")


# Fields added after runs were recorded, with the value that reproduces the old behaviour. While a field
# holds that legacy value it is left out of the hash, so a recorded config keeps its exact hash (and its
# replay stays exact); any other value is hashed normally.
HASH_LEGACY_DEFAULTS: dict[tuple[str, str], Any] = {("ai", "repair_plan_nesting"): False}


def config_hash(cfg: Config) -> str:
    tree = cfg.model_dump(mode="json")
    for (section, key), legacy in HASH_LEGACY_DEFAULTS.items():
        if tree.get(section, {}).get(key, legacy) == legacy:
            tree.get(section, {}).pop(key, None)
    return hashlib.sha256(canonical_json(tree)).hexdigest()[:12]


def account_label(mode: str, feed: str = "live") -> str:
    """The account an engine trades and learns on: ``mode`` on the live feed, ``{mode}_{feed}``
    otherwise (``shadow`` vs ``shadow_synthetic``). The state DB, the LLM cache (next to the logs)
    and the reviewer's runtime overlay are kept per account."""
    return mode if feed == "live" else f"{mode}_{feed}"


def state_db_path(cfg: Config, mode: str, feed: str = "live") -> str:
    """The engine's state DB for ``mode`` and ``feed`` (moved here from ``wta.engine`` so the
    read-only tools — ``wta status``, ``wta tui`` — resolve it without importing the engine).

    The account label is ``mode`` on the live feed and ``{mode}_{feed}`` otherwise (``shadow`` vs
    ``shadow_synthetic``): synthetic prices never touch the real-feed shadow account, its candles or
    its z-score history. ``data.state_sqlite_path`` with ``{mode}`` replaced by that label; a path
    without ``{mode}`` gets it inserted before the suffix (``x/state.sqlite`` ->
    ``x/state.shadow.sqlite``), so shadow and live can never share a file. Left at its default while
    ``data.sqlite_path`` is overridden, it is derived from the market DB instead:
    ``<dir>/<stem>.state_{mode}.sqlite``. ``:memory:`` stays in memory.
    """
    label = account_label(mode, feed)
    template = cfg.data.state_sqlite_path
    default_state = str(DataCfg.model_fields["state_sqlite_path"].default)
    default_market = str(DataCfg.model_fields["sqlite_path"].default)
    if template == default_state and cfg.data.sqlite_path != default_market:
        if cfg.data.sqlite_path == ":memory:":
            return ":memory:"
        market = Path(cfg.data.sqlite_path)
        template = str(market.parent / f"{market.stem}.state_{{mode}}.sqlite")
    if template == ":memory:":
        return template
    if "{mode}" not in template:
        path = Path(template)
        return str(path.with_name(f"{path.stem}.{label}{path.suffix}"))
    return template.replace("{mode}", label)


def load_settings() -> Settings:
    return Settings()

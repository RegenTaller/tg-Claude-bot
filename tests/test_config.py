import pytest

from bot import config as cfg


def test_cost_uses_model_specific_cache_read_price():
    """api-8: чтение кэша у Opus 5.5 — $0.20 (0.05×), у Fable 5.1 — $0.25 (0.025×), а не 0.1× входа."""
    assert cfg.cost_usd("claude-opus-5-5", 0, 0, 1_000_000, 0) == pytest.approx(0.20)
    assert cfg.cost_usd("claude-fable-5-1", 0, 0, 1_000_000, 0) == pytest.approx(0.25)
    assert cfg.cost_usd("claude-sonnet-5-5", 0, 0, 1_000_000, 0) == pytest.approx(0.20)


def test_cost_cache_write_is_125_percent_of_input():
    assert cfg.cost_usd("claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(5.0)


def test_haiku_long_prompt_switches_whole_request_to_second_rate_card():
    small = cfg.cost_usd("claude-haiku-5-5", 100_000, 1_000_000, 0, 0)
    assert small == pytest.approx(100_000 * 0.10 / 1e6 + 0.50)
    # промпт = вход + чтение кэша + запись в кэш; больше 100K — вся сетка ×5
    big = cfg.cost_usd("claude-haiku-5-5", 1_000, 1_000_000, 100_000, 0)
    assert big == pytest.approx(1_000 * 0.50 / 1e6 + 100_000 * 0.05 / 1e6 + 2.50)


def test_fallback_models_have_prices_and_prefix_does_not_collide():
    """api-3: резервные модели не должны считаться бесплатными; claude-opus-5-5 ≠ claude-opus-5."""
    assert cfg.cost_usd("claude-opus-4-8", 1_000_000, 0, 0, 0) == pytest.approx(5.0)
    assert cfg.cost_usd("claude-opus-5", 1_000_000, 0, 0, 0) == pytest.approx(5.0)
    assert cfg.cost_usd("claude-opus-5-5", 1_000_000, 0, 0, 0) == pytest.approx(4.0)
    assert cfg.cost_usd("claude-sonnet-5", 0, 1_000_000, 0, 0) == pytest.approx(10.0)


def test_unknown_model_has_no_price():
    assert cfg.cost_usd("claude-unknown-9", 10, 10, 0, 0) is None


@pytest.fixture
def env(monkeypatch):
    for k in ("ALLOWED_USER_IDS", "ADMIN_USER_IDS", "MAX_TOKENS", "USER_MONTHLY_LIMIT_USD", "HISTORY_RETENTION_DAYS",
              "DEFAULT_MODEL", "DEFAULT_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    return monkeypatch


def test_defaults(env):
    c = cfg.load()
    assert c.max_tokens == 64000
    assert c.default_model == "claude-opus-5-5"
    assert c.user_monthly_limit_usd == 0
    assert c.history_retention_days == 30


def test_admins_are_not_implied_by_allowed_users(env):
    env.setenv("ALLOWED_USER_IDS", "1, 2")
    c = cfg.load()
    assert c.allowed_user_ids == {1, 2}
    assert c.admin_user_ids == set()


@pytest.mark.parametrize("name,value", [
    ("ALLOWED_USER_IDS", "12;34"),
    ("MAX_TOKENS", "200000"),
    ("MAX_TOKENS", "много"),
    ("USER_MONTHLY_LIMIT_USD", "-1"),
    ("DEFAULT_MODEL", "gpt"),
])
def test_bad_values_stop_with_message(env, name, value):
    env.setenv(name, value)
    with pytest.raises(SystemExit) as e:
        cfg.load()
    assert name in str(e.value)

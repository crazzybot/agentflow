"""Tests for Settings — cost-tier resolution."""
from agentflow.config import settings


def test_resolve_known_tiers():
    assert settings.resolve_model_tier("economy") == settings.model_tier_economy
    assert settings.resolve_model_tier("standard") == settings.model_tier_standard
    assert settings.resolve_model_tier("premium") == settings.model_tier_premium


def test_resolve_none_returns_none():
    assert settings.resolve_model_tier(None) is None


def test_resolve_unknown_tier_returns_none():
    assert settings.resolve_model_tier("deluxe") is None

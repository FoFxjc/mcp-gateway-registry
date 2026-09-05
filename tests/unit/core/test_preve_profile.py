"""
Unit tests for the Preve Personal Edition Slice 1 config preset.

Covers ``registry.core.config._apply_preve_personal_profile`` and
``PREVE_PERSONAL_PROFILE_DEFAULTS``: the additive, reversible bundle of
environment-variable defaults activated by ``PREVE_PROFILE=personal``.

These tests call ``_apply_preve_personal_profile()`` directly rather than
re-instantiating ``Settings()``, because the function runs once at module
import time (before the module-level ``settings = Settings()``), not on
every ``Settings()`` call -- see docs/preve/profile-behavior.md.
"""

import pytest

from registry.core.config import PREVE_PERSONAL_PROFILE_DEFAULTS, _apply_preve_personal_profile


@pytest.mark.unit
@pytest.mark.core
class TestPrevePersonalProfilePreset:
    """Test the PREVE_PROFILE=personal environment-variable preset."""

    def test_noop_when_preve_profile_unset(self, monkeypatch) -> None:
        """No PREVE_PROFILE set: not a single preset variable is touched."""
        monkeypatch.delenv("PREVE_PROFILE", raising=False)
        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            monkeypatch.delenv(key, raising=False)

        _apply_preve_personal_profile()

        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            assert key not in __import__("os").environ

    def test_noop_when_preve_profile_is_some_other_value(self, monkeypatch) -> None:
        """An unrelated PREVE_PROFILE value must not activate the preset."""
        monkeypatch.setenv("PREVE_PROFILE", "enterprise")
        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            monkeypatch.delenv(key, raising=False)

        _apply_preve_personal_profile()

        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            assert key not in __import__("os").environ

    def test_applies_every_documented_default_when_personal(self, monkeypatch) -> None:
        """PREVE_PROFILE=personal seeds every documented default."""
        monkeypatch.setenv("PREVE_PROFILE", "personal")
        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            monkeypatch.delenv(key, raising=False)

        _apply_preve_personal_profile()

        import os

        for key, expected_value in PREVE_PERSONAL_PROFILE_DEFAULTS.items():
            assert os.environ[key] == expected_value

    def test_case_and_whitespace_insensitive(self, monkeypatch) -> None:
        """PREVE_PROFILE matching is case-insensitive and trims whitespace."""
        monkeypatch.setenv("PREVE_PROFILE", "  Personal  ")
        monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)

        _apply_preve_personal_profile()

        import os

        assert os.environ["DEPLOYMENT_MODE"] == "registry-only"

    def test_never_overrides_an_operator_supplied_value(self, monkeypatch) -> None:
        """An operator-set value always wins over the preset (setdefault semantics)."""
        monkeypatch.setenv("PREVE_PROFILE", "personal")
        monkeypatch.setenv("DEPLOYMENT_MODE", "with-gateway")
        monkeypatch.setenv("TELEMETRY_ENABLED", "true")

        _apply_preve_personal_profile()

        import os

        assert os.environ["DEPLOYMENT_MODE"] == "with-gateway"
        assert os.environ["TELEMETRY_ENABLED"] == "true"
        # Every other (non-overridden) default still applies.
        assert os.environ["STORAGE_BACKEND"] == "mongodb-ce"
        assert os.environ["A2A_REVERSE_PROXY_ENABLED"] == "false"

    def test_settings_reflect_preset_defaults(self, monkeypatch, tmp_path) -> None:
        """Settings() built after the preset runs resolves the personal defaults."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SECRET_KEY", "test-key-for-preve-profile-at-least-32-bytes")
        monkeypatch.setenv("PREVE_PROFILE", "personal")
        for key in PREVE_PERSONAL_PROFILE_DEFAULTS:
            monkeypatch.delenv(key, raising=False)

        _apply_preve_personal_profile()

        from registry.core.config import DeploymentMode, Settings

        settings = Settings()

        assert settings.deployment_mode == DeploymentMode.REGISTRY_ONLY
        assert settings.a2a_reverse_proxy_enabled is False
        assert settings.storage_backend == "mongodb-ce"
        assert settings.rate_limiting_enabled is False
        assert settings.registration_gate_enabled is False
        assert settings.egress_auth_enabled is False
        assert settings.telemetry_enabled is False

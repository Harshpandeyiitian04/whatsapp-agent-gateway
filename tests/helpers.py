import os

from app.config import Settings

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://app:change-me-local-only@localhost:5455/gateway_test")


def make_settings(**overrides: object) -> Settings:
    """Fast settings for tests: no real sleeps, tiny lab timeouts."""
    base: dict[str, object] = {
        "database_url": TEST_DSN,
        "lab_timeout_s": 0.05,
        "lab_ambiguous_margin_s": 0.0,
        "lab_backoff_base_s": 0.0,
        "lab_backoff_cap_s": 0.0,
        "whatsapp_app_secret": "test-secret",
        "admin_token": "test-admin",
        "allow_reset": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]

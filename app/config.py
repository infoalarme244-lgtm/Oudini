"""Configuration lue depuis l'environnement. Aucun secret n'a de valeur par défaut en production."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.getenv("SHOPFORGE_ENV", "dev"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./shopforge.db"))
    secret_key: str = field(default_factory=lambda: os.getenv("SHOPFORGE_SECRET_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("SHOPFORGE_BASE_URL", "http://localhost:8000"))
    jobs_inline: bool = field(default_factory=lambda: _bool("SHOPFORGE_JOBS_INLINE", False))
    anthropic_model: str = field(default_factory=lambda: os.getenv("SHOPFORGE_AI_MODEL", "claude-opus-5-5"))
    ai_enabled: bool = field(default_factory=lambda: bool(os.getenv("ANTHROPIC_API_KEY")) and _bool("SHOPFORGE_AI", True))
    default_ai_budget_usd: float = field(default_factory=lambda: float(os.getenv("SHOPFORGE_AI_BUDGET_USD", "5")))
    fetch_timeout: float = 10.0
    fetch_max_bytes: int = 2_000_000
    fetch_allowed_ports: tuple = (80, 443)
    user_agent: str = "ShopForgeBot/0.1 (+analyse concurrentielle; respecte robots.txt)"
    email_provider: str = field(default_factory=lambda: os.getenv("EMAIL_PROVIDER", "outbox"))  # outbox | postmark | brevo | smtp
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))
    postmark_token: str = field(default_factory=lambda: os.getenv("POSTMARK_SERVER_TOKEN", ""))
    brevo_api_key: str = field(default_factory=lambda: os.getenv("BREVO_API_KEY", ""))
    smtp_url: str = field(default_factory=lambda: os.getenv("SMTP_URL", ""))  # smtp(s)://user:pass@host:port
    sentry_dsn: str = field(default_factory=lambda: os.getenv("SENTRY_DSN", ""))

    def __post_init__(self):
        if not self.secret_key:
            if self.env == "prod":
                raise RuntimeError("SHOPFORGE_SECRET_KEY est obligatoire en production")
            self.secret_key = "dev-insecure-key-change-me"


settings = Settings()

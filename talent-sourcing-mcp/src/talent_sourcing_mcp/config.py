"""Settings read from environment variables. Every API key is optional."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass
class Settings:
    github_token: str | None = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    stackexchange_key: str | None = field(default_factory=lambda: _env("STACKEXCHANGE_KEY"))
    pdl_api_key: str | None = field(default_factory=lambda: _env("PDL_API_KEY"))
    hunter_api_key: str | None = field(default_factory=lambda: _env("HUNTER_API_KEY"))
    data_dir: Path = field(
        default_factory=lambda: Path(
            _env("TALENT_MCP_DATA_DIR") or Path.home() / ".talent-sourcing-mcp"
        )
    )
    http_auth_token: str | None = field(default_factory=lambda: _env("TALENT_MCP_AUTH_TOKEN"))
    timeout_seconds: float = field(
        default_factory=lambda: float(_env("TALENT_MCP_TIMEOUT") or 20)
    )

    def sources(self) -> dict[str, dict[str, object]]:
        return {
            "github": {
                "enabled": True,
                "authenticated": bool(self.github_token),
                "note": "Works without a key (60 requests/hour). Set GITHUB_TOKEN for 5,000/hour.",
            },
            "stackoverflow": {
                "enabled": True,
                "authenticated": bool(self.stackexchange_key),
                "note": "Works without a key (300 requests/day). Set STACKEXCHANGE_KEY for 10,000/day.",
            },
            "peopledatalabs": {
                "enabled": bool(self.pdl_api_key),
                "note": "Any role or industry (not just tech). Requires PDL_API_KEY.",
            },
            "hunter": {
                "enabled": bool(self.hunter_api_key),
                "note": "Finds and verifies work emails. Requires HUNTER_API_KEY.",
            },
        }

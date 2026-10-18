"""Configuration: .env for secrets and paths, YAML files for policy, models and templates.

Policy and model choices are data. Code reads them; agents can never write them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    home: Path
    db_path: Path
    config_dir: Path
    data_dir: Path
    dry_run: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, *, home: str | Path | None = None, db_path: str | Path | None = None,
             dry_run: bool | None = None, **overrides: Any) -> "Settings":
        home = Path(home or os.environ.get("BIZOPS_HOME") or Path.cwd()).resolve()
        load_dotenv(home / ".env")
        config_dir = Path(os.environ.get("BIZOPS_CONFIG_DIR") or home / "config")
        if not (config_dir / "policy.yaml").is_file():
            config_dir = REPO_CONFIG
        data_dir = Path(os.environ.get("BIZOPS_DATA_DIR") or home / "data")
        db = Path(db_path or os.environ.get("BIZOPS_DB") or data_dir / "bizops.db")
        if dry_run is None:
            dry_run = os.environ.get("BIZOPS_DRY_RUN", "").lower() in ("1", "true", "yes")
        return cls(home=home, db_path=db, config_dir=config_dir, data_dir=data_dir,
                   dry_run=dry_run, overrides=overrides)

    def _yaml(self, name: str) -> dict[str, Any]:
        path = self.config_dir / name
        return yaml.safe_load(path.read_text()) or {}

    @cached_property
    def policy(self) -> dict[str, Any]:
        data = self._yaml("policy.yaml")
        return deep_merge(data, self.overrides.get("policy", {}))

    @cached_property
    def models(self) -> dict[str, Any]:
        data = self._yaml("models.yaml")
        return deep_merge(data, self.overrides.get("models", {}))

    @cached_property
    def roles(self) -> dict[str, Any]:
        return self._yaml("roles.yaml")

    def role(self, name: str) -> dict[str, Any]:
        if name not in self.roles:
            raise ValueError(f"unknown role: {name} (see config/roles.yaml)")
        return self.roles[name]

    def template(self, name: str) -> dict[str, Any]:
        path = self.config_dir / "templates" / f"{name}.yaml"
        if not path.is_file():
            raise ValueError(f"unknown org template: {name}")
        return yaml.safe_load(path.read_text())

    def template_names(self) -> list[str]:
        return sorted(p.stem for p in (self.config_dir / "templates").glob("*.yaml"))

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def secret(self, name: str) -> str | None:
        """The only way code reads a credential. Tools call this; agents never see it."""
        value = os.environ.get(name)
        return value or None


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=VALUE pairs in a .env file (created with owner-only permissions) and in os.environ."""
    lines = path.read_text().splitlines() if path.is_file() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            seen.add(key)
            if updates[key] != "":
                out.append(f"{key}={updates[key]}")
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen and v != "":
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    for k, v in updates.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

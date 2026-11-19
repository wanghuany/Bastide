"""Machine-local vault location, separate from version-controlled application code."""

import json
import os
from pathlib import Path

from app.core.errors import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[4]


def resolve_vault_path(explicit: Path | None = None, repo_root: Path = REPO_ROOT) -> Path:
    """CLI override > environment > ignored local JSON > legacy repository default.

    This only resolves a path. It never creates, migrates, or decrypts a ledger.
    Configured paths must be absolute so a different working directory is harmless.
    """
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = os.environ.get("BASTIDE_PERSONAL_VAULT")
    if not configured:
        config = repo_root / "personal.local.json"
        if config.exists():
            try:
                values = json.loads(config.read_text(encoding="utf-8-sig"))
                configured = values["vault_path"]
                if not isinstance(configured, str) or not configured.strip():
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValidationError("personal.local.json 无效，请检查 vault_path 配置。") from exc
    if configured:
        selected = Path(configured).expanduser()
        if not selected.is_absolute() or selected.suffix.lower() != ".bfin":
            raise ValidationError("账本配置需要使用以 .bfin 结尾的绝对路径。")
        legacy = repo_root / ".personal-data" / "ledger.bfin"
        if legacy.exists() and selected.resolve() != legacy.resolve():
            raise ValidationError(
                "旧目录仍有账本。请先停止软件，备份并迁移 .personal-data/ledger.bfin，"
                "核对后移走旧文件，避免误建第二份账本。"
            )
        return selected.resolve()
    return repo_root / ".personal-data" / "ledger.bfin"

"""Machine-local path selection must survive moving the source checkout."""

import json
from pathlib import Path

import pytest

from app.core.errors import ValidationError
from app.features.personal.config import resolve_vault_path


def test_external_location_and_explicit_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BASTIDE_PERSONAL_VAULT", raising=False)
    repo = tmp_path / "PersonalLedger"
    repo.mkdir()
    external = tmp_path / "虚构同步目录" / "ledger.bfin"
    (repo / "personal.local.json").write_text(
        json.dumps({"vault_path": str(external)}), encoding="utf-8"
    )
    assert resolve_vault_path(repo_root=repo) == external.resolve()
    assert not external.exists()
    environment = tmp_path / "environment.bfin"
    monkeypatch.setenv("BASTIDE_PERSONAL_VAULT", str(environment))
    assert resolve_vault_path(repo_root=repo) == environment.resolve()
    explicit = tmp_path / "explicit.bfin"
    assert resolve_vault_path(explicit, repo_root=repo) == explicit.resolve()


@pytest.mark.parametrize(
    "contents", ["{", "{}", "[]", '{"vault_path":12}', '{"vault_path":"relative.bfin"}']
)
def test_invalid_config_fails_instead_of_creating_another_ledger(tmp_path, monkeypatch, contents):
    monkeypatch.delenv("BASTIDE_PERSONAL_VAULT", raising=False)
    (tmp_path / "personal.local.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValidationError):
        resolve_vault_path(repo_root=tmp_path)


def test_legacy_ledger_requires_deliberate_migration(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BASTIDE_PERSONAL_VAULT", raising=False)
    legacy = tmp_path / ".personal-data" / "ledger.bfin"
    assert resolve_vault_path(repo_root=tmp_path) == legacy
    legacy.parent.mkdir()
    legacy.write_bytes(b"fictional opaque legacy file")
    selected = tmp_path / "external" / "ledger.bfin"
    (tmp_path / "personal.local.json").write_text(
        json.dumps({"vault_path": str(selected)}), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="旧目录"):
        resolve_vault_path(repo_root=tmp_path)
    assert legacy.read_bytes() == b"fictional opaque legacy file"
    assert not selected.exists()

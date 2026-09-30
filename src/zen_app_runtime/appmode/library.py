"""App definition library: one validated JSON file per app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .definition import AppDefinition, definition_from_dict, definition_to_dict, validate_app_id
from .errors import AppModeError


def write_private_file(path: Path, data: bytes, mode: int = 0o600) -> bool:
    """Atomically replace ``path`` with ``data``; return False when unchanged."""
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return True


def render_definition(definition: AppDefinition) -> bytes:
    return (json.dumps(definition_to_dict(definition), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, app_id: str) -> Path:
        return self.root / f"{validate_app_id(app_id)}.json"

    def exists(self, app_id: str) -> bool:
        return self.path_for(app_id).is_file()

    def load(self, app_id: str) -> AppDefinition:
        path = self.path_for(app_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise AppModeError(f"no app named {app_id!r}; create it with `zen-app new`", code="notFound") from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AppModeError(f"{path} is not valid JSON: {error}") from error
        definition = definition_from_dict(data)
        if definition.id != app_id:
            raise AppModeError(f"{path} declares id {definition.id!r}")
        return definition

    def save(self, definition: AppDefinition) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        path = self.path_for(definition.id)
        write_private_file(path, render_definition(definition))
        return path

    def create(self, definition: AppDefinition) -> Path:
        if self.exists(definition.id):
            raise AppModeError(
                f"an app named {definition.id!r} already exists; pick another --id or remove it first",
                code="exists",
            )
        return self.save(definition)

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        ids = []
        for entry in sorted(self.root.glob("*.json")):
            try:
                ids.append(validate_app_id(entry.stem))
            except AppModeError:
                continue
        return ids

    def delete(self, app_id: str) -> bool:
        try:
            self.path_for(app_id).unlink()
        except FileNotFoundError:
            return False
        return True

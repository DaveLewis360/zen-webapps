"""The Zen Internet (my-internet) site style catalog.

Zen Internet (https://github.com/sameerasw/zen-internet) styles ~670 sites
from the community catalog https://sameerasw.github.io/my-internet/styles.json
(MIT, (c) Transparent Zen). App mode ships a pinned copy and can update it;
site matching mirrors the extension so a site looks the same as in Zen.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
import urllib.request

from .errors import AppModeError
from .library import write_private_file
from .paths import AppModePaths

CATALOG_URL = "https://sameerasw.github.io/my-internet/styles.json"
BUNDLED_CATALOG = Path(__file__).resolve().parent / "data" / "zen-internet-styles.json"
BUNDLED_META = Path(__file__).resolve().parent / "data" / "zen-internet-styles.meta.json"
MAX_CATALOG_BYTES = 16 * 1024 * 1024


def updated_catalog_path(paths: AppModePaths) -> Path:
    return paths.data / "zen-internet-styles.json"


def catalog_path(paths: AppModePaths) -> Path:
    updated = updated_catalog_path(paths)
    return updated if updated.is_file() else BUNDLED_CATALOG


def validate_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("website"), dict):
        raise AppModeError("the Zen Internet catalog has no 'website' table")
    for site, features in data["website"].items():
        if not isinstance(site, str) or not site.endswith(".css") or not isinstance(features, dict):
            raise AppModeError(f"the Zen Internet catalog has an invalid entry {site!r}")
        for name, css in features.items():
            if not isinstance(name, str) or not isinstance(css, str):
                raise AppModeError(f"the Zen Internet catalog has an invalid feature in {site!r}")
    mapping = data.get("mapping", {})
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(t, str) for t in v) for k, v in mapping.items()
    ):
        raise AppModeError("the Zen Internet catalog has an invalid 'mapping' table")
    return data


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        return validate_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise AppModeError(f"cannot read the Zen Internet catalog {path}: {error}") from error


def _normalize(hostname: str) -> str:
    hostname = hostname.lower().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def best_match(website: dict[str, Any], hostname: str, mapping: dict[str, list[str]] | None = None) -> str | None:
    """Same precedence as Zen Internet: exact, +wildcard, -TLD wildcard, suffix, mapping."""
    host = _normalize(hostname)
    best: str | None = None
    best_length = 0
    for key in website:
        site = key[:-4] if key.endswith(".css") else key
        normalized = _normalize(site)
        if host == normalized:
            return key
        if site.startswith("+"):
            base = site[1:]
            if (host == base or host.endswith("." + base)) and len(base) > best_length:
                best, best_length = key, len(base)
        elif site.startswith("-"):
            domain = ".".join(site[1:].split(".")[:-1])
            parts = host.split(".")
            host_domain = ".".join(parts[:-1]) if len(parts) > 1 else host
            if domain and host_domain == domain and len(domain) > best_length:
                best, best_length = key, len(domain)
        elif host != normalized and host.endswith("." + normalized) and len(normalized) > best_length:
            best, best_length = key, len(normalized)
    if best:
        return best
    for source, targets in (mapping or {}).items():
        if host in targets and source in website:
            return source
    return None


def site_has_transparency(features: dict[str, str]) -> bool:
    return any("transpar" in name.lower() for name in features)


def _site_base(key: str) -> str:
    site = key[:-4] if key.endswith(".css") else key
    return _normalize(site.lstrip("+-"))


def match_hosts(
    catalog: dict[str, Any], hosts: list[str], exact_only: frozenset[str] | set[str] = frozenset()
) -> dict[str, list[str]]:
    """Catalog key -> hosts it styles. Hosts in ``exact_only`` (sign-in hops)
    only take a style written for exactly that host."""
    matches: dict[str, list[str]] = {}
    for host in hosts:
        exact = host in exact_only
        if host.startswith("*."):
            host = host[2:]
        key = best_match(catalog["website"], host, catalog.get("mapping"))
        if key and exact and _site_base(key) != _normalize(host):
            key = None
        if key:
            matches.setdefault(key, [])
            if host not in matches[key]:
                matches[key].append(host)
    return matches


def update_catalog(paths: AppModePaths, *, url: str = CATALOG_URL, opener=urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "zen-app-runtime"})
    with opener(request, timeout=30) as response:
        raw = response.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise AppModeError("the downloaded Zen Internet catalog is unexpectedly large")
    try:
        data = validate_catalog(json.loads(raw))
    except ValueError as error:
        raise AppModeError(f"the downloaded Zen Internet catalog is not valid JSON: {error}") from error
    target = updated_catalog_path(paths)
    changed = write_private_file(target, raw, mode=0o644)
    return {
        "path": str(target),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sites": len(data["website"]),
        "changed": changed,
    }


def catalog_meta(paths: AppModePaths) -> dict[str, Any]:
    path = catalog_path(paths)
    if path == BUNDLED_CATALOG:
        try:
            meta = json.loads(BUNDLED_META.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return {"path": str(path), "origin": "bundled", **meta}
    raw = path.read_bytes()
    return {"path": str(path), "origin": "updated", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

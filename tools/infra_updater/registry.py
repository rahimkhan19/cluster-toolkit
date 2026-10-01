# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Package registry (packages.yaml) and blueprint discovery.

Each package declares "pins": regexes that locate its version in blueprint text. The same pins are
used to discover blueprint instances (and read each blueprint's current version) and to rewrite
them on apply, so a package needs no code of its own. Rewritable named groups:

  version      the version; the current version is read from it
  major_minor  '<major>.<minor>' of the new version
  url          the candidate's artifact URL
  filename     basename of the candidate's artifact URL
  image        a container image reference, moved to the new version keeping its own variant

A group nested inside another rewritten group is only read (e.g. version inside url).
"""

import fnmatch
import functools
import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from versions import match_v_prefix, oldest_version, parse_semver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(BASE_DIR, "packages.yaml")
REWRITE_GROUPS = ("version", "major_minor", "url", "filename", "image")
_LINE_KEY_RE = re.compile(r"[ \t]*(?:-[ \t]+)?([\w.-]+):")


@dataclass
class PackageDef:
    id: str
    name: str
    upstream_type: str
    source_url: str
    source_options: Dict[str, Any]
    pins: List[re.Pattern]
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)

    def applies_to(self, path: str) -> bool:
        if self.include and not any(fnmatch.fnmatch(path, g) for g in self.include):
            return False
        return not any(fnmatch.fnmatch(path, g) for g in self.exclude)


@dataclass
class Registry:
    packages: Dict[str, PackageDef]
    roots: List[str]
    test_overrides: Dict[str, str]


@functools.lru_cache(maxsize=None)
def load_registry(path: str = REGISTRY_PATH) -> Registry:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    packages = {}
    for p in raw.get("packages", []):
        packages[p["id"]] = PackageDef(
            id=p["id"], name=p.get("name", p["id"]),
            upstream_type=p["upstream"]["type"], source_url=p["upstream"]["url"],
            source_options=p.get("source_options") or {},
            pins=[re.compile(rx, re.MULTILINE) for rx in p.get("pins", [])],
            include=p.get("include") or [], exclude=p.get("exclude") or [],
        )
    return Registry(packages=packages, roots=(raw.get("discovery") or {}).get("roots", ["examples/**/*.yaml"]),
                    test_overrides=raw.get("test_overrides") or {})


def _line_key(text: str, pos: int) -> str:
    """YAML key of the line containing pos (e.g. 'url'); for list items and script lines, the parent key."""
    lines = text[:text.find("\n", pos) if text.find("\n", pos) != -1 else len(text)].split("\n")
    m = _LINE_KEY_RE.match(lines[-1])
    if m:
        return m.group(1)
    indent = len(lines[-1]) - len(lines[-1].lstrip())
    indent += lines[-1].lstrip().startswith("- ")  # YAML allows list items at their parent key's indent
    for line in reversed(lines[:-1]):
        stripped = line.lstrip()
        if stripped and len(line) - len(stripped) < indent and (m := _LINE_KEY_RE.match(line)):
            return m.group(1)
    return "-"


# ------------------------------------------------------------------ discovery

def scan(pkg: PackageDef, text: str) -> List[Tuple[str, str]]:
    """(line key, version) for every pinned version of pkg in text."""
    found = []
    for rx in pkg.pins:
        if "version" not in rx.groupindex:
            continue
        for m in rx.finditer(text):
            found.append((_line_key(text, m.start("version")), m.group("version")))
    return found


def discover(workspace: str, registry: Optional[Registry] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Scans the workspace's blueprints once and returns {package_id: [instance, ...]}."""
    registry = registry or load_registry()
    paths = sorted({os.path.relpath(p, workspace) for root in registry.roots
                    for p in glob.glob(os.path.join(workspace, root), recursive=True)})
    found: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in registry.packages}
    for path in paths:
        try:
            with open(os.path.join(workspace, path), "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for pkg in registry.packages.values():
            if not pkg.applies_to(path):
                continue
            pins = [(k, v) for k, v in scan(pkg, text) if parse_semver(v)]
            if not pins:
                continue
            keys = list(dict.fromkeys(k for k, _ in pins))
            found[pkg.id].append({
                "instance_id": path,
                "blueprint_path": path,
                "current_version": oldest_version(v for _, v in pins),
                "variable_name": keys[0],
                "coupled_vars": [{"variable_name": k} for k in keys[1:]],
                "pin_count": len(pins),
            })
    return found


def package_current_version(blueprints: List[Dict[str, Any]], disabled: List[str]) -> Optional[str]:
    """Oldest version across the selected blueprints (all blueprints if none is selected)."""
    selected = [b for b in blueprints if b.get("instance_id") not in set(disabled or [])] or blueprints
    return oldest_version(b.get("current_version") for b in selected)


# ------------------------------------------------------------------ rewrite

def render_values(version: str, url: Optional[str]) -> Dict[str, Optional[str]]:
    core = parse_semver(version)
    return {
        "version": version,
        "major_minor": f"{core.major}.{core.minor}" if core else None,
        "url": url or None,
        "filename": os.path.basename(url.split("?", 1)[0]) if url else None,
    }


def _group_value(name: str, old: str, values: Dict[str, Optional[str]],
                 image_ref: Callable[[str, str], Optional[str]]) -> Optional[str]:
    if name == "version":
        return match_v_prefix(old, values["version"])
    if name == "image":
        new = image_ref(old, values["version"])
        if not new:
            print(f"[WARN] No '{values['version']}'-matching variant of {old} found; left unchanged.")
        return new
    return values.get(name)


def rewrite(text: str, pkg: PackageDef, values: Dict[str, Optional[str]],
            image_ref: Optional[Callable[[str, str], Optional[str]]] = None) -> Tuple[str, List[Dict[str, str]]]:
    """Rewrites every pin of pkg in text; returns (new_text, [{variable, old_value, new_value}, ...])."""
    if image_ref is None:
        from fetchers import variant_image_ref as image_ref  # network lookup only when an image pin exists
    changes: List[Dict[str, str]] = []

    def _sub(m: re.Match, rx: re.Pattern) -> str:
        spans = sorted(((m.span(g), g) for g in rx.groupindex if g in REWRITE_GROUPS and m.span(g) != (-1, -1)),
                       key=lambda s: (s[0][0], -s[0][1]))  # outer group first
        out, pos, base, outer_end = [], m.start(), m.start(), m.start()
        for (start, end), name in spans:
            if start < outer_end:  # nested in an outer rewrite group: only read
                continue
            outer_end = end
            old = m.group(name)
            new = _group_value(name, old, values, image_ref)
            if new is None or new == old:
                continue
            out.append(m.string[pos:start] + new)
            pos = end
            changes.append({"variable": _line_key(m.string, start), "old_value": old, "new_value": new})
        out.append(m.string[pos:m.end()])
        return "".join(out) if pos != base else m.group(0)

    for rx in pkg.pins:
        text = rx.sub(lambda m, rx=rx: _sub(m, rx), text)
    return text, changes

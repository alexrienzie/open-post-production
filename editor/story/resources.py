"""Load editorial story reference files from `story/_resources/`.

Manifest schema:

    {
      "macro_structure": [
        {"id": "harmon_story_circle", "filter_path": "macro_structure/harmon_story_circle.json", ...},
        ...
      ],
      "micro_structure": [
        {"id": "documentary_craft", "path": "micro_structure/documentary_craft.json", ...},
        ...
      ]
    }

"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from project import resources_dir


class MacroEntry(BaseModel):
    """One entry in manifest.macro_structure[].

    Either `path` (for plain reference docs) or
    `filter_path` (for v1-schema framework filters) is present. We accept both.
    """

    id: str
    title: str = ""
    path: str | None = None
    filter_path: str | None = None
    legacy_filter_path: str | None = None
    kind: str | None = None  # "framework_filter" | "reference_doc"
    schema_version: str | None = None  # "v1" | "v0_legacy"
    tradition: str | None = None
    engine: str | None = None
    layer: str | None = None
    status: str | None = None
    project_overlay_key: str | None = None
    project_data_key: str | None = None

    def primary_path(self) -> str | None:
        """Whichever path field this entry uses."""
        return self.path or self.filter_path


class MicroEntry(BaseModel):
    """One entry in manifest.micro_structure[] — a family of micro-principles."""

    id: str
    title: str = ""
    family: str | None = None
    path: str
    status: str = "active"
    tradition_id: str | None = None


class ResourcesManifest(BaseModel):
    default_macro_filter_id: str | None = None
    macro_structure: list[MacroEntry] = Field(default_factory=list)
    micro_structure: list[MicroEntry] = Field(default_factory=list)


# -- Generic loaders -------------------------------------------------------


def manifest_path() -> Path:
    return resources_dir() / "manifest.json"


def load_manifest() -> ResourcesManifest:
    raw = json.loads(manifest_path().read_text(encoding="utf-8"))
    return ResourcesManifest.model_validate(raw)


# -- Macro (frameworks + reference docs) -----------------------------------


def macro_entry(filter_id: str) -> MacroEntry:
    manifest = load_manifest()
    for entry in manifest.macro_structure:
        if entry.id == filter_id:
            return entry
    raise KeyError(
        f"Unknown macro filter id {filter_id!r}; check story/_resources/manifest.json"
    )


def macro_filter_path(filter_id: str) -> Path:
    entry = macro_entry(filter_id)
    rel = entry.primary_path()
    if rel is None:
        raise ValueError(f"macro entry {filter_id!r} has no path or filter_path")
    return resources_dir() / rel


def load_macro_filter(filter_id: str) -> dict:
    """Return the raw filter JSON. Schema varies (v0 legacy vs v1 framework filter),
    so callers branch on `kind`/`schema_version` from the manifest entry."""
    return json.loads(macro_filter_path(filter_id).read_text(encoding="utf-8"))


def load_default_macro_filter() -> dict:
    """Load the manifest's `default_macro_filter_id`, if one is set.

    We ship no default on purpose — pick the framework that fits the question
    (or set a default in your own manifest if your cut anchors on one ladder).
    """
    manifest = load_manifest()
    if not manifest.default_macro_filter_id:
        raise ValueError(
            "No default_macro_filter_id set in story/_resources/manifest.json — "
            "call load_macro_filter(<framework_id>) explicitly, or set a default."
        )
    return load_macro_filter(manifest.default_macro_filter_id)


# -- Micro (editorial principle families) ---------------------------------


def micro_entry(family_id: str) -> MicroEntry:
    manifest = load_manifest()
    for entry in manifest.micro_structure:
        if entry.id == family_id:
            return entry
    raise KeyError(
        f"Unknown micro principle family {family_id!r}; check story/_resources/manifest.json"
    )


def micro_family_path(family_id: str) -> Path:
    return resources_dir() / micro_entry(family_id).path


def load_micro_family(family_id: str) -> dict:
    return json.loads(micro_family_path(family_id).read_text(encoding="utf-8"))


def load_all_micro_families() -> list[dict]:
    """Convenience: load every registered micro-principle family file."""
    manifest = load_manifest()
    return [
        json.loads((resources_dir() / e.path).read_text(encoding="utf-8"))
        for e in manifest.micro_structure
    ]

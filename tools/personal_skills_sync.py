"""Personal skill sync — mirror the Curator's skill set to/from OpenViking.

Personal skills are the agent-created/evolved skills owned by a single user
and managed by the Hermes **Curator** (lifecycle: prune / archive /
consolidate).  They are deliberately kept OUT of SkillClaw's reach:

  - **Team** skills live under the shared ``viking://resources/...`` trees
    (``resources/skills/`` + every SkillClaw group prefix).  SkillClaw
    evolves them; the local hub installs them as ``source="openviking"`` and
    the Curator never touches them.
  - **Personal** skills live under the caller's PRIVATE
    ``viking://user/<you>/skills/`` space.  Only that user can read them, so
    SkillClaw — which only scans the shared resource trees — never consumes
    them for evolution.  They are marked ``created_by="agent"`` locally so
    the Curator manages them.

Classification is by server namespace prefix (see
``skills_hub_openviking_source.classify_skill_uri``).

This module gives the user-confirmed "本地管理 + 双向同步" behaviour:

  - :func:`pull_personal_skills` — at startup, copy any personal skills on
    the server that are missing locally into ``~/.hermes/skills/`` and mark
    them ``created_by="agent"`` so the Curator owns them.
  - :func:`push_personal_skills` — after a Curator pass (or an agent-created
    skill is written), upload the current local agent-created skill set to
    the user's private space so it survives a machine swap.

OpenViking exposes no delete endpoint, so deletions are NOT force-propagated
to the server: a locally archived skill simply stops being pushed (and is
not re-pulled because :func:`pull_personal_skills` only fills in skills that
are absent locally — it never resurrects one the Curator archived this same
session).  ``push`` overwrites file contents in place (mode=update).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return get_hermes_home() / "skills"


def _build_client() -> Any:
    """Reuse OpenVikingSkillSource's client factory (env-gated)."""
    try:
        from tools.skills_hub_openviking_source import OpenVikingSkillSource
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("personal skill sync unavailable: %s", exc)
        return None
    return OpenVikingSkillSource._build_client()


def _list_dir(client: Any, uri: str) -> List[Dict[str, Any]]:
    """List immediate children of a viking:// URI via /api/v1/fs/ls."""
    resp = client.get("/api/v1/fs/ls", params={"uri": uri.rstrip("/") + "/"})
    result = resp.get("result")
    if isinstance(result, dict):
        raw = result.get("entries") or result.get("items") or result.get("children") or []
    elif isinstance(result, list):
        raw = result
    else:
        raw = []
    entries: List[Dict[str, Any]] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        entries.append({
            "uri": e.get("uri", ""),
            "is_dir": bool(e.get("isDir") or e.get("is_dir") or e.get("type") == "dir"),
            "size": e.get("size", 0) or 0,
        })
    return entries


def _read_file(client: Any, uri: str) -> Optional[str]:
    try:
        resp = client.get("/api/v1/content/read", params={"uri": uri})
    except Exception as exc:
        logger.debug("personal skill read failed for %s: %s", uri, exc)
        return None
    result = resp.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content") or result.get("text")
        if isinstance(content, str):
            return content
    return None


# ---------------------------------------------------------------------------
# Pull — server -> local (startup)
# ---------------------------------------------------------------------------

def pull_personal_skills(quiet: bool = True) -> Dict[str, int]:
    """Copy personal skills from the user's private space into the local set.

    Only fills in skills that are ABSENT locally (never overwrites a local
    copy, never resurrects an archived skill).  Pulled skills are marked
    ``created_by="agent"`` so the Curator manages them.

    Returns ``{"pulled": int, "skipped": int, "failed": int, "total": int}``.
    No-op (all zeros) when OPENVIKING is unconfigured or OPENVIKING_USER is
    unset.  Never raises.
    """
    counts = {"pulled": 0, "skipped": 0, "failed": 0, "total": 0}
    if not os.environ.get("OPENVIKING_ENDPOINT"):
        return counts

    from tools.skills_hub_openviking_source import personal_skill_prefix
    prefix = personal_skill_prefix()
    if not prefix:
        return counts

    client = _build_client()
    if client is None:
        return counts

    try:
        entries = _list_dir(client, prefix)
    except Exception as exc:
        logger.debug("personal skill listing failed: %s", exc)
        return counts

    base = _skills_dir()
    try:
        from tools.skill_usage import (
            _read_bundled_manifest_names,
            _read_hub_installed_names,
            mark_agent_created,
        )
        off_limits = _read_bundled_manifest_names() | _read_hub_installed_names()
    except Exception:
        off_limits = set()
        mark_agent_created = None  # type: ignore

    for entry in entries:
        uri = entry.get("uri") or ""
        if not entry.get("is_dir") or not uri.startswith(prefix):
            continue
        name = uri[len(prefix):].rstrip("/").split("/")[0]
        if not name:
            continue
        counts["total"] += 1

        # Never clobber a same-named local skill (incl. bundled/hub/local).
        if name in off_limits or (base / name).exists():
            counts["skipped"] += 1
            continue

        try:
            files = _walk_tree(client, uri.rstrip("/"))
        except Exception as exc:
            logger.debug("personal skill walk failed for %s: %s", uri, exc)
            counts["failed"] += 1
            continue
        if not files or "SKILL.md" not in files:
            counts["skipped"] += 1
            continue

        try:
            _write_local_skill(base / name, files)
            if mark_agent_created is not None:
                mark_agent_created(name)
            counts["pulled"] += 1
        except Exception as exc:
            logger.debug("personal skill write failed for %s: %s", name, exc)
            counts["failed"] += 1

    if not quiet and counts["pulled"]:
        logger.info("Personal skill sync: pulled %d", counts["pulled"])
    return counts


def _walk_tree(client: Any, root_uri: str, max_depth: int = 8) -> Dict[str, str]:
    """Recursively read a personal skill tree into ``relpath -> content``."""
    files: Dict[str, str] = {}
    frontier: List[Tuple[str, int]] = [(root_uri, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth > max_depth:
            continue
        for entry in _list_dir(client, current):
            child = entry.get("uri") or ""
            if not child.startswith(root_uri):
                continue
            rel = child[len(root_uri):].lstrip("/")
            if not rel:
                continue
            if entry.get("is_dir"):
                frontier.append((child, depth + 1))
                continue
            content = _read_file(client, child)
            if content is not None:
                files[rel] = content
    return files


def _write_local_skill(skill_dir: Path, files: Dict[str, str]) -> None:
    """Write a fetched skill tree under ``skill_dir`` (parents created)."""
    for rel, content in files.items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Push — local -> server (after curator runs / agent creates a skill)
# ---------------------------------------------------------------------------

def push_personal_skills(quiet: bool = True) -> Dict[str, int]:
    """Upload the local agent-created (Curator-managed) skills to the server.

    Writes each skill as a directory tree under
    ``viking://user/<you>/skills/<name>/`` (private space).  Uses
    ``mode="update"`` so re-pushing overwrites stale content.  Skills the
    Curator archived are no longer in the agent-created set and so stop being
    pushed.

    Returns ``{"pushed": int, "failed": int, "total": int}``.  No-op when
    OPENVIKING is unconfigured / OPENVIKING_USER unset.  Never raises.
    """
    counts = {"pushed": 0, "failed": 0, "total": 0}
    if not os.environ.get("OPENVIKING_ENDPOINT"):
        return counts

    from tools.skills_hub_openviking_source import personal_skill_prefix
    prefix = personal_skill_prefix()
    if not prefix:
        return counts

    client = _build_client()
    if client is None:
        return counts

    try:
        from tools.skill_usage import list_agent_created_skill_names
        from agent.skill_utils import is_excluded_skill_path
        names = list_agent_created_skill_names()
    except Exception as exc:
        logger.debug("personal skill push setup failed: %s", exc)
        return counts

    base = _skills_dir()
    for name in names:
        skill_dir = base / name
        if not (skill_dir / "SKILL.md").exists():
            # Flat-vs-nested: find the dir actually holding this skill.
            located = _locate_skill_dir(base, name)
            if located is None:
                continue
            skill_dir = located
        counts["total"] += 1
        base_uri = f"{prefix}{name}"
        ok = True
        for file_path in sorted(skill_dir.rglob("*")):
            if not file_path.is_file() or file_path.is_symlink():
                continue
            if is_excluded_skill_path(file_path):
                continue
            rel = file_path.relative_to(skill_dir).as_posix()
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            file_uri = f"{base_uri}/{rel}"
            if not _write_remote(client, file_uri, content):
                ok = False
                break
        if ok:
            counts["pushed"] += 1
        else:
            counts["failed"] += 1

    if not quiet and counts["pushed"]:
        logger.info("Personal skill sync: pushed %d", counts["pushed"])
    return counts


def _locate_skill_dir(base: Path, name: str) -> Optional[Path]:
    """Find the directory holding skill *name* (flat or nested layout)."""
    from agent.skill_utils import is_excluded_skill_path
    for skill_md in base.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        if skill_md.parent.name == name:
            return skill_md.parent
    return None


def _write_remote(client: Any, uri: str, content: str) -> bool:
    """Write one file to OpenViking, creating or updating as needed."""
    for mode in ("update", "create"):
        try:
            client.post("/api/v1/content/write", {
                "uri": uri,
                "content": content,
                "mode": mode,
            })
            return True
        except Exception as exc:
            logger.debug("content/write %s (%s) failed: %s", uri, mode, exc)
    return False

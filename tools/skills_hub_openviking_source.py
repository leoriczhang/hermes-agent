"""
OpenViking SkillSource adapter.

Treat OpenViking as a *private team skill marketplace*: skill bundles are
uploaded as directory trees under viking://resources/skills/<name>/ via
viking_add_resource, retrievable by walking the resource tree.  Anyone in
the same OpenViking account can install with:

    hermes skills install viking://resources/skills/<name>

Why this lives in tools/ next to skills_hub.py rather than inside the
openviking plugin: SkillSource implementations need to be importable
during `hermes skills install` even when the OpenViking memory plugin
isn't being used as a memory backend.  The HTTP client is reused from
the plugin module to avoid duplicating headers/retry logic.

Important OpenViking facts (verified empirically, not from docs):

- viking_add_resource only accepts ``viking://resources/...`` as the
  ``to`` parameter — there is NO ``viking://shared/`` prefix.  The
  ``resources/`` namespace is account-scoped and team-shared by design,
  independent of the user/__team__ user-space split that memory uses.
- Uploading a directory yields a directory tree on the server, not a
  bundle.zip — files are accessible individually via GET
  ``/api/v1/content/read?uri=...`` and listed via viking_browse's POST
  ``/api/v1/content/list``.
- viking_search ``scope`` enum is ``["all", "private", "shared"]``,
  but it filters memory hits — resources are returned regardless and
  always visible to the whole account.

Future evolution hook: each fetched bundle records a ``usage_signal``
channel under viking://resources/skills/<name>/usage/ so the cloud-side
analyzer can correlate cross-tenant install/invoke/error events to:
(a) detect skill regressions impacting multiple tenants and
(b) propose new skills synthesized from shared workflow patterns.  The
client just emits structured signals; the synthesis runs server-side.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from tools.skills_hub import SkillBundle, SkillMeta, SkillSource

logger = logging.getLogger(__name__)

_VIKING_SKILL_PREFIX = "viking://resources/skills/"
# SkillClaw evolve_server writes evolved skills under
#   viking://resources/<root_prefix>/<group_id>/skills/<name>/
# (default: viking://resources/skillclaw/default/skills/...).  We scan
# this layout in addition to the legacy hermes-uploaded path so skills
# evolved on the cloud become installable without manual mirroring.
_SKILLCLAW_ROOT_PREFIX = os.environ.get("SKILLCLAW_VIKING_ROOT_PREFIX", "skillclaw")


def _skillclaw_group_ids() -> List[str]:
    """Return the SkillClaw group ids whose evolved skills we expose.

    ``SKILLCLAW_VIKING_GROUP_IDS`` (comma-separated) overrides;
    ``SKILLCLAW_VIKING_GROUP_ID`` works as a single-value fallback;
    when neither is set we expose the ``default`` group only.
    """
    multi = os.environ.get("SKILLCLAW_VIKING_GROUP_IDS", "").strip()
    if multi:
        return [g.strip() for g in multi.split(",") if g.strip()]
    single = os.environ.get("SKILLCLAW_VIKING_GROUP_ID", "").strip()
    return [single] if single else ["default"]


def _skillclaw_skill_prefix(group_id: str) -> str:
    return f"viking://resources/{_SKILLCLAW_ROOT_PREFIX}/{group_id}/skills/"


def personal_skill_prefix() -> str:
    """The per-user private skill namespace: ``viking://user/<you>/skills/``.

    Personal skills are agent-created/evolved skills owned by a single user
    and managed by the Hermes Curator (NOT SkillClaw).  They live in the
    user's private OpenViking space (only that user can read them), so
    SkillClaw — which only scans the shared ``resources/...`` skill trees —
    never consumes them for team evolution.  Returns an empty string when
    ``OPENVIKING_USER`` is unset (personal sync then becomes a no-op).
    """
    user = os.environ.get("OPENVIKING_USER", "").strip()
    if not user:
        return ""
    return f"viking://user/{user}/skills/"


# Skill ownership classes, decided purely by the server namespace prefix
# (the user-confirmed source of truth for "personal" vs "team").
SKILL_CLASS_PERSONAL = "personal"   # viking://user/<you>/skills/   — Curator-managed
SKILL_CLASS_TEAM = "team"           # viking://resources/...        — SkillClaw-managed
SKILL_CLASS_UNKNOWN = "unknown"


def classify_skill_uri(uri: str) -> str:
    """Classify a viking:// skill URI as personal, team, or unknown.

    Personal  = the caller's private ``viking://user/<you>/skills/`` space
                (managed by the Curator).
    Team      = the shared ``viking://resources/skills/`` publish prefix and
                every SkillClaw group prefix (managed by SkillClaw).
    """
    if not uri:
        return SKILL_CLASS_UNKNOWN
    personal = personal_skill_prefix()
    if personal and uri.startswith(personal):
        return SKILL_CLASS_PERSONAL
    if _matching_skill_prefix(uri) is not None:
        return SKILL_CLASS_TEAM
    return SKILL_CLASS_UNKNOWN


_SOURCE_ID = "openviking"
# File extensions stored as decoded text in SkillBundle.files.  Anything
# else stays as bytes so binary assets survive round-tripping.
_TEXT_SUFFIXES = (
    ".md", ".txt", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".sh", ".html", ".css", ".toml",
)
# Cap individual file fetches to keep a misbehaving server from
# stalling installs.  Skill files are normally tiny; anything past this
# is suspicious.
_MAX_FILE_BYTES = 5 * 1024 * 1024
# Cap recursion depth so an accidental cycle in resource listings can't
# wedge the install command.
_MAX_TREE_DEPTH = 8


def _all_skill_prefixes() -> List[str]:
    """All viking:// prefixes we treat as skill roots.

    Hermes-uploaded skills live under ``viking://resources/skills/`` and
    SkillClaw evolve_server writes to one prefix per group_id.
    """
    prefixes = [_VIKING_SKILL_PREFIX]
    for gid in _skillclaw_group_ids():
        prefixes.append(_skillclaw_skill_prefix(gid))
    return prefixes


def _matching_skill_prefix(uri: str) -> Optional[str]:
    """Return the configured skill prefix that ``uri`` belongs to, or None."""
    if not uri:
        return None
    for prefix in _all_skill_prefixes():
        if uri.startswith(prefix):
            return prefix
    return None


def _is_viking_skill_identifier(identifier: str) -> bool:
    """Return True for IDs this source owns."""
    if not identifier:
        return False
    if identifier.startswith(f"{_SOURCE_ID}:"):
        return True
    return _matching_skill_prefix(identifier) is not None


def _normalize_identifier(identifier: str) -> str:
    """Accept ``openviking:foo`` shorthand and full ``viking://`` URIs."""
    if identifier.startswith(f"{_SOURCE_ID}:"):
        rest = identifier[len(_SOURCE_ID) + 1 :].lstrip("/")
        return f"{_VIKING_SKILL_PREFIX}{rest}"
    # Trim a trailing slash so ``viking://resources/skills/foo/`` and
    # ``viking://resources/skills/foo`` resolve identically.
    return identifier.rstrip("/")


def _slug_from_uri(uri: str) -> str:
    """``viking://resources/.../skills/flight-search/`` -> ``flight-search``."""
    prefix = _matching_skill_prefix(uri)
    if not prefix:
        return ""
    rest = uri[len(prefix) :].rstrip("/")
    return rest.split("/")[0] if rest else ""


class OpenVikingSkillSource(SkillSource):
    """Pulls skill bundles from a team's OpenViking shared resources space."""

    def __init__(self, client: Any = None) -> None:
        # Lazily build the OpenViking HTTP client; if the plugin isn't
        # importable or env vars aren't set, this source becomes a no-op
        # rather than crashing the whole CLI.
        self._client = client
        if self._client is None:
            self._client = self._build_client()

    @staticmethod
    def _build_client() -> Any:
        endpoint = os.environ.get("OPENVIKING_ENDPOINT")
        if not endpoint:
            return None
        try:
            from plugins.memory.openviking import _VikingClient  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("OpenViking client unavailable: %s", exc)
            return None
        try:
            # Resources are account-scoped, not user-scoped — using the
            # caller's regular OPENVIKING_USER is fine; the team
            # namespace split (__team__) only matters for memory writes.
            return _VikingClient(
                endpoint=endpoint,
                api_key=os.environ.get("OPENVIKING_API_KEY", ""),
                account=os.environ.get("OPENVIKING_ACCOUNT", "default"),
                user=os.environ.get("OPENVIKING_USER", ""),
                agent=os.environ.get("OPENVIKING_AGENT", "hermes"),
            )
        except Exception as exc:
            logger.debug("Failed to construct OpenViking client: %s", exc)
            return None

    # ------------------------------------------------------------------
    # SkillSource ABC
    # ------------------------------------------------------------------

    def source_id(self) -> str:
        return _SOURCE_ID

    def trust_level_for(self, identifier: str) -> str:
        # Team-published skills sit between ``community`` and ``trusted``:
        # the team account vouched for them but they didn't go through
        # Hermes' bundled review.  ``team`` is recognised in the hub UI.
        return "team"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        """List published skills under all configured skill prefixes.

        Filters by ``query`` substring against the slug (case-insensitive).
        Uses viking_browse (``/api/v1/content/list``) rather than memory
        search because resources don't get embedded into the memory index.
        """
        if self._client is None:
            return []
        q = (query or "").strip().lower()
        results: List[SkillMeta] = []
        seen_names: set[str] = set()
        for prefix in _all_skill_prefixes():
            try:
                entries = self._list_dir(prefix)
            except Exception as exc:
                logger.debug("OpenViking skill listing failed for %s: %s", prefix, exc)
                continue
            for entry in entries:
                uri = entry.get("uri") or ""
                if not uri.startswith(prefix):
                    continue
                name = _slug_from_uri(uri)
                if not name or not entry.get("is_dir", False):
                    continue
                if q and q not in name.lower():
                    continue
                # The hermes-uploaded prefix wins on name collision so a
                # tenant can pin a hand-curated version over the
                # cloud-evolved one.
                if name in seen_names:
                    continue
                seen_names.add(name)
                description = self._read_description(uri)
                results.append(
                    SkillMeta(
                        name=name,
                        description=description,
                        source=_SOURCE_ID,
                        identifier=uri.rstrip("/"),
                        trust_level="team",
                        extra={"viking_uri": uri},
                    )
                )
                if len(results) >= max(1, limit):
                    return results
        return results

    def _resolve_skill_uri(self, uri: str) -> Optional[str]:
        """Locate the skill bundle directory for ``uri`` across prefixes.

        - Full URIs that already match a configured prefix are returned
          as-is.
        - Shorthand-derived URIs (always under the legacy hermes prefix)
          fall back to each SkillClaw group prefix until SKILL.md is
          found.  Returns None when no prefix has the skill.
        """
        if _matching_skill_prefix(uri) and uri != _VIKING_SKILL_PREFIX.rstrip("/"):
            # Direct hit: user picked the URI explicitly (e.g. from
            # search() output).  Trust it without probing.
            if uri.startswith(_VIKING_SKILL_PREFIX):
                # Legacy prefix: probe SKILL.md so we can fall back to
                # SkillClaw paths if the hermes-uploaded copy was never
                # published.
                if self._read_file(f"{uri}/SKILL.md") is not None:
                    return uri
            else:
                return uri
        name = _slug_from_uri(uri) or uri.rsplit("/", 1)[-1]
        if not name:
            return None
        for prefix in _all_skill_prefixes():
            candidate = f"{prefix}{name}"
            if self._read_file(f"{candidate}/SKILL.md") is not None:
                return candidate
        return None

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        if self._client is None or not _is_viking_skill_identifier(identifier):
            return None
        uri = _normalize_identifier(identifier)
        resolved = self._resolve_skill_uri(uri)
        if not resolved:
            return None
        name = _slug_from_uri(resolved)
        if not name:
            return None
        description = self._read_description(resolved)
        if description is None:
            return None
        return SkillMeta(
            name=name,
            description=description,
            source=_SOURCE_ID,
            identifier=resolved,
            trust_level="team",
        )

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        if self._client is None or not _is_viking_skill_identifier(identifier):
            return None
        uri = _normalize_identifier(identifier)
        resolved = self._resolve_skill_uri(uri)
        if not resolved:
            return None
        name = _slug_from_uri(resolved)
        if not name:
            return None
        try:
            files = self._walk_tree(resolved)
        except Exception as exc:
            logger.debug("OpenViking fetch failed for %s: %s", identifier, exc)
            return None
        if not files:
            return None
        return SkillBundle(
            name=name,
            files=files,
            source=_SOURCE_ID,
            identifier=resolved,
            trust_level="team",
            metadata={
                "viking_uri": resolved,
                # Evolution hook: the cloud-side analyzer correlates
                # client install events + later usage_signal records
                # keyed on this channel to (a) detect regressions
                # impacting multiple tenants, (b) propose new skills
                # synthesized from shared workflow patterns.
                "usage_signal": {
                    "channel": f"{resolved}/usage",
                    "events": ["install", "invoke", "error", "feedback"],
                },
            },
        )

    # ------------------------------------------------------------------
    # OpenViking helpers
    # ------------------------------------------------------------------

    def _list_dir(self, uri: str) -> List[Dict[str, Any]]:
        """List immediate children of a viking://resources/... URI.

        Uses the real OpenViking filesystem endpoint ``GET /api/v1/fs/ls``.
        Entries are normalized to ``{"uri": ..., "is_dir": bool, "size": int}``
        so the rest of this module is agnostic to OpenViking's field names
        (it returns ``isDir`` and a flat ``result`` list).
        """
        resp = self._client.get(
            "/api/v1/fs/ls",
            params={"uri": uri.rstrip("/") + "/"},
        )
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

    def _read_description(self, base_uri: str) -> Optional[str]:
        """Fetch SKILL.md and pull ``description:`` out of the frontmatter.

        Returns an empty string if SKILL.md is present but has no
        description (so the caller can still distinguish "skill exists"
        from "skill missing"); returns None on read failure.
        """
        try:
            body = self._read_file(f"{base_uri.rstrip('/')}/SKILL.md")
        except Exception as exc:
            logger.debug("Failed to read SKILL.md for %s: %s", base_uri, exc)
            return None
        if body is None:
            return None
        return _extract_description(body)

    def _read_file(self, uri: str) -> Optional[str]:
        try:
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
        except Exception as exc:
            # Missing files (e.g. an empty-shell skill directory with no
            # SKILL.md) surface as RuntimeError("NOT_FOUND: ...") from the
            # client.  Treat any read failure as "absent" so callers can
            # skip the skill cleanly instead of crashing the whole sync.
            logger.debug("OpenViking read failed for %s: %s", uri, exc)
            return None
        result = resp.get("result")
        # OpenViking content/read returns the body as a plain string in
        # ``result``; older shapes nested it under result.content.
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            content = result.get("content") or result.get("text")
            if isinstance(content, str):
                return content
            if isinstance(content, (bytes, bytearray)):
                try:
                    return content.decode("utf-8")
                except UnicodeDecodeError:
                    return None
        return None

    def _walk_tree(self, root_uri: str) -> Dict[str, Any]:
        """Recursively walk a resource tree and return relative_path -> content.

        Text files (per ``_TEXT_SUFFIXES``) are decoded to str; everything
        else is left as bytes.  The root prefix is stripped so the bundle
        files map matches what the local skills/<name>/ tree looks like.
        """
        files: Dict[str, Any] = {}
        # (uri, depth) frontier
        frontier: List[tuple] = [(root_uri.rstrip("/"), 0)]
        while frontier:
            current, depth = frontier.pop()
            if depth > _MAX_TREE_DEPTH:
                logger.debug("OpenViking tree too deep at %s", current)
                continue
            try:
                entries = self._list_dir(current)
            except Exception as exc:
                logger.debug("list_dir(%s) failed: %s", current, exc)
                continue
            for entry in entries:
                child_uri = entry.get("uri") or ""
                if not child_uri.startswith(root_uri):
                    continue
                rel = child_uri[len(root_uri) :].lstrip("/")
                if not rel:
                    continue
                is_dir = bool(entry.get("is_dir"))
                if is_dir:
                    frontier.append((child_uri, depth + 1))
                    continue
                size = entry.get("size", 0) or 0
                if size > _MAX_FILE_BYTES:
                    logger.debug("Skipping oversized file %s (%d bytes)", child_uri, size)
                    continue
                content = self._read_file(child_uri)
                if content is None:
                    continue
                if rel.lower().endswith(_TEXT_SUFFIXES) or isinstance(content, str):
                    files[rel] = content
                else:
                    # _read_file returned non-str non-None: shouldn't
                    # happen given current API shape, but be defensive.
                    files[rel] = content
        return files


def _extract_description(skill_md: str) -> str:
    """Pull ``description:`` out of SKILL.md frontmatter.  Empty string if absent."""
    if not skill_md.startswith("---"):
        return ""
    end = skill_md.find("\n---", 3)
    if end < 0:
        return ""
    front = skill_md[3:end]
    for line in front.splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            value = stripped.split(":", 1)[1].strip()
            return value.strip("\"'")
    return ""

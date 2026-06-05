"""OpenViking memory plugin — full bidirectional MemoryProvider interface.

Context database by Volcengine (ByteDance) that organizes agent knowledge
into a filesystem hierarchy (viking:// URIs) with tiered context loading,
automatic memory extraction, and session management.

Original PR #3369 by Mibayy, rewritten to use the full OpenViking session
lifecycle instead of read-only search endpoints.

Config via environment variables (profile-scoped via each profile's .env):
  OPENVIKING_ENDPOINT  — Server URL (default: http://127.0.0.1:1933)
  OPENVIKING_API_KEY   — API key (required for authenticated servers)
  OPENVIKING_ACCOUNT   — Tenant account (default: default)
  OPENVIKING_USER      — Tenant user (default: default)
  OPENVIKING_AGENT   — Tenant agent (default: hermes)

Capabilities:
  - Automatic memory extraction on session commit (6 categories)
  - Tiered context: L0 (~100 tokens), L1 (~2k), L2 (full)
  - Semantic search with hierarchical directory retrieval
  - Filesystem-style browsing via viking:// URIs
  - Resource ingestion (URLs, docs, code)
"""

from __future__ import annotations

import atexit
import json
import logging
import mimetypes
import os
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)


def _current_os_user() -> str:
    """Return the current OS login name to use as the default OPENVIKING_USER.

    Always lowercased so the OpenViking user namespace doesn't fragment by
    case (e.g. "LiuYue" vs "liuyue" creating two disjoint memory spaces).
    Falls back to "default" if the user cannot be determined (e.g. headless
    env where neither $USER nor pwd lookup succeed).
    """
    try:
        import getpass
        name = getpass.getuser()
        if name:
            return name.lower()
    except Exception:
        pass
    name = os.environ.get("USER") or os.environ.get("USERNAME") or "default"
    return name.lower()

_DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
_TIMEOUT = 30.0
_REMOTE_RESOURCE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")

# Maps the viking_remember `category` enum to a viking:// subdirectory.
# Keep in sync with REMEMBER_SCHEMA.parameters.properties.category.enum.
_CATEGORY_SUBDIR_MAP = {
    "preference": "preferences",
    "entity": "entities",
    "event": "events",
    "case": "cases",
    "pattern": "patterns",
}
_DEFAULT_MEMORY_SUBDIR = "preferences"

# Maps the built-in memory tool's `target` ("user" vs "memory") to a subdir
# for on_memory_write mirroring. User profile facts → preferences; agent
# notes / observations → patterns. Anything unknown falls back to the default.
_MEMORY_WRITE_TARGET_SUBDIR_MAP = {
    "user": "preferences",
    "memory": "patterns",
}

# Content keywords that indicate a personal fact (L1) rather than general
# knowledge (L2).  When on_memory_write receives target="memory" but the
# content matches one of these patterns, we re-route to preferences/entities.
_PERSONAL_FACT_KEYWORDS = (
    "name is", "username", "叫", "名字是", "我的名字", "I am ", "I'm ",
    "my name", "我的偏好", "my preference", "我喜欢的", "我喜欢",
    "my environment", "我的环境", "我使用的", "I use ",
    "我的项目", "my project", "我的工作", "my role", "我的角色",
)


# ---------------------------------------------------------------------------
# Process-level atexit safety net — ensures pending sessions are committed
# even if shutdown_memory_provider is never called (e.g. gateway crash,
# SIGKILL, or exception in the session expiry watcher preventing shutdown).
# ---------------------------------------------------------------------------
_last_active_provider: Optional["OpenVikingMemoryProvider"] = None


def _atexit_commit_sessions():
    """Fire on_session_end for the last active provider on process exit."""
    global _last_active_provider
    provider = _last_active_provider
    if provider is None:
        return
    _last_active_provider = None
    try:
        provider.on_session_end([])
    except Exception:
        pass  # best-effort at shutdown time


atexit.register(_atexit_commit_sessions)


# ---------------------------------------------------------------------------
# HTTP helper — uses httpx to avoid requiring the openviking SDK
# ---------------------------------------------------------------------------

def _get_httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError:
        return None


class _VikingClient:
    """Thin HTTP client for the OpenViking REST API."""

    def __init__(self, endpoint: str, api_key: str = "",
                 account: str = "", user: str = "", agent: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or os.environ.get("OPENVIKING_ACCOUNT", "default")
        self._user = user or os.environ.get("OPENVIKING_USER") or _current_os_user()
        self._agent = agent or os.environ.get("OPENVIKING_AGENT", "hermes")
        self._httpx = _get_httpx()
        if self._httpx is None:
            raise ImportError("httpx is required for OpenViking: pip install httpx")

    def _headers(self) -> dict:
        # Always send tenant headers when account/user are configured.
        # OpenViking 0.3.x requires X-OpenViking-Account and X-OpenViking-User
        # for ROOT API key requests to tenant-scoped APIs — omitting them
        # causes INVALID_ARGUMENT errors even when account="default".
        # User-level keys can omit them (server derives tenancy from the key),
        # but ROOT keys must always include them explicitly.
        h = {
            "Content-Type": "application/json",
            "X-OpenViking-Agent": self._agent,
        }
        if self._account:
            h["X-OpenViking-Account"] = self._account
        if self._user:
            h["X-OpenViking-User"] = self._user
        if self._api_key:
            h["X-API-Key"] = self._api_key
            h["Authorization"] = "Bearer " + self._api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _multipart_headers(self) -> dict:
        headers = self._headers()
        headers.pop("Content-Type", None)
        return headers

    def _parse_response(self, resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            data = None

        if resp.status_code >= 400:
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    code = error.get("code", "HTTP_ERROR")
                    message = error.get("message", resp.text)
                    raise RuntimeError(f"{code}: {message}")
                if data.get("status") == "error":
                    raise RuntimeError(str(data))
            resp.raise_for_status()

        if isinstance(data, dict) and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code", "OPENVIKING_ERROR")
                message = error.get("message", "")
                raise RuntimeError(f"{code}: {message}")
            raise RuntimeError(str(data))

        if data is None:
            return {}
        return data

    def get(self, path: str, **kwargs) -> dict:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def post(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.post(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def upload_temp_file(self, file_path: Path) -> str:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as f:
            resp = self._httpx.post(
                self._url("/api/v1/resources/temp_upload"),
                files={"file": (file_path.name, f, mime_type)},
                headers=self._multipart_headers(),
                timeout=_TIMEOUT,
            )
        data = self._parse_response(resp)
        result = data.get("result", {})
        temp_file_id = result.get("temp_file_id", "")
        if not temp_file_id:
            raise RuntimeError("OpenViking temp upload did not return temp_file_id")
        return temp_file_id

    def health(self) -> bool:
        try:
            resp = self._httpx.get(
                self._url("/health"), headers=self._headers(), timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "viking_search",
    "description": (
        "Semantic search over the OpenViking knowledge base. "
        "Returns ranked results with viking:// URIs for deeper reading. "
        "Use mode='deep' for complex queries that need reasoning across "
        "multiple sources, 'fast' for simple lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "mode": {
                "type": "string", "enum": ["auto", "fast", "deep"],
                "description": "Search depth (default: auto).",
            },
            "scope": {
                "type": "string",
                "description": "Viking URI prefix to scope search (e.g. 'viking://resources/docs/').",
            },
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
            "scope": {
                "type": "string",
                "enum": ["all", "private", "shared"],
                "description": "Search scope: all=both spaces (default), private=personal only, shared=team only.",
            },
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "viking_read",
    "description": (
        "Read content at a viking:// URI. Three detail levels mapped to memory layers:\n"
        "  abstract — L0 Context Memory: ~100 token summary for quick reference\n"
        "  overview — L1 Stable Facts: ~2k token key points for preferences/facts\n"
        "  full — L2 Deep Knowledge: complete content for SOPs/workflows\n"
        "Start with abstract (L0) or overview (L1), only use full (L2) when you need details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "viking:// URI to read."},
            "level": {
                "type": "string", "enum": ["abstract", "overview", "full"],
                "description": "Detail level (default: overview).",
            },
        },
        "required": ["uri"],
    },
}

BROWSE_SCHEMA = {
    "name": "viking_browse",
    "description": (
        "Browse the OpenViking knowledge store like a filesystem.\n"
        "  list — show directory contents\n"
        "  tree — show hierarchy\n"
        "  stat — show metadata for a URI"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": ["tree", "list", "stat"],
                "description": "Browse action.",
            },
            "path": {
                "type": "string",
                "description": "Viking URI path (default: viking://). Examples: 'viking://resources/', 'viking://user/memories/'.",
            },
        },
        "required": ["action"],
    },
}

REMEMBER_SCHEMA = {
    "name": "viking_remember",
    "description": (
        "Explicitly store a fact or memory in the OpenViking knowledge base. "
        "Use for important information the agent should remember long-term. "
        "The system automatically categorizes and indexes the memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
            "category": {
                "type": "string",
                "enum": ["preference", "entity", "event", "case", "pattern"],
                "description": "Memory category (default: auto-detected).",
            },
            "layer": {
                "type": "string",
                "enum": ["auto", "L1", "L2"],
                "description": (
                    "Memory layer. L1=stable facts (preferences, environment), "
                    "L2=deep knowledge (SOPs, workflows). auto=system decides (default)."
                ),
            },
            "verified": {
                "type": "boolean",
                "description": (
                    "Whether this information has been verified. Set true when confirmed "
                    "by execution, user feedback, or explicit confirmation. Default: false."
                ),
            },
            "verification_type": {
                "type": "string",
                "enum": ["execution", "user_feedback", "explicit_confirmation", "auto_extracted"],
                "description": (
                    "How this information was verified. execution=confirmed by running code/command, "
                    "user_feedback=user confirmed it, explicit_confirmation=agent explicitly verified, "
                    "auto_extracted=extracted from session (default)."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["auto", "private", "shared"],
                "description": (
                    "Memory scope. auto=L0/L1→private, L2/L3→shared (default). "
                    "private=force write to personal space. shared=force write to team space."
                ),
            },
        },
        "required": ["content"],
    },
}

FORGET_SCHEMA = {
    "name": "viking_forget",
    "description": (
        "Archive or deprioritize a memory. Does NOT delete — moves to archive "
        "where it can be restored. Use when information is outdated, incorrect, "
        "or no longer relevant."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "viking:// URI of the memory to forget."},
            "mode": {
                "type": "string",
                "enum": ["archive", "deprioritize"],
                "description": (
                    "archive: move to _archived/ dir (removes from active search). "
                    "deprioritize: lower search weight (still searchable but ranked lower)."
                ),
            },
            "reason": {"type": "string", "description": "Why this memory is being forgotten."},
        },
        "required": ["uri"],
    },
}

FEEDBACK_SCHEMA = {
    "name": "viking_feedback",
    "description": (
        "Provide feedback on a shared memory's usefulness. Records whether a "
        "skill/SOP/workflow succeeded or failed in practice. The Cluster Curator "
        "uses this feedback to optimize shared knowledge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "viking:// URI of the memory to provide feedback on."},
            "outcome": {
                "type": "string",
                "enum": ["success", "failure", "partial"],
                "description": "Whether the memory was helpful.",
            },
            "note": {"type": "string", "description": "Explanation of the outcome (e.g., 'worked for Python project')."},
        },
        "required": ["uri", "outcome"],
    },
}

ADD_RESOURCE_SCHEMA = {
    "name": "viking_add_resource",
    "description": (
        "Add a remote URL or local file/directory to the OpenViking knowledge base. "
        "Remote resources must be public http(s), git, or ssh URLs. "
        "Local files are uploaded first using OpenViking temp_upload. "
        "The system automatically parses, indexes, and generates summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Remote URL or local file/directory path to add."},
            "reason": {
                "type": "string",
                "description": "Why this resource is relevant (improves search).",
            },
            "to": {
                "type": "string",
                "description": "Optional target viking:// URI for the resource.",
            },
            "parent": {
                "type": "string",
                "description": "Optional parent viking:// URI. Cannot be used with to.",
            },
            "instruction": {
                "type": "string",
                "description": "Optional processing instruction for semantic extraction.",
            },
            "wait": {
                "type": "boolean",
                "description": "Whether to wait for processing to complete.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds when wait is true.",
            },
        },
        "required": ["url"],
    },
}


def _zip_directory(dir_path: Path) -> Path:
    """Create a temporary zip file containing a directory tree."""
    root = dir_path.resolve()
    zip_path = Path(tempfile.gettempdir()) / f"openviking_upload_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in dir_path.rglob("*"):
            if file_path.is_symlink():
                continue
            if file_path.is_file():
                try:
                    file_path.resolve().relative_to(root)
                except ValueError:
                    continue
                arcname = str(file_path.relative_to(dir_path)).replace("\\", "/")
                zipf.write(file_path, arcname=arcname)
    return zip_path


def _is_windows_absolute_path(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    )


def _is_remote_resource_source(value: str) -> bool:
    return value.startswith(_REMOTE_RESOURCE_PREFIXES)


def _is_local_path_reference(value: str) -> bool:
    if not value or "\n" in value or "\r" in value:
        return False
    if _is_remote_resource_source(value):
        return False
    if _is_windows_absolute_path(value):
        return True
    return (
        value.startswith(("/", "./", "../", "~/", ".\\", "..\\", "~\\"))
        or "/" in value
        or "\\" in value
    )


def _path_from_file_uri(uri: str) -> Path | str:
    parsed = urlparse(uri)
    if parsed.netloc not in {"", "localhost"}:
        return f"Unsupported non-local file URI: {uri}"
    return Path(url2pathname(parsed.path)).expanduser()


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class OpenVikingMemoryProvider(MemoryProvider):
    """Full bidirectional memory via OpenViking context database."""

    def __init__(self):
        self._client: Optional[_VikingClient] = None
        self._endpoint = ""
        self._api_key = ""
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._shared_client: Optional[_VikingClient] = None
        self._team_user = "__team__"
        self._feedback_tracking = False

    @property
    def name(self) -> str:
        return "openviking"

    def is_available(self) -> bool:
        """Check if OpenViking endpoint is configured. No network calls."""
        return bool(os.environ.get("OPENVIKING_ENDPOINT"))

    def get_config_schema(self):
        return [
            {
                "key": "endpoint",
                "description": "OpenViking server URL",
                "required": True,
                "default": _DEFAULT_ENDPOINT,
                "env_var": "OPENVIKING_ENDPOINT",
            },
            {
                "key": "api_key",
                "description": "OpenViking API key (leave blank for local dev mode)",
                "secret": True,
                "env_var": "OPENVIKING_API_KEY",
            },
            {
                "key": "account",
                "description": "OpenViking tenant account ID ([default], used when local mode, OPENVIKING_API_KEY is empty)",
                "default": "default",
                "env_var": "OPENVIKING_ACCOUNT",
            },
            {
                "key": "user",
                "description": "OpenViking user ID within the account (defaults to the current OS user, used when local mode, OPENVIKING_API_KEY is empty)",
                "default": _current_os_user(),
                "env_var": "OPENVIKING_USER",
            },
            {
                "key": "agent",
                "description": "OpenViking agent ID within the account ([hermes], useful in multi-agent mode)",
                "default": "hermes",
                "env_var": "OPENVIKING_AGENT",
            },
            {
                "key": "team_user",
                "description": "OpenViking user ID for shared team space (default: __team__)",
                "default": "__team__",
                "env_var": "OPENVIKING_TEAM_USER",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._endpoint = os.environ.get("OPENVIKING_ENDPOINT", _DEFAULT_ENDPOINT)
        self._api_key = os.environ.get("OPENVIKING_API_KEY", "")
        self._account = os.environ.get("OPENVIKING_ACCOUNT", "default")
        self._user = os.environ.get("OPENVIKING_USER") or _current_os_user()
        self._agent = os.environ.get("OPENVIKING_AGENT", "hermes")
        self._session_id = session_id
        self._turn_count = 0

        try:
            self._client = _VikingClient(
                self._endpoint, self._api_key,
                account=self._account, user=self._user, agent=self._agent,
            )
            if not self._client.health():
                logger.warning("OpenViking server at %s is not reachable", self._endpoint)
                self._client = None
        except ImportError:
            logger.warning("httpx not installed — OpenViking plugin disabled")
            self._client = None

        # Shared client for team L2/L3 knowledge
        self._team_user = os.environ.get("OPENVIKING_TEAM_USER", "__team__")
        self._feedback_tracking = os.environ.get("OPENVIKING_FEEDBACK_TRACKING", "").lower() in ("1", "true", "yes")
        try:
            self._shared_client = _VikingClient(
                self._endpoint, self._api_key,
                account=self._account, user=self._team_user, agent=self._agent,
            )
            if not self._shared_client.health():
                logger.warning("OpenViking shared client at %s is not reachable", self._endpoint)
                self._shared_client = None
        except Exception:
            self._shared_client = None

        # Register as the last active provider for atexit safety net
        global _last_active_provider
        _last_active_provider = self

    def _user_needs_onboarding(self) -> bool:
        """Return True when the active user has no personal profile yet.

        A brand-new teammate (fresh OPENVIKING_USER) starts with an empty
        private namespace — no ``memories/profile.md``.  We use that as the
        cold-start signal to trigger a one-time onboarding interview.  The
        stat probe raises on NOT_FOUND, which is exactly the new-user case,
        so a raised error maps to "needs onboarding".
        """
        if not self._client:
            return False
        profile_uri = f"viking://user/{self._user}/memories/profile.md"
        try:
            resp = self._client.get("/api/v1/fs/stat", params={"uri": profile_uri})
        except Exception:
            return True  # NOT_FOUND surfaces as RuntimeError -> needs onboarding
        result = self._unwrap_result(resp)
        # A clean stat response means the profile exists already.
        return not bool(result)

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        try:
            resp = self._client.get("/api/v1/fs/ls", params={"uri": "viking://"})
            result = resp.get("result", [])
            children = len(result) if isinstance(result, list) else 0
            if children == 0:
                return ""

            shared_section = ""
            if self._shared_client:
                shared_section = (
                    "\n\n## Shared Team Knowledge\n"
                    f"Team space available (user: {self._team_user}). "
                    "L2/L3 memories are automatically written to the shared team space. "
                    "Use `scope=shared` in viking_remember to force writing to team space, "
                    "or `scope=private` to keep it personal. "
                    "Use `scope=shared` in viking_search to search only team knowledge.\n"
                    "After using a shared memory, provide feedback with `viking_feedback` "
                    "to help the Cluster Curator optimize shared knowledge quality."
                )

            onboarding_section = ""
            try:
                if self._user_needs_onboarding():
                    onboarding_section = (
                        "\n\n## First-Run Onboarding (IMPORTANT — new teammate detected)\n"
                        f"The current user (`{self._user}`) has no personal profile yet "
                        "(empty private memory). At the very start of this session, before "
                        "diving into their first task, run a brief, friendly cold-start interview:\n"
                        "1. FIRST call `viking_search` with `scope=shared` for the team overview "
                        "(try queries like \"团队介绍\" / \"team overview\" / \"start here\"). "
                        "If results exist, give a 2-3 sentence summary of what this team does "
                        "and what shared knowledge is available. If the team space is empty, skip this.\n"
                        "2. THEN ask the user 2-4 light questions to get to know them: their name, "
                        "their role/team, what they're working on, and any working preferences "
                        "(language, tools, style). Ask conversationally, not as a rigid form — "
                        "one short message, let them answer freely.\n"
                        "3. AFTER they reply, persist what you learned with `viking_remember` "
                        "into their PRIVATE space (`scope=private`): identity facts (name, role, "
                        "team) as `category=entity`, working preferences (language, tools, style) "
                        "as `category=preference`. The session will also auto-extract a "
                        "`profile.md` on exit, so this onboarding only happens once.\n"
                        "4. Keep it lightweight: if the user clearly just wants to start a task, "
                        "do NOT block them — ask only the most essential questions (or just their "
                        "name/role) and proceed. Never repeat onboarding once a profile exists.\n"
                    )
            except Exception as e:
                logger.debug("OpenViking onboarding check failed: %s", e)

            identity_section = (
                "## Active User Identity (ground truth)\n"
                f"You are talking to OpenViking user `{self._user}`. This is the "
                "authoritative identity for THIS session, set from the environment — "
                "NOT something to be inferred from search results.\n"
                f"- This user's personal memory lives under `viking://user/{self._user}/`.\n"
                f"- The shared team space is `viking://user/{self._team_user}/`.\n"
                "- When the user asks about themselves (\"who am I\", \"我是谁\", their "
                "name/role/preferences), answer ONLY from this user's OWN private space "
                "(use `viking_search` with `scope=private`, or read "
                f"`viking://user/{self._user}/memories/profile.md`). NEVER attribute "
                "another teammate's profile, name, or facts to the current user, even if "
                "such a result ranks highly in a broad search.\n"
                "- If this user's private space has no identity info yet, say so and run "
                "the onboarding interview below rather than guessing from team/other-user data.\n\n"
            )

            return (
                "# OpenViking Knowledge Base (Layered Memory)\n"
                f"Active. Endpoint: {self._endpoint}\n\n"
                + identity_section
                + "## Memory Layers\n"
                "- **L0 Context Memory** [PRIVATE] (~100 tokens): Auto-injected each turn via prefetch. "
                "Quick abstracts of relevant knowledge. Use `viking_read level=abstract`.\n"
                "- **L1 Stable Facts** [PRIVATE] (~2k tokens): User preferences, environment info, project facts. "
                "Use `viking_read level=overview`.\n"
                "- **L2 Deep Knowledge** [SHARED] (full): SOPs, workflows, technical detail, code patterns. "
                "Use `viking_read level=full`.\n"
                "- **L3 Session Archive** [SHARED]: Cross-session search. Sessions auto-commit on exit, "
                "extracting memories into 6 categories (profile, preferences, entities, events, cases, patterns).\n\n"
                "## Tools\n"
                "- `viking_search` — Semantic search across all layers (supports scope: all/private/shared)\n"
                "- `viking_read` — Read at a URI with layer-aware detail levels\n"
                "- `viking_browse` — Filesystem-style navigation\n"
                "- `viking_remember` — Store a fact (specify layer: L1 for stable facts, L2 for deep knowledge; "
                "scope: auto/private/shared)\n"
                "- `viking_forget` — Archive or deprioritize a memory\n"
                "- `viking_feedback` — Provide feedback on a shared memory's usefulness (success/failure/partial)\n"
                "- `viking_add_resource` — Ingest URLs/docs into the knowledge base"
                + shared_section
                + onboarding_section
            )
        except Exception as e:
            logger.warning("OpenViking system_prompt_block failed: %s", e)
            tools_list = "viking_search, viking_read, viking_browse, viking_remember, viking_forget, viking_feedback, viking_add_resource"
            return (
                "# OpenViking Knowledge Base (Layered Memory)\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "L0 [PRIVATE]: abstract | L1 [PRIVATE]: overview (stable facts) | "
                "L2 [SHARED]: full (deep knowledge) | L3 [SHARED]: session archive.\n"
                f"Use {tools_list}."
            )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return prefetched results from the background thread."""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## OpenViking Context\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background search to pre-load L0+L1 context."""
        if not self._client or not query:
            return

        def _run():
            try:
                client = _VikingClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                # Scope prefetch to the active user's private space + the
                # team space (+ shared resources).  A ROOT key ignores the
                # user header, so without an explicit per-namespace
                # target_uri this would surface other teammates' private
                # memories as "your" context.  Merge hits, best score wins.
                merged: Dict[str, Dict[str, dict]] = {}
                for target_uri in self._scoped_target_uris("all"):
                    try:
                        resp = client.post("/api/v1/search/find", {
                            "query": query,
                            "top_k": 5,
                            "target_uri": target_uri,
                        })
                    except Exception as exc:
                        logger.debug("OpenViking prefetch failed for %s: %s", target_uri, exc)
                        continue
                    sub = resp.get("result", {}) or {}
                    for ctx_type in ("memories", "resources"):
                        bucket = merged.setdefault(ctx_type, {})
                        for item in sub.get(ctx_type, []) or []:
                            uri = item.get("uri", "")
                            if not uri:
                                continue
                            prev = bucket.get(uri)
                            if prev is None or (item.get("score") or 0.0) > (prev.get("score") or 0.0):
                                bucket[uri] = item
                result = {ctx_type: list(items.values()) for ctx_type, items in merged.items()}
                l0_parts = []
                top_uri = ""
                for ctx_type in ("memories", "resources"):
                    items = sorted(
                        result.get(ctx_type, []),
                        key=lambda it: it.get("score") or 0.0,
                        reverse=True,
                    )
                    for item in items[:3]:
                        uri = item.get("uri", "")
                        abstract = item.get("abstract", "")
                        score = item.get("score", 0)
                        if abstract:
                            l0_parts.append(f"- [L0 {score:.2f}] {abstract} ({uri})")
                        if not top_uri and uri:
                            top_uri = uri
                l1_text = ""
                if top_uri:
                    try:
                        ov_resp = client.get("/api/v1/content/overview", params={"uri": top_uri})
                        ov_result = self._unwrap_result(ov_resp)
                        if isinstance(ov_result, str):
                            l1_text = ov_result[:2000]
                        elif isinstance(ov_result, dict):
                            l1_text = (ov_result.get("content", "") or ov_result.get("text", ""))[:2000]
                    except Exception:
                        pass
                parts = []
                if l0_parts:
                    parts.append("### L0 Context Memory (abstracts)")
                    parts.extend(l0_parts)
                if l1_text:
                    parts.append(f"### L1 Stable Facts (overview of top match: {top_uri})")
                    parts.append(l1_text)
                if parts:
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(parts)
            except Exception as e:
                logger.debug("OpenViking prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="openviking-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Record the conversation turn in OpenViking's session (non-blocking)."""
        if not self._client:
            return

        self._turn_count += 1

        def _sync():
            try:
                client = _VikingClient(
                    self._endpoint, self._api_key,
                    account=self._account, user=self._user, agent=self._agent,
                )
                sid = self._session_id

                # Add user message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "user",
                    "content": user_content[:4000],  # trim very long messages
                })
                # Add assistant message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "assistant",
                    "content": assistant_content[:4000],
                })
            except Exception as e:
                logger.debug("OpenViking sync_turn failed: %s", e)

        # Wait for any previous sync to finish before starting a new one
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="openviking-sync"
        )
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Commit the session to trigger memory extraction.

        OpenViking automatically extracts 6 categories of memories:
        profile, preferences, entities, events, cases, and patterns.
        """
        if not self._client:
            return

        # Wait for any pending sync to finish first — do this before the
        # turn_count check so the last turn's messages are flushed even if
        # the count hasn't been incremented yet.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

        if self._turn_count == 0:
            return

        try:
            self._client.post(f"/api/v1/sessions/{self._session_id}/commit")
            logger.info("OpenViking session %s committed (%d turns)", self._session_id, self._turn_count)
        except Exception as e:
            logger.warning("OpenViking session commit failed: %s", e)

        # Commit shared session (if shared client exists)
        if self._shared_client:
            try:
                shared_session_id = f"{self._session_id}_shared"
                self._shared_client.post(f"/api/v1/sessions/{shared_session_id}/commit")
                logger.info("OpenViking shared session %s committed", shared_session_id)
            except Exception as e:
                logger.debug("OpenViking shared session commit failed: %s", e)

    def _build_memory_uri(self, subdir: str, scope: str = "private") -> str:
        """Build a viking:// memory URI under the appropriate user/subdir.
        scope: "private" uses personal user, "shared" uses team user.
        """
        slug = uuid.uuid4().hex[:12]
        user = self._team_user if scope == "shared" else self._user
        return f"viking://user/{user}/memories/{subdir}/mem_{slug}.md"

    def _client_for_scope(self, scope: str) -> Optional[_VikingClient]:
        """Return the appropriate client for the given scope."""
        if scope == "shared" and self._shared_client:
            return self._shared_client
        return self._client

    @staticmethod
    def _scope_for_layer(layer: str) -> str:
        """Determine scope (private/shared) from memory layer. L0/L1 → private, L2/L3 → shared."""
        if layer in ("L2", "L3"):
            return "shared"
        return "private"

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to OpenViking via content/write."""
        if not self._client or action != "add" or not content:
            return

        subdir = _MEMORY_WRITE_TARGET_SUBDIR_MAP.get(target, _DEFAULT_MEMORY_SUBDIR)
        # Re-route personal facts to L1 private, regardless of which subdir
        # they would otherwise land in.  The team (shared) space must never
        # auto-absorb first-person facts like "my name is", "我叫…", "I use
        # Python 3.11", because every tenant on the same account would then
        # be polluted with one user's identity.
        content_lower = content.lower()
        is_personal_fact = any(kw in content_lower for kw in _PERSONAL_FACT_KEYWORDS)
        if is_personal_fact and subdir not in ("preferences", "entities"):
            subdir = "preferences"
        # Infer verification metadata from write origin
        verification_type = "auto_extracted"
        if metadata:
            write_origin = metadata.get("write_origin", "")
            exec_ctx = metadata.get("execution_context", "")
            if write_origin == "background_review":
                verification_type = "auto_extracted"
            elif write_origin == "assistant_tool" and exec_ctx == "foreground":
                verification_type = "explicit_confirmation"
            elif write_origin == "user_direct":
                verification_type = "user_feedback"
        effective_layer = "L1" if subdir in ("preferences", "entities") else "L2"
        write_scope = self._scope_for_layer(effective_layer)
        front_matter = self._build_front_matter(
            verified=(verification_type != "auto_extracted"),
            verification_type=verification_type,
            layer=effective_layer,
            created_by=self._user,
            scope=write_scope,
        )
        full_content = front_matter + content

        def _write():
            try:
                write_client = self._client_for_scope(write_scope)
                if not write_client:
                    write_client = self._client
                if not write_client:
                    return
                write_uri = self._build_memory_uri(subdir, scope=write_scope)
                write_client.post("/api/v1/content/write", {
                    "uri": write_uri,
                    "content": full_content,
                    "mode": "create",
                })
            except Exception as e:
                logger.debug("OpenViking memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="openviking-memwrite")
        t.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA, FEEDBACK_SCHEMA, ADD_RESOURCE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._client:
            return tool_error("OpenViking server not connected")

        try:
            if tool_name == "viking_search":
                return self._tool_search(args)
            elif tool_name == "viking_read":
                return self._tool_read(args)
            elif tool_name == "viking_browse":
                return self._tool_browse(args)
            elif tool_name == "viking_remember":
                return self._tool_remember(args)
            elif tool_name == "viking_forget":
                return self._tool_forget(args)
            elif tool_name == "viking_feedback":
                return self._tool_feedback(args)
            elif tool_name == "viking_add_resource":
                return self._tool_add_resource(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return tool_error(str(e))

    def shutdown(self) -> None:
        # Wait for background threads to finish
        for t in (self._sync_thread, self._prefetch_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Clear atexit reference so it doesn't double-commit
        global _last_active_provider
        if _last_active_provider is self:
            _last_active_provider = None

    # -- Tool implementations ------------------------------------------------

    @staticmethod
    def _unwrap_result(resp: Any) -> Any:
        """Return OpenViking payload body regardless of wrapped/unwrapped shape."""
        if isinstance(resp, dict) and "result" in resp:
            return resp.get("result")
        return resp

    @staticmethod
    def _normalize_summary_uri(uri: str) -> str:
        """Map pseudo summary files to their parent directory URI for L0/L1 reads."""
        if not uri:
            return uri
        for suffix in ("/.abstract.md", "/.overview.md", "/.read.md", "/.full.md"):
            if uri.endswith(suffix):
                return uri[: -len(suffix)] or "viking://"
        return uri

    def _is_directory_uri(self, uri: str) -> bool | None:
        """Probe fs/stat to decide if a URI is a directory.

        Returns True/False when the server answers cleanly, and None when the
        probe itself fails (network error, unexpected shape). Callers should
        treat None as "unknown" and fall back to the exception-based path.
        """
        try:
            resp = self._client.get("/api/v1/fs/stat", params={"uri": uri})
        except Exception:
            return None
        result = self._unwrap_result(resp)
        if isinstance(result, dict):
            if "isDir" in result:
                return bool(result.get("isDir"))
            if "is_dir" in result:
                return bool(result.get("is_dir"))
            if result.get("type") == "dir":
                return True
            if result.get("type") == "file":
                return False
        return None

    def _scoped_target_uris(self, scope: str) -> List[str]:
        """Return the viking:// namespaces a search may touch for ``scope``.

        With a ROOT API key, OpenViking's /search/find ignores the
        X-OpenViking-User header and ranks across *every* user's private
        memory — so an explicit ``target_uri`` per namespace is the only way
        to keep one teammate from reading another's private memories.

        Shared resources are deliberately limited to the SkillClaw-evolved
        *skills* trees.  The raw ``sessions/`` logs under skillclaw also live
        in resources/ but contain verbatim personal conversations (names,
        roles, private context), so surfacing them to other teammates would
        re-introduce the very cross-user leak we are guarding against.  Only
        the evolved skills — sanitised, reusable knowledge — are shareable.
        """
        user_uri = f"viking://user/{self._user}/"
        team_uri = f"viking://user/{self._team_user}/"
        # Shared skill trees only — NOT raw session logs (see docstring).
        # Skills are published & versioned by SkillClaw's evolution pipeline
        # under resources/<root_prefix>/<group_id>/skills/, mirroring
        # SKILLCLAW_VIKING_ROOT_PREFIX / SKILLCLAW_VIKING_GROUP_IDS so
        # non-"team-a" deployments still work.
        shared_resources: List[str] = []
        root_prefix = os.environ.get("SKILLCLAW_VIKING_ROOT_PREFIX", "skillclaw").strip("/")
        group_ids = os.environ.get("SKILLCLAW_VIKING_GROUP_IDS", "").strip()
        for gid in (g.strip() for g in group_ids.split(",")):
            if gid:
                shared_resources.append(
                    f"viking://resources/{root_prefix}/{gid}/skills/"
                )
        if scope == "private":
            return [user_uri] + shared_resources
        if scope == "shared":
            return [team_uri] + shared_resources
        # "all" (default): personal + team + shared skills
        return [user_uri, team_uri] + shared_resources

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")

        scope = args.get("scope", "all")

        # An explicit viking:// URI in ``scope`` is treated as a literal
        # target prefix (advanced/manual use); the enum values
        # all/private/shared expand to the namespace allow-list instead.
        raw_scope = str(args.get("scope") or "").strip()
        if raw_scope.startswith("viking://"):
            target_uris = [raw_scope]
        else:
            target_uris = self._scoped_target_uris(scope if scope in ("all", "private", "shared") else "all")

        mode = args.get("mode", "auto")
        top_k = args.get("limit")

        # Query each allowed namespace separately and merge — a single
        # unscoped query would leak other users' private memories under a
        # ROOT key.  De-duplicate merged hits by URI, keeping the best score.
        merged: Dict[str, Dict[str, list]] = {}
        for target_uri in target_uris:
            payload: Dict[str, Any] = {"query": query, "target_uri": target_uri}
            if mode != "auto":
                payload["mode"] = mode
            if top_k:
                payload["top_k"] = top_k
            try:
                resp = self._client.post("/api/v1/search/find", payload)
            except Exception as exc:
                logger.debug("OpenViking search failed for %s: %s", target_uri, exc)
                continue
            sub = resp.get("result", {}) or {}
            for ctx_type in ("memories", "resources", "skills"):
                bucket = merged.setdefault(ctx_type, {})
                for item in sub.get(ctx_type, []) or []:
                    uri = item.get("uri", "")
                    if not uri:
                        continue
                    prev = bucket.get(uri)
                    if prev is None or (item.get("score") or 0.0) > (prev.get("score") or 0.0):
                        bucket[uri] = item
        result = {ctx_type: list(items.values()) for ctx_type, items in merged.items()}


        # Format results for the model — keep it concise, label with memory layer
        scored_entries = []
        for ctx_type in ("memories", "resources", "skills"):
            items = result.get(ctx_type, [])
            for item in items:
                raw_score = item.get("score")
                sort_score = raw_score if raw_score is not None else 0.0
                uri = item.get("uri", "")
                # Determine layer from URI path
                layer_label = "L1"
                if "/_archived/" in uri:
                    layer_label = "L3"
                elif ctx_type == "skills" or "/patterns/" in uri or "/cases/" in uri:
                    layer_label = "L2"
                elif "/preferences/" in uri or "/entities/" in uri:
                    layer_label = "L1"
                elif ctx_type == "resources":
                    layer_label = "L1"
                entry = {
                    "uri": uri,
                    "type": ctx_type.rstrip("s"),
                    "layer": layer_label,
                    "score": round(raw_score, 3) if raw_score is not None else 0.0,
                    "abstract": item.get("abstract", ""),
                }
                # Add verification tag from abstract content if present
                abstract_text = item.get("abstract", "")
                if "verified: true" in abstract_text:
                    entry["verification"] = "verified"
                elif "verified: false" in abstract_text:
                    entry["verification"] = "unverified"
                if item.get("relations"):
                    entry["related"] = [r.get("uri") for r in item["relations"][:3]]
                scored_entries.append((sort_score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        # Scope is already enforced at query time via per-namespace
        # target_uri (see _scoped_target_uris), so no post-filtering is
        # needed here.
        # Filter out archived entries (redirect markers), deprioritize low-priority entries
        active_entries = []
        deprioritized_entries = []
        for sort_score, entry in scored_entries:
            uri = entry.get("uri", "")
            abstract = entry.get("abstract", "")
            # Team space and the shared resources/ tree are both team-wide;
            # only viking://user/<other>/ would be private (and is already
            # excluded by query-time scoping).
            is_shared = (
                f"/user/{self._team_user}/" in uri
                or uri.startswith("viking://resources/")
            )
            scope_label = "SHARED" if is_shared else "PRIVATE"
            entry["scope"] = scope_label
            # Skip archived entries (redirect markers or _archived/ URIs)
            if "/_archived/" in uri or abstract.startswith("-> "):
                continue
            # Check for deprioritized marker
            if "[DEPRIORITIZED]" in abstract or "priority: low" in abstract:
                deprioritized_entries.append(entry)
            else:
                active_entries.append(entry)
        formatted = active_entries + deprioritized_entries

        return json.dumps({
            "results": formatted,
            "total": result.get("total", len(formatted)),
        }, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        level = args.get("level", "overview")

        summary_level = level in {"abstract", "overview"}
        # OpenViking expects directory URIs for pseudo summary files
        # (e.g. viking://user/hermes/.overview.md).
        resolved_uri = self._normalize_summary_uri(uri) if summary_level else uri
        used_fallback = False

        # abstract/overview endpoints are directory-only on OpenViking
        # (v0.3.x returns 500/412 for file URIs). When the caller asks for a
        # summary level on a non-pseudo URI, probe fs/stat first and route
        # file URIs straight to /content/read instead of eating a failing
        # round-trip. The pseudo-URI path already points at a directory, so
        # skip the probe there.
        if summary_level and resolved_uri == uri:
            is_dir = self._is_directory_uri(uri)
            if is_dir is False:
                resolved_uri = uri
                used_fallback = True

        # Map our level names to OpenViking GET endpoints.
        endpoint = "/api/v1/content/read"
        if not used_fallback:
            if level == "abstract":
                endpoint = "/api/v1/content/abstract"
            elif level == "overview":
                endpoint = "/api/v1/content/overview"

        try:
            resp = self._client.get(endpoint, params={"uri": resolved_uri})
        except Exception:
            # OpenViking may return HTTP 500 for abstract/overview reads on normal
            # file URIs (mem_*.md). For those, gracefully fallback to full read.
            if not summary_level or resolved_uri != uri or used_fallback:
                raise
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
            used_fallback = True

        result = self._unwrap_result(resp)
        # Content endpoints may return either plain strings or objects.
        if isinstance(result, str):
            content = result
        elif isinstance(result, dict):
            content = result.get("content", "") or result.get("text", "")
        else:
            content = ""

        # Truncate long content to avoid flooding context.
        max_len = 8000
        if level == "overview":
            max_len = 4000
        elif level == "abstract":
            max_len = 1200

        if len(content) > max_len:
            content = content[:max_len] + "\n\n[... truncated, use a more specific URI or full level]"

        payload = {
            "uri": uri,
            "resolved_uri": resolved_uri,
            "level": level,
            "content": content,
        }
        if used_fallback:
            payload["fallback"] = "content/read"

        return json.dumps(payload, ensure_ascii=False)

    def _tool_browse(self, args: dict) -> str:
        action = args.get("action", "list")
        path = args.get("path", "viking://")

        # Map action to the correct fs endpoint (all GET with uri= param)
        endpoint_map = {"tree": "/api/v1/fs/tree", "list": "/api/v1/fs/ls", "stat": "/api/v1/fs/stat"}
        endpoint = endpoint_map.get(action, "/api/v1/fs/ls")
        resp = self._client.get(endpoint, params={"uri": path})
        result = self._unwrap_result(resp)

        # Format list/tree results for readability
        if action in {"list", "tree"}:
            raw_entries = result
            if isinstance(result, dict):
                raw_entries = result.get("entries") or result.get("items") or result.get("children") or []

            if isinstance(raw_entries, list):
                entries = []
                for e in raw_entries[:50]:  # cap at 50 entries
                    uri = e.get("uri", "")
                    name = e.get("rel_path") or e.get("name") or (uri.rsplit("/", 1)[-1] if uri else "")
                    is_dir = bool(e.get("isDir") or e.get("is_dir") or e.get("type") == "dir")
                    entries.append({
                        "name": name,
                        "uri": uri,
                        "type": "dir" if is_dir else "file",
                        "abstract": e.get("abstract", ""),
                    })
                return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False)

    # Map layer parameter to category subdirectory for explicit layer selection.
    _LAYER_SUBDIR_MAP = {
        "L1": "preferences",  # Stable facts: preferences, environment info
        "L2": "patterns",     # Deep knowledge: SOPs, workflows, patterns
    }

    @staticmethod
    def _build_front_matter(verified: bool, verification_type: str, layer: str,
                            created_by: str = "", scope: str = "private") -> str:
        """Build YAML front-matter for memory content with verification and feedback metadata."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        lines = [
            "---",
            f"verified: {'true' if verified else 'false'}",
            f"verification_type: {verification_type}",
            f"created_at: {ts}",
            f"layer: {layer}",
            f"scope: {scope}",
        ]
        if created_by:
            lines.append(f"created_by: {created_by}")
        if scope == "shared":
            lines.extend([
                "usage_count: 0",
                "success_count: 0",
                "failure_count: 0",
                f"last_used_at: {ts}",
                "last_outcome: none",
            ])
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")

        category = args.get("category", "")
        layer = args.get("layer", "auto")
        verified = args.get("verified", False)
        verification_type = args.get("verification_type", "auto_extracted")
        # Layer takes precedence over category for subdir selection.
        if layer in self._LAYER_SUBDIR_MAP:
            subdir = self._LAYER_SUBDIR_MAP[layer]
        elif category:
            subdir = _CATEGORY_SUBDIR_MAP.get(category, _DEFAULT_MEMORY_SUBDIR)
        else:
            subdir = _DEFAULT_MEMORY_SUBDIR
        # Determine effective layer label for front-matter
        effective_layer = layer if layer != "auto" else ("L1" if subdir in ("preferences", "entities") else "L2")
        # Re-route personal facts from patterns (L2) to preferences (L1) on auto routing.
        content_lower = content.lower()
        if effective_layer == "L2" and any(kw in content_lower for kw in _PERSONAL_FACT_KEYWORDS):
            subdir = "preferences"
            effective_layer = "L1"
        # Determine scope: where to write this memory
        scope_param = args.get("scope", "auto")
        if scope_param == "private":
            write_scope = "private"
        elif scope_param == "shared":
            write_scope = "shared"
        else:
            write_scope = self._scope_for_layer(effective_layer)
        # Hard guard: a first-person personal fact must never land in the
        # shared team space, even if the caller explicitly asked for shared.
        # Team space holds collective knowledge — pollute it with one
        # tenant's identity once and every other tenant inherits it forever.
        if write_scope == "shared" and any(kw in content_lower for kw in _PERSONAL_FACT_KEYWORDS):
            write_scope = "private"
            if effective_layer == "L2":
                subdir = "preferences"
                effective_layer = "L1"
        uri = self._build_memory_uri(subdir, scope=write_scope)
        # Prepend verification front-matter to content
        front_matter = self._build_front_matter(verified, verification_type, effective_layer, created_by=self._user, scope=write_scope)
        full_content = front_matter + content

        # Write directly via content/write API.
        # This creates the file, stores the content, and queues vector indexing
        # in a single call — no dependency on session commit / VLM extraction.
        write_client = self._client_for_scope(write_scope)
        if not write_client:
            return tool_error("OpenViking server not connected for scope: " + write_scope)
        try:
            result = write_client.post("/api/v1/content/write", {
                "uri": uri,
                "content": full_content,
                "mode": "create",
            })
            written = result.get("result", {}).get("written_bytes", 0)
            return json.dumps({
                "status": "stored",
                "layer": effective_layer,
                "scope": write_scope,
                "verified": verified,
                "verification_type": verification_type,
                "message": f"Memory stored ({written}b) and queued for vector indexing.",
            })
        except Exception as e:
            logger.error("OpenViking content/write failed: %s", e)
            return tool_error(f"Failed to store memory: {e}")

    def _tool_forget(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        mode = args.get("mode", "archive")
        reason = args.get("reason", "")

        # Gate: shared space archival requires curator confirmation
        is_shared = f"/user/{self._team_user}/" in uri
        if is_shared and mode == "archive":
            if "__curator__" not in reason:
                return json.dumps({
                    "status": "needs_curator_approval",
                    "uri": uri,
                    "message": (
                        "Archiving shared memories requires Cluster Curator approval. "
                        "The curator will review this in the next maintenance cycle. "
                        "To force, include '__curator__' in the reason."
                    ),
                })

        if mode == "archive":
            # Read original content, write to _archived/, overwrite original with redirect marker
            try:
                # Read original
                resp = self._client.get("/api/v1/content/read", params={"uri": uri})
                result = self._unwrap_result(resp)
                if isinstance(result, str):
                    original_content = result
                elif isinstance(result, dict):
                    original_content = result.get("content", "") or result.get("text", "")
                else:
                    original_content = ""

                if not original_content:
                    return tool_error(f"No content found at {uri}")

                # Build archive URI: replace /memories/ with /memories/_archived/
                archive_uri = uri.replace("/memories/", "/memories/_archived/", 1)
                if archive_uri == uri:
                    # Fallback: prepend _archived/ before the filename
                    parts = uri.rsplit("/", 1)
                    if len(parts) == 2:
                        archive_uri = f"{parts[0]}/_archived/{parts[1]}"

                # Write to archive location
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).isoformat()
                archive_content = original_content
                if reason:
                    archive_content += f"\n\n[Archived at {ts}. Reason: {reason}]"

                self._client.post("/api/v1/content/write", {
                    "uri": archive_uri,
                    "content": archive_content,
                    "mode": "create",
                })

                # Overwrite original with redirect marker (use "update" mode, fallback to "create")
                redirect_marker = f"-> {archive_uri}\n[Archived at {ts}]"
                if reason:
                    redirect_marker += f"\nReason: {reason}"
                try:
                    self._client.post("/api/v1/content/write", {
                        "uri": uri,
                        "content": redirect_marker,
                        "mode": "update",
                    })
                except Exception:
                    # If update mode fails, try create (may leave duplicate)
                    self._client.post("/api/v1/content/write", {
                        "uri": uri,
                        "content": redirect_marker,
                        "mode": "create",
                    })

                return json.dumps({
                    "status": "archived",
                    "original_uri": uri,
                    "archive_uri": archive_uri,
                    "message": f"Memory archived. Original replaced with redirect marker.",
                })
            except Exception as e:
                logger.error("OpenViking forget/archive failed: %s", e)
                return tool_error(f"Failed to archive memory: {e}")

        elif mode == "deprioritize":
            # Write a .meta.json sidecar with low priority
            try:
                meta_uri = uri.rsplit(".", 1)
                if len(meta_uri) == 2:
                    meta_uri = f"{meta_uri[0]}.meta.json"
                else:
                    meta_uri = f"{uri}.meta.json"

                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).isoformat()
                meta_content = json.dumps({
                    "priority": "low",
                    "deprioritized_at": ts,
                    "reason": reason or "deprioritized by agent",
                })

                self._client.post("/api/v1/content/write", {
                    "uri": meta_uri,
                    "content": meta_content,
                    "mode": "create",
                })

                return json.dumps({
                    "status": "deprioritized",
                    "uri": uri,
                    "meta_uri": meta_uri,
                    "message": "Memory deprioritized. Will rank lower in search results.",
                })
            except Exception as e:
                logger.error("OpenViking forget/deprioritize failed: %s", e)
                return tool_error(f"Failed to deprioritize memory: {e}")

        return tool_error(f"Unknown forget mode: {mode}")

    def _tool_feedback(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")
        outcome = args.get("outcome", "")
        if outcome not in ("success", "failure", "partial"):
            return tool_error("outcome must be one of: success, failure, partial")
        note = args.get("note", "")

        # Determine which client to use based on URI
        is_shared = f"/user/{self._team_user}/" in uri
        feedback_client = self._shared_client if is_shared else self._client
        if not feedback_client:
            return tool_error("OpenViking server not connected")

        # Read current content
        try:
            resp = feedback_client.get("/api/v1/content/read", params={"uri": uri})
            result = self._unwrap_result(resp)
            if isinstance(result, str):
                current_content = result
            elif isinstance(result, dict):
                current_content = result.get("content", "") or result.get("text", "")
            else:
                current_content = ""

            if not current_content:
                return tool_error(f"No content found at {uri}")
        except Exception as e:
            return tool_error(f"Failed to read memory for feedback: {e}")

        # Parse and update YAML front-matter
        import re
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()

        front_matter_match = re.match(r'^---\n(.*?)\n---\n', current_content, re.DOTALL)
        if front_matter_match:
            fm_text = front_matter_match.group(1)
            body = current_content[front_matter_match.end():]
            # Update counters
            usage_count = 0
            success_count = 0
            failure_count = 0
            for line in fm_text.split("\n"):
                if line.startswith("usage_count:"):
                    try:
                        usage_count = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("success_count:"):
                    try:
                        success_count = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("failure_count:"):
                    try:
                        failure_count = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass

            usage_count += 1
            if outcome == "success":
                success_count += 1
            elif outcome == "failure":
                failure_count += 1

            # Rebuild front-matter lines
            new_fm_lines = []
            skip_keys = {"usage_count", "success_count", "failure_count", "last_used_at", "last_outcome"}
            for line in fm_text.split("\n"):
                key = line.split(":", 1)[0].strip() if ":" in line else ""
                if key in skip_keys:
                    continue
                new_fm_lines.append(line)
            new_fm_lines.append(f"usage_count: {usage_count}")
            new_fm_lines.append(f"success_count: {success_count}")
            new_fm_lines.append(f"failure_count: {failure_count}")
            new_fm_lines.append(f"last_used_at: {ts}")
            new_fm_lines.append(f"last_outcome: {outcome}")

            new_fm = "---\n" + "\n".join(new_fm_lines) + "\n---\n"
            new_content = new_fm + body
        else:
            # No front-matter — append feedback as a comment
            new_content = current_content + f"\n\n[Feedback at {ts}: {outcome}" + (f" — {note}" if note else "") + "]"

        # Write back — use a feedback sidecar URI to avoid ALREADY_EXISTS on the original
        feedback_slug = uuid.uuid4().hex[:8]
        base_uri = uri.rsplit(".", 1)[0] if "." in uri else uri
        feedback_uri = f"{base_uri}_feedback_{feedback_slug}.md"
        try:
            feedback_client.post("/api/v1/content/write", {
                "uri": feedback_uri,
                "content": new_content,
                "mode": "create",
            })
        except Exception as e:
            return tool_error(f"Failed to write feedback: {e}")

        return json.dumps({
            "status": "feedback_recorded",
            "uri": uri,
            "outcome": outcome,
            "note": note,
            "message": f"Feedback recorded: {outcome}",
        })

    def _tool_add_resource(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return tool_error("url is required")

        if args.get("to") and args.get("parent"):
            return tool_error("Cannot specify both 'to' and 'parent'")

        payload: Dict[str, Any] = {}
        for key in ("reason", "to", "parent", "instruction", "wait", "timeout"):
            if key in args and args[key] not in {None, ""}:
                payload[key] = args[key]

        parsed_url = urlparse(url)
        if _is_remote_resource_source(url):
            source_path = None
        elif parsed_url.scheme == "file":
            source_path = _path_from_file_uri(url)
            if isinstance(source_path, str):
                return tool_error(source_path)
        elif parsed_url.scheme and not _is_windows_absolute_path(url):
            source_path = None
        else:
            source_path = Path(url).expanduser()

        cleanup_path: Optional[Path] = None
        try:
            if source_path is not None:
                if source_path.exists():
                    if source_path.is_dir():
                        payload["source_name"] = source_path.name
                        cleanup_path = _zip_directory(source_path)
                        upload_path = cleanup_path
                    elif source_path.is_file():
                        payload["source_name"] = source_path.name
                        upload_path = source_path
                    else:
                        return tool_error(f"Unsupported local resource path: {url}")
                    payload["temp_file_id"] = self._client.upload_temp_file(upload_path)
                elif _is_local_path_reference(url):
                    return tool_error(f"Local resource path does not exist: {url}")
                else:
                    payload["path"] = url
            else:
                payload["path"] = url

            resp = self._client.post("/api/v1/resources", payload)
            result = resp.get("result", {})
        finally:
            if cleanup_path:
                cleanup_path.unlink(missing_ok=True)

        return json.dumps({
            "status": "added",
            "root_uri": result.get("root_uri", ""),
            "message": "Resource queued for processing. Use viking_search after a moment to find it.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register OpenViking as a memory provider plugin."""
    ctx.register_memory_provider(OpenVikingMemoryProvider())

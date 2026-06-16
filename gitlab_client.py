"""
Shared GitLab REST client for the inventory scripts.

Features
--------
* TokenPool: round-robin rotation across N personal access tokens.
* Per-token rate-limit tracking (RateLimit-Remaining / RateLimit-Reset).
* Automatic 429 retry honouring Retry-After (both integer seconds and
  HTTP-date format).
* Transparent 5xx retry with exponential back-off and timeout retry.
* Thread-safe: designed for use with concurrent.futures.ThreadPoolExecutor.
* Generator-based pagination that follows X-Next-Page.
* CheckpointStore: append-only file of completed project IDs for resume.
* CsvSink: append-mode CSV writer safe for many writers.

This module lives at the workspace root and is imported by both scripts via
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

Important note about GitLab rate limits
---------------------------------------
GitLab.com applies primary rate limits *per authenticated user*. Three PATs
issued to the same user share one budget. To get a real Nx throughput,
each PAT must belong to a distinct user (or service account).
Self-managed GitLab may also rate-limit per IP — check your instance's
Application settings.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import quote

import requests

log = logging.getLogger("gitlab_client")


# ---------------------------------------------------------------------------
# .env autoload (runs at import time so module-level os.environ reads work)
# ---------------------------------------------------------------------------

def _autoload_dotenv() -> None:
    """
    Load environment variables from .env files at import time.

    Search order (first match wins per variable, but later files do not
    override earlier ones or any variable already set in the real
    environment):

      1. .env in the current working directory (or any parent).
      2. .env next to this module (the workspace root).

    Real shell environment variables ALWAYS take precedence over .env
    files (override=False).

    If python-dotenv is not installed, this is a silent no-op — the
    scripts will then rely on real environment variables only.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return

    import sys as _sys
    loaded: list[str] = []

    cwd_env = find_dotenv(usecwd=True)
    if cwd_env:
        load_dotenv(cwd_env, override=False)
        loaded.append(cwd_env)

    here_env = Path(__file__).resolve().parent / ".env"
    if here_env.exists() and str(here_env) != cwd_env:
        load_dotenv(here_env, override=False)
        loaded.append(str(here_env))

    if loaded:
        # Print to stderr so it doesn't pollute any stdout that callers
        # might pipe (CSV-like progress output stays clean).
        print(
            "[env] loaded "
            + ", ".join(loaded),
            file=_sys.stderr,
        )


_autoload_dotenv()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_retry_after(value: str | None, default: float = 5.0) -> float:
    """Retry-After may be integer seconds OR an HTTP-date (RFC 7231)."""
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(target.tzinfo)).total_seconds()
        return max(delta, 0.0)
    except (TypeError, ValueError):
        return default


def _mask(token: str) -> str:
    if not token or len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def encode_group(group: str | int) -> str:
    """URL-encode a group identifier. Numeric IDs pass through."""
    if isinstance(group, int):
        return str(group)
    s = str(group)
    if s.isdigit():
        return s
    return quote(s, safe="")


# ---------------------------------------------------------------------------
# token pool
# ---------------------------------------------------------------------------

@dataclass
class _TokenState:
    token: str
    masked: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    remaining: int | None = None  # RateLimit-Remaining from last response
    reset_at: float = 0.0         # epoch seconds (RateLimit-Reset)
    in_use: int = 0               # how many workers are holding it


class TokenPool:
    """
    Round-robin token rotation with per-token rate-limit awareness.

    The pool picks the next token in rotation. If that token's
    RateLimit-Remaining is below `min_remaining` and the window hasn't
    reset yet, it skips to the next. If every token is throttled, the
    caller sleeps until the soonest reset.
    """

    def __init__(self, tokens: list[str], min_remaining: int = 50):
        clean = [t for t in (t.strip() for t in tokens) if t]
        if not clean:
            raise ValueError("TokenPool needs at least one non-empty token")
        self._states = [_TokenState(t, _mask(t)) for t in clean]
        self._cursor = 0
        self._cursor_lock = threading.Lock()
        self.min_remaining = min_remaining

    def __len__(self) -> int:
        return len(self._states)

    @property
    def masks(self) -> list[str]:
        return [s.masked for s in self._states]

    def _next_index(self) -> int:
        with self._cursor_lock:
            i = self._cursor
            self._cursor = (self._cursor + 1) % len(self._states)
            return i

    def acquire(self) -> _TokenState:
        """Return the next available token (sleeps if all are throttled)."""
        n = len(self._states)
        for _ in range(n):
            s = self._states[self._next_index()]
            with s.lock:
                now = time.time()
                ok = (
                    s.remaining is None
                    or s.remaining > self.min_remaining
                    or now >= s.reset_at
                )
                if ok:
                    s.in_use += 1
                    return s
        # All throttled. Sleep until the soonest reset.
        soonest = min(self._states, key=lambda x: x.reset_at)
        wait = max(soonest.reset_at - time.time(), 0.0) + 0.5
        log.warning(
            "All %d tokens throttled — sleeping %.1fs for %s",
            n, wait, soonest.masked,
        )
        time.sleep(wait)
        with soonest.lock:
            soonest.remaining = None  # force a fresh probe
            soonest.in_use += 1
        return soonest

    def release(self, state: _TokenState) -> None:
        with state.lock:
            if state.in_use > 0:
                state.in_use -= 1

    def update_from_response(
        self, state: _TokenState, resp: requests.Response
    ) -> None:
        """Read RateLimit-* headers from a response and store on the state."""
        with state.lock:
            r = resp.headers.get("RateLimit-Remaining")
            t = resp.headers.get("RateLimit-Reset")
            if r is not None:
                try:
                    state.remaining = int(r)
                except ValueError:
                    pass
            if t is not None:
                try:
                    state.reset_at = float(t)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# pooled HTTP client
# ---------------------------------------------------------------------------

class PooledClient:
    """
    Thread-safe GitLab REST client backed by a TokenPool.

    Sessions are reused per token. urllib3's connection pool is thread-safe
    for read-only requests (we never mutate session headers after init).
    """

    DEFAULT_PER_PAGE = 100

    def __init__(
        self,
        base_url: str,
        pool: TokenPool,
        *,
        timeout: int = 30,
        max_retries: int = 5,
    ):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self.pool = pool
        self.timeout = timeout
        self.max_retries = max_retries
        self._sessions: dict[str, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def _session_for(self, state: _TokenState) -> requests.Session:
        with self._sessions_lock:
            s = self._sessions.get(state.token)
            if s is None:
                s = requests.Session()
                s.headers.update({"PRIVATE-TOKEN": state.token})
                self._sessions[state.token] = s
            return s

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if path.startswith("/api/v4"):
            return f"{self.base_url}{path}"
        if path.startswith("/"):
            return f"{self.api}{path}"
        return f"{self.api}/{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        url = self._absolute(path)
        last: requests.Response | None = None
        for attempt in range(self.max_retries):
            state = self.pool.acquire()
            sess = self._session_for(state)
            try:
                resp = sess.request(
                    method, url, params=params,
                    timeout=timeout or self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                self.pool.release(state)
                wait = min(2 ** attempt, 30)
                log.warning(
                    "[%s] network error on %s %s (attempt %d): %s; "
                    "retry in %ds",
                    state.masked, method, url, attempt + 1, exc, wait,
                )
                time.sleep(wait)
                continue

            self.pool.update_from_response(state, resp)
            self.pool.release(state)

            if resp.status_code == 429:
                wait = _parse_retry_after(resp.headers.get("Retry-After"))
                log.warning(
                    "[%s] 429 on %s — sleeping %.1fs",
                    state.masked, url, wait,
                )
                with state.lock:
                    state.reset_at = max(state.reset_at, time.time() + wait)
                    state.remaining = 0
                time.sleep(wait)
                last = resp
                continue

            if 500 <= resp.status_code < 600:
                wait = min(2 ** attempt, 30)
                log.warning(
                    "[%s] %d on %s — retry in %ds",
                    state.masked, resp.status_code, url, wait,
                )
                time.sleep(wait)
                last = resp
                continue

            return resp

        if last is not None:
            return last
        raise RuntimeError(f"All retries exhausted for {method} {url}")

    def get(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        return self.request("GET", path, params=params, timeout=timeout)

    def head(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        return self.request("HEAD", path, params=params, timeout=timeout)

    def paginated(
        self,
        path: str,
        *,
        params: dict | None = None,
    ) -> Iterator[dict]:
        """Iterate every page until the server stops sending X-Next-Page."""
        params = dict(params or {})
        params.setdefault("per_page", self.DEFAULT_PER_PAGE)
        page = 1
        while True:
            params["page"] = page
            resp = self.get(path, params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"GET {path} failed [{resp.status_code}]: "
                    f"{resp.text[:300]}"
                )
            batch = resp.json()
            if not batch:
                return
            for item in batch:
                yield item
            nxt = resp.headers.get("X-Next-Page")
            if not nxt:
                return
            try:
                page = int(nxt)
            except ValueError:
                return


# ---------------------------------------------------------------------------
# group / project helpers
# ---------------------------------------------------------------------------

def get_group(client: PooledClient, group: str | int) -> dict:
    g = encode_group(group)
    resp = client.get(f"/groups/{g}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not load group '{group}' [{resp.status_code}]: "
            f"{resp.text[:300]}"
        )
    return resp.json()


def list_group_projects(
    client: PooledClient,
    group: str | int,
    *,
    include_subgroups: bool = True,
    archived: str | None = None,
    with_shared: str = "false",
    statistics: bool = False,
) -> Iterator[dict]:
    """
    Yield every project under a group, recursing through nested subgroups.

    Pass `archived="false"` to skip archived projects (the pipeline script
    does this). Omit `archived` to include them all (the repo script does
    this and reports the archived flag in its CSV).
    """
    g = encode_group(group)
    params: dict = {
        "include_subgroups": "true" if include_subgroups else "false",
        "with_shared": with_shared,
    }
    if archived is not None:
        params["archived"] = archived
    if statistics:
        params["statistics"] = "true"
    yield from client.paginated(f"/groups/{g}/projects", params=params)


# ---------------------------------------------------------------------------
# token / project-list loading
# ---------------------------------------------------------------------------

def load_tokens(
    args_tokens: str | None = None,
    tokens_file: str | Path | None = None,
    fallback_env_single: str = "GITLAB_TOKEN",
    fallback_env_multi: str = "GITLAB_TOKENS",
) -> list[str]:
    """
    Resolve tokens in priority order:
      1. args_tokens ("a,b,c")
      2. tokens_file (one per line; '#' comments allowed)
      3. env GITLAB_TOKENS (comma-separated)
      4. env GITLAB_TOKEN (single)
    """
    out: list[str] = []
    if args_tokens:
        out = [t.strip() for t in args_tokens.split(",")]
    elif tokens_file:
        with open(tokens_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    elif os.environ.get(fallback_env_multi):
        out = [t.strip() for t in os.environ[fallback_env_multi].split(",")]
    elif os.environ.get(fallback_env_single):
        out = [os.environ[fallback_env_single].strip()]
    return [t for t in out if t]


def load_projects_csv(csv_path: str | Path) -> list[dict]:
    """
    Load a project list from a `gitlab-stats.csv` (output of the repo
    inventory script). Returns dicts shaped like GitLab API project objects
    so the pipeline inventory can consume them directly.

    Skips rows whose `id` isn't an integer.
    """
    out: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pid = int(row["id"])
            except (KeyError, ValueError, TypeError):
                continue
            out.append({
                "id": pid,
                "path_with_namespace": (row.get("path") or "").strip(),
                "default_branch": (row.get("default_branch") or "").strip(),
                "web_url": (row.get("web_url") or "").strip(),
                # ci_config_path is not preserved in the repo CSV — the
                # pipeline script will fall back to `.gitlab-ci.yml`.
                "ci_config_path": None,
            })
    return out


# ---------------------------------------------------------------------------
# checkpoint + csv sink
# ---------------------------------------------------------------------------

class CheckpointStore:
    """Append-only file of completed project IDs. Crash-safe."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._done: set[int] = set()
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.isdigit():
                        self._done.add(int(s))

    def is_done(self, project_id: int) -> bool:
        return project_id in self._done

    def mark_done(self, project_id: int) -> None:
        with self._lock:
            if project_id in self._done:
                return
            self._done.add(project_id)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(f"{project_id}\n")

    @property
    def done_count(self) -> int:
        return len(self._done)

    def reset(self) -> None:
        with self._lock:
            self._done.clear()
            if self.path.exists():
                self.path.unlink()


class CsvSink:
    """
    Thread-safe CSV writer.

    `append=True` keeps existing rows (and skips the header) when the file
    already has content — useful for resume.
    """

    def __init__(
        self,
        path: str | Path,
        fieldnames: list[str],
        *,
        append: bool = False,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fieldnames = list(fieldnames)

        write_header = True
        mode = "w"
        if append and self.path.exists() and self.path.stat().st_size > 0:
            mode = "a"
            write_header = False

        self._file = self.path.open(mode, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file,
            fieldnames=self._fieldnames,
            extrasaction="ignore",
        )
        if write_header:
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()

    def write_many(self, rows: Iterable[dict]) -> None:
        with self._lock:
            for r in rows:
                self._writer.writerow(r)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

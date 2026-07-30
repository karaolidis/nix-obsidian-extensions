"""Regenerate data/plugins.json and data/themes.json from the registry."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

REGISTRY = (
    "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master"
)
GRAPHQL_URL = "https://api.github.com/graphql"

PLUGIN_BATCH = 25
THEME_BATCH = 100
DOWNLOAD_WORKERS = 8

MAX_ATTEMPTS = 8
BASE_DELAY = 5.0
MAX_BACKOFF = 300.0
SECONDARY_MIN_WAIT = 60.0
MAX_RATE_WAIT = 3600.0
HTTP_TIMEOUT = 60

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
ABSENT_STATUS = {404, 410}

REPO_RE = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
REF_RE = re.compile(r"\A[A-Za-z0-9._+-]+\Z")
OID_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SLUG_RE = re.compile(r"[^a-z0-9]+")

TRANSIENT_RE = re.compile(
    r"rate limit|secondary|abuse|timeout|timed out|something went wrong"
    r"|temporarily unavailable|try again",
    re.IGNORECASE,
)
TRANSIENT_TYPES = {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}

log = logging.getLogger("update")


class Unavailable(Exception):
    """A transient failure that survived every retry."""


def sri(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def slug(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")


def backoff(attempt: int) -> float:
    return min(BASE_DELAY * 2 ** (attempt - 1), MAX_BACKOFF)


def clamp_wait(seconds: float) -> float:
    return max(1.0, min(seconds, MAX_RATE_WAIT))


@dataclass
class Response:
    status: int | None
    body: bytes
    headers: Any
    error: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def header_int(self, name: str) -> int | None:
        if self.headers is None:
            return None
        try:
            return int(self.headers.get(name))
        except (TypeError, ValueError):
            return None


def request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return Response(resp.status, resp.read(), resp.headers)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(), exc.headers, str(exc))
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
    ) as exc:
        return Response(None, b"", None, str(exc))


class Throttle:
    """Shared pause honoured by every download worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                delay = self._until - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 5.0))

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)


class GitHub:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "nix-obsidian-extensions-update",
        }

    def graphql(self, query: str) -> dict[str, Any] | None:
        """Resolve a batch, or return None if it could not be resolved."""
        partial: dict[str, Any] | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = request(
                GRAPHQL_URL,
                data=json.dumps({"query": query}).encode(),
                headers=self._headers,
            )

            body = resp.json()
            errors = (body or {}).get("errors") or []
            notes = [e.get("message") or "" for e in errors]
            top = (body or {}).get("message")
            if isinstance(top, str):
                notes.append(top)
            transient = any(
                (e.get("type") or "").upper() in TRANSIENT_TYPES
                for e in errors
            ) or any(TRANSIENT_RE.search(m) for m in notes)
            usable = (
                resp.status == 200
                and isinstance(body, dict)
                and body.get("data") is not None
            )
            permanent = (
                resp.status is not None and resp.status not in RETRY_STATUS
            )

            if usable and not transient:
                self._report_nodes(errors)
                return body
            if usable:
                if partial is None or len(errors) < len(
                    partial.get("errors") or []
                ):
                    partial = body
            elif not transient and permanent:
                log.error("graphql: unrecoverable: %s", describe(resp, errors))
                return None

            if attempt == MAX_ATTEMPTS:
                break
            log.warning(
                "graphql: attempt %d/%d: %s",
                attempt,
                MAX_ATTEMPTS,
                describe(resp, errors),
            )
            self._wait_before_retry(attempt, resp, transient)

        if partial is not None:
            log.warning(
                "graphql: accepting partial data after %d attempts",
                MAX_ATTEMPTS,
            )
            self._report_nodes((partial.get("errors") or []))
            return partial
        return None

    def _report_nodes(self, errors: Sequence[dict[str, Any]]) -> None:
        if errors:
            messages = "; ".join(e.get("message", "") for e in errors)
            log.warning(
                "graphql: %d unresolved node(s): %s",
                len(errors),
                messages[:300],
            )

    def _wait_before_retry(
        self, attempt: int, resp: Response, transient: bool
    ) -> None:
        retry_after = resp.header_int("retry-after")
        remaining = resp.header_int("x-ratelimit-remaining")
        reset = resp.header_int("x-ratelimit-reset")

        if retry_after is not None:
            delay = float(retry_after)
        elif remaining == 0 and reset is not None:
            delay = reset - time.time() + 5
        else:
            delay = backoff(attempt)
            if resp.status == 429 or transient:
                delay = max(delay, SECONDARY_MIN_WAIT)
        time.sleep(clamp_wait(delay))


def describe(resp: Response, errors: Sequence[dict[str, Any]]) -> str:
    detail = "; ".join(e.get("message", "") for e in errors) or resp.error
    return f"{resp.status or 'no response'}: {detail[:300]}"


class Downloader:
    """Asset fetcher."""

    def __init__(self) -> None:
        self._headers = {"User-Agent": "nix-obsidian-extensions-update"}
        self._throttle = Throttle()

    def fetch(self, url: str) -> str | None:
        """SRI hash, or None if the asset is definitively absent."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle.wait()
            resp = request(url, headers=self._headers)

            if resp.status == 200:
                return sri(resp.body)
            if resp.status in ABSENT_STATUS:
                return None

            delay = resp.header_int("retry-after")
            throttled = resp.status == 429 or (
                resp.status == 403 and delay is not None
            )
            if (
                resp.status is not None
                and resp.status not in RETRY_STATUS
                and not throttled
            ):
                raise Unavailable(f"HTTP {resp.status} for {url}")
            if attempt == MAX_ATTEMPTS:
                break

            wait = float(delay) if delay is not None else backoff(attempt)
            if throttled:
                wait = max(wait, SECONDARY_MIN_WAIT)
                self._throttle.pause(clamp_wait(wait))
            else:
                time.sleep(clamp_wait(wait))

        raise Unavailable(f"{url}: {resp.status or resp.error}")


@dataclass
class Plugin:
    id: str
    name: str
    repo: str
    manifest_version: str | None = None
    latest_tag: str | None = None
    resolved: bool = True

    @property
    def key(self) -> str:
        return self.id

    @property
    def version(self) -> str | None:
        return self.manifest_version

    def metadata(self) -> dict[str, Any]:
        return {"name": self.name, "repo": self.repo}


@dataclass
class Theme:
    name: str
    repo: str
    modes: list[str]
    base: str
    owner: str
    attr: str = ""
    rev: str | None = None
    resolved: bool = True

    @property
    def key(self) -> str:
        return self.attr

    @property
    def version(self) -> str | None:
        return self.rev

    def metadata(self) -> dict[str, Any]:
        return {"name": self.name, "repo": self.repo, "modes": self.modes}


@dataclass
class Triage:
    carry: dict[str, Any] = field(default_factory=dict)
    tohash: list[Any] = field(default_factory=list)
    unresolved: list[Any] = field(default_factory=list)
    gone: list[Any] = field(default_factory=list)


def cached_for(previous: dict[str, Any], rec: Any) -> dict[str, Any] | None:
    cached = previous.get(rec.key)
    if cached is None or cached.get("repo") != rec.repo:
        return None
    return cached


def triage(
    records: Iterable[Any], previous: dict[str, Any], version_key: str
) -> Triage:
    """Split records into what to keep, re-hash, report, or refuse."""
    out = Triage()
    for rec in records:
        cached = cached_for(previous, rec)
        current = rec.version
        prior = cached.get(version_key) if cached else None

        if cached is not None and (current is None or prior == current):
            out.carry[rec.key] = {**cached, **rec.metadata()}
        elif current is not None:
            out.tohash.append(rec)
        elif not rec.resolved:
            out.unresolved.append(rec)
        else:
            out.gone.append(rec)
    return out


def fetch_registry(name: str) -> list[dict[str, Any]]:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = request(f"{REGISTRY}/{name}")
        if resp.status == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            sys.exit(f"{name}: expected a JSON array")
        if attempt == MAX_ATTEMPTS or (
            resp.status is not None and resp.status not in RETRY_STATUS
        ):
            sys.exit(f"{name}: {resp.status or resp.error}")
        time.sleep(backoff(attempt))
    sys.exit(f"{name}: unreachable")


def load_previous(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def build_plugins(raw: Sequence[dict[str, Any]]) -> list[Plugin]:
    plugins: list[Plugin] = []
    for entry in raw:
        pid, repo = str(entry.get("id", "")), str(entry.get("repo", ""))
        if not REF_RE.match(pid) or not REPO_RE.match(repo):
            log.warning(
                "plugins: skipping malformed entry %r", entry.get("id")
            )
            continue
        plugins.append(
            Plugin(id=pid, name=str(entry.get("name") or pid), repo=repo)
        )
    return plugins


def build_themes(
    raw: Sequence[dict[str, Any]], previous: dict[str, Any]
) -> list[Theme]:
    themes: list[Theme] = []
    for entry in raw:
        repo = str(entry.get("repo", ""))
        if not REPO_RE.match(repo):
            log.warning(
                "themes: skipping malformed entry %r", entry.get("name")
            )
            continue
        owner, repo_name = repo.split("/", 1)
        name = str(entry.get("name") or "")
        base = slug(name) or slug(repo_name) or "theme"
        modes = [str(m) for m in (entry.get("modes") or [])]
        themes.append(
            Theme(
                name=name, repo=repo, modes=modes, base=base, owner=slug(owner)
            )
        )
    assign_attrs(themes, previous)
    for theme in themes:
        if not theme.name:
            theme.name = theme.attr
    return themes


def assign_attrs(themes: Sequence[Theme], previous: dict[str, Any]) -> None:
    by_repo = {
        str(record.get("repo")): attr
        for attr, record in previous.items()
        if isinstance(record, dict)
    }
    taken: set[str] = set()

    for theme in themes:
        prior = by_repo.get(theme.repo)
        if prior and prior not in taken:
            theme.attr = prior
            taken.add(prior)

    for theme in themes:
        if theme.attr:
            continue
        candidates = [theme.base, f"{theme.owner}-{theme.base}"]
        choice = next((c for c in candidates if c not in taken), None)
        if choice is None:
            suffix = 2
            while f"{theme.owner}-{theme.base}-{suffix}" in taken:
                suffix += 1
            choice = f"{theme.owner}-{theme.base}-{suffix}"
        if choice != theme.base:
            log.warning("themes: %s takes attribute %r", theme.repo, choice)
        theme.attr = choice
        taken.add(choice)


def resolve(
    gh: GitHub,
    records: Sequence[Any],
    kind: str,
    size: int,
    query_for: Callable[[Sequence[Any]], str],
    apply: Callable[[Any, dict[str, Any] | None], None],
) -> None:
    total = len(records)
    for start in range(0, total, size):
        end = min(start + size, total)
        batch = records[start:end]
        log.info(
            "%s: resolving %d-%d / %d",
            kind,
            start + 1,
            start + len(batch),
            total,
        )
        body = gh.graphql(query_for(batch))
        if body is None:
            log.warning(
                "%s: batch %d-%d unresolved",
                kind,
                start + 1,
                start + len(batch),
            )
            for rec in batch:
                rec.resolved = False
            continue
        data = body["data"]
        for index, rec in enumerate(batch):
            apply(rec, data.get(f"r{index}"))


def plugin_query(batch: Sequence[Plugin]) -> str:
    parts = []
    for index, plugin in enumerate(batch):
        owner, name = plugin.repo.split("/", 1)
        parts.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ "
            'manifest: object(expression: "HEAD:manifest.json") '
            "{ ... on Blob { text } } "
            "releases(first: 1, orderBy: "
            "{field: CREATED_AT, direction: DESC}) "
            "{ nodes { tagName } } }"
        )
    return "query { " + " ".join(parts) + " }"


def apply_plugin(plugin: Plugin, node: dict[str, Any] | None) -> None:
    if not node:
        return
    manifest = (node.get("manifest") or {}).get("text") or ""
    try:
        version = json.loads(manifest).get("version")
    except (ValueError, AttributeError):
        version = None
    plugin.manifest_version = safe_ref(version)

    nodes = (node.get("releases") or {}).get("nodes") or []
    plugin.latest_tag = safe_ref(nodes[0].get("tagName")) if nodes else None


def theme_query(batch: Sequence[Theme]) -> str:
    parts = []
    for index, theme in enumerate(batch):
        owner, name = theme.repo.split("/", 1)
        parts.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ defaultBranchRef "
            "{ target { oid } } }"
        )
    return "query { " + " ".join(parts) + " }"


def apply_theme(theme: Theme, node: dict[str, Any] | None) -> None:
    if not node:
        return
    ref = node.get("defaultBranchRef") or {}
    oid = (ref.get("target") or {}).get("oid")
    theme.rev = oid if isinstance(oid, str) and OID_RE.match(oid) else None


def safe_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not REF_RE.match(value):
        return None
    return value


def hash_plugin(
    dl: Downloader, plugin: Plugin
) -> tuple[str, dict[str, Any] | None, str]:
    base = f"https://github.com/{plugin.repo}/releases/download"
    version = plugin.manifest_version
    try:
        main = dl.fetch(f"{base}/{version}/main.js")
        if main is None and plugin.latest_tag and plugin.latest_tag != version:
            main = dl.fetch(f"{base}/{plugin.latest_tag}/main.js")
            if main is not None:
                version = plugin.latest_tag
        if main is None:
            return (
                plugin.id,
                None,
                f"main.js missing at {plugin.manifest_version}",
            )

        manifest = dl.fetch(f"{base}/{version}/manifest.json")
        if manifest is None:
            return plugin.id, None, f"manifest.json missing at {version}"

        files = {"main.js": main, "manifest.json": manifest}
        styles = dl.fetch(f"{base}/{version}/styles.css")
        if styles is not None:
            files["styles.css"] = styles
    except Unavailable as exc:
        return plugin.id, None, str(exc)

    return (
        plugin.id,
        {
            "name": plugin.name,
            "repo": plugin.repo,
            "version": version,
            "files": files,
        },
        "",
    )


def hash_theme(
    dl: Downloader, theme: Theme
) -> tuple[str, dict[str, Any] | None, str]:
    base = f"https://raw.githubusercontent.com/{theme.repo}/{theme.rev}"
    try:
        css_name, css_hash = "theme.css", dl.fetch(f"{base}/theme.css")
        if css_hash is None:
            css_name, css_hash = (
                "obsidian.css",
                dl.fetch(f"{base}/obsidian.css"),
            )
        if css_hash is None:
            return (
                theme.attr,
                None,
                f"no theme.css or obsidian.css at {theme.rev}",
            )

        files = {css_name: css_hash}
        manifest = dl.fetch(f"{base}/manifest.json")
        if manifest is not None:
            files["manifest.json"] = manifest
    except Unavailable as exc:
        return theme.attr, None, str(exc)

    return (
        theme.attr,
        {
            "name": theme.name,
            "repo": theme.repo,
            "rev": theme.rev,
            "modes": theme.modes,
            "files": files,
        },
        "",
    )


def hash_all(
    dl: Downloader,
    kind: str,
    records: Sequence[Any],
    previous: dict[str, Any],
    worker: Callable[
        [Downloader, Any], tuple[str, dict[str, Any] | None, str]
    ],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    total = len(records)
    done = 0

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(worker, dl, rec): rec for rec in records}
        for future in as_completed(futures):
            done += 1
            rec = futures[future]
            key, record, reason = future.result()
            if record is not None:
                out[key] = record
                log.info("%s: hashed [%d/%d] %s", kind, done, total, key)
                continue
            cached = cached_for(previous, rec)
            if cached is not None:
                out[key] = {**cached, **rec.metadata()}
                log.info("%s: %s: %s; kept previous record", kind, key, reason)
            else:
                log.info("%s: %s: %s; skipped", kind, key, reason)
    return out


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def find_repo_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "flake.nix").is_file() and (
            candidate / "overlays" / "mk-plugin.nix"
        ).is_file():
            return candidate
    sys.exit("could not locate repo root")


def github_token() -> str:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(var)
        if token:
            return token.strip()
    if shutil.which("gh"):
        try:
            return subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    sys.exit("no GitHub auth; set GH_TOKEN or run 'gh auth login'")


def report(kind: str, result: Triage) -> bool:
    if result.gone:
        log.info(
            "%s: %d new entr(ies) skipped, repo or manifest unreadable:",
            kind,
            len(result.gone),
        )
        for rec in result.gone:
            log.info("  %s (%s)", rec.key, rec.repo)
    if result.unresolved:
        log.error(
            "%s: %d entr(ies) unresolved with no cached record:",
            kind,
            len(result.unresolved),
        )
        for rec in result.unresolved:
            log.error("  %s (%s)", rec.key, rec.repo)
        return False
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stderr
    )

    root = find_repo_root()
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    plugins_file = data_dir / "plugins.json"
    themes_file = data_dir / "themes.json"

    old_plugins = load_previous(plugins_file)
    old_themes = load_previous(themes_file)

    token = github_token()
    gh = GitHub(token)

    log.info("plugins: fetching community-plugins.json")
    plugins = build_plugins(fetch_registry("community-plugins.json"))
    log.info("themes: fetching community-css-themes.json")
    themes = build_themes(
        fetch_registry("community-css-themes.json"), old_themes
    )

    resolve(gh, plugins, "plugins", PLUGIN_BATCH, plugin_query, apply_plugin)
    resolve(gh, themes, "themes", THEME_BATCH, theme_query, apply_theme)

    plugin_triage = triage(plugins, old_plugins, "version")
    theme_triage = triage(themes, old_themes, "rev")

    ok = report("plugins", plugin_triage) & report("themes", theme_triage)
    if not ok:
        sys.exit(
            "refusing to publish a truncated set; "
            "rerun once GitHub is reachable"
        )

    dl = Downloader()

    log.info(
        "plugins: %d up to date, %d to hash",
        len(plugin_triage.carry),
        len(plugin_triage.tohash),
    )
    out_plugins = dict(plugin_triage.carry)
    out_plugins.update(
        hash_all(dl, "plugins", plugin_triage.tohash, old_plugins, hash_plugin)
    )

    log.info(
        "themes: %d up to date, %d to hash",
        len(theme_triage.carry),
        len(theme_triage.tohash),
    )
    out_themes = dict(theme_triage.carry)
    out_themes.update(
        hash_all(dl, "themes", theme_triage.tohash, old_themes, hash_theme)
    )

    write_json(plugins_file, out_plugins)
    write_json(themes_file, out_themes)

    log.info("plugins: wrote %d entries", len(out_plugins))
    log.info("themes: wrote %d entries", len(out_themes))


if __name__ == "__main__":
    main()

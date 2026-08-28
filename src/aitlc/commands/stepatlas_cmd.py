"""`aitlc stepatlas ...` -- build/serve the StepAtlas site, and look up a step.

StepAtlas is a separate, sibling tool (its own repo, its own uv/pnpm
environment) that catalogs every real Gherkin step via the same
behave.step_registry walk aitlc's own `steps unused` uses. These commands
are a thin wrapper: they shell out to StepAtlas's own CLI/site tooling and
read its generated catalog.json, they don't reimplement any of it.
"""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlsplit

import typer
from aitlc.config import AitlcConfig

app = typer.Typer(help="Build/serve the StepAtlas site, and look up a step.")

_MAX_MATCHES = 20
_URL_MATCH_TOP_N = 3
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TOKEN_STOPWORDS = {"page", "the", "a", "an", "of", "and"}


def _require_stepatlas_path(config):
    path = config.stepatlas_path()
    if path is None:
        typer.echo(
            json.dumps(
                {
                    "error": "no [stepatlas] path configured",
                    "hint": 'add [stepatlas]\\npath = "../StepAtlas" to aitlc.toml',
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    return path


@app.command("build")
def build() -> None:
    """Run `stepatlas build` against this project (no site preview)."""
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    proc = subprocess.run(
        [
            "uv", "run", "stepatlas", "build",
            "--project", str(config.root_dir),
            "--step-dir", config.step_dir,
            "--feature-dir", config.feature_dir,
            "--skip-site-build",
        ],
        cwd=stepatlas_path,
    )
    raise typer.Exit(code=proc.returncode)


@app.command("serve")
def serve(
    rebuild: bool = typer.Option(
        None,
        "--rebuild/--skip-build",
        help="Force a regenerate, or skip it and serve whatever's already built. "
        "Default: regenerate only if nothing's built yet (dist/ is empty/missing).",
    ),
) -> None:
    """Serve the StepAtlas site, regenerating first only if needed."""
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    already_built = (stepatlas_path / "site" / "dist" / "index.html").is_file()
    do_build = rebuild if rebuild is not None else not already_built
    if do_build:
        proc = subprocess.run(
            [
                "uv", "run", "stepatlas", "build",
                "--project", str(config.root_dir),
                "--step-dir", config.step_dir,
                "--feature-dir", config.feature_dir,
            ],
            cwd=stepatlas_path,
        )
        if proc.returncode != 0:
            raise typer.Exit(code=proc.returncode)
    proc = subprocess.run(["pnpm", "run", "preview"], cwd=stepatlas_path / "site")
    raise typer.Exit(code=proc.returncode)


@app.command("stop")
def stop() -> None:
    """Stop a running `stepatlas serve` preview server.

    `pnpm run preview` wraps astro's own server as a child process; a
    Ctrl+C sometimes only signals the pnpm wrapper (exit 143) and leaves
    astro.mjs holding the port. This finds and kills it directly.
    """
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    pattern = f"{stepatlas_path / 'site'}.*astro.*preview"
    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [pid for pid in found.stdout.split() if pid]
    for pid in pids:
        subprocess.run(["kill", pid])
    typer.echo(json.dumps({"stopped": [int(pid) for pid in pids]}))


def _matches_file_line(step: dict, file: str, line: int) -> bool:
    return step["file"].endswith(file) and step["line"] == line


def _nearest_before(steps: list[dict], file: str, line: int) -> dict | None:
    candidates = [s for s in steps if s["file"].endswith(file) and s["line"] <= line]
    return max(candidates, key=lambda s: s["line"]) if candidates else None


def _category(step: dict) -> dict:
    return step.get("category") or {}


def _filter_by_page(steps: list[dict], page: str) -> list[dict]:
    """Match a page/category by slug or label -- e.g. --page admin-page,
    --page 'Account Settings', or just --page account (substring)."""
    p = page.lower()
    return [
        s
        for s in steps
        if p in _category(s).get("slug", "").lower() or p in _category(s).get("label", "").lower()
    ]


def _filter_by_group(steps: list[dict], group: str) -> list[dict]:
    g = group.lower()
    return [s for s in steps if g in _category(s).get("group", "").lower()]


def _filter_by_keyword(steps: list[dict], keyword: str) -> list[dict]:
    kw = keyword.lower()
    return [s for s in steps if kw in [k.lower() for k in s.get("keywords", [])]]


def _filter_by_uses_api(steps: list[dict], uses_api: bool) -> list[dict]:
    return [s for s in steps if bool(s.get("uses_api")) == uses_api]


def _filter_by_file(steps: list[dict], file_fragment: str) -> list[dict]:
    f = file_fragment.lower()
    return [s for s in steps if f in s["file"].lower()]


def _tokenize(text: str) -> set[str]:
    """Split `text` into lowercase word tokens, camelCase/kebab/snake-aware.

    "directMail" and "direct-mail" and "Direct Mail" all tokenize to the
    same {"direct", "mail"} -- URL path segments are typically camelCase
    or kebab-case, category slugs are kebab-case, category labels are
    Title Case; none of those are the SAME casing convention as any other,
    so matching has to normalize past casing entirely, not assume one.
    """
    text = _CAMEL_BOUNDARY.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text.lower())
    return {t for t in text.split() if t and t not in _TOKEN_STOPWORDS}


def _url_path_tokens(url: str) -> set[str]:
    """Token set from a URL's PATH only -- never its query string or host.

    `acId=QWNjb3VudFR5...` (a real query param seen live on an admin
    account settings page) is a base64-ish opaque id, not a word; matching
    against it would only ever add noise. The path segments (`admin`,
    `accounts`, `settings`, `directMail`) are the only part of a URL an
    app's own routing choices actually name meaningfully.
    """
    path = urlsplit(url).path if "://" in url else url.split("?", 1)[0]
    tokens: set[str] = set()
    for segment in path.split("/"):
        tokens |= _tokenize(segment)
    return tokens


def _category_key(step: dict) -> tuple[str, str, str]:
    cat = _category(step)
    return (cat.get("group", ""), cat.get("slug", ""), cat.get("label", ""))


def _score_categories_by_url(steps: list[dict], url: str) -> list[dict]:
    """Every distinct category in `steps`, scored against a URL's path tokens.

    Jaccard similarity (intersection / union of token sets) between the
    URL's path tokens and each category's own slug+label tokens -- no
    stored URL-to-step mapping anywhere (one doesn't exist: an SPA's
    client-side route lives in the frontend's router, invisible to a
    Python/behave catalog), just a static heuristic over the catalog as
    it already stands. Sorted best-first; zero-score categories are
    dropped entirely rather than padding the list with noise.
    """
    url_tokens = _url_path_tokens(url)
    seen: dict[tuple[str, str, str], set[str]] = {}
    for step in steps:
        key = _category_key(step)
        if key not in seen:
            group, slug, label = key
            seen[key] = _tokenize(slug) | _tokenize(label)
    scored = []
    for (group, slug, label), cat_tokens in seen.items():
        if not cat_tokens or not url_tokens:
            continue
        overlap = url_tokens & cat_tokens
        if not overlap:
            continue
        union = url_tokens | cat_tokens
        scored.append(
            {
                "group": group,
                "slug": slug,
                "label": label,
                "score": round(len(overlap) / len(union), 3),
                "matched_tokens": sorted(overlap),
            }
        )
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def _filter_by_url(steps: list[dict], url: str) -> tuple[list[dict], list[dict]]:
    """Steps under the best-matching categories for a URL, plus the scores.

    Takes the top `_URL_MATCH_TOP_N` scoring categories, not just the
    single best, since a route like `/admin/accounts/settings/directMail`
    can legitimately span more than one category (an "admin-page" step to
    get there, a "direct-mail" step once there) -- picking only the single
    highest-scoring category would silently drop the other one. The
    scores themselves are always returned so a caller can see how close
    (or not) the runner-up categories actually were.
    """
    scored = _score_categories_by_url(steps, url)
    if not scored:
        return [], []
    kept = scored[:_URL_MATCH_TOP_N]
    keep_slugs = {c["slug"] for c in kept}
    matches = [s for s in steps if _category(s).get("slug") in keep_slugs]
    return matches, kept


def _filter_by_text(steps: list[dict], query: str) -> list[dict]:
    q = query.lower()
    return [
        s
        for s in steps
        if q in s["pattern"].lower()
        or q in s["function"].lower()
        or q in " ".join(s["keywords"]).lower()
    ]


@app.command("info")
def info(
    query: str = typer.Argument(
        None,
        help='A text fragment (pattern/function), or "file.py:line". Optional when '
        "one or more of --page/--group/--keyword/--uses-api/--file already narrows "
        "the search on their own.",
    ),
    page: str = typer.Option(
        None,
        "--page",
        "-p",
        help="Filter to one page/category -- matches the catalog's category slug or "
        "label (e.g. --page admin-page, --page 'Account Settings'), case-insensitive "
        "substring. This is the step's originating page object/step-definition "
        "module, exactly what the generated site groups by.",
    ),
    group: str = typer.Option(
        None,
        "--group",
        help="Filter by category group -- e.g. 'pages' (product page steps) vs "
        "'common' (shared/cross-page steps), case-insensitive substring.",
    ),
    keyword: str = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Filter to steps registered under this Gherkin keyword "
        "(given/when/then), case-insensitive.",
    ),
    uses_api: bool = typer.Option(
        None,
        "--uses-api/--no-uses-api",
        help="Filter to steps that do (or don't) call an API directly, per the "
        "catalog's uses_api flag.",
    ),
    file: str = typer.Option(
        None,
        "--file",
        help="Filter by a substring of the step definition's file path -- unlike "
        "the positional 'file.py:line' form, this is a fuzzy filter, not an exact "
        "lookup, and composes with the other filters/query.",
    ),
    url: str = typer.Option(
        None,
        "--url",
        help="Filter to the page/category (or categories) whose slug/label best "
        "match this URL's path segments -- a static token heuristic (camelCase/"
        "kebab/snake all normalized), NOT a stored URL-to-step mapping (no such "
        "mapping exists: an SPA's client-side route lives in the frontend "
        "router, invisible to this catalog). Bridges straight from `debug eval "
        "\"window.location.href\"` / `cdp inspect --interactive` to 'what steps "
        "exist here' without hand-picking a --page slug. The reply's "
        "'url_match' shows which categories matched and their score, so a "
        "weak/wrong match is visible rather than silently trusted.",
    ),
) -> None:
    """Look up step(s) from StepAtlas's catalog.json.

    Four ways in, freely combined:

    \b
    - "file.py:line" as the query -- exact/nearest-before lookup, ignores every
      other filter (it already names one precise location).
    - A free-text query -- substring match against pattern/function/keywords.
    - --page/--group/--keyword/--uses-api/--file -- structured filters. Query
      is optional once at least one of these is given, so `aitlc stepatlas info
      --page admin-page` alone lists everything catalogued under that page.
    - --url -- heuristic page match from a live URL's path, when you don't
      already know which --page slug it corresponds to.

    Examples:

    \b
        aitlc stepatlas info "select database"
        aitlc stepatlas info features/steps/step_definition_search_page.py:573
        aitlc stepatlas info --page admin-page
        aitlc stepatlas info --page "Account Settings" --uses-api
        aitlc stepatlas info select --group pages --keyword when
        aitlc stepatlas info --url "https://app.example.com/admin/accounts/settings/directMail"
    """
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    catalog_path = stepatlas_path / "catalog.json"
    if not catalog_path.is_file():
        typer.echo(
            json.dumps(
                {
                    "error": f"{catalog_path} not found",
                    "hint": "run `aitlc stepatlas build` first",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    steps = json.loads(catalog_path.read_text())["steps"]

    file_line = re.match(r"^(.+):(\d+)$", query) if query else None
    matches: list[dict]
    url_match: list[dict] | None = None
    if file_line:
        file_arg, line = file_line.group(1), int(file_line.group(2))
        exact = [s for s in steps if _matches_file_line(s, file_arg, line)]
        if exact:
            matches = exact
        else:
            nearest = _nearest_before(steps, file_arg, line)
            matches = [nearest] if nearest else []
    else:
        filters_given = any(
            v is not None for v in (page, group, keyword, uses_api, file, url)
        )
        if not query and not filters_given:
            typer.echo(
                json.dumps(
                    {
                        "error": "provide a query, a file:line, or at least one of "
                        "--page/--group/--keyword/--uses-api/--file/--url"
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        matches = steps
        if url is not None:
            matches, url_match = _filter_by_url(matches, url)
            if not url_match:
                typer.echo(
                    json.dumps(
                        {
                            "query": query,
                            "url": url,
                            "url_match": [],
                            "count": 0,
                            "matches": [],
                            "hint": "no category's slug/label shares a token with "
                            "this URL's path -- try --page with a text fragment "
                            "instead",
                        }
                    ),
                    err=True,
                )
                raise typer.Exit(code=2)
        if page is not None:
            matches = _filter_by_page(matches, page)
        if group is not None:
            matches = _filter_by_group(matches, group)
        if keyword is not None:
            matches = _filter_by_keyword(matches, keyword)
        if uses_api is not None:
            matches = _filter_by_uses_api(matches, uses_api)
        if file is not None:
            matches = _filter_by_file(matches, file)
        if query:
            matches = _filter_by_text(matches, query)

    if not matches:
        typer.echo(json.dumps({"query": query, "count": 0, "matches": []}), err=True)
        raise typer.Exit(code=2)

    truncated = len(matches) > _MAX_MATCHES
    payload = {
        "query": query,
        **({"url": url, "url_match": url_match} if url_match is not None else {}),
        "count": len(matches),
        "truncated": truncated,
        "matches": matches[:_MAX_MATCHES],
    }
    typer.echo(json.dumps(payload, indent=2))


# Mounted by commands/_registry.py.
COMMAND = {"name": "stepatlas", "attr": "app", "kind": "group", "order": 250}

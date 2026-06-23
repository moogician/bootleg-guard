#!/usr/bin/env python3
"""
bootlegg — polyfill.io and Funnull CDN malware scanner

Given a URL, bootlegg:
  1. Fetches the live page (mobile + desktop UA) and scans for malicious CDN references.
  2. If the URL is a *.github.io page, searches GitHub for the backing source repo and
     scans all matching source files via the GitHub code-search API.
  3. Crawls linked pages on the live site (BFS, up to --max-pages, default 30).
  4. Reports all findings with context snippets. Exits with code 1 if infected.

Detected CDN families
---------------------
  polyfill.io   — acquired by Funnull Feb 2024, injected malware Jun 2024
  cdn.polyfill.io, polyfill.cn, polyfill.com (mirror / typosquat)
  bootcss.com   — confirmed Funnull operator (malicious since Jun 2023)
  bootcdn.net   — confirmed Funnull operator
  staticfile.org / staticfile.net — confirmed Funnull (OFAC-sanctioned May 2025)
  Typosquat fronts: jquecy.com, jsdclivr.com, clondflare.com, bytedauce.com,
                    bdustatic.com, ailyunoss.com
  Post-sanction CDNs (Jun 2025+): cdn1.ai, bolecnd.com, yunray.ai, cdn5.com, ctgcdn.com
  C2 infrastructure: macoms.la, unionadjs.com, xhsbpza.com, newcrbpc.com

Usage
-----
    bootlegg https://user.github.io/repo/
    bootlegg https://user.github.io/ --token ghp_xxx
    bootlegg https://example.com --no-github
    bootlegg https://user.github.io/ --max-pages 1   # single page only
    bootlegg https://user.github.io/ --json          # JSON output

GitHub token
------------
Optional but strongly recommended — raises GitHub Search API from 10 → 30 req/min.
Pass via --token or export GITHUB_TOKEN=ghp_...
https://github.com/settings/tokens  (no scopes needed for public repos)
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import deque
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from bootlegg import __version__


# ---------------------------------------------------------------------------
# CDN patterns
# ---------------------------------------------------------------------------

_POLYFILL_FAMILY: list[tuple[str, str, re.Pattern]] = [
    ("polyfill", "polyfill.io",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*polyfill\.io(?=[/:?#'\"\s]|$)", re.I)),
    ("polyfill", "polyfill.cn",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*polyfill\.cn(?=[/:?#'\"\s]|$)", re.I)),
    ("polyfill", "polyfill.com", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*polyfill\.com(?=[/:?#'\"\s]|$)", re.I)),
]

_FUNNULL_CONFIRMED: list[tuple[str, str, re.Pattern]] = [
    ("funnull_confirmed", "bootcss.com",    re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*bootcss\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("funnull_confirmed", "bootcdn.net",    re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*bootcdn\.net(?=[/:?#'\"\s]|$)", re.I)),
    ("funnull_confirmed", "staticfile.org", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*staticfile\.org(?=[/:?#'\"\s]|$)", re.I)),
    ("funnull_confirmed", "staticfile.net", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*staticfile\.net(?=[/:?#'\"\s]|$)", re.I)),
]

_TYPOSQUATS: list[tuple[str, str, re.Pattern]] = [
    ("typosquat", "jquecy.com",     re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*jquecy\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("typosquat", "jsdclivr.com",   re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*jsdclivr\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("typosquat", "clondflare.com", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*clondflare\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("typosquat", "bytedauce.com",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*bytedauce\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("typosquat", "bdustatic.com",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*bdustatic\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("typosquat", "ailyunoss.com",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*ailyunoss\.com(?=[/:?#'\"\s]|$)", re.I)),
]

_POST_SANCTION: list[tuple[str, str, re.Pattern]] = [
    ("post_sanction", "cdn1.ai",     re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*cdn1\.ai(?=[/:?#'\"\s]|$)", re.I)),
    ("post_sanction", "bolecnd.com", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*bolecnd\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("post_sanction", "yunray.ai",   re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*yunray\.ai(?=[/:?#'\"\s]|$)", re.I)),
    ("post_sanction", "cdn5.com",    re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*cdn5\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("post_sanction", "ctgcdn.com",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*ctgcdn\.com(?=[/:?#'\"\s]|$)", re.I)),
]

_C2_INFRA: list[tuple[str, str, re.Pattern]] = [
    ("c2_infra", "macoms.la",     re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*macoms\.la(?=[/:?#'\"\s]|$)", re.I)),
    ("c2_infra", "unionadjs.com", re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*unionadjs\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("c2_infra", "xhsbpza.com",   re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*xhsbpza\.com(?=[/:?#'\"\s]|$)", re.I)),
    ("c2_infra", "newcrbpc.com",  re.compile(r"(?:https?:)?//(?:[a-z0-9-]+\.)*newcrbpc\.com(?=[/:?#'\"\s]|$)", re.I)),
]

ALL_PATTERNS: list[tuple[str, str, re.Pattern]] = (
    _POLYFILL_FAMILY + _FUNNULL_CONFIRMED + _TYPOSQUATS + _POST_SANCTION + _C2_INFRA
)


def find_matches(text: str) -> list[dict]:
    results = []
    seen: set[tuple[str, str]] = set()
    for family, name, pat in ALL_PATTERNS:
        for m in pat.finditer(text):
            val = m.group()
            if (name, val) in seen:
                continue
            seen.add((name, val))
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 100)
            results.append({
                "family": family,
                "cdn": name,
                "value": val,
                "context": text[start:end].replace("\n", " ").strip(),
            })
    return results


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
RESULTS_PER_PAGE = 100
MAX_SEARCH_PAGES = 10

_GH_SEARCH_QUERIES = [
    "polyfill.io",
    "bootcss.com",
    "bootcdn.net",
    "staticfile.org",
    "staticfile.net",
    "jquecy.com OR jsdclivr.com OR clondflare.com",
    "cdn1.ai OR bolecnd.com OR yunray.ai",
    "macoms.la OR unionadjs.com",
]


def github_io_repo(url: str) -> Optional[tuple[str, str, str]]:
    """
    Parse a github.io URL into (owner, repo, live_root_url).
    Returns None for non-github.io URLs.

    https://owner.github.io/        → owner/owner.github.io, root /
    https://owner.github.io/repo/  → owner/repo, root /repo/
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host.endswith(".github.io"):
        return None
    owner = host[: -len(".github.io")]
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if path_parts:
        repo = path_parts[0]
        live_url = f"https://{host}/{repo}/"
    else:
        repo = f"{owner}.github.io"
        live_url = f"https://{host}/"
    return owner, repo, live_url


class RateLimiter:
    def __init__(self, rate: float, per: float = 60.0):
        self._rate = rate
        self._per = per
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / self._per))
            if self._tokens < 1:
                wait = (1 - self._tokens) * (self._per / self._rate)
                logging.debug("Rate-limit sleep %.2fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


async def _gh_request(
    session: aiohttp.ClientSession,
    rl: RateLimiter,
    headers: dict,
    url: str,
    params: dict,
) -> Optional[dict]:
    for attempt in range(4):
        await rl.acquire()
        try:
            async with session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in (403, 429):
                    reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                    wait = max(5.0, reset - time.time() + 2)
                    logging.warning("GitHub rate-limited; sleeping %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status in (422, 404):
                    return None
                await asyncio.sleep(2 ** attempt)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt)
    return None


async def scan_github_source(
    session: aiohttp.ClientSession,
    rl: RateLimiter,
    headers: dict,
    owner: str,
    repo: str,
) -> list[dict]:
    """Search GitHub code for all known malicious CDN refs in owner/repo."""
    seen_files: set[str] = set()
    source_matches = []
    for qterm in _GH_SEARCH_QUERIES:
        query = f"{qterm} repo:{owner}/{repo}"
        for page in range(1, MAX_SEARCH_PAGES + 1):
            data = await _gh_request(
                session, rl, headers,
                f"{GITHUB_API}/search/code",
                {"q": query, "per_page": RESULTS_PER_PAGE, "page": page},
            )
            if not data:
                break
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                fpath = item.get("path", "")
                if fpath in seen_files:
                    continue
                seen_files.add(fpath)
                source_matches.append({
                    "file": fpath,
                    "html_url": item.get("html_url", ""),
                    "name": item.get("name", ""),
                })
            if len(items) < RESULTS_PER_PAGE:
                break
    return source_matches


# ---------------------------------------------------------------------------
# Live site crawler
# ---------------------------------------------------------------------------

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.144 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT = 15


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if href and not href.startswith(("#", "mailto:", "javascript:")):
            self.links.append(urljoin(self.base_url, href))


def _same_origin(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


async def _fetch_page(session: aiohttp.ClientSession, url: str) -> tuple[str, int, Optional[str]]:
    for ua in (MOBILE_UA, DESKTOP_UA):
        try:
            async with session.get(
                url,
                headers={"User-Agent": ua},
                timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT),
                allow_redirects=True,
                ssl=False,
            ) as resp:
                html = await resp.text(errors="replace")
                if resp.status == 404:
                    return html, 404, None
                if resp.status >= 400:
                    if ua == DESKTOP_UA:
                        return html, resp.status, f"HTTP {resp.status}"
                    continue
                return html, resp.status, None
        except asyncio.TimeoutError:
            if ua == DESKTOP_UA:
                return "", 0, "timeout"
        except aiohttp.ClientConnectorError as exc:
            return "", 0, f"connection error: {exc.os_error}"
        except aiohttp.ClientError as exc:
            return "", 0, f"{type(exc).__name__}: {str(exc)[:80]}"
    return "", 0, "all UA attempts failed"


async def crawl_site(
    session: aiohttp.ClientSession,
    start_url: str,
    max_pages: int = 30,
) -> dict:
    """BFS crawl, collecting CDN matches from each page."""
    queue: deque[str] = deque([start_url])
    visited: set[str] = set()
    pages_checked: list[str] = []
    infected_pages: list[dict] = []
    errors: list[str] = []
    pages_up = 0

    while queue and len(visited) < max_pages:
        url = queue.popleft().split("#")[0].rstrip("/") or start_url
        if url in visited:
            continue
        visited.add(url)

        html, status, err = await _fetch_page(session, url)
        pages_checked.append(url)

        if err and status == 0:
            errors.append(f"{url}: {err}")
            continue

        matches = find_matches(html)
        if matches:
            infected_pages.append({"url": url, "matches": matches})

        if status in (404,) or status >= 400:
            if status not in (0, 404):
                errors.append(f"{url}: HTTP {status}")
            continue

        pages_up += 1
        if len(visited) < max_pages:
            parser = _LinkParser(url)
            try:
                parser.feed(html)
            except Exception:
                pass
            for link in parser.links:
                norm = link.split("#")[0].rstrip("/")
                if _same_origin(link, start_url) and norm not in visited:
                    queue.append(link)

    verdict = "infected" if infected_pages else ("site_down" if pages_up == 0 else "clean")
    return {
        "verdict": verdict,
        "pages_crawled": len(pages_checked),
        "pages_up": pages_up,
        "pages_with_cdns": len(infected_pages),
        "infected_pages": infected_pages,
        "errors": errors[:5],
    }


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

async def check(url: str, token: Optional[str], max_pages: int, no_github: bool) -> dict:
    gh_headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"bootlegg/{__version__} (defensive security research)",
    }
    if token:
        gh_headers["Authorization"] = f"token {token}"

    search_rate = 25.0 if token else 9.0
    rl = RateLimiter(rate=search_rate, per=60.0)

    result: dict = {
        "url": url,
        "version": __version__,
        "github_info": None,
        "source_scan": None,
        "live_scan": None,
        "summary": {},
    }

    gh_info = github_io_repo(url) if not no_github else None
    if gh_info:
        owner, repo, live_root = gh_info
        result["github_info"] = {
            "owner": owner,
            "repo": repo,
            "github_url": f"https://github.com/{owner}/{repo}",
            "live_root": live_root,
        }

    connector = aiohttp.TCPConnector(limit=16, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        if gh_info and not no_github:
            owner, repo, live_root = gh_info
            logging.info("Scanning GitHub source: %s/%s", owner, repo)
            source_matches = await scan_github_source(session, rl, gh_headers, owner, repo)
            result["source_scan"] = {
                "repo": f"{owner}/{repo}",
                "files_matched": len(source_matches),
                "matches": source_matches,
            }
            crawl_url = live_root
        else:
            crawl_url = url

        logging.info("Crawling live site: %s  (max %d pages)", crawl_url, max_pages)
        live = await crawl_site(session, crawl_url, max_pages=max_pages)
        result["live_scan"] = live

    all_cdns: set[str] = set()
    all_families: set[str] = set()
    for page in live.get("infected_pages", []):
        for m in page.get("matches", []):
            all_cdns.add(m["cdn"])
            all_families.add(m["family"])

    src_infected = bool(result["source_scan"] and result["source_scan"]["files_matched"] > 0)
    live_infected = live["verdict"] == "infected"

    result["summary"] = {
        "infected": src_infected or live_infected,
        "source_infected": src_infected,
        "live_infected": live_infected,
        "cdn_families_found": sorted(all_families),
        "cdns_found": sorted(all_cdns),
        "pages_crawled": live["pages_crawled"],
        "infected_pages": live["pages_with_cdns"],
    }
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_report(result: dict) -> None:
    s = result["summary"]
    W = 62
    print(f"\n{'─' * W}")
    print(f"  bootlegg v{result['version']}  —  CDN malware scanner")
    print(f"  {result['url']}")
    print(f"{'─' * W}")

    if s["infected"]:
        print("\n  ✗  INFECTED")
    else:
        print("\n  ✓  Clean (no malicious CDNs detected)")

    if result["github_info"]:
        gi = result["github_info"]
        print(f"\n  GitHub:    {gi['github_url']}")
        print(f"  Live root: {gi['live_root']}")

    ss = result["source_scan"]
    if ss:
        print(f"\n  Source scan  ({ss['repo']})")
        print(f"    Files with CDN references: {ss['files_matched']}")
        for m in ss["matches"][:8]:
            print(f"    · {m['file']}")
            print(f"      {m['html_url']}")
        if len(ss["matches"]) > 8:
            print(f"    … and {len(ss['matches']) - 8} more")

    ls = result["live_scan"]
    print(f"\n  Live crawl")
    print(f"    Pages crawled:  {ls['pages_crawled']}")
    print(f"    Pages up:       {ls['pages_up']}")
    print(f"    Infected pages: {ls['pages_with_cdns']}")

    if s["cdns_found"]:
        print(f"\n  CDNs detected:")
        for cdn in s["cdns_found"]:
            fam = next((f for f, n, _ in ALL_PATTERNS if n == cdn), "unknown")
            print(f"    [{fam}]  {cdn}")

    if ls.get("infected_pages"):
        print(f"\n  Infected pages (first 5):")
        for page in ls["infected_pages"][:5]:
            print(f"\n    {page['url']}")
            for m in page["matches"][:3]:
                print(f"      [{m['family']}]  {m['value']}")
                print(f"      context: …{m['context'][:110]}…")

    if ls.get("errors"):
        print(f"\n  Errors ({len(ls['errors'])}):")
        for e in ls["errors"][:3]:
            print(f"    {e}")

    print(f"\n{'─' * W}\n")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="bootlegg",
        description="Detect polyfill.io and Funnull CDN malware on websites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="URL to check")
    parser.add_argument("--version", "-V", action="version", version=f"bootlegg {__version__}")
    parser.add_argument(
        "--token", "-t", metavar="TOKEN",
        help="GitHub personal access token (or set GITHUB_TOKEN). Raises rate limit from 10 → 30 req/min.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=30, metavar="N",
        help="Max pages to crawl per site (default: 30). Use 1 for single-page check.",
    )
    parser.add_argument(
        "--no-github", action="store_true",
        help="Skip GitHub source search even for github.io URLs.",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_out",
        help="Output full results as JSON.",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token and not args.no_github and github_io_repo(args.url):
        print(
            "NOTE: No GitHub token — source scan rate-limited to 10 req/min.\n"
            "      Pass --token or export GITHUB_TOKEN=ghp_... for 30 req/min.\n",
            file=sys.stderr,
        )

    result = await check(args.url, token, args.max_pages, args.no_github)

    if args.json_out:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)

    sys.exit(1 if result["summary"]["infected"] else 0)


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()

# bootlegg

Detect GitHub Pages sites loading scripts from Funnull-controlled CDNs —
polyfill.io, BootCSS, BootCDN, Staticfile, and a growing set of typosquat fronts.

**[Check your site →](https://hwang628.github.io/bootleg/)**

Our scan found **1,960 GitHub Pages sites** still loading malicious CDN scripts as of June 2026:
786 via polyfill.io (weaponized June 2024), 1,191 via Funnull's BootCSS / BootCDN / Staticfile
CDNs (malicious since June 2023, OFAC-sanctioned May 2025). Infected sites collectively carry
over 530,000 GitHub stars — including microsoft/AirSim (18k ⭐), deeplearning-ai/machine-learning-yearning-cn
(7.8k ⭐), and CyC2018/CS-Notes (184k ⭐), the primary technical interview reference for Chinese
software engineers.

---

## Install

```
pip install bootlegg
```

Or run directly without installing:

```
python3 -m bootlegg https://user.github.io/repo/
```

## Usage

```
bootlegg https://user.github.io/repo/
```

For github.io URLs, bootlegg automatically finds the source repo and runs two checks:

1. **Source scan** — searches GitHub Code API for CDN references in the repo's files
2. **Live crawl** — fetches the site (mobile UA + desktop fallback), walks linked pages
   up to `--max-pages` (default: 30), and scans each for malicious script tags

```
# GitHub token raises source scan from 10 → 30 req/min
bootlegg https://user.github.io/repo/ --token ghp_xxx
# or: export GITHUB_TOKEN=ghp_xxx

# Any site (no GitHub source search)
bootlegg https://example.com --no-github

# Single-page check, no crawl
bootlegg https://user.github.io/ --max-pages 1

# JSON output for scripting; exits 1 if infected
bootlegg https://user.github.io/ --json | jq .summary
```

## What it detects

| CDN | Status | Notes |
|-----|--------|-------|
| polyfill.io | **Malicious** | Acquired by Funnull Feb 2024; malware injected Jun 2024 |
| cdn.polyfill.io | **Malicious** | Same domain, different subdomain |
| polyfill.cn / polyfill.com | **Malicious** | Mirror / typosquat |
| bootcss.com | **Malicious** | Confirmed Funnull operator; malicious since Jun 2023 |
| bootcdn.net | **Malicious** | Confirmed Funnull operator |
| staticfile.org / staticfile.net | **Malicious** | Confirmed Funnull; OFAC-sanctioned May 2025 |
| jquecy.com | **Malicious** | Typosquats jQuery |
| jsdclivr.com | **Malicious** | Typosquats jsDelivr |
| clondflare.com | **Malicious** | Typosquats Cloudflare |
| bytedauce.com | **Malicious** | Typosquats ByteDance |
| bdustatic.com | **Malicious** | Typosquats BDU Static |
| ailyunoss.com | **Malicious** | Typosquats Alibaba Cloud OSS |
| cdn1.ai | **Suspected** | Post-sanction Funnull front, stood up Jun 2025 |
| bolecnd.com | **Suspected** | Post-sanction Funnull CDN front |
| yunray.ai | **Suspected** | Post-sanction Funnull CDN front |
| cdn5.com | **Suspected** | Post-sanction Funnull CDN front |
| ctgcdn.com | **Suspected** | Post-sanction Funnull CDN front |
| macoms.la / unionadjs.com | **C2 infra** | Funnull redirect / C2 infrastructure |

## Fix

Remove `<script>` tags referencing any of these CDNs.

For polyfill.io specifically: most use cases are unnecessary in modern browsers.
If you do need a polyfill, use [Fastly's drop-in mirror](https://polyfill-fastly.io/v3/polyfill.min.js)
or bundle it with your build tool.

## Scan data

[`infected_sites.md`](infected_sites.md) — 1,960 GitHub Pages sites confirmed
loading malicious CDN scripts across two June 2026 scans (subdomain BFS crawl up to 30 pages
per site + Sourcegraph-based discovery).

## Background

In February 2024, the polyfill.io domain was acquired by Funnull Technology Inc.,
a Chinese CDN operator. In June 2024, Cloudflare and Sansec discovered that Funnull
had modified the served JavaScript to inject malware targeting mobile browsers —
redirecting users to gambling and adult sites via fake browser-update popups.
Over 100,000 sites were affected globally at peak.

Sansec and Censys later confirmed (via shared Cloudflare account credentials) that
BootCSS, BootCDN, and Staticfile are operated by the same entity and had been
injecting malicious code since at least June 2023, a year before the polyfill
incident became public. The US Treasury sanctioned Funnull / Triad Nexus in May 2025.

References:
- [Sansec: polyfill.io supply chain attack](https://sansec.io/research/polyfill-supply-chain-attack)
- [Cloudflare: polyfill.io now available on cdnjs](https://blog.cloudflare.com/polyfill-io-now-available-on-cdnjs-reduce-your-supply-chain-risk)
- [OFAC sanction: Funnull / Triad Nexus](https://ofac.treasury.gov/recent-actions/20250515)

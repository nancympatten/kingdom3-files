#!/usr/bin/env python3
"""Build a static file-format directory and optionally submit URLs to IndexNow.

Uses only the Python standard library. Run from the directory containing data.csv:
    python3 build.py
    python3 build.py --ping
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# HARDCODED CONFIGURATION — intentionally kept together and unchanged.
# ---------------------------------------------------------------------------
DOMAIN = "file-format-atlas.netlify.app"
BASE_URL = "https://file-format-atlas.netlify.app"
INDEXNOW_KEY = "b223b6b305c1482abbf0ed50c7ff86f7"
CSV_FILE = "data.csv"
OUTPUT_DIR = "dist"
STATE_FILE = "submitted_urls.json"
DRIP_BATCH_SIZE = 250

REQUIRED_HEADERS = (
    "slug", "title", "meta_description", "h1", "category", "summary",
    "data_table_html", "widget_html", "faq_q1", "faq_a1", "faq_q2", "faq_a2",
)
SITEMAP_LIMIT = 5000
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

CSS = r"""
:root{color-scheme:dark;--bg:#071321;--panel:#0d2033;--panel2:#10283d;--line:#1d3b52;--text:#e7f2fa;--muted:#9db4c6;--cyan:#42d9ee;--blue:#65aaff;--good:#8be3c0}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(ellipse at 15% 0%,#102c43 0,transparent 42%),var(--bg);color:var(--text);font:16px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--cyan);text-decoration:none}a:hover{text-decoration:underline;color:#a4f4ff}
header,.wrap,footer{width:min(1120px,calc(100% - 36px));margin-inline:auto}header{padding:28px 0 18px;border-bottom:1px solid var(--line)}
.brand{font-weight:800;letter-spacing:.02em;font-size:1.15rem;color:var(--text)}.brand span{color:var(--cyan)}
nav{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:.92rem;color:var(--muted)}
.wrap{padding:34px 0 60px}h1{font-size:clamp(2rem,5vw,3.35rem);line-height:1.12;letter-spacing:-.035em;margin:10px 0 20px}
h2{font-size:1.45rem;line-height:1.25;margin-top:38px}h3{font-size:1.08rem}p{color:#c7d7e4}
.eyebrow,.freshness{color:var(--good);font-size:.82rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.ai-tldr{background:linear-gradient(120deg,#103449,#10273d);border:1px solid #236078;border-left:4px solid var(--cyan);border-radius:14px;padding:20px 22px;margin:22px 0 26px;color:#eafaff}
.panel,.ad-unit,.result-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:20px 0}
.ad-unit{min-height:90px;display:flex;align-items:center;justify-content:center;color:#607b90;border-style:dashed;font-size:.8rem}
table{width:100%;border-collapse:collapse;overflow:hidden;border-radius:10px;background:#0b1c2b}td,th{padding:12px 14px;border:1px solid var(--line);text-align:left}th{color:var(--cyan);background:#10283d}
input[type=search]{width:100%;background:#081a29;border:1px solid #31516a;border-radius:10px;color:var(--text);padding:14px 16px;font:inherit;outline:none}input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px #42d9ee22}
.pills{display:flex;flex-wrap:wrap;gap:9px;margin:14px 0 22px}.pill{border:1px solid #31516a;border-radius:999px;padding:7px 13px;background:#0b1c2b;color:#cfe2ef;cursor:pointer}.pill[aria-pressed=true]{background:#12445a;border-color:var(--cyan);color:white}
.results{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:14px}.result-card{margin:0;padding:16px}.result-card h3{margin:0 0 6px}.result-card p{font-size:.9rem;margin:0}
.related{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.related a{padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
footer{border-top:1px solid var(--line);padding:24px 0 38px;color:var(--muted);font-size:.9rem}
code{background:#10283d;padding:2px 6px;border-radius:5px;color:#a9eaf2}
@media(max-width:600px){header,.wrap,footer{width:min(100% - 26px,1120px)}.wrap{padding-top:24px}td,th{padding:9px;font-size:.9rem}}
"""

LIVE_SCRIPT = r"""
<script>
(()=>{const now=new Date();document.querySelectorAll('.live-year').forEach(e=>e.textContent=now.getFullYear());
const month=now.toLocaleDateString(undefined,{month:'long',year:'numeric'});
document.querySelectorAll('.live-updated').forEach(e=>e.textContent=month);})();
</script>
"""

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)

def clean_slug(value: str) -> str:
    """Return a URL-safe, non-empty, single path segment."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    if not value:
        raise ValueError("A CSV row has an empty/invalid slug.")
    return value

def load_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"Required input file not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [name for name in REQUIRED_HEADERS if name not in headers]
        if missing:
            raise ValueError("data.csv is missing required headers: " + ", ".join(missing))
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for line_number, raw in enumerate(reader, start=2):
            row = {key: (raw.get(key) or "").strip() for key in REQUIRED_HEADERS}
            if not any(row.values()):
                continue
            row["slug"] = clean_slug(row["slug"])
            if row["slug"] in seen:
                raise ValueError(f"Duplicate slug {row['slug']!r} at CSV line {line_number}.")
            seen.add(row["slug"])
            if not row["title"] or not row["h1"]:
                raise ValueError(f"CSV line {line_number} needs both title and h1.")
            rows.append(row)
    return rows

def page_url(slug: str | None = None) -> str:
    return BASE_URL + (f"/{quote(slug, safe='-')}/" if slug else "/")

def json_ld_script(graph: list[dict[str, Any]]) -> str:
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph},
                         ensure_ascii=False, separators=(",", ":"))
    # Prevent a data value from terminating the script element.
    payload = payload.replace("</", "<\\/")
    return '<script type="application/ld+json">' + payload + '</script>'

def structured_data(row: dict[str, str], url: str, year: int) -> str:
    title, summary = row["title"], row["summary"]
    graph: list[dict[str, Any]] = [{
        "@type": "TechArticle",
        "@id": url + "#article",
        "headline": title,
        "description": row["meta_description"] or summary,
        "articleSection": row["category"],
        "mainEntityOfPage": url,
        "datePublished": date.today().isoformat(),
        "dateModified": date.today().isoformat(),
        "author": {"@type": "Organization", "name": "File Format Atlas"},
        "publisher": {"@type": "Organization", "name": "File Format Atlas"},
        "inLanguage": "en",
    }]
    questions = []
    for index in (1, 2):
        question, answer = row[f"faq_q{index}"], row[f"faq_a{index}"]
        if question and answer:
            questions.append({
                "@type": "Question", "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            })
    if questions:
        graph.append({"@type": "FAQPage", "mainEntity": questions})
    steps = [
        {"@type": "HowToStep", "name": "Identify the file format",
         "text": f"Confirm that the file uses the {title} format."},
        {"@type": "HowToStep", "name": "Choose a compatible application",
         "text": f"Use software that supports {title}; check the application's documentation."},
        {"@type": "HowToStep", "name": "Open or convert a copy",
         "text": "Open a copy of the file or convert a duplicate, then verify the result."},
    ]
    graph.append({
        "@type": "HowTo", "name": f"How to work with {title}",
        "description": summary or f"Basic steps for identifying and working with {title}.",
        "step": steps,
    })
    return json_ld_script(graph)

def common_head(title: str, description: str, canonical: str,
                structured: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<meta name="twitter:card" content="summary">
<style>{CSS}</style>
{structured}
</head>"""

def shell(title: str, body: str, description: str, canonical: str,
          structured: str = "") -> str:
    return (common_head(title, description, canonical, structured) +
            f"""<body>
<header><a class="brand" href="{BASE_URL}/">FILE FORMAT <span>ATLAS</span></a>
<nav><a href="{BASE_URL}/">Directory</a><a href="{BASE_URL}/sitemap.xml">Sitemap</a></nav></header>
<main class="wrap">{body}</main>
<footer>© <span class="live-year"></span> File Format Atlas · Updated <span class="live-updated"></span></footer>
{LIVE_SCRIPT}
</body></html>""")

def render_page(row: dict[str, str], related: list[dict[str, str]], year: int) -> str:
    url = page_url(row["slug"])
    title = f"{row['title']} Guide, Details & Uses ({year})"
    description = row["meta_description"] or row["summary"] or row["title"]
    summary = row["summary"] or description
    related_html = "".join(
        f'<a href="{esc(page_url(item["slug"]))}">{esc(item["title"])}</a>'
        for item in related
    )
    faq_html = ""
    for index in (1, 2):
        q, a = row[f"faq_q{index}"], row[f"faq_a{index}"]
        if q and a:
            faq_html += f'<section><h3>{esc(q)}</h3><p>{esc(a)}</p></section>'
    body = f"""
<div class="eyebrow">{esc(row['category'])} · File format guide</div>
<h1>{esc(row['h1'])}</h1>
<div class="freshness">Reviewed for <span class="live-year">{year}</span> · Updated <span class="live-updated"></span></div>
<div class="ai-tldr"><strong>AI Quick Answer</strong><p>{esc(summary)}</p></div>
<div class="ad-unit" aria-label="Advertisement"></div>
<section><h2>Overview</h2><p>{esc(summary)}</p></section>
{f'<section><h2>Format details</h2><div class="panel">{row["data_table_html"]}</div></section>' if row["data_table_html"] else ''}
{f'<section><h2>Interactive tool</h2><div class="panel">{row["widget_html"]}</div></section>' if row["widget_html"] else ''}
{f'<section><h2>Frequently asked questions</h2>{faq_html}</section>' if faq_html else ''}
<section><h2>Explore Related {esc(row['category'])} File Formats</h2>
<div class="related">{related_html or '<p>No related guides are available yet.</p>'}</div></section>
<div class="ad-unit" aria-label="Advertisement"></div>
"""
    return shell(title, body, description, url, structured_data(row, url, year))

def render_home(rows: list[dict[str, str]], year: int) -> str:
    categories = sorted({r["category"] for r in rows if r["category"]})
    body = f"""
<div class="eyebrow">Technical documentation · Global directory</div>
<h1>File Format Atlas</h1>
<p>Search practical guides to file extensions, formats, compatibility and usage.</p>
<div class="panel">
<label for="atlas-search"><strong>Search all file formats</strong></label>
<input id="atlas-search" type="search" placeholder="Search by extension, name or description…" autocomplete="off">
<div class="pills" id="category-pills"><button class="pill" data-category="all" aria-pressed="true">All formats</button>
{''.join(f'<button class="pill" data-category="{esc(c)}" aria-pressed="false">{esc(c)}</button>' for c in categories)}</div>
<div id="search-status" aria-live="polite"></div>
<div class="results" id="search-results"></div>
<noscript><p>JavaScript is required for instant directory search. You can still use the sitemap.</p></noscript>
</div>
<div class="ad-unit" aria-label="Advertisement"></div>
<p>Browse {len(rows):,} format guides. Content is maintained with a rolling year indicator.</p>
<div class="ad-unit" aria-label="Advertisement"></div>
<script>
(async()=>{{
 const input=document.getElementById('atlas-search'),results=document.getElementById('search-results');
 const status=document.getElementById('search-status'),pills=[...document.querySelectorAll('[data-category]')];
 let records=[],category='all';
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
 function draw(){{
  const q=input.value.trim().toLowerCase();
  const found=records.filter(r=>(category==='all'||r.category===category)&&(!q||(r.title+' '+r.slug+' '+r.category+' '+r.description).toLowerCase().includes(q))).slice(0,150);
  results.innerHTML=found.map(r=>`<a class="result-card" href="${{esc(r.url)}}"><h3>${{esc(r.title)}}</h3><p>${{esc(r.description)}}</p></a>`).join('');
  status.textContent=found.length+' result'+(found.length===1?'':'s')+(records.length>150&&found.length===150?' (showing first 150)':'');
 }}
 try{{const response=await fetch('/search-index.json');if(!response.ok)throw Error('Search index unavailable');records=await response.json();draw();}}
 catch(e){{status.textContent='Search index could not be loaded. Please try again later.';}}
 input.addEventListener('input',draw);
 pills.forEach(button=>button.addEventListener('click',()=>{{category=button.dataset.category;pills.forEach(p=>p.setAttribute('aria-pressed',String(p===button)));draw();}}));
}})();
</script>
"""
    structured = json_ld_script([{
        "@type": "WebSite", "name": "File Format Atlas", "url": page_url(),
        "dateModified": date.today().isoformat(),
    }])
    return shell(f"File Format Atlas — File Extension Directory ({year})",
                 body, "Search a directory of file format and file extension guides.",
                 page_url(), structured)

def write_sitemaps(out_dir: Path, urls: list[str]) -> None:
    today = date.today().isoformat()
    chunks = [urls[i:i + SITEMAP_LIMIT] for i in range(0, len(urls), SITEMAP_LIMIT)]
    if not chunks:
        chunks = [[]]
    sitemap_names = []
    for number, chunk in enumerate(chunks, start=1):
        name = f"sitemap-{number}.xml"
        sitemap_names.append(name)
        entries = "\n".join(
            "  <url><loc>" + esc(url) + "</loc><lastmod>" + today +
            "</lastmod><changefreq>weekly</changefreq><priority>0.8</priority></url>"
            for url in chunk
        )
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
               entries + '\n</urlset>\n')
        (out_dir / name).write_text(xml, encoding="utf-8")
    index_entries = "\n".join(
        f"  <sitemap><loc>{esc(BASE_URL + '/' + name)}</loc><lastmod>{today}</lastmod></sitemap>"
        for name in sitemap_names
    )
    index_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
                 index_entries + '\n</sitemapindex>\n')
    (out_dir / "sitemap.xml").write_text(index_xml, encoding="utf-8")
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n",
        encoding="utf-8")

def build() -> list[str]:
    rows = load_rows(Path(CSV_FILE))
    year = datetime.now().year
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    Path("netlify.toml").write_text(
        '[build]\n  command = "python3 build.py"\n  publish = "dist"\n',
        encoding="utf-8")

    (out_dir / f"{INDEXNOW_KEY}.txt").write_text(INDEXNOW_KEY, encoding="utf-8")

    by_category: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    urls = [page_url()]
    for row in rows:
        folder = out_dir / row["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        related_pool = [r for r in by_category.get(row["category"], []) if r["slug"] != row["slug"]]
        related = related_pool[:6]
        (folder / "index.html").write_text(
            render_page(row, related, year), encoding="utf-8")
        urls.append(page_url(row["slug"]))

    search_index = [{
        "slug": row["slug"],
        "title": row["title"],
        "category": row["category"],
        "description": row["meta_description"] or row["summary"],
        "url": page_url(row["slug"]),
    } for row in rows]
    (out_dir / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    (out_dir / "index.html").write_text(
        render_home(rows, year), encoding="utf-8")
    write_sitemaps(out_dir, urls)
    return urls

def read_submitted(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, list) or any(not isinstance(x, str) for x in data):
        raise ValueError(f"{path} must contain a JSON array of URL strings.")
    return set(data)

def ping_indexnow() -> int:
    # Ensure the URL inventory and verification key file are current.
    urls = build()
    state_path = Path(STATE_FILE)
    submitted = read_submitted(state_path)
    batch = [url for url in urls if url not in submitted][:DRIP_BATCH_SIZE]
    if not batch:
        print("IndexNow: no unsubmitted URLs remain.")
        return 0

    payload = json.dumps({
        "host": DOMAIN,
        "key": INDEXNOW_KEY,
        "keyLocation": f"{BASE_URL}/{INDEXNOW_KEY}.txt",
        "urlList": batch,
    }).encode("utf-8")
    request = urllib.request.Request(
        INDEXNOW_ENDPOINT, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "FileFormatAtlas-IndexNow/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.getcode()
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"IndexNow returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"IndexNow request failed: {exc}") from exc

    if status not in (200, 202):
        raise RuntimeError(f"IndexNow returned unexpected HTTP status {status}.")
    submitted.update(batch)
    state_path.write_text(json.dumps(sorted(submitted), indent=2) + "\n",
                          encoding="utf-8")
    print(f"IndexNow: successfully submitted {len(batch)} URLs (HTTP {status}).")
    return len(batch)

def main() -> int:
    parser = argparse.ArgumentParser(description="Build File Format Atlas.")
    parser.add_argument("--ping", action="store_true",
                        help="build the site and submit the next 250 unsubmitted URLs to IndexNow")
    args = parser.parse_args()
    try:
        if args.ping:
            ping_indexnow()
        else:
            urls = build()
            print(f"Built {len(urls) - 1} format pages and homepage ({len(urls)} URLs) in {OUTPUT_DIR}/.")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

"""
The Backlog Threat (v5)
-----------------------
Emails a gaming report whenever there is something new:

  1. Newly "Overwhelmingly Positive" on Steam   (reported once, ever)
  2. Free to keep across stores, ranked by Steam rating
     (Epic direct + GamerPower: Steam, GOG, Prime, Ubisoft)
     Once per giveaway. Same game free again a year later IS reported;
     re-lists within 60 days are suppressed.
  3. Hidden gems: recent releases already highly rated with few reviews

Excluded everywhere: visual novels, dating sims, adult content,
itch.io / IndieGala / key-aggregator giveaways.

Steam matching (v4 fix): cached app-list index -> fuzzy match ->
live Steam store search fallback. Only new giveaways ever trigger a
live lookup, and results are cached for 30 days.

Env vars (GitHub secrets): EMAIL_USER, EMAIL_PASS, EMAIL_TO
Optional: SMTP_HOST (smtp.gmail.com), SMTP_PORT (587)
"""

import difflib
import gzip
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent

# ------------------------------------------------------------ state files
STATE_OP = HERE / "seen.json"
STATE_GEMS = HERE / "seen_gems.json"
STATE_GIVEAWAYS = HERE / "seen_giveaways.json"
STATE_TITLES = HERE / "notified_titles.json"
CACHE_FILE = HERE / "steam_cache.json"
MATCH_FILE = HERE / "title_matches.json"      # norm title -> appid or ""
APPLIST_FILE = HERE / "applist.json.gz"
META_FILE = HERE / "last_scan.json"
LIST_FILE = HERE / "op_games.md"

# ------------------------------------------------------------ endpoints
SEARCH_URL = "https://store.steampowered.com/search/results/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/"
APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
EPIC_URL = ("https://store-site-backend-static.ak.epicgames.com"
            "/freeGamesPromotions?locale=en-US&country=US&allowCountries=US")
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways?type=game"

# ------------------------------------------------------------ tuning
PAGE_SIZE = 50
MAX_PAGES_OP = 80
MAX_PAGES_GEMS = 40
REQUEST_DELAY = 1.5
HEAVY_SCAN_INTERVAL_H = 20
APPLIST_REFRESH_DAYS = 7
CACHE_TTL_DAYS = 30
TITLE_COOLDOWN_DAYS = 60
FUZZY_CUTOFF = 0.90

GEM_MIN_PCT = 85
GEM_MIN_REVIEWS = 50
GEM_MAX_REVIEWS = 2000

# Sort free games by rating (best first). Set False to group by store.
SORT_FREE_BY_RATING = True

# Stores whose giveaways we never want to hear about
SKIP_STORES = ("itch.io", "itchio", "indiegala", "indie gala", "keyhub",
               "alienware", "drm-free", "gleam", "giveaway of the day")
# Stores we DO want
KEEP_STORES = ("steam", "epic", "gog", "amazon", "ubisoft")

HEADERS = {"User-Agent": "Mozilla/5.0 (game-newsletter; personal use)"}

# Taste matching
LIKE_TERMS = ["action", "roguelike", "roguelite", "deckbuild", "rhythm",
              "platformer", "shooter", "metroidvania", "arcade", "racing",
              "bullet", "fast-paced", "hack and slash"]
DISLIKE_TERMS = ["hidden object", "idle", "clicker", "match 3", "casual",
                 "point & click"]

# Hard exclusions (never shown, any section)
EXCLUDE_TERMS = ["visual novel", "dating sim", "dating simulator", "hentai",
                 "eroge", "nsfw", "sexual content", "otome", "adult only",
                 "nudity"]
# Steam content descriptors: 3 = adult sexual content, 4 = frequent nudity
EXCLUDE_DESCRIPTOR_IDS = {3, 4}


# ================================================================ helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=0, sort_keys=True))


def normalize(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"\[.*?\]", " ", t)
    for junk in ["definitive edition", "deluxe edition", "goty edition",
                 "game of the year edition", "complete edition",
                 "enhanced edition", "collectors edition", "standard edition",
                 "remastered", "free game", "giveaway", "key", "drm free",
                 "epic games", "steam", "gog", "pc"]:
        t = t.replace(junk, " ")
    return re.sub(r"[^a-z0-9]+", "", t)


def meta_get(key: str):
    return load_json(META_FILE, {}).get(key)


def meta_set(key: str, value) -> None:
    meta = load_json(META_FILE, {})
    meta[key] = value
    save_json(META_FILE, meta)


def hours_since(iso: str | None) -> float:
    if not iso:
        return 99999.0
    try:
        return (now_utc() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        return 99999.0


# ================================================================ steam scans

def scan_steam_search(sort_by: str, max_pages: int):
    empty_streak = 0
    for page in range(max_pages):
        params = {"query": "", "start": page * PAGE_SIZE, "count": PAGE_SIZE,
                  "sort_by": sort_by, "category1": "998",
                  "supportedlang": "english", "json": "1", "infinite": "1"}
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS,
                             timeout=30)
            r.raise_for_status()
            blob = r.json().get("results_html", "")
        except Exception as e:
            print(f"WARN: search page {page} ({sort_by}): {e}", file=sys.stderr)
            time.sleep(10)
            continue
        soup = BeautifulSoup(blob, "html.parser")
        rows = soup.select("a.search_result_row")
        if not rows:
            return
        for row in rows:
            appid = row.get("data-ds-appid")
            if not appid:
                continue
            title_el = row.select_one("span.title")
            name = title_el.get_text(strip=True) if title_el else f"App {appid}"
            sum_el = row.select_one("span.search_review_summary")
            tooltip = sum_el.get("data-tooltip-html", "") if sum_el else ""
            yield appid, name, tooltip
        empty_streak = 0
        time.sleep(REQUEST_DELAY)


def parse_tooltip(tooltip: str):
    m = re.match(r"([A-Za-z ]+)<br>(\d+)% of the ([\d,]+) user reviews",
                 tooltip)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2)), int(m.group(3).replace(",", ""))


def fetch_op_games() -> dict[str, str]:
    op: dict[str, str] = {}
    misses = 0
    for appid, name, tooltip in scan_steam_search("Reviews_DESC", MAX_PAGES_OP):
        if tooltip.startswith("Overwhelmingly Positive"):
            op[appid] = name
            misses = 0
        else:
            misses += 1
            if misses > 150:
                break
    return op


def fetch_hidden_gems() -> dict[str, dict]:
    gems: dict[str, dict] = {}
    for appid, name, tooltip in scan_steam_search("Released_DESC",
                                                  MAX_PAGES_GEMS):
        parsed = parse_tooltip(tooltip)
        if not parsed:
            continue
        verdict, pct, total = parsed
        if (pct >= GEM_MIN_PCT and GEM_MIN_REVIEWS <= total <= GEM_MAX_REVIEWS
                and verdict in ("Very Positive", "Overwhelmingly Positive")):
            gems[appid] = {"name": name, "pct": pct, "total": total,
                           "verdict": verdict}
    return gems


# ================================================================ steam lookups

def fetch_steam_info(appid: str, cache: dict) -> dict:
    entry = cache.get(str(appid))
    if entry and hours_since(entry.get("ts")) < CACHE_TTL_DAYS * 24:
        return entry
    info: dict = {"ts": now_utc().isoformat()}
    try:
        r = requests.get(f"{APPREVIEWS_URL}{appid}",
                         params={"json": "1", "language": "all",
                                 "purchase_type": "all", "num_per_page": "0"},
                         headers=HEADERS, timeout=30)
        s = r.json().get("query_summary", {})
        total = s.get("total_reviews", 0)
        if total:
            info["pct"] = round(100 * s.get("total_positive", 0) / total)
            info["total"] = total
            info["verdict"] = s.get("review_score_desc", "")
    except Exception as e:
        print(f"WARN: reviews {appid}: {e}", file=sys.stderr)
    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(APPDETAILS_URL,
                         params={"appids": appid, "cc": "us", "l": "en"},
                         headers=HEADERS, timeout=30)
        data = r.json().get(str(appid), {})
        if data.get("success"):
            d = data["data"]
            info["name"] = d.get("name", "")
            info["genres"] = [g["description"] for g in d.get("genres", [])[:4]]
            info["desc"] = re.sub(r"<[^>]+>", "",
                                  d.get("short_description", "")).strip()
            info["price"] = ("Free" if d.get("is_free") else
                             d.get("price_overview", {})
                             .get("final_formatted", ""))
            info["release"] = d.get("release_date", {}).get("date", "")
            info["descriptors"] = (d.get("content_descriptors", {})
                                   .get("ids", []) or [])
    except Exception as e:
        print(f"WARN: details {appid}: {e}", file=sys.stderr)
    time.sleep(REQUEST_DELAY)
    cache[str(appid)] = info
    return info


def load_applist() -> dict[str, str]:
    fresh = hours_since(meta_get("applist_ts")) < APPLIST_REFRESH_DAYS * 24
    if APPLIST_FILE.exists() and fresh:
        try:
            idx = json.loads(gzip.decompress(APPLIST_FILE.read_bytes()).decode())
            print(f"App list index loaded from cache: {len(idx)} entries.")
            return idx
        except Exception:
            pass
    print("Refreshing Steam app list...")
    for attempt in (1, 2, 3):
        try:
            r = requests.get(APPLIST_URL, headers=HEADERS, timeout=180)
            r.raise_for_status()
            apps = r.json()["applist"]["apps"]
            break
        except Exception as e:
            print(f"WARN: applist attempt {attempt} failed: {e}",
                  file=sys.stderr)
            apps = None
            time.sleep(5 * attempt)
    if not apps:
        if APPLIST_FILE.exists():
            try:
                idx = json.loads(gzip.decompress(
                    APPLIST_FILE.read_bytes()).decode())
                print(f"WARN: using STALE app list ({len(idx)} entries).",
                      file=sys.stderr)
                return idx
            except Exception:
                pass
        print("WARN: no app list available; relying on live Steam search.",
              file=sys.stderr)
        return {}
    index: dict[str, str] = {}
    for app in apps:
        key = normalize(app.get("name", ""))
        if key:
            index.setdefault(key, str(app["appid"]))
    APPLIST_FILE.write_bytes(gzip.compress(
        json.dumps(index).encode(), compresslevel=9))
    meta_set("applist_ts", now_utc().isoformat())
    print(f"App list index built: {len(index)} entries.")
    return index


def steam_store_search(title: str) -> str | None:
    """Live Steam store search. Same thing you'd do by hand."""
    try:
        r = requests.get(STORESEARCH_URL,
                         params={"term": title, "l": "en", "cc": "us"},
                         headers=HEADERS, timeout=30)
        items = r.json().get("items", [])
    except Exception as e:
        print(f"WARN: store search {title!r}: {e}", file=sys.stderr)
        return None
    finally:
        time.sleep(REQUEST_DELAY)
    if not items:
        return None
    want = normalize(title)
    for it in items[:5]:
        if normalize(it.get("name", "")) == want:
            return str(it["id"])
    best = difflib.get_close_matches(
        want, [normalize(i.get("name", "")) for i in items[:5]],
        n=1, cutoff=0.80)
    if best:
        for it in items[:5]:
            if normalize(it.get("name", "")) == best[0]:
                return str(it["id"])
    return str(items[0]["id"])   # top hit is usually right


def match_appid(title: str, norm: str, applist: dict, matches: dict) -> str | None:
    """Index -> fuzzy index -> live search. Result memoized in matches."""
    if norm in matches:
        return matches[norm] or None
    appid = applist.get(norm)
    if not appid and applist:
        close = difflib.get_close_matches(norm, applist.keys(), n=1,
                                          cutoff=FUZZY_CUTOFF)
        if close:
            appid = applist[close[0]]
            print(f"  fuzzy matched {title!r} -> appid {appid}")
    if not appid:
        appid = steam_store_search(title)
        if appid:
            print(f"  live search matched {title!r} -> appid {appid}")
    matches[norm] = appid or ""
    return appid


def is_excluded(name: str, info: dict) -> bool:
    if set(info.get("descriptors", [])) & EXCLUDE_DESCRIPTOR_IDS:
        return True
    blob = " ".join([name, " ".join(info.get("genres", [])),
                     info.get("desc", "")]).lower()
    return any(t in blob for t in EXCLUDE_TERMS)


def taste_score(info: dict) -> int:
    blob = " ".join([" ".join(info.get("genres", [])),
                     info.get("desc", "")]).lower()
    if any(t in blob for t in DISLIKE_TERMS):
        return -1
    if any(t in blob for t in LIKE_TERMS):
        return 1
    return 0


# ================================================================ free games

def fetch_epic_free() -> list[dict]:
    out = []
    try:
        r = requests.get(EPIC_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        elements = (r.json().get("data", {}).get("Catalog", {})
                    .get("searchStore", {}).get("elements", []))
    except Exception as e:
        print(f"WARN: Epic fetch failed: {e}", file=sys.stderr)
        return out
    for el in elements:
        title = (el.get("title") or "").strip()
        if not title or title.lower() == "mystery game":
            continue
        promos = el.get("promotions") or {}
        end_date, active = "", False
        for block in (promos.get("promotionalOffers") or []):
            for offer in block.get("promotionalOffers", []):
                if offer.get("discountSetting", {}).get(
                        "discountPercentage") == 0:
                    active = True
                    end_date = (offer.get("endDate") or "")[:10]
        if not active:
            continue
        slug = (el.get("productSlug") or "")
        if not slug:
            for m in (el.get("catalogNs", {}) or {}).get("mappings", []) or []:
                slug = m.get("pageSlug", "") or slug
        url = (f"https://store.epicgames.com/en-US/p/{slug}" if slug
               else "https://store.epicgames.com/en-US/free-games")
        out.append({"key": f"epic:{el.get('id', title)}:{end_date}",
                    "title": title, "store": "Epic", "url": url,
                    "end": end_date, "worth": ""})
    return out


PAREN_JUNK = re.compile(
    r"\s*\((?:[^()]*\b(?:steam|epic|gog|pc|amazon|prime|ubisoft|game|"
    r"giveaway|key|drm[- ]free)\b[^()]*)\)\s*$", re.I)
TRAIL_JUNK = re.compile(
    r"\s*(?:free\s+)?(?:pc\s+)?(?:game\s+)?(?:key\s+)?giveaway\s*$", re.I)


def clean_title(raw: str) -> str:
    """Strip store/giveaway noise without ever emptying the title."""
    t = (raw or "").strip()
    for _ in range(4):
        before = t
        t = TRAIL_JUNK.sub("", t).strip()
        t = PAREN_JUNK.sub("", t).strip()
        t = re.sub(r"[\s\-\u2013|:]+$", "", t).strip()
        if t == before:
            break
    return t or (raw or "").strip()


def fetch_gamerpower() -> list[dict]:
    out = []
    try:
        r = requests.get(GAMERPOWER_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            return out
    except Exception as e:
        print(f"WARN: GamerPower fetch failed: {e}", file=sys.stderr)
        return out
    for it in items:
        platforms = (it.get("platforms") or "").lower()
        if any(s in platforms for s in SKIP_STORES):
            continue
        if not any(p in platforms for p in KEEP_STORES):
            continue
        title = clean_title(it.get("title", ""))
        if not title:
            continue
        store = "PC"
        for tag, label in [("steam", "Steam"), ("epic", "Epic"),
                           ("gog", "GOG"), ("amazon", "Prime"),
                           ("ubisoft", "Ubisoft")]:
            if tag in platforms:
                store = label
                break
        end = (it.get("end_date") or "")[:10]
        if end in ("N/A", "0000-00-00"):
            end = ""
        worth = (it.get("worth") or "").replace("N/A", "").strip()
        out.append({"key": f"gp:{it.get('id')}", "title": title,
                    "store": store,
                    "url": (it.get("open_giveaway_url")
                            or it.get("gamerpower_url", "")),
                    "end": end, "worth": worth})
    return out


def collect_new_giveaways() -> list[dict]:
    seen_keys = set(load_json(STATE_GIVEAWAYS, []))
    titles = load_json(STATE_TITLES, {})

    merged: dict[str, dict] = {}
    for g in fetch_epic_free() + fetch_gamerpower():
        norm = normalize(g["title"])
        if not norm:
            continue
        if norm in merged:
            if not merged[norm]["end"] and g["end"]:
                merged[norm]["end"] = g["end"]
            if not merged[norm]["worth"] and g["worth"]:
                merged[norm]["worth"] = g["worth"]
            continue
        g["norm"] = norm
        merged[norm] = g

    fresh: list[dict] = []
    cutoff = now_utc() - timedelta(days=TITLE_COOLDOWN_DAYS)
    for g in merged.values():
        if g["key"] in seen_keys:
            continue
        last = titles.get(g["norm"])
        if last:
            try:
                if datetime.fromisoformat(last) > cutoff:
                    seen_keys.add(g["key"])
                    continue
            except Exception:
                pass
        fresh.append(g)

    for g in fresh:
        seen_keys.add(g["key"])
        titles[g["norm"]] = now_utc().isoformat()
    save_json(STATE_GIVEAWAYS, sorted(seen_keys))
    save_json(STATE_TITLES, titles)
    return fresh


# ================================================================ email

def esc(s) -> str:
    return html.escape(str(s or ""))


BADGE_COLORS = {
    "Overwhelmingly Positive": ("#1a7f37", "#e8f5ec"),
    "Very Positive": ("#2b8a4a", "#eef7f0"),
    "Positive": ("#3d7f5c", "#f1f7f3"),
    "Mostly Positive": ("#7a6a1f", "#fbf6e3"),
    "Mixed": ("#8a6d1f", "#fdf6e0"),
}


def badge(info: dict) -> str:
    verdict = info.get("verdict")
    if not verdict or info.get("pct") is None:
        return ('<span style="color:#999;font-size:12px;">no Steam page</span>')
    fg, bg = BADGE_COLORS.get(verdict, ("#666", "#f0f0f0"))
    return (f'<span style="display:inline-block;padding:2px 7px;border-radius:'
            f'10px;background:{bg};color:{fg};font-size:12px;white-space:nowrap;">'
            f'{esc(info["pct"])}%</span>'
            f'<div style="color:#888;font-size:11px;">'
            f'{esc(verdict)}<br>{info.get("total", 0):,} reviews</div>')


TH = ('<th align="left" style="font:bold 12px Arial;color:#555;'
      'border-bottom:1px solid #ddd;padding:6px 8px;">{}</th>')
TD = '<td style="font:13px Arial;padding:8px;border-bottom:1px solid #f0f0f0;{extra}">{v}</td>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(TH.format(esc(h)) for h in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            TD.format(v=c, extra="vertical-align:top;") for c in r) + "</tr>"
    return (f'<table cellspacing="0" cellpadding="0" width="100%" '
            f'style="border-collapse:collapse;margin-bottom:22px;">'
            f'<tr>{head}</tr>{body}</table>')


def title_cell(name: str, url: str, desc: str, star: bool) -> str:
    if len(desc) > 95:
        desc = desc[:95].rsplit(" ", 1)[0] + "..."
    s = '<span title="matches your taste">&#9733; </span>' if star else ""
    return (f'{s}<a href="{esc(url)}" style="color:#1a6fb0;font-weight:bold;'
            f'text-decoration:none;">{esc(name)}</a>'
            f'<div style="color:#777;font-size:11px;">{esc(desc)}</div>')


SECTION = ('<h2 style="font:bold 17px Arial;color:#1b2838;margin:26px 0 8px;'
           'border-bottom:2px solid #66c0f4;padding-bottom:5px;">{}</h2>')


def send_email(subject: str, text_body: str, html_body: str) -> None:
    user = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_PASS"]
    to = os.environ["EMAIL_TO"]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    print(f"Email sent: {subject}")


# ================================================================ main

def main() -> None:
    cache = load_json(CACHE_FILE, {})
    matches = load_json(MATCH_FILE, {})
    text_parts: list[str] = []
    html_parts: list[str] = []
    summary: list[str] = []
    subject_bits: list[str] = []

    heavy_due = hours_since(meta_get("last_heavy_scan")) >= HEAVY_SCAN_INTERVAL_H

    # ---------- 1. Newly Overwhelmingly Positive ----------
    if heavy_due:
        print("Scanning Steam for OP games...")
        current = fetch_op_games()
        print(f"Found {len(current)} OP games.")
        if current:
            games = sorted(current.items(), key=lambda x: x[1].lower())
            LIST_FILE.write_text("\n".join(
                [f"# Overwhelmingly Positive games ({len(current)})", ""]
                + [f"- [{n}](https://store.steampowered.com/app/{a}/)"
                   for a, n in games]))
            if not STATE_OP.exists():
                save_json(STATE_OP, sorted(current.keys()))
                print("Seeded OP list (no email).")
            else:
                seen = set(load_json(STATE_OP, []))
                new_ids = sorted(set(current.keys()) - seen)
                rows, t_lines = [], []
                for a in new_ids:
                    info = fetch_steam_info(a, cache)
                    if is_excluded(current[a], info):
                        continue
                    url = f"https://store.steampowered.com/app/{a}/"
                    rows.append([
                        badge(info),
                        title_cell(current[a], url, info.get("desc", ""),
                                   taste_score(info) > 0),
                        esc(", ".join(info.get("genres", []))),
                        esc(info.get("price", "")),
                    ])
                    t_lines.append(
                        f"* {current[a]} | {info.get('pct', '?')}% | "
                        f"{info.get('price', '')}\n  {url}")
                if rows:
                    html_parts.append(
                        SECTION.format("Newly Overwhelmingly Positive")
                        + table(["Rating", "Game", "Genres", "Price"], rows))
                    text_parts.append("NEWLY OVERWHELMINGLY POSITIVE\n\n"
                                      + "\n\n".join(t_lines))
                    subject_bits.append(f"{len(rows)} new OP")
                    summary.append(f"{len(rows)} newly Overwhelmingly Positive")
                if new_ids:
                    seen.update(new_ids)
                    save_json(STATE_OP, sorted(seen))

    # ---------- 2. Free to keep ----------
    applist = load_applist()
    fresh = collect_new_giveaways()
    print(f"New giveaways this run: {len(fresh)}")
    if fresh:
        for g in fresh:
            appid = match_appid(g["title"], g["norm"], applist, matches)
            g["appid"] = appid
            g["info"] = fetch_steam_info(appid, cache) if appid else {}
        fresh = [g for g in fresh if not is_excluded(g["title"], g["info"])]
        if SORT_FREE_BY_RATING:
            fresh.sort(key=lambda g: (g["info"].get("pct", -1),
                                      g["info"].get("total", 0)), reverse=True)
        else:
            fresh.sort(key=lambda g: (g["store"],
                                      -(g["info"].get("pct") or 0)))
        rows, t_lines = [], []
        for g in fresh:
            info = g["info"]
            rows.append([
                badge(info),
                title_cell(g["title"], g["url"], info.get("desc", ""),
                           taste_score(info) > 0),
                esc(g["store"]),
                esc(g["worth"] or info.get("price", "")),
                esc(g["end"] or "no deadline"),
            ])
            t_lines.append(
                f"* {g['title']} [{g['store']}] | "
                f"{info.get('pct', 'not on Steam')}"
                f"{'%' if info.get('pct') else ''} | "
                f"claim by {g['end'] or 'no deadline'}\n  {g['url']}")
        if rows:
            html_parts.append(
                SECTION.format("Free")
                + table(["Rating", "Game", "Store", "Worth", "Claim by"], rows))
            text_parts.append("FREE\n\n" + "\n\n".join(t_lines))
            subject_bits.append(f"{len(rows)} free")
            summary.append(f"{len(rows)} free")

    # ---------- 3. Hidden gems ----------
    if heavy_due:
        print("Scanning for hidden gems...")
        gems = fetch_hidden_gems()
        print(f"Gem candidates: {len(gems)}")
        seen_gems = set(load_json(STATE_GEMS, []))
        first_gems = not STATE_GEMS.exists()
        new_gems = {a: v for a, v in gems.items() if a not in seen_gems}
        if first_gems and len(new_gems) > 10:
            ranked = sorted(new_gems.items(),
                            key=lambda kv: (kv[1]["pct"], kv[1]["total"]),
                            reverse=True)
            new_gems = dict(ranked[:10])
            seen_gems.update(gems.keys())
        rows, t_lines = [], []
        for a, v in sorted(new_gems.items(), key=lambda kv: kv[1]["pct"],
                           reverse=True):
            info = fetch_steam_info(a, cache)
            if is_excluded(v["name"], info):
                continue
            url = f"https://store.steampowered.com/app/{a}/"
            merged_info = {**info, "pct": v["pct"], "total": v["total"],
                           "verdict": v["verdict"]}
            rows.append([
                badge(merged_info),
                title_cell(v["name"], url, info.get("desc", ""),
                           taste_score(info) > 0),
                esc(", ".join(info.get("genres", []))),
                esc(info.get("price", "")),
                esc(info.get("release", "")),
            ])
            t_lines.append(f"* {v['name']} | {v['pct']}% of {v['total']:,} "
                           f"reviews | {info.get('price', '')}\n  {url}")
        if rows:
            html_parts.append(
                SECTION.format("Hidden Gems")
                + table(["Rating", "Game", "Genres", "Price", "Released"],
                        rows))
            text_parts.append("HIDDEN GEMS\n\n" + "\n\n".join(t_lines))
            subject_bits.append(f"{len(rows)} gems")
            summary.append(f"{len(rows)} hidden gems")
        seen_gems.update(new_gems.keys())
        save_json(STATE_GEMS, sorted(seen_gems))
        meta_set("last_heavy_scan", now_utc().isoformat())

    save_json(CACHE_FILE, cache)
    save_json(MATCH_FILE, matches)

    # ---------- send ----------
    if not html_parts:
        print("Nothing new this run. No email sent.")
        return

    today = now_utc().strftime("%b %d, %Y")
    header = (f'<div style="font:bold 22px Arial;color:#1b2838;'
              f'letter-spacing:-0.3px;">The Backlog Threat</div>'
              f'<div style="font:13px Arial;color:#777;margin-bottom:4px;">'
              f'{today} &nbsp;&middot;&nbsp; {esc(" | ".join(summary))}</div>')
    footer = ('<p style="color:#999;font:11px Arial;margin-top:18px;">'
              '&#9733; = matches your taste profile. '
              'Ratings are from Steam.</p>')
    html_doc = (f'<div style="max-width:680px;">{header}'
                f'{"".join(html_parts)}{footer}</div>')
    subject = "The Backlog Threat: " + ", ".join(subject_bits)
    text_header = f"THE BACKLOG THREAT\n{today}\n{' | '.join(summary)}\n"
    send_email(subject, text_header + "\n\n" + "\n\n\n".join(text_parts),
               html_doc)


if __name__ == "__main__":
    main()

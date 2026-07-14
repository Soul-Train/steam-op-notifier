"""
Personal Gaming Newsletter
--------------------------
Emails a compact gaming report whenever there is something new. Sections:

  1. Newly "Overwhelmingly Positive" on Steam   (reported once, ever)
  2. Free to keep across stores, ranked by Steam rating
     (Epic direct + GamerPower aggregator: GOG, Steam, Prime, Ubisoft, ...)
     Reported once per giveaway. The same game free again a year later
     IS reported again; re-lists within 60 days are suppressed.
  3. Hidden gems: recent releases already rated Very Positive or better
     with a small review count                  (reported once, ever)

Efficiency:
  - Steam OP scan + hidden gems scan run at most once per ~20h
  - Free-game sources are checked every run (every 4h)
  - Steam app list cached locally (weekly refresh) so title->appid
    matching costs zero Steam calls
  - Ratings/details cached per appid for 30 days

Env vars (GitHub secrets): EMAIL_USER, EMAIL_PASS, EMAIL_TO
Optional: SMTP_HOST (smtp.gmail.com), SMTP_PORT (587)
"""

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
STATE_OP = HERE / "seen.json"                # OP appids, once-ever
STATE_GEMS = HERE / "seen_gems.json"         # hidden-gem appids, once-ever
STATE_GIVEAWAYS = HERE / "seen_giveaways.json"   # giveaway instance keys
STATE_TITLES = HERE / "notified_titles.json"     # {norm_title: iso_date} cooldown
CACHE_FILE = HERE / "steam_cache.json"       # {appid: {...details, ts}}
APPLIST_FILE = HERE / "applist.json.gz"      # normalized name -> appid
META_FILE = HERE / "last_scan.json"
LIST_FILE = HERE / "op_games.md"

# ------------------------------------------------------------ endpoints
SEARCH_URL = "https://store.steampowered.com/search/results/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/"
APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
EPIC_URL = ("https://store-site-backend-static.ak.epicgames.com"
            "/freeGamesPromotions?locale=en-US&country=US&allowCountries=US")
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways?type=game"

# ------------------------------------------------------------ tuning
PAGE_SIZE = 50
MAX_PAGES_OP = 80
MAX_PAGES_GEMS = 40
REQUEST_DELAY = 1.5
HEAVY_SCAN_INTERVAL_H = 20      # OP + gems scans at most once per ~day
APPLIST_REFRESH_DAYS = 7
CACHE_TTL_DAYS = 30
TITLE_COOLDOWN_DAYS = 60        # same title free again within this = silent
GEM_MIN_PCT = 85
GEM_MIN_REVIEWS = 50
GEM_MAX_REVIEWS = 2000
GEM_MAX_AGE_DAYS = 60
SKIP_STORES = {"itch.io", "itchio"}
HEADERS = {"User-Agent": "Mozilla/5.0 (game-newsletter; personal use)"}

# Taste matching (genres / keywords from Steam details)
LIKE_TERMS = ["action", "roguelike", "roguelite", "deckbuild", "rhythm",
              "platformer", "shooter", "metroidvania", "speedrun",
              "arcade", "racing", "bullet"]
DISLIKE_TERMS = ["visual novel", "hidden object", "idle", "clicker",
                 "match 3", "casual", "point & click", "dating"]

# Hard exclusions: never show these anywhere in the report
EXCLUDE_TERMS = ["visual novel", "dating sim", "dating simulator", "hentai",
                 "eroge", "nsfw", "sexual content", "otome", "adult only"]
# Steam content descriptor ids: 3 = Adult Only Sexual Content,
#                               4 = Frequent Nudity or Sexual Content
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
    """Normalize a game title for matching across stores."""
    t = title.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    for junk in ["definitive edition", "deluxe edition", "goty edition",
                 "game of the year edition", "complete edition",
                 "enhanced edition", "remastered", "giveaway", "steam",
                 "epic games", "pc"]:
        t = t.replace(junk, " ")
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


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
    """Yield (appid, name, tooltip) rows from Steam's search listing."""
    empty_streak = 0
    for page in range(max_pages):
        params = {"query": "", "start": page * PAGE_SIZE, "count": PAGE_SIZE,
                  "sort_by": sort_by, "category1": "998",
                  "supportedlang": "english", "json": "1", "infinite": "1"}
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS,
                             timeout=30)
            r.raise_for_status()
            html_blob = r.json().get("results_html", "")
        except Exception as e:
            print(f"WARN: search page {page} ({sort_by}) failed: {e}",
                  file=sys.stderr)
            time.sleep(10)
            continue

        soup = BeautifulSoup(html_blob, "html.parser")
        rows = soup.select("a.search_result_row")
        if not rows:
            return
        page_yielded = 0
        for row in rows:
            appid = row.get("data-ds-appid")
            if not appid:
                continue
            title_el = row.select_one("span.title")
            name = title_el.get_text(strip=True) if title_el else f"App {appid}"
            summary_el = row.select_one("span.search_review_summary")
            tooltip = summary_el.get("data-tooltip-html", "") if summary_el else ""
            page_yielded += 1
            yield appid, name, tooltip, page
        empty_streak = 0 if page_yielded else empty_streak + 1
        if empty_streak >= 3:
            return
        time.sleep(REQUEST_DELAY)


def fetch_op_games() -> dict[str, str]:
    """{appid: name} for everything currently Overwhelmingly Positive."""
    op: dict[str, str] = {}
    pages_with_op_miss = 0
    for appid, name, tooltip, page in scan_steam_search("Reviews_DESC",
                                                        MAX_PAGES_OP):
        if tooltip.startswith("Overwhelmingly Positive"):
            op[appid] = name
            pages_with_op_miss = 0
        else:
            pages_with_op_miss += 1
            if pages_with_op_miss > 3 * PAGE_SIZE:
                break   # far past the OP tier
    return op


def parse_tooltip(tooltip: str):
    """('Very Positive', 92, 1534) from a review tooltip, or None."""
    m = re.match(r"([A-Za-z ]+)<br>(\d+)% of the ([\d,]+) user reviews",
                 tooltip)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2)), int(m.group(3).replace(",", ""))


def fetch_hidden_gems() -> dict[str, dict]:
    """Recent releases already rated highly with a modest review count."""
    gems: dict[str, dict] = {}
    for appid, name, tooltip, page in scan_steam_search("Released_DESC",
                                                        MAX_PAGES_GEMS):
        parsed = parse_tooltip(tooltip)
        if not parsed:
            continue
        desc, pct, total = parsed
        if (pct >= GEM_MIN_PCT and GEM_MIN_REVIEWS <= total <= GEM_MAX_REVIEWS
                and desc in ("Very Positive", "Overwhelmingly Positive")):
            gems[appid] = {"name": name, "pct": pct, "total": total,
                           "verdict": desc}
    return gems


# ================================================================ steam lookups

def load_cache() -> dict:
    return load_json(CACHE_FILE, {})


def cache_get(cache: dict, appid: str) -> dict | None:
    entry = cache.get(str(appid))
    if entry and hours_since(entry.get("ts")) < CACHE_TTL_DAYS * 24:
        return entry
    return None


def fetch_steam_info(appid: str, cache: dict) -> dict:
    """Rating + details for an appid, cached. Two Steam calls on a miss."""
    hit = cache_get(cache, appid)
    if hit:
        return hit
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
            info["genres"] = [g["description"]
                              for g in d.get("genres", [])[:4]]
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
    """normalized name -> appid map, cached locally, refreshed weekly."""
    fresh = hours_since(meta_get("applist_ts")) < APPLIST_REFRESH_DAYS * 24
    if APPLIST_FILE.exists() and fresh:
        try:
            return json.loads(gzip.decompress(
                APPLIST_FILE.read_bytes()).decode())
        except Exception:
            pass
    print("Refreshing Steam app list...")
    try:
        r = requests.get(APPLIST_URL, headers=HEADERS, timeout=120)
        r.raise_for_status()
        apps = r.json()["applist"]["apps"]
    except Exception as e:
        print(f"WARN: applist refresh failed: {e}", file=sys.stderr)
        if APPLIST_FILE.exists():
            try:
                return json.loads(gzip.decompress(
                    APPLIST_FILE.read_bytes()).decode())
            except Exception:
                return {}
        return {}
    index: dict[str, str] = {}
    for app in apps:
        key = normalize(app.get("name", ""))
        if key:
            index.setdefault(key, str(app["appid"]))
    APPLIST_FILE.write_bytes(gzip.compress(
        json.dumps(index).encode(), compresslevel=9))
    meta_set("applist_ts", now_utc().isoformat())
    print(f"App list cached: {len(index)} entries.")
    return index


def is_excluded(name: str, info: dict) -> bool:
    """True for visual novels, dating sims, and adult content."""
    if set(info.get("descriptors", [])) & EXCLUDE_DESCRIPTOR_IDS:
        return True
    blob = " ".join([name, " ".join(info.get("genres", [])),
                     info.get("desc", "")]).lower()
    return any(t in blob for t in EXCLUDE_TERMS)


def taste_score(info: dict) -> int:
    """+1 taste match, -1 taste anti-match, 0 neutral."""
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
        title = el.get("title", "").strip()
        if not title or title.lower() == "mystery game":
            continue
        promos = el.get("promotions") or {}
        end_date = ""
        active = False
        for block in (promos.get("promotionalOffers") or []):
            for offer in block.get("promotionalOffers", []):
                if offer.get("discountSetting", {}).get(
                        "discountPercentage") == 0:
                    active = True
                    end_date = (offer.get("endDate") or "")[:10]
        if not active:
            continue
        key = f"epic:{el.get('id', title)}:{end_date}"
        slug = (el.get("productSlug")
                or el.get("catalogNs", {}).get("mappings", [{}])[0]
                .get("pageSlug", ""))
        url = (f"https://store.epicgames.com/en-US/p/{slug}" if slug
               else "https://store.epicgames.com/en-US/free-games")
        out.append({"key": key, "title": title, "store": "Epic",
                    "url": url, "end": end_date, "worth": ""})
    return out


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
            if not any(p in platforms for p in
                       ("steam", "epic", "gog", "amazon", "ubisoft", "pc")):
                continue
        if not any(p in platforms for p in
                   ("steam", "epic", "gog", "amazon", "ubisoft", "pc")):
            continue
        title = re.sub(r"\s*\((?:Steam|Epic Games|GOG|PC|Amazon)?\s*"
                       r"(?:Game)?\s*Giveaway\)\s*$", "",
                       it.get("title", ""), flags=re.I).strip()
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
        out.append({"key": f"gp:{it.get('id')}", "title": title,
                    "store": store,
                    "url": it.get("open_giveaway_url") or it.get("gamerpower_url", ""),
                    "end": end,
                    "worth": (it.get("worth") or "").replace("N/A", "")})
    return out


def collect_new_giveaways() -> list[dict]:
    """Merge sources, dedupe, apply once-per-giveaway + 60-day cooldown."""
    seen_keys = set(load_json(STATE_GIVEAWAYS, []))
    titles = load_json(STATE_TITLES, {})
    first_run = not STATE_GIVEAWAYS.exists()

    merged: dict[str, dict] = {}
    for g in fetch_epic_free() + fetch_gamerpower():
        norm = normalize(g["title"])
        if not norm:
            continue
        # within-run duplicate (Epic listed by both sources): keep first
        if norm in merged:
            if not merged[norm]["end"] and g["end"]:
                merged[norm]["end"] = g["end"]
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
                    seen_keys.add(g["key"])   # re-list, absorb silently
                    continue
            except Exception:
                pass
        fresh.append(g)

    for g in fresh:
        seen_keys.add(g["key"])
        titles[g["norm"]] = now_utc().isoformat()
    save_json(STATE_GIVEAWAYS, sorted(seen_keys))
    save_json(STATE_TITLES, titles)
    if first_run:
        print(f"First giveaway run: notifying all {len(fresh)} "
              "currently-active giveaways.")
    return fresh


# ================================================================ email

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


def esc(s: str) -> str:
    return html.escape(s or "")


H_SECTION = ('<h2 style="font-family:Arial,sans-serif;color:#1b2838;'
             'border-bottom:2px solid #66c0f4;padding-bottom:4px;">{}</h2>')
H_CARD = ('<div style="font-family:Arial,sans-serif;margin:0 0 14px 0;">'
          '<div style="font-size:15px;">{star}<a href="{url}" '
          'style="color:#1a6fb0;font-weight:bold;text-decoration:none;">'
          '{title}</a>{store}</div>'
          '<div style="color:#444;font-size:13px;">{meta}</div>'
          '{descline}</div>')


def card(title, url, meta, desc="", store="", star=False) -> str:
    return H_CARD.format(
        star="&#9733; " if star else "",
        url=esc(url), title=esc(title),
        store=f' <span style="color:#888;font-size:12px;">[{esc(store)}]</span>'
              if store else "",
        meta=esc(meta),
        descline=(f'<div style="color:#666;font-size:12px;">{esc(desc)}</div>'
                  if desc else ""))


# ================================================================ main

def main() -> None:
    cache = load_cache()
    text_parts: list[str] = []
    html_parts: list[str] = []
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
            seen = set(load_json(STATE_OP, []))
            if not STATE_OP.exists():
                save_json(STATE_OP, sorted(current.keys()))
            else:
                new_ids = sorted(set(current.keys()) - seen)
                if new_ids:
                    t_lines, h_lines = [], []
                    for a in new_ids:
                        info = fetch_steam_info(a, cache)
                        if is_excluded(current[a], info):
                            continue
                        url = f"https://store.steampowered.com/app/{a}/"
                        meta = " | ".join(x for x in [
                            info.get("price", ""),
                            ", ".join(info.get("genres", [])),
                            f"{info.get('pct', '?')}% of "
                            f"{info.get('total', 0):,} reviews"] if x)
                        t_lines.append(f"* {current[a]}\n  {meta}\n  {url}")
                        h_lines.append(card(current[a], url, meta,
                                            info.get("desc", ""),
                                            star=taste_score(info) > 0))
                    if t_lines:
                        text_parts.append(
                            "NEWLY OVERWHELMINGLY POSITIVE\n\n"
                            + "\n\n".join(t_lines))
                        html_parts.append(
                            H_SECTION.format("Newly Overwhelmingly Positive")
                            + "".join(h_lines))
                        subject_bits.append(f"{len(t_lines)} new OP")
                    seen.update(new_ids)
                    save_json(STATE_OP, sorted(seen))

    # ---------- 2. Free to keep, ranked by Steam rating ----------
    applist = load_applist()
    fresh = collect_new_giveaways()
    print(f"New giveaways this run: {len(fresh)}")
    if fresh:
        for g in fresh:
            appid = applist.get(g["norm"])
            g["appid"] = appid
            g["info"] = fetch_steam_info(appid, cache) if appid else {}
        fresh = [g for g in fresh
                 if not is_excluded(g["title"], g["info"])]
        fresh.sort(key=lambda g: (g["info"].get("pct", -1),
                                  g["info"].get("total", 0)), reverse=True)
        t_lines, h_lines = [], []
        for g in fresh:
            info = g["info"]
            bits = []
            if info.get("pct") is not None:
                bits.append(f"Steam: {info.get('verdict', '')} "
                            f"({info['pct']}%, {info.get('total', 0):,})")
            else:
                bits.append("Not found on Steam")
            if g["worth"]:
                bits.append(f"normally {g['worth']}")
            if info.get("genres"):
                bits.append(", ".join(info["genres"]))
            if g["end"]:
                bits.append(f"claim by {g['end']}")
            meta = " | ".join(bits)
            t_lines.append(f"* {g['title']}  [{g['store']}]\n  {meta}\n"
                           f"  {g['url']}")
            h_lines.append(card(g["title"], g["url"], meta,
                                info.get("desc", ""), store=g["store"],
                                star=taste_score(info) > 0))
        if t_lines:
            text_parts.append("FREE TO KEEP\n\n" + "\n\n".join(t_lines))
            html_parts.append(H_SECTION.format("Free to Keep")
                              + "".join(h_lines))
            subject_bits.append(f"{len(fresh)} free")

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
            seen_gems.update(gems.keys())    # seed the rest silently
        if new_gems:
            t_lines, h_lines = [], []
            for a, v in sorted(new_gems.items(),
                               key=lambda kv: kv[1]["pct"], reverse=True):
                info = fetch_steam_info(a, cache)
                if is_excluded(v["name"], info):
                    continue
                url = f"https://store.steampowered.com/app/{a}/"
                meta = " | ".join(x for x in [
                    f"{v['verdict']} ({v['pct']}%, {v['total']:,} reviews)",
                    info.get("price", ""),
                    ", ".join(info.get("genres", [])),
                    info.get("release", "")] if x)
                t_lines.append(f"* {v['name']}\n  {meta}\n  {url}")
                h_lines.append(card(v["name"], url, meta,
                                    info.get("desc", ""),
                                    star=taste_score(info) > 0))
            if t_lines:
                text_parts.append(
                    "HIDDEN GEMS (new releases, highly rated, "
                    "under the radar)\n\n" + "\n\n".join(t_lines))
                html_parts.append(H_SECTION.format("Hidden Gems")
                                  + "".join(h_lines))
                subject_bits.append(f"{len(t_lines)} gems")
            seen_gems.update(new_gems.keys())
        save_json(STATE_GEMS, sorted(seen_gems))
        meta_set("last_heavy_scan", now_utc().isoformat())

    save_json(CACHE_FILE, cache)

    # ---------- send ----------
    if not text_parts:
        print("Nothing new this run. No email sent.")
        return
    subject = "Gaming report: " + ", ".join(subject_bits)
    legend = ("\n\n---\n* items marked with a star match your taste profile")
    html_doc = ('<div style="max-width:640px;">' + "".join(html_parts)
                + '<p style="color:#999;font-family:Arial;font-size:11px;">'
                  '&#9733; = matches your taste profile</p></div>')
    send_email(subject, "\n\n\n".join(text_parts) + legend, html_doc)


if __name__ == "__main__":
    main()

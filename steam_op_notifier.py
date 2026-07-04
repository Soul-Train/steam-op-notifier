"""
Steam Overwhelmingly Positive Notifier
--------------------------------------
Scans Steam for games currently rated "Overwhelmingly Positive",
compares against a permanent seen list, and emails a summary of
any games that are newly OP (new releases OR back-catalog games
that crossed the threshold).

A game is only ever reported once. Dips and recoveries are ignored.

First run (no seen.json): seeds the state file silently and sends
a one-time confirmation email instead of a 500-game dump.

Required environment variables (set as GitHub repo secrets):
  EMAIL_USER  - SMTP username / from address (e.g. your Gmail)
  EMAIL_PASS  - SMTP password (Gmail app password)
  EMAIL_TO    - destination address
Optional:
  SMTP_HOST   - default smtp.gmail.com
  SMTP_PORT   - default 587
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "seen.json"
SEARCH_URL = "https://store.steampowered.com/search/results/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
PAGE_SIZE = 50
MAX_PAGES = 80          # 80 * 50 = 4000 top-rated entries scanned per run
REQUEST_DELAY = 1.5     # seconds between requests, be polite
HEADERS = {"User-Agent": "Mozilla/5.0 (OP-notifier; personal use)"}


def fetch_op_games() -> dict[str, str]:
    """Return {appid: name} for all games currently Overwhelmingly Positive.

    Scans Steam search sorted by review score and parses the review
    tooltip in each result row. Stops early once several consecutive
    pages contain no OP games (score sort means we've passed them).
    """
    op: dict[str, str] = {}
    empty_streak = 0

    for page in range(MAX_PAGES):
        params = {
            "query": "",
            "start": page * PAGE_SIZE,
            "count": PAGE_SIZE,
            "sort_by": "Reviews_DESC",   # sort by review score
            "category1": "998",          # games only (no DLC/soundtracks)
            "supportedlang": "english",
            "json": "1",
            "infinite": "1",
        }
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            html = r.json().get("results_html", "")
        except Exception as e:
            print(f"WARN: search page {page} failed: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("a.search_result_row")
        if not rows:
            break

        found_this_page = 0
        for row in rows:
            appid = row.get("data-ds-appid")
            if not appid:
                continue
            title_el = row.select_one("span.title")
            name = title_el.get_text(strip=True) if title_el else f"App {appid}"
            summary_el = row.select_one("span.search_review_summary")
            tooltip = summary_el.get("data-tooltip-html", "") if summary_el else ""
            if tooltip.startswith("Overwhelmingly Positive"):
                op[appid] = name
                found_this_page += 1

        empty_streak = empty_streak + 1 if found_this_page == 0 else 0
        if empty_streak >= 3:
            break  # well past the OP tier in the score-sorted list
        time.sleep(REQUEST_DELAY)

    return op


def fetch_details(appid: str) -> dict:
    """Get short description, price, and genres for a single game."""
    try:
        r = requests.get(
            APPDETAILS_URL,
            params={"appids": appid, "cc": "us", "l": "en"},
            headers=HEADERS,
            timeout=30,
        )
        data = r.json().get(str(appid), {})
        if not data.get("success"):
            return {}
        d = data["data"]
        price = "Free" if d.get("is_free") else d.get(
            "price_overview", {}).get("final_formatted", "?")
        genres = ", ".join(g["description"] for g in d.get("genres", [])[:3])
        desc = re.sub(r"<[^>]+>", "", d.get("short_description", "")).strip()
        return {"price": price, "genres": genres, "desc": desc,
                "release": d.get("release_date", {}).get("date", "")}
    except Exception as e:
        print(f"WARN: appdetails failed for {appid}: {e}", file=sys.stderr)
        return {}


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=0))


def send_email(subject: str, body: str) -> None:
    user = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_PASS"]
    to = os.environ["EMAIL_TO"]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    print(f"Email sent: {subject}")


def format_game(appid: str, name: str) -> str:
    info = fetch_details(appid)
    time.sleep(REQUEST_DELAY)
    lines = [f"* {name}"]
    meta = " | ".join(x for x in [info.get("price"), info.get("genres"),
                                  info.get("release")] if x)
    if meta:
        lines.append(f"  {meta}")
    if info.get("desc"):
        lines.append(f"  {info['desc']}")
    lines.append(f"  https://store.steampowered.com/app/{appid}/")
    return "\n".join(lines)


def main() -> None:
    seen = load_seen()
    first_run = not STATE_FILE.exists()

    print("Scanning Steam for Overwhelmingly Positive games...")
    current = fetch_op_games()
    print(f"Found {len(current)} OP games currently on Steam.")

    if not current:
        print("ERROR: scan returned zero games, aborting without "
              "touching state (Steam layout may have changed).")
        sys.exit(1)

    if first_run:
        save_seen(set(current.keys()))
        send_email(
            "Steam OP Notifier is live",
            f"Setup complete. Seeded {len(current)} games currently rated "
            "Overwhelmingly Positive.\n\nFrom now on you'll only get an "
            "email when a game newly reaches that tier.",
        )
        return

    new_ids = sorted(set(current.keys()) - seen)
    if not new_ids:
        print("No new OP games today. No email sent.")
        return

    print(f"{len(new_ids)} newly OP: {[current[i] for i in new_ids]}")
    blocks = [format_game(appid, current[appid]) for appid in new_ids]
    count = len(new_ids)
    subject = (f"New Overwhelmingly Positive game: {current[new_ids[0]]}"
               if count == 1
               else f"{count} games newly Overwhelmingly Positive on Steam")
    body = ("The following just reached Overwhelmingly Positive "
            "for the first time:\n\n" + "\n\n".join(blocks))
    send_email(subject, body)

    seen.update(new_ids)
    save_seen(seen)


if __name__ == "__main__":
    main()

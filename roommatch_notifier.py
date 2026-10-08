"""Check roommatch.nl for new listings matching config.json and post them to Discord.

Usage:
    python roommatch_notifier.py            # notify about new matches, update seen.json
    python roommatch_notifier.py --dry-run  # print matches, send nothing, change nothing

The Discord webhook URL is read from the DISCORD_WEBHOOK_URL environment variable.
On the very first run (no seen.json yet) existing listings are recorded without
notifying, so you only get pinged about listings that appear afterwards.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://www.roommatch.nl"
LISTINGS_URL = BASE_URL + "/portal/object/frontend/getallobjects/format/json"
DETAILS_URL = BASE_URL + "/aanbod/studentenwoningen/details/{}"
USER_AGENT = "Mozilla/5.0 (roommatch-notifier)"

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
SEEN_RETENTION_DAYS = 90

# How the room is allocated; "reactiedatum" means first come, first served.
MODEL_LABELS = {
    "inschrijfduur": "Registration time",
    "reactiedatum": "First come, first served",
    "hospiteren": "Hospiteren (viewing/selection)",
    "hospitereninschrijfduur": "Hospiteren + registration time",
}


def fetch_listings():
    request = urllib.request.Request(
        LISTINGS_URL,
        data=b"",
        method="POST",
        headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["result"]


def matches(listing, config):
    def allowed(key, value):
        wanted = config.get(key) or []
        return not wanted or value.lower() in (w.lower() for w in wanted)

    if not allowed("cities", listing["city"]["name"]):
        return False
    if not allowed("housing_types", listing["woningsoort"]["localizedNaam"]):
        return False
    if not allowed("furnishing", listing["dwellingType"]["localizedName"]):
        return False
    if config.get("max_total_rent") and listing["totalRent"] > config["max_total_rent"]:
        return False
    if config.get("min_area") and listing["areaDwelling"] < config["min_area"]:
        return False
    return True


def build_embed(listing):
    address = f'{listing["street"]} {listing["houseNumber"]}{listing["houseNumberAddition"]}'.strip()
    model = listing["model"]["modelCategorie"]["code"]
    fields = [
        ("Total rent", f'€{listing["totalRent"]:.2f}'),
        ("Net rent", f'€{listing["netRent"]:.2f}'),
        ("Size", f'{listing["areaDwelling"]} m²'),
        ("Type", listing["woningsoort"]["localizedNaam"]),
        ("Furnishing", listing["dwellingType"]["localizedName"]),
        ("Allocation", MODEL_LABELS.get(model, model)),
        ("Available from", listing.get("availableFromDate") or "-"),
        ("Respond before", listing.get("closingDate") or "-"),
    ]
    embed = {
        "title": f'{address}, {listing["city"]["name"]}',
        "url": DETAILS_URL.format(listing["urlKey"]),
        "color": 0xE4572E if model == "reactiedatum" else 0x2E86AB,
        "fields": [{"name": n, "value": str(v), "inline": True} for n, v in fields],
        "footer": {"text": f'Published {listing["publicationDate"]}'},
    }
    if listing["pictures"]:
        embed["image"] = {"url": BASE_URL + listing["pictures"][0]["uri"]}
    return embed


def send_to_discord(webhook_url, listings):
    # Discord allows at most 10 embeds per message.
    for start in range(0, len(listings), 10):
        chunk = listings[start:start + 10]
        payload = {
            "content": f"🏠 {len(listings)} new room(s) on ROOM" if start == 0 else None,
            "embeds": [build_embed(l) for l in chunk],
        }
        request = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        urllib.request.urlopen(request, timeout=30).close()
        time.sleep(1)  # stay well under the webhook rate limit


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return None


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    listings = [l for l in fetch_listings() if matches(l, config)]
    listings.sort(key=lambda l: l["publicationDate"])

    if dry_run:
        for l in listings:
            print(f'{l["id"]:>7}  €{l["totalRent"]:>7.2f}  {l["areaDwelling"]:>3} m²  '
                  f'{l["city"]["name"]:<12} {l["street"]} {l["houseNumber"]}  '
                  f'{DETAILS_URL.format(l["urlKey"])}')
        print(f"{len(listings)} matching listing(s)")
        return

    now = datetime.now(timezone.utc)
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}

    new = [l for l in listings if l["id"] not in seen]
    if new and not first_run:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook_url:
            sys.exit("DISCORD_WEBHOOK_URL is not set")
        send_to_discord(webhook_url, new)

    for l in new:
        seen[l["id"]] = now.date().isoformat()
    cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save_seen(seen)

    if first_run:
        print(f"First run: recorded {len(new)} existing matching listing(s) without notifying")
    else:
        print(f"{len(listings)} matching listing(s), {len(new)} new")


if __name__ == "__main__":
    main()

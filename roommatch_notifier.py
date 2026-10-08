"""Check roommatch.nl for new listings matching config.json and post them to Discord.

Usage:
    python roommatch_notifier.py            # notify about new matches, update seen.json
    python roommatch_notifier.py --dry-run  # print matches, send nothing, change nothing
    python roommatch_notifier.py --test     # send the newest match to Discord, change nothing
    python roommatch_notifier.py --loop 3300 --interval 30
        # check every 30 seconds for 3300 seconds, committing and pushing seen.json and
        # status.json, posting a warning on repeated failures and a daily summary

The Discord webhook URL is read from the DISCORD_WEBHOOK_URL environment variable.
On the very first run (no seen.json yet) existing listings are recorded without
notifying, so you only get pinged about listings that appear afterwards.
First-come-first-served listings are posted with config["urgent_mention"]
(e.g. "@everyone" or a role mention like "<@&123>") so Discord pushes them.
"""

import json
import os
import subprocess
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
STATUS_FILE = ROOT / "status.json"
SEEN_RETENTION_DAYS = 90
FAILURE_ALERT_AFTER = 5  # consecutive failed checks before posting a warning

# Discord message flag that posts without sending a push notification.
SUPPRESS_NOTIFICATIONS = 1 << 12

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
except Exception:  # no tz database (e.g. Windows without tzdata)
    LOCAL_TZ = timezone.utc

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


def matching_listings(config):
    listings = [l for l in fetch_listings() if matches(l, config)]
    listings.sort(key=lambda l: l["publicationDate"])
    return listings


def is_first_come(listing):
    return listing["model"]["modelCategorie"]["code"] == "reactiedatum"


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
        "color": 0xE4572E if is_first_come(listing) else 0x2E86AB,
        "fields": [{"name": n, "value": str(v), "inline": True} for n, v in fields],
        "footer": {"text": f'Published {listing["publicationDate"]}'},
    }
    if listing["pictures"]:
        embed["image"] = {"url": BASE_URL + listing["pictures"][0]["uri"]}
    return embed


def post_to_discord(webhook_url, payload):
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    urllib.request.urlopen(request, timeout=30).close()
    time.sleep(1)  # stay well under the webhook rate limit


def send_message(webhook_url, text, silent=False):
    payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}
    if silent:
        payload["flags"] = SUPPRESS_NOTIFICATIONS
    post_to_discord(webhook_url, payload)


def send_listings(webhook_url, listings, header):
    # Discord allows at most 10 embeds per message.
    for start in range(0, len(listings), 10):
        post_to_discord(webhook_url, {
            "content": header if start == 0 else None,
            "embeds": [build_embed(l) for l in listings[start:start + 10]],
            "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
        })


def notify(webhook_url, listings, config):
    urgent = [l for l in listings if is_first_come(l)]
    other = [l for l in listings if not is_first_come(l)]
    if urgent:
        mention = config.get("urgent_mention") or ""
        header = f"{mention} 🚨 {len(urgent)} new first-come-first-served room(s) on ROOM, respond fast!"
        send_listings(webhook_url, urgent, header.strip())
    if other:
        send_listings(webhook_url, other, f"🏠 {len(other)} new room(s) on ROOM")


def require_webhook():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("DISCORD_WEBHOOK_URL is not set")
    return webhook_url


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return None


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load_status():
    status = {"date": None, "checks": 0, "new": 0, "failures": 0, "consecutive_failures": 0}
    if STATUS_FILE.exists():
        status.update(json.loads(STATUS_FILE.read_text(encoding="utf-8")))
    return status


def save_status(status):
    STATUS_FILE.write_text(json.dumps(status, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def commit_and_push():
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT).returncode

    git("add", SEEN_FILE.name, STATUS_FILE.name)
    if git("diff", "--cached", "--quiet") == 0:
        return
    if git("commit", "-q", "-m", "Update seen listings") != 0:
        return
    if git("pull", "-q", "--rebase") != 0:
        git("rebase", "--abort")
        print("Pull failed, will retry on the next commit")
    elif git("push", "-q") != 0:
        print("Push failed, will retry on the next commit")


def check(config, webhook_url):
    """Notify about new matching listings and update seen.json. Returns (matching, new)."""
    listings = matching_listings(config)
    now = datetime.now(timezone.utc)
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}

    new = [l for l in listings if l["id"] not in seen]
    if new and not first_run:
        notify(webhook_url, new, config)

    for l in new:
        seen[l["id"]] = now.date().isoformat()
    cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save_seen(seen)

    if first_run:
        print(f"First run: recorded {len(new)} existing matching listing(s) without notifying")
        return len(listings), 0
    return len(listings), len(new)


def roll_over_day(webhook_url, status):
    """Post the previous day's summary once the local date changes. Returns True if it did."""
    today = datetime.now(LOCAL_TZ).date().isoformat()
    if status["date"] == today:
        return False
    if status["date"] is not None:
        text = (f'📊 Daily summary for {status["date"]}: {status["checks"]} checks, '
                f'{status["new"]} new listing(s), {status["failures"]} failed check(s)')
        try:
            send_message(webhook_url, text, silent=True)
        except Exception as e:
            print(f"Could not send daily summary: {e!r}")
    status.update(date=today, checks=0, new=0, failures=0)
    return True


def run_loop(duration, interval):
    webhook_url = require_webhook()
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    status = load_status()
    deadline = time.monotonic() + duration

    while True:
        started = time.monotonic()
        changed = roll_over_day(webhook_url, status)
        stamp = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
        try:
            matching, new = check(config, webhook_url)
            print(f"{stamp} {matching} matching listing(s), {new} new")
            status["checks"] += 1
            status["new"] += new
            changed = changed or new > 0
            if status["consecutive_failures"] >= FAILURE_ALERT_AFTER:
                send_message(webhook_url, "✅ ROOM checker recovered, checks are working again")
                changed = True
            status["consecutive_failures"] = 0
        except Exception as e:
            print(f"{stamp} Check failed: {e!r}")
            status["failures"] += 1
            status["consecutive_failures"] += 1
            if status["consecutive_failures"] == FAILURE_ALERT_AFTER:
                try:
                    send_message(webhook_url, f"⚠️ ROOM checker failed {FAILURE_ALERT_AFTER} times "
                                              f"in a row. Last error: `{e!r}`")
                except Exception as e2:
                    print(f"Could not send failure warning: {e2!r}")
                changed = True
        save_status(status)
        if changed:
            commit_and_push()

        next_check = started + interval
        if next_check >= deadline:
            break
        time.sleep(max(0, next_check - time.monotonic()))

    commit_and_push()


def main():
    if "--loop" in sys.argv:
        duration = float(sys.argv[sys.argv.index("--loop") + 1])
        interval = 60.0
        if "--interval" in sys.argv:
            interval = float(sys.argv[sys.argv.index("--interval") + 1])
        run_loop(duration, interval)
        return

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    if "--dry-run" in sys.argv:
        listings = matching_listings(config)
        for l in listings:
            print(f'{l["id"]:>7}  €{l["totalRent"]:>7.2f}  {l["areaDwelling"]:>3} m²  '
                  f'{l["city"]["name"]:<12} {l["street"]} {l["houseNumber"]}  '
                  f'{DETAILS_URL.format(l["urlKey"])}')
        print(f"{len(listings)} matching listing(s)")
        return

    webhook_url = require_webhook()

    if "--test" in sys.argv:
        listings = matching_listings(config)
        if not listings:
            sys.exit("No matching listings to send as a test")
        notify(webhook_url, listings[-1:], config)
        print(f'Sent test notification for listing {listings[-1]["id"]}')
        return

    matching, new = check(config, webhook_url)
    print(f"{matching} matching listing(s), {new} new")


if __name__ == "__main__":
    main()

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dateutil import parser as date_parser
from playwright.sync_api import sync_playwright

STATE_PATH = Path("state/last_seen.json")

ACCOUNTS = [x.strip().lstrip("@") for x in os.getenv("X_ACCOUNTS", "").split(",") if x.strip()]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_LIMIT = int(os.getenv("CHECK_LIMIT", "3"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "45000"))

UTC8 = timezone(timedelta(hours=8))


def log(msg):
    print(msg, flush=True)


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_time_to_utc8(raw_time):
    if not raw_time:
        return "未知时间"
    try:
        dt = date_parser.parse(raw_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(UTC8).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        return raw_time


def shorten(text, max_len=1000):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def send_telegram_message(text):
    import urllib.request
    import urllib.parse

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return body


def format_message(username, tweet):
    tweet_text = shorten(tweet.get("text", "") or "（未抓取到正文）", 1000)
    created_at = tweet.get("created_at", "未知时间")
    tweet_url = tweet.get("url", f"https://x.com/{username}")

    body = [
        "📢 ENI 生态监控提醒",
        "",
        f"👤 发推用户：@{username}",
        f"🕒 发布时间：{created_at}",
        "",
        "📝 推文内容：",
        tweet_text,
        "",
        "🔗 推文原链接：",
        tweet_url,
    ]
    return "\n".join(body)


def extract_tweet_id_from_url(url):
    if not url:
        return None
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def scrape_account(page, username):
    url = f"https://x.com/{username}"
    log(f"[INFO] Visiting {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(5000)

    # 尝试接受可能的登录/弹窗前的页面渲染
    articles = page.locator("article")
    count = articles.count()

    if count == 0:
        # 某些情况下再等一下
        page.wait_for_timeout(5000)
        count = articles.count()

    if count == 0:
        raise RuntimeError(f"No articles found for @{username}")

    tweets = []
    seen_ids = set()

    for i in range(min(count, CHECK_LIMIT * 2)):
        article = articles.nth(i)

        text_parts = article.locator('[data-testid="tweetText"]')
        full_text = ""
        if text_parts.count() > 0:
            parts = []
            for j in range(text_parts.count()):
                try:
                    parts.append(text_parts.nth(j).inner_text().strip())
                except Exception:
                    pass
            full_text = "\n".join([p for p in parts if p.strip()])

        links = article.locator('a[href*="/status/"]')
        tweet_url = None
        tweet_id = None

        for j in range(links.count()):
            try:
                href = links.nth(j).get_attribute("href")
                if href and f"/{username}/status/" in href:
                    tweet_url = href if href.startswith("http") else f"https://x.com{href}"
                    tweet_id = extract_tweet_id_from_url(tweet_url)
                    if tweet_id:
                        break
            except Exception:
                continue

        if not tweet_id or tweet_id in seen_ids:
            continue

        seen_ids.add(tweet_id)

        time_node = article.locator("time")
        raw_time = ""
        if time_node.count() > 0:
            try:
                raw_time = time_node.first.get_attribute("datetime") or ""
            except Exception:
                pass

        tweets.append({
            "id": tweet_id,
            "text": full_text.strip(),
            "url": tweet_url,
            "created_at": parse_time_to_utc8(raw_time),
        })

        if len(tweets) >= CHECK_LIMIT:
            break

    tweets.sort(key=lambda x: int(x["id"]), reverse=True)
    return tweets


def main():
    if not ACCOUNTS:
        raise RuntimeError("X_ACCOUNTS is empty")

    state = load_state()
    changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        for username in ACCOUNTS:
            try:
                log(f"[INFO] Checking @{username}")
                tweets = scrape_account(page, username)

                if not tweets:
                    log(f"[WARN] No tweet data for @{username}")
                    continue

                latest_id = tweets[0]["id"]
                last_seen = state.get(username)

                # 首次运行只初始化，不推送
                if not last_seen:
                    state[username] = latest_id
                    changed = True
                    log(f"[INIT] @{username} -> {latest_id}")
                    continue

                new_tweets = [t for t in tweets if int(t["id"]) > int(last_seen)]

                if not new_tweets:
                    log(f"[OK] @{username} no new tweet")
                    continue

                new_tweets.sort(key=lambda x: int(x["id"]))
                for tweet in new_tweets:
                    msg = format_message(username, tweet)
                    send_telegram_message(msg)
                    log(f"[SEND] @{username} -> {tweet['id']}")
                    time.sleep(2)

                state[username] = latest_id
                changed = True
                log(f"[DONE] @{username} {len(new_tweets)} new tweet(s)")

            except Exception as e:
                log(f"[ERROR] @{username}: {e}")

        context.close()
        browser.close()

    if changed:
        save_state(state)
        log("[INFO] State updated")
    else:
        log("[INFO] No state changes")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[FATAL] {e}")
        sys.exit(1)

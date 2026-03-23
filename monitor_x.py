import json
import os
import re
import time
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state/last_seen.json")

ACCOUNTS = [x.strip().lstrip("@") for x in os.getenv("X_ACCOUNTS", "").split(",") if x.strip()]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_LIMIT = int(os.getenv("CHECK_LIMIT", "5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
RETRY_TIMES = int(os.getenv("RETRY_TIMES", "3"))

MIRRORS = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    )
}


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


def request_with_retry(url, method="GET", json_data=None):
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            if method == "POST":
                r = requests.post(url, headers=HEADERS, json=json_data, timeout=REQUEST_TIMEOUT)
            else:
                r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r
            log(f"[WARN] HTTP {r.status_code} for {url} attempt {attempt}/{RETRY_TIMES}")
        except Exception as e:
            log(f"[WARN] Request failed for {url} attempt {attempt}/{RETRY_TIMES}: {e}")
        time.sleep(2 * attempt)
    return None


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    r = request_with_retry(url, method="POST", json_data=payload)
    if not r:
        raise RuntimeError("Failed to send Telegram message after retries")
    return r.json()


def clean_text(text):
    text = unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten(text, max_len=220):
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def extract_tweets_from_xcancel(html, username, limit=5):
    soup = BeautifulSoup(html, "html.parser")
    tweets = []

    # xcancel / nitter 常见结构
    for article in soup.select("div.timeline-item"):
        link = article.select_one(f'a[href^="/{username}/status/"]')
        if not link:
            continue

        href = link.get("href", "")
        m = re.search(rf"/{re.escape(username)}/status/(\d+)", href)
        if not m:
            continue

        tweet_id = m.group(1)
        text_node = article.select_one("div.tweet-content")
        text = clean_text(text_node.get_text(" ", strip=True) if text_node else "")

        tweet = {
            "id": tweet_id,
            "text": text,
            "url": f"https://x.com/{username}/status/{tweet_id}",
        }
        tweets.append(tweet)

    # 兜底：直接从全文抓 status id
    if not tweets:
        ids = extract_status_ids(html, username)
        for tweet_id in ids[:limit]:
            tweets.append({
                "id": tweet_id,
                "text": "",
                "url": f"https://x.com/{username}/status/{tweet_id}",
            })

    # 去重并排序
    dedup = {}
    for t in tweets:
        dedup[t["id"]] = t
    tweets = list(dedup.values())
    tweets.sort(key=lambda x: int(x["id"]), reverse=True)
    return tweets[:limit]


def extract_status_ids(html, username):
    patterns = [
        rf'/{re.escape(username)}/status/(\d+)',
        rf'https://x\.com/{re.escape(username)}/status/(\d+)',
        rf'https://twitter\.com/{re.escape(username)}/status/(\d+)',
    ]
    ids = []
    for p in patterns:
        ids.extend(re.findall(p, html))
    return sorted(set(ids), key=lambda x: int(x), reverse=True)


def get_latest_tweets(username, limit=5):
    for base in MIRRORS:
        try:
            url = f"{base}/{username}"
            r = request_with_retry(url)
            if not r or not r.text:
                continue

            tweets = extract_tweets_from_xcancel(r.text, username, limit=limit)
            if tweets:
                return tweets, base
            log(f"[WARN] No tweets parsed from {base} for @{username}")
        except Exception as e:
            log(f"[WARN] Failed parsing {base} for @{username}: {e}")
    return [], None


def format_message(username, tweet, source):
    text_preview = shorten(tweet.get("text", ""))
    body = [
        "🚨 X推文更新",
        f"账号: @{username}",
    ]
    if text_preview:
        body.append(f"内容: {text_preview}")
    body.extend([
        f"链接: {tweet['url']}",
        f"来源: {source}",
    ])
    return "\n".join(body)


def main():
    if not ACCOUNTS:
        raise RuntimeError("X_ACCOUNTS is empty")

    state = load_state()
    changed = False

    for username in ACCOUNTS:
        log(f"[INFO] Checking @{username}")
        try:
            tweets, source = get_latest_tweets(username, limit=CHECK_LIMIT)

            if not tweets:
                log(f"[WARN] No tweet data for @{username}")
                continue

            latest_id = tweets[0]["id"]
            last_seen = state.get(username)

            # 首次运行只记录，不推送
            if not last_seen:
                state[username] = latest_id
                changed = True
                log(f"[INIT] @{username} -> {latest_id}")
                continue

            new_tweets = [t for t in tweets if int(t["id"]) > int(last_seen)]

            if not new_tweets:
                log(f"[OK] @{username} no new tweet")
                continue

            # 从旧到新推送
            new_tweets.sort(key=lambda x: int(x["id"]))
            for tweet in new_tweets:
                msg = format_message(username, tweet, source)
                send_telegram(msg)
                log(f"[SEND] @{username} -> {tweet['id']}")
                time.sleep(2)

            state[username] = latest_id
            changed = True
            log(f"[DONE] @{username} {len(new_tweets)} new tweet(s)")

        except Exception as e:
            log(f"[ERROR] @{username}: {e}")

    if changed:
        save_state(state)
        log("[INFO] State updated")
    else:
        log("[INFO] No state changes")


if __name__ == "__main__":
    main()

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

STATE_PATH = Path("state/last_seen.json")

ACCOUNTS = [x.strip().lstrip("@") for x in os.getenv("X_ACCOUNTS", "").split(",") if x.strip()]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_LIMIT = int(os.getenv("CHECK_LIMIT", "5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
RETRY_TIMES = int(os.getenv("RETRY_TIMES", "3"))

UTC8 = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 多源策略：
# 1. xcancel / nitter 镜像
# 2. 搜索页回退
# 3. rsshub twitter route 回退
PROFILE_SOURCES = [
    "https://xcancel.com/{username}",
    "https://nitter.poast.org/{username}",
    "https://nitter.privacydev.net/{username}",
    "https://nitter.1d4.us/{username}",
]

SEARCH_SOURCES = [
    "https://xcancel.com/search?f=tweets&q=from%3A{username}",
    "https://nitter.poast.org/search?f=tweets&q=from%3A{username}",
]

RSS_SOURCES = [
    "https://rsshub.app/twitter/user/{username}",
    "https://rsshub.rssforever.com/twitter/user/{username}",
]

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

def shorten(text, max_len=800):
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."

def parse_time_to_utc8(raw_time):
    if not raw_time:
        return "未知时间"

    try:
        dt = date_parser.parse(raw_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc8 = dt.astimezone(UTC8)
        return dt_utc8.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        return clean_text(raw_time)

def dedup_tweets(tweets):
    m = {}
    for t in tweets:
        tweet_id = t.get("id")
        if not tweet_id:
            continue
        old = m.get(tweet_id, {})
        # 保留信息更完整的版本
        merged = {
            "id": tweet_id,
            "text": t.get("text") or old.get("text") or "",
            "url": t.get("url") or old.get("url") or "",
            "created_at": t.get("created_at") or old.get("created_at") or "",
            "source": t.get("source") or old.get("source") or "",
        }
        m[tweet_id] = merged
    items = list(m.values())
    items.sort(key=lambda x: int(x["id"]), reverse=True)
    return items

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

def parse_profile_html(html, username, source_url):
    soup = BeautifulSoup(html, "html.parser")
    tweets = []

    # xcancel / nitter timeline item
    for item in soup.select("div.timeline-item"):
        link = item.select_one(f'a[href^="/{username}/status/"]')
        if not link:
            continue

        href = link.get("href", "")
        m = re.search(rf"/{re.escape(username)}/status/(\d+)", href)
        if not m:
            continue

        tweet_id = m.group(1)
        text_node = item.select_one("div.tweet-content")
        time_node = item.select_one("span.tweet-date a") or item.select_one("a.tweet-link")
        title_time = ""
        if time_node:
            title_time = time_node.get("title") or time_node.get("href") or ""

        tweets.append({
            "id": tweet_id,
            "text": clean_text(text_node.get_text(" ", strip=True) if text_node else ""),
            "url": f"https://x.com/{username}/status/{tweet_id}",
            "created_at": parse_time_to_utc8(title_time),
            "source": source_url,
        })

    # 回退：OpenGraph / 页面文本提 id
    if not tweets:
        ids = extract_status_ids(html, username)
        for tweet_id in ids[:CHECK_LIMIT]:
            tweets.append({
                "id": tweet_id,
                "text": "",
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "created_at": "未知时间",
                "source": source_url,
            })

    return dedup_tweets(tweets)

def parse_rss(xml_text, username, source_url):
    soup = BeautifulSoup(xml_text, "xml")
    tweets = []

    items = soup.find_all("item")
    for item in items[:CHECK_LIMIT]:
        link = item.find("link")
        title = item.find("title")
        pub_date = item.find("pubDate")

        tweet_url = clean_text(link.text if link else "")
        if not tweet_url:
            continue

        m = re.search(r"/status/(\d+)", tweet_url)
        if not m:
            continue

        tweet_id = m.group(1)
        tweet_text = clean_text(title.text if title else "")
        # RSS title 有时是 "username: text"
        tweet_text = re.sub(rf"^{re.escape(username)}:\s*", "", tweet_text, flags=re.I)

        tweets.append({
            "id": tweet_id,
            "text": tweet_text,
            "url": tweet_url,
            "created_at": parse_time_to_utc8(pub_date.text if pub_date else ""),
            "source": source_url,
        })

    return dedup_tweets(tweets)

def fetch_from_profile_sources(username):
    for template in PROFILE_SOURCES:
        url = template.format(username=username)
        r = request_with_retry(url)
        if not r or not r.text:
            continue
        tweets = parse_profile_html(r.text, username, url)
        if tweets:
            log(f"[INFO] Profile source ok for @{username}: {url}")
            return tweets
    return []

def fetch_from_search_sources(username):
    for template in SEARCH_SOURCES:
        url = template.format(username=quote(username))
        r = request_with_retry(url)
        if not r or not r.text:
            continue
        tweets = parse_profile_html(r.text, username, url)
        if tweets:
            log(f"[INFO] Search source ok for @{username}: {url}")
            return tweets
    return []

def fetch_from_rss_sources(username):
    for template in RSS_SOURCES:
        url = template.format(username=username)
        r = request_with_retry(url)
        if not r or not r.text:
            continue
        tweets = parse_rss(r.text, username, url)
        if tweets:
            log(f"[INFO] RSS source ok for @{username}: {url}")
            return tweets
    return []

def get_latest_tweets(username):
    tweets = fetch_from_profile_sources(username)
    if tweets:
        return tweets[:CHECK_LIMIT]

    tweets = fetch_from_search_sources(username)
    if tweets:
        return tweets[:CHECK_LIMIT]

    tweets = fetch_from_rss_sources(username)
    if tweets:
        return tweets[:CHECK_LIMIT]

    return []

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

def main():
    if not ACCOUNTS:
        raise RuntimeError("X_ACCOUNTS is empty")

    state = load_state()
    changed = False

    for username in ACCOUNTS:
        log(f"[INFO] Checking @{username}")
        try:
            tweets = get_latest_tweets(username)

            if not tweets:
                log(f"[WARN] No tweet data for @{username}")
                continue

            latest_id = tweets[0]["id"]
            last_seen = state.get(username)

            # 首次运行仅记录
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

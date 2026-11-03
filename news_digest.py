"""
每日國際科技新聞整理 → 中英對照 → 發送到 Discord

流程：抓 RSS → 篩選過去 24 小時的新聞 → 請 AI 挑重點並翻譯 → 用 Webhook 發到 Discord
預設使用 Google Gemini API 免費方案（需要 GEMINI_API_KEY）
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# ───────────── 可自行調整的設定 ─────────────

RSS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "MIT Technology Review": "https://www.technologyreview.com/feed/",
    "Hacker News (100+ 分)": "https://hnrss.org/frontpage?points=100",
}

TOP_N = 5                 # 每天挑幾則
MAX_PER_FEED = 8          # 每個來源最多取幾則候選（控制送給 AI 的長度）
LOOKBACK_HOURS = 24       # 只看過去多少小時的新聞
FOCUS = "AI、半導體、能源與儲能、資安、重大科技公司動態"   # 想優先關注的主題

# AI 供應商設定（可在 workflow 的 env 覆寫）
PROVIDER = os.getenv("AI_PROVIDER", "gemini")
PROVIDERS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        # gemini-flash-latest 會自動指向最新的 Flash 模型，不怕舊型號下架
        "model": os.getenv("AI_MODEL") or "gemini-flash-latest",
    },
}

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DRY_RUN = os.getenv("DRY_RUN") == "1"   # 設為 1 時只印出結果、不發到 Discord

# ───────────── 1. 抓 RSS ─────────────

def clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_articles():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    articles, seen_titles = [], set()

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (news-digest-bot)")
        except Exception as e:
            print(f"[警告] 無法讀取 {source}: {e}")
            continue

        count = 0
        for entry in feed.entries:
            if count >= MAX_PER_FEED:
                break
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            if t and datetime(*t[:6], tzinfo=timezone.utc) < cutoff:
                continue
            title = clean(entry.get("title", ""), 200)
            key = title.lower()
            if not title or key in seen_titles:      # 簡單去重
                continue
            seen_titles.add(key)
            articles.append({
                "id": len(articles),
                "source": source,
                "title": title,
                "summary": clean(entry.get("summary", "")),
                "link": entry.get("link", ""),
            })
            count += 1
        print(f"{source}: 取得 {count} 則")

    return articles

# ───────────── 2. 請 AI 整理＋翻譯 ─────────────

PROMPT = """你是專業的科技新聞編輯。以下是過去 24 小時的國際科技新聞候選清單（JSON）。

請挑出最重要的 {n} 則，優先考慮：{focus}；同一事件只保留一則。
每則輸出中英對照內容，翻譯使用台灣繁體中文用語，專有名詞（如 GPU、LLM、公司名）保留英文。

只輸出 JSON，不要加任何說明或 ```，格式如下：
{{"items": [
  {{"id": 候選的 id 數字,
    "title_en": "英文標題",
    "title_zh": "中文標題",
    "points": [{{"en": "英文重點一句", "zh": "對應中文翻譯"}}, ...共 3 句],
    "why_zh": "一句中文說明為什麼重要"}}
]}}

候選清單：
{articles}
"""


def ask_ai(articles):
    cfg = PROVIDERS[PROVIDER]
    api_key = os.getenv(cfg["key_env"])
    if not api_key:
        sys.exit(f"[錯誤] 找不到環境變數 {cfg['key_env']}")

    compact = [{k: a[k] for k in ("id", "source", "title", "summary")} for a in articles]
    prompt = PROMPT.format(n=TOP_N, focus=FOCUS,
                           articles=json.dumps(compact, ensure_ascii=False))

    for attempt in range(3):
        resp = requests.post(
            cfg["url"],
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": cfg["model"],
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3},
            timeout=120,
        )
        if resp.status_code == 429:           # 速率限制：等一下再試
            print("[提示] 遇到速率限制，60 秒後重試")
            time.sleep(60)
            continue
        if not resp.ok:
            sys.exit(f"[錯誤] AI API 回傳 {resp.status_code}：{resp.text[:500]}")
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            print(f"[警告] AI 回應無法解析：{resp.text[:500]}")
            continue
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            return json.loads(text)["items"]
        except (json.JSONDecodeError, KeyError):
            print(f"[警告] AI 回傳格式不正確，重試（第 {attempt + 1} 次）")
    sys.exit("[錯誤] AI 呼叫失敗")

# ───────────── 3. 發送到 Discord ─────────────

def post_discord(payload):
    if DRY_RUN:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    time.sleep(1)   # 避免連發太快


def send_to_discord(items, articles):
    if not DISCORD_WEBHOOK_URL and not DRY_RUN:
        sys.exit("[錯誤] 找不到 DISCORD_WEBHOOK_URL")

    tw_today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y/%m/%d")
    post_discord({"content": f"## 📰 國際科技新聞 Daily Tech Digest｜{tw_today}"})

    by_id = {a["id"]: a for a in articles}
    for i, item in enumerate(items, 1):
        src = by_id.get(item.get("id"), {})
        lines = []
        for p in item.get("points", []):
            lines.append(f"• {p.get('en', '')}\n　{p.get('zh', '')}")
        lines.append(f"\n💡 **為什麼重要：** {item.get('why_zh', '')}")

        post_discord({"embeds": [{
            "title": f"{i}. {item.get('title_zh', '')}"[:256],
            "url": src.get("link") or None,
            "description": (f"*{item.get('title_en', '')}*\n\n" + "\n".join(lines))[:4000],
            "footer": {"text": f"來源 Source：{src.get('source', '')}"},
            "color": 0x5865F2,
        }]})

# ───────────── 主程式 ─────────────

def main():
    articles = fetch_articles()
    print(f"共 {len(articles)} 則候選新聞")
    if not articles:
        print("今天沒有新文章，結束。")
        return
    items = ask_ai(articles)
    send_to_discord(items, articles)
    print(f"完成，已發送 {len(items)} 則")


if __name__ == "__main__":
    main()

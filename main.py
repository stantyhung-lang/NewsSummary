"""
新聞彙整系統 - FastAPI 後端
部署於 Render，使用 Gemini API（含 Google Search grounding）搜尋並彙整當日新聞。

環境變數：
    GEMINI_API_KEY              - 必填，Gemini API 金鑰
    LINE_CHANNEL_ACCESS_TOKEN   - 必填，LINE Messaging API Channel Access Token
    LINE_USER_ID                - 必填，推播目標的 LINE User ID

端點：
    GET  /              健康檢查
    GET  /digest        取得最新快取彙整結果（前端用）
    POST /run-digest    觸發彙整、更新快取、推播 LINE（Render Cron Job 呼叫）
    GET  /test-digest   單一分類快速測試，不更新快取、不推播
"""

import os
import json
import logging
import urllib.request
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-digest")

app = FastAPI(title="News Digest API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

API_KEY        = os.environ.get("GEMINI_API_KEY")
LINE_TOKEN     = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID   = os.environ.get("LINE_USER_ID")
FRONTEND_URL   = "https://stantyhung-lang.github.io/NewsSummary/"
MODEL_NAME     = "gemini-2.5-flash"

_cache: dict = {
    "generated_at": None,
    "categories": {},
}

CATEGORIES = {
    "world":  "今天全球發生的重大國際新聞（政治、經濟、社會、災難等級事件，非地緣政治、非科技類別），列出 3-5 則",
    "geo":    "今天的地緣政治新聞（國家間軍事、外交、戰爭、貿易制裁、領土爭議等），列出 3-5 則",
    "tech":   "今天全球重大科技新聞（AI、半導體、大型科技公司、重要產品發布），列出 3-5 則，優先引用 BBC、Reuters、The Verge、TechCrunch、Wired、Bloomberg 等英文主流媒體",
    "taiwan": "今天台灣本地的重大新聞（政治、經濟、社會、產業、天氣災害、兩岸關係等），列出 8-12 則，涵蓋面向盡量廣泛",
}

CATEGORY_LABELS = {
    "world":  "國際重大",
    "geo":    "地緣政治",
    "tech":   "科技",
    "taiwan": "台灣",
}

CATEGORY_EMOJI = {
    "world":  "🌐",
    "geo":    "📍",
    "tech":   "💻",
    "taiwan": "📌",
}

SYSTEM_PROMPT = """你是一個專業的新聞編輯助手，負責彙整當天的重大新聞。
請使用搜尋工具找出今天真實發生的新聞，不要使用你訓練資料中的舊新聞或捏造內容。

請以下列 JSON 格式輸出（不要有任何 markdown 標記或前後綴文字，只輸出純 JSON）：

{
  "items": [
    {
      "title": "新聞標題（簡潔有力，20字以內，一律使用繁體中文）",
      "summary": "三到五句話的摘要，需包含事件背景、關鍵細節、可能影響，一律使用繁體中文書寫，若原始來源為英文請翻譯成繁體中文",
      "source": "新聞來源媒體名稱",
      "time": "HH:MM"
    }
  ]
}

注意事項：
1. 必須是今天的新聞，不要使用過時或捏造的內容
2. summary 禁止逐字複製原文，必須用自己的話改寫，並全程使用繁體中文
3. title 若原始來源為英文，請翻譯成繁體中文
4. time 欄位只能填 HH:MM 格式的時間（如 09:30），若無法從新聞確認確切時間，請填入當前台灣時間（UTC+8）
5. 如果某類別找不到足夠的當日重大新聞，寧可少給也不要硬湊或捏造
6. 嚴格輸出合法 JSON，不要加註解或多餘文字
"""

TW_TZ = timezone(timedelta(hours=8))


def tw_now() -> datetime:
    return datetime.now(TW_TZ)


def normalize_time(time_str: str) -> str:
    import re
    if re.match(r"^\d{2}:\d{2}$", (time_str or "").strip()):
        return time_str.strip()
    return tw_now().strftime("%H:%M")


def get_gemini_client() -> genai.Client:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="未設定 GEMINI_API_KEY")
    return genai.Client(api_key=API_KEY)


# ── LINE 推播 ────────────────────────────────────────

def build_line_message(digest: dict) -> str:
    """將彙整結果組成 LINE 推播文字，每類取前兩則標題"""
    now = tw_now()
    date_str = f"{now.month}月{now.day}日"
    weekdays = ["週日","週一","週二","週三","週四","週五","週六"]
    lines = [
        f"📰 {date_str} {weekdays[now.weekday()]} 晚間新聞彙整",
        "─────────────────",
    ]

    cats = digest.get("categories", {})
    for key in ["world", "geo", "tech", "taiwan"]:
        cat_data = cats.get(key, {})
        items = cat_data.get("items", [])
        if not items:
            continue
        emoji = CATEGORY_EMOJI[key]
        label = CATEGORY_LABELS[key]
        lines.append(f"\n{emoji} {label}")
        for item in items[:2]:          # 每類取前兩則
            lines.append(f"・{item['title']}")

    lines.append("\n─────────────────")
    lines.append(f"📖 看完整新聞\n{FRONTEND_URL}")
    return "\n".join(lines)


def push_line_message(text: str) -> bool:
    """用 Messaging API push message 給指定 User ID，成功回傳 True"""
    if not LINE_TOKEN or not LINE_USER_ID:
        logger.warning("未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，跳過推播")
        return False

    payload = json.dumps({
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            logger.info(f"LINE 推播成功，狀態碼：{res.status}")
            return True
    except Exception as e:
        logger.error(f"LINE 推播失敗：{e}")
        return False


# ── 新聞彙整核心 ─────────────────────────────────────

def fetch_category_news(client: genai.Client, category_key: str, category_desc: str) -> dict:
    today_str = tw_now().strftime("%Y年%m月%d日")
    user_prompt = f"今天是{today_str}（台灣時間 UTC+8）。請搜尋並彙整：{category_desc}"

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3,
        ),
    )

    raw_text = (response.text or "").strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.warning(f"{category_key} JSON 解析失敗: {e}")
        parsed = {"items": [], "error": str(e)}

    for item in parsed.get("items", []):
        item["time"] = normalize_time(item.get("time", ""))

    parsed["label"] = CATEGORY_LABELS.get(category_key, category_key)
    return parsed


def run_full_digest() -> dict:
    """跑完四個分類，更新快取，回傳結果"""
    client = get_gemini_client()
    result = {}
    keys = list(CATEGORIES.keys())
    for i, (key, desc) in enumerate(CATEGORIES.items()):
        logger.info(f"彙整分類：{key}")
        try:
            result[key] = fetch_category_news(client, key, desc)
        except Exception as e:
            logger.error(f"{key} 失敗: {e}")
            result[key] = {"items": [], "label": CATEGORY_LABELS[key], "error": str(e)}
        # 每個分類之間等待 5 秒，避免觸發 Gemini API rate limit
        if i < len(keys) - 1:
            logger.info("等待 5 秒後處理下一個分類...")
            time.sleep(5)

    generated_at = tw_now().isoformat()
    _cache["generated_at"] = generated_at
    _cache["categories"] = result
    return {"generated_at": generated_at, "categories": result}


# ── 端點 ─────────────────────────────────────────────

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "cache_generated_at": _cache["generated_at"],
        "time": tw_now().isoformat(),
    }


@app.get("/digest")
def get_digest():
    if not _cache["generated_at"]:
        raise HTTPException(
            status_code=404,
            detail="尚無彙整資料，請先呼叫 POST /run-digest",
        )
    return JSONResponse(content=_cache)


@app.post("/run-digest")
def trigger_digest():
    """Render Cron Job 每天定時呼叫此端點：彙整新聞 → 更新快取 → 推播 LINE"""
    logger.info("開始執行完整新聞彙整...")
    digest = run_full_digest()
    logger.info("彙整完成，準備推播 LINE")

    message = build_line_message(digest)
    push_line_message(message)

    return JSONResponse(content=digest)


@app.get("/test-digest")
def test_digest(category: Optional[str] = None):
    """單一分類快速測試，不更新快取、不推播 LINE"""
    if category and category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"未知分類 '{category}'，可用分類：{list(CATEGORIES.keys())}",
        )
    client = get_gemini_client()
    targets = {category: CATEGORIES[category]} if category else CATEGORIES
    result = {}
    for key, desc in targets.items():
        try:
            result[key] = fetch_category_news(client, key, desc)
        except Exception as e:
            result[key] = {"items": [], "label": CATEGORY_LABELS[key], "error": str(e)}
    return JSONResponse(content={"generated_at": tw_now().isoformat(), "categories": result})


@app.post("/test-line")
def test_line_push():
    """測試 LINE 推播是否正常，用假訊息打通連線"""
    test_msg = "✅ LINE 推播測試成功！\n新聞彙整小幫手已就緒。"
    ok = push_line_message(test_msg)
    if ok:
        return {"status": "success", "message": "LINE 推播成功"}
    raise HTTPException(status_code=500, detail="LINE 推播失敗，請確認環境變數設定")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

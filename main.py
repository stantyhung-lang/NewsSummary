"""
新聞彙整系統 - FastAPI 後端
部署於 Render，使用 Gemini API（含 Google Search grounding）搜尋並彙整當日新聞。

環境變數：
    GEMINI_API_KEY  - 必填，Gemini API 金鑰

端點：
    GET  /              健康檢查
    GET  /digest        取得最新快取彙整結果（前端用）
    POST /run-digest    觸發彙整並更新快取（定時排程 or 手動呼叫）
    GET  /test-digest   指定單一分類測試，不更新快取
"""

import os
import json
import logging
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

# 允許前端跨域存取
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.5-flash"

# 記憶體快取，存放最新一次彙整結果
_cache: dict = {
    "generated_at": None,
    "categories": {},
}

CATEGORIES = {
    "world": "今天全球發生的重大國際新聞（政治、經濟、社會、災難等級事件，非地緣政治、非科技類別），列出 3-5 則",
    "geo":   "今天的地緣政治新聞（國家間軍事、外交、戰爭、貿易制裁、領土爭議等），列出 3-5 則",
    "tech":  "今天全球重大科技新聞（AI、半導體、大型科技公司、重要產品發布），列出 3-5 則，優先引用 BBC、Reuters、The Verge、TechCrunch、Wired、Bloomberg 等英文主流媒體",
    "taiwan":"今天台灣本地的重大新聞（政治、經濟、社會、產業、天氣災害、兩岸關係等），列出 8-12 則，涵蓋面向盡量廣泛",
}

CATEGORY_LABELS = {
    "world":  "國際重大",
    "geo":    "地緣政治",
    "tech":   "科技",
    "taiwan": "台灣",
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


def get_client() -> genai.Client:
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="伺服器未設定 GEMINI_API_KEY 環境變數，請在 Render 的 Environment 設定中加入。",
        )
    return genai.Client(api_key=API_KEY)


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
    """跑完四個分類，回傳結果並更新快取"""
    client = get_client()
    result = {}
    for key, desc in CATEGORIES.items():
        logger.info(f"彙整分類：{key}")
        try:
            result[key] = fetch_category_news(client, key, desc)
        except Exception as e:
            logger.error(f"{key} 失敗: {e}")
            result[key] = {"items": [], "label": CATEGORY_LABELS[key], "error": str(e)}

    generated_at = tw_now().isoformat()
    _cache["generated_at"] = generated_at
    _cache["categories"] = result
    return {"generated_at": generated_at, "categories": result}


# ── 端點 ────────────────────────────────────────────

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "cache_generated_at": _cache["generated_at"],
        "time": tw_now().isoformat(),
    }


@app.get("/digest")
def get_digest():
    """前端呼叫：取得快取的彙整結果。若尚未產生快取，回傳 404。"""
    if not _cache["generated_at"]:
        raise HTTPException(
            status_code=404,
            detail="尚無彙整資料，請先呼叫 POST /run-digest 產生第一筆資料。",
        )
    return JSONResponse(content=_cache)


@app.post("/run-digest")
def trigger_digest():
    """觸發完整彙整並更新快取。Render Cron Job 或手動 POST 呼叫。"""
    logger.info("開始執行完整新聞彙整...")
    result = run_full_digest()
    logger.info("彙整完成")
    return JSONResponse(content=result)


@app.get("/test-digest")
def test_digest(category: Optional[str] = None):
    """單一分類快速測試，不更新快取。"""
    if category and category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"未知分類 '{category}'，可用分類：{list(CATEGORIES.keys())}",
        )
    client = get_client()
    targets = {category: CATEGORIES[category]} if category else CATEGORIES
    result = {}
    for key, desc in targets.items():
        try:
            result[key] = fetch_category_news(client, key, desc)
        except Exception as e:
            result[key] = {"items": [], "label": CATEGORY_LABELS[key], "error": str(e)}
    return JSONResponse(content={"generated_at": tw_now().isoformat(), "categories": result})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

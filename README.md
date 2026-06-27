# News Digest API

每日新聞彙整系統後端。使用 Gemini API（含 Google Search grounding）搜尋並彙整當日的國際重大新聞、地緣政治、科技、台灣新聞。

## 部署到 Render

1. 把這個資料夾建成一個新的 GitHub repo，push 上去。
2. 到 Render 建立新的 **Web Service**，連接這個 repo。
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. 在 Render 的 **Environment** 設定中加入環境變數：
   - `GEMINI_API_KEY`：你的 Gemini API 金鑰
6. 部署完成後，Render 會給你一個網址，例如 `https://news-digest-xxxx.onrender.com`

## 測試方式

部署完成後，直接用瀏覽器打開：

```
https://你的render網址/test-digest
```

會回傳四個分類（world / geo / tech / taiwan）的彙整 JSON 結果，每個分類預計需要等待數秒到 10 幾秒（因為要等 Gemini 搜尋並彙整）。

如果只想測單一分類（速度較快，方便除錯）：

```
https://你的render網址/test-digest?category=tech
```

## 已知注意事項

- Render 免費方案有 spin-down 機制，閒置一段時間後會休眠，第一次請求會比較慢（cold start）。
- 若回傳 `"error": "ClientError: ..."`，通常代表 API Key 無效、配額用盡，或是 Gemini API 服務本身的問題，可以看錯誤訊息細節判斷。
- 若 JSON 解析失敗（`raw` 欄位會出現原始文字），代表 Gemini 沒有照指示輸出純 JSON，可能需要微調 prompt 或加 retry 邏輯。

## 下一步（尚未實作）

- [ ] LINE Bot 推播整合（複用你旅遊助手專案的 LINE Messaging API 邏輯）
- [ ] Render Cron Job 排程，每天晚上 8-9 點自動觸發
- [ ] 資料庫儲存歷史彙整記錄（可用 Supabase）
- [ ] 前端網頁呈現（手機卡片版面，已用 Claude Artifact 驗證過設計）

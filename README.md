# 🎙 Podcast AI 摘要 — Streamlit 版

輸入 Apple Podcasts / RSS / MP3 連結，自動轉錄並生成 Podket 風格摘要。

## 部署到 Streamlit Cloud（免費）

### 第一步：上傳到 GitHub

1. 登入 [github.com](https://github.com)，點右上角 **+** → **New repository**
2. Repository name 填 `podcast-summarizer`，選 **Public**，按 **Create**
3. 把這個資料夾的三個檔案上傳：
   - `app.py`
   - `requirements.txt`
   - `README.md`

   方法：點 **Add file** → **Upload files** → 拖曳三個檔案進去 → **Commit changes**

### 第二步：部署到 Streamlit Cloud

1. 前往 [share.streamlit.io](https://share.streamlit.io)，用 GitHub 帳號登入
2. 點 **New app**
3. 填入：
   - Repository：`你的帳號/podcast-summarizer`
   - Branch：`main`
   - Main file path：`app.py`
4. 按 **Deploy!**
5. 等約 3 分鐘，得到一個公開網址

### 第三步：使用

1. 開啟你的 Streamlit 網址
2. 左側填入 Anthropic API Key
3. 貼上 Apple Podcasts 連結
4. 按「開始分析」

---

## 本地執行

```bash
pip install streamlit openai-whisper anthropic
streamlit run app.py
```

---

## 注意事項

- Streamlit Cloud 免費版記憶體約 1GB，建議用 `base` 模型
- API Key 不會被儲存，每次使用需重新輸入
- 音檔在 Streamlit Cloud 伺服器上處理，不經過你的電腦

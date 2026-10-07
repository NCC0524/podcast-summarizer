import streamlit as st
import os, re, json, tempfile, urllib.request, urllib.parse, subprocess, shutil
import xml.etree.ElementTree as ET
from pathlib import Path

# ── ffmpeg 路徑修正（Railway / Nix 環境）─────────────────────────
def _fix_ffmpeg_path():
    """確保 ffmpeg 在 PATH 裡，找不到就用 which/where 找"""
    if shutil.which("ffmpeg"):
        return  # 已經在 PATH，沒問題
    # Nix 環境常見路徑
    candidates = [
        "/nix/var/nix/profiles/default/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for p in candidates:
        if Path(p).exists():
            os.environ["PATH"] = str(Path(p).parent) + ":" + os.environ.get("PATH", "")
            return
    # 最後嘗試 find
    try:
        result = subprocess.run(["find", "/nix", "-name", "ffmpeg", "-type", "f"],
                                capture_output=True, text=True, timeout=10)
        for line in result.stdout.strip().splitlines():
            if line.endswith("/ffmpeg"):
                os.environ["PATH"] = str(Path(line).parent) + ":" + os.environ.get("PATH", "")
                return
    except Exception:
        pass

_fix_ffmpeg_path()

st.set_page_config(
    page_title="Podcast AI 摘要",
    page_icon="🎙",
    layout="centered",
)

# ── 樣式 ──────────────────────────────────────────────────────────
st.markdown("""
<style>
.main { max-width: 760px; }
.stTextInput>div>div>input { font-size: 14px; }
.report-box {
    background: #f8f9fa;
    border: 1px solid #e0e0e0;
    border-radius: 10px;
    padding: 1.5rem 2rem;
    font-size: 14px;
    line-height: 1.8;
}
.tag {
    display: inline-block;
    background: #e8f0fe;
    color: #1a73e8;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 500;
    margin-bottom: 8px;
}
.stock-bull { color: #0f6e56; font-weight: 500; }
.stock-bear { color: #c0392b; font-weight: 500; }
.stock-neu  { color: #666;    font-weight: 500; }
.section-title {
    font-size: 13px;
    font-weight: 600;
    color: #444;
    border-bottom: 1px solid #eee;
    padding-bottom: 4px;
    margin: 1rem 0 0.5rem 0;
}
</style>
""", unsafe_allow_html=True)

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 工具函式 ──────────────────────────────────────────────────────

def parse_apple_url(url):
    pid = re.search(r'/id(\d+)', url)
    parsed = urllib.parse.urlparse(url)
    eid = urllib.parse.parse_qs(parsed.query).get("i", [None])[0]
    return (pid.group(1) if pid else None), eid

def get_rss_from_apple_id(podcast_id):
    api = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"
    with urllib.request.urlopen(urllib.request.Request(api, headers=HEADERS), timeout=15) as r:
        data = json.loads(r.read())
    results = data.get("results", [])
    if not results:
        raise ValueError("iTunes API 找不到此節目")
    feed = results[0].get("feedUrl", "")
    if not feed:
        raise ValueError("iTunes API 沒有回傳 RSS Feed URL")
    return feed

def parse_rss(rss_url):
    with urllib.request.urlopen(urllib.request.Request(rss_url, headers=HEADERS), timeout=20) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    ch = root.find("channel")
    show = ch.findtext("title", "未知節目").strip()
    episodes = []
    for item in ch.findall("item")[:20]:
        enc = item.find("enclosure")
        if enc is None: continue
        mp3 = enc.get("url", "")
        if not mp3: continue
        dur = item.find("itunes:duration", ns)
        episodes.append({
            "show": show,
            "title": item.findtext("title", "無標題").strip(),
            "url": mp3,
            "guid": item.findtext("guid", "").strip(),
            "duration": dur.text.strip() if dur is not None else "",
            "pub_date": item.findtext("pubDate", "").strip(),
        })
    return episodes

def find_episode(rss_url, episode_id, podcast_id):
    eps = parse_rss(rss_url)
    if not eps:
        raise ValueError("RSS 中找不到音檔")
    if episode_id:
        for ep in eps:
            if episode_id in ep["guid"] or episode_id in ep["url"]:
                return ep, eps
        # 用 iTunes Episode API 比對
        try:
            api = f"https://itunes.apple.com/lookup?id={episode_id}&entity=podcastEpisode"
            with urllib.request.urlopen(urllib.request.Request(api, headers=HEADERS), timeout=10) as r:
                d = json.loads(r.read())
            if d.get("results"):
                t = d["results"][0].get("trackName", "").strip()
                for ep in eps:
                    if t.lower() in ep["title"].lower() or ep["title"].lower() in t.lower():
                        return ep, eps
        except Exception:
            pass
    return eps[0], eps  # fallback：最新一集

def resolve_url(url):
    low = url.lower()
    if re.search(r'\.(mp3|m4a|aac|ogg)(\?|$)', low):
        return {"show": "自訂音檔", "title": Path(url.split("?")[0]).stem, "url": url}, None
    if "podcasts.apple.com" in low:
        pid, eid = parse_apple_url(url)
        if not pid:
            raise ValueError("無法解析 Apple Podcasts URL")
        rss = get_rss_from_apple_id(pid)
        ep, all_eps = find_episode(rss, eid, pid)
        return ep, all_eps
    # 當 RSS 處理
    eps = parse_rss(url)
    if not eps:
        raise ValueError("RSS 中找不到音檔")
    return eps[0], eps

def download_audio(url, dest, progress_cb=None):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk: break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(downloaded / total)

def transcribe(audio_path, model_size="base"):
    import whisper
    model = whisper.load_model(model_size)
    result = model.transcribe(str(audio_path), language="zh", verbose=False, fp16=False)
    return result["text"].strip()

def claude_summarize(transcript, ep_title, show_name, api_key):
    import anthropic
    MAX = 15000
    if len(transcript) > MAX:
        transcript = transcript[:MAX] + "\n\n[...節錄...]"

    prompt = f"""你是一個專業財經 Podcast 分析師，風格類似 Podket。請根據以下逐字稿生成一份深度摘要報告。

節目：{show_name}
集數標題：{ep_title}

=== 逐字稿 ===
{transcript}
=== 結束 ===

請用繁體中文，以 JSON 格式輸出（不要加 markdown 反引號）：

{{
  "title": "分析角度標題（一句話，能引發思考）",
  "category": "MARKETS / MACRO / SEMICON / CRYPTO / REALESTATE / OTHER 之一",
  "tldr": "三到五句核心論點摘要，要有具體觀點",
  "key_points": ["重點一", "重點二", "重點三"],
  "insight_strength": "主持人分析中最值得學習的觀點（一到兩句）",
  "insight_caveat": "主持人論點的盲點或不足（一到兩句）",
  "stocks": [
    {{"ticker": "代碼", "name": "公司名", "stance": "bullish/bearish/neutral", "reason": "一句話"}}
  ],
  "anchor_prop": "最有爭議性的核心命題（一句話）",
  "macro_theme": "本集總經或產業主題",
  "notable_quotes": ["金句一（若有）"]
}}

注意：stocks 若無明確個股回傳 []，notable_quotes 若無回傳 []，所有內容忠實反映逐字稿。"""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ── UI ────────────────────────────────────────────────────────────

st.title("🎙 Podcast AI 摘要")
st.caption("輸入 Apple Podcasts / RSS / MP3 連結，自動轉錄並生成 Podket 風格摘要")

with st.sidebar:
    st.header("⚙️ 設定")
    api_key = st.text_input(
        "Anthropic API Key",
        type="password",
        placeholder="sk-ant-...",
        help="前往 console.anthropic.com/settings/keys 取得"
    )
    model_size = st.selectbox(
        "Whisper 模型",
        ["tiny", "base", "small", "medium"],
        index=1,
        help="base=快速、small=平衡、medium=中文最準（記憶體需求較高）"
    )
    st.divider()
    st.markdown("**費用估算**")
    st.markdown("- Whisper：免費（本地）\n- Claude：約 NT$1–3 / 集")
    st.divider()
    st.markdown("**支援格式**")
    st.markdown("- Apple Podcasts 連結\n- RSS Feed URL\n- 直接 MP3 網址")

url_input = st.text_input(
    "Podcast 連結",
    placeholder="https://podcasts.apple.com/tw/podcast/...",
)

col1, col2 = st.columns([1, 3])
run_btn = col1.button("▶ 開始分析", type="primary", use_container_width=True)

# 若有多集可選
if "all_eps" in st.session_state and st.session_state.all_eps:
    eps = st.session_state.all_eps
    options = [f"[{i+1}] {e['title']} ({e['duration']})" for i, e in enumerate(eps)]
    chosen = st.selectbox("選擇集數", options, index=0)
    chosen_idx = options.index(chosen)
    st.session_state.chosen_ep = eps[chosen_idx]

if run_btn:
    if not api_key:
        st.error("請在左側填入 Anthropic API Key")
        st.stop()
    if not url_input.strip():
        st.error("請輸入 Podcast 連結")
        st.stop()

    st.session_state.pop("result", None)
    st.session_state.pop("all_eps", None)

    with st.status("🔍 解析連結中...", expanded=True) as status:
        try:
            ep, all_eps = resolve_url(url_input.strip())
            st.write(f"✅ 節目：**{ep['show']}**")
            st.write(f"✅ 集數：**{ep['title']}**")
            if all_eps and len(all_eps) > 1:
                st.session_state.all_eps = all_eps
        except Exception as e:
            status.update(label="解析失敗", state="error")
            st.error(f"連結解析失敗：{e}")
            st.stop()

        st.write("⬇️ 下載音檔中...")
        suffix = ".mp3"
        for ext in [".mp3", ".m4a", ".aac"]:
            if ext in ep["url"].lower():
                suffix = ext; break

        tmp_dir = tempfile.mkdtemp()
        audio_path = Path(tmp_dir) / f"episode{suffix}"
        prog_bar = st.progress(0, text="下載中...")
        try:
            download_audio(ep["url"], audio_path,
                           progress_cb=lambda p: prog_bar.progress(p, text=f"下載中 {p*100:.0f}%"))
            prog_bar.progress(1.0, text="下載完成 ✅")
        except Exception as e:
            status.update(label="下載失敗", state="error")
            st.error(f"音檔下載失敗：{e}")
            st.stop()

        st.write(f"🎤 Whisper 轉錄中（{model_size} 模型）...")
        try:
            transcript = transcribe(audio_path, model_size)
            st.write(f"✅ 轉錄完成，共 {len(transcript):,} 字")
        except Exception as e:
            status.update(label="轉錄失敗", state="error")
            st.error(f"Whisper 轉錄失敗：{e}")
            st.stop()

        st.write("🤖 Claude 分析摘要中...")
        try:
            data = claude_summarize(transcript, ep["title"], ep["show"], api_key)
            st.session_state.result = {"data": data, "ep": ep, "transcript": transcript}
            status.update(label="✅ 分析完成！", state="complete", expanded=False)
        except Exception as e:
            status.update(label="分析失敗", state="error")
            st.error(f"Claude 分析失敗：{e}")
            st.stop()

# ── 顯示結果 ─────────────────────────────────────────────────────

if "result" in st.session_state:
    d = st.session_state.result["data"]
    ep = st.session_state.result["ep"]
    transcript = st.session_state.result["transcript"]

    st.divider()

    # 標題區
    st.markdown(f'<span class="tag">{d.get("category","")}</span>', unsafe_allow_html=True)
    st.subheader(d.get("title", ep["title"]))
    st.caption(f"{ep['show']}  ·  {ep.get('pub_date','')[:16]}")

    # TL;DR
    st.markdown('<div class="section-title">TL;DR</div>', unsafe_allow_html=True)
    st.markdown(d.get("tldr", ""))

    # 重點整理
    st.markdown('<div class="section-title">重點整理</div>', unsafe_allow_html=True)
    for pt in d.get("key_points", []):
        st.markdown(f"- {pt}")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('<div class="section-title">✦ 分析亮點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_strength", "—"))
    with col_b:
        st.markdown('<div class="section-title">⚠ 潛在盲點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_caveat", "—"))

    # 個股
    stocks = d.get("stocks", [])
    if stocks:
        st.markdown('<div class="section-title">提及個股</div>', unsafe_allow_html=True)
        scols = st.columns(len(stocks))
        stance_icon = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}
        for i, s in enumerate(stocks):
            with scols[i]:
                icon = stance_icon.get(s.get("stance",""), "")
                st.markdown(f"**{icon} {s['ticker']}**  \n{s['name']}  \n_{s['reason']}_")

    # 金句
    quotes = d.get("notable_quotes", [])
    if quotes:
        st.markdown('<div class="section-title">金句</div>', unsafe_allow_html=True)
        for q in quotes:
            st.info(f"「{q}」")

    # 辯論命題
    if d.get("anchor_prop"):
        st.markdown('<div class="section-title">核心辯論命題</div>', unsafe_allow_html=True)
        st.warning(f"「{d['anchor_prop']}」")

    if d.get("macro_theme"):
        st.caption(f"主題：{d['macro_theme']}")

    st.divider()

    # 下載區
    dl_col1, dl_col2 = st.columns(2)
    report_txt = f"""節目：{ep['show']}
集數：{ep['title']}

【TL;DR】
{d.get('tldr','')}

【重點整理】
{chr(10).join(f"  {i+1}. {p}" for i,p in enumerate(d.get('key_points',[])))}

【分析亮點】
  {d.get('insight_strength','')}

【潛在盲點】
  {d.get('insight_caveat','')}

【提及個股】
{chr(10).join(f"  {s['ticker']} {s['name']} ({s['stance']}) — {s['reason']}" for s in d.get('stocks',[]))}

【核心辯論命題】
  「{d.get('anchor_prop','')}」

【主題】{d.get('macro_theme','')}
"""
    dl_col1.download_button(
        "⬇ 下載摘要報告",
        report_txt,
        file_name=f"{ep['title'][:30]}_摘要.txt",
        mime="text/plain",
    )
    dl_col2.download_button(
        "⬇ 下載逐字稿",
        transcript,
        file_name=f"{ep['title'][:30]}_逐字稿.txt",
        mime="text/plain",
    )

    with st.expander("查看原始 JSON"):
        st.json(d)
    with st.expander("查看逐字稿"):
        st.text_area("逐字稿", transcript, height=300)

import streamlit as st
import os, re, json, tempfile, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
import io

st.set_page_config(
    page_title="Podcast AI 摘要",
    page_icon="🎙",
    layout="centered",
)

st.markdown("""
<style>
.main { max-width: 760px; }
.stTextInput>div>div>input { font-size: 14px; }
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
.section-title {
    font-size: 13px;
    font-weight: 600;
    color: #444;
    border-bottom: 1px solid #eee;
    padding-bottom: 4px;
    margin: 1rem 0 0.5rem 0;
}
.ep-card {
    background: #f8f9fa;
    border: 1px solid #e8eaed;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 6px;
    font-size: 13px;
    line-height: 1.5;
}
</style>
""", unsafe_allow_html=True)

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── Session 初始化 ─────────────────────────────────────────────────
for k, v in [("history", []), ("result", None), ("episode_list", None),
              ("selected_ep", None), ("show_name", ""), ("viewing_history", False)]:
    if k not in st.session_state:
        st.session_state[k] = v

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

def parse_rss(rss_url, limit=20):
    with urllib.request.urlopen(urllib.request.Request(rss_url, headers=HEADERS), timeout=20) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    ch = root.find("channel")
    show = ch.findtext("title", "未知節目").strip()
    episodes = []
    for item in ch.findall("item")[:limit]:
        enc = item.find("enclosure")
        if enc is None: continue
        mp3 = enc.get("url", "")
        if not mp3: continue
        dur = item.find("itunes:duration", ns)
        pub = item.findtext("pubDate", "").strip()
        # 日期簡化：Wed, 15 Jan 2025 → 2025/01/15
        try:
            from email.utils import parsedate
            t = parsedate(pub)
            pub_short = f"{t[0]}/{t[1]:02d}/{t[2]:02d}" if t else pub[:10]
        except Exception:
            pub_short = pub[:10]
        episodes.append({
            "show": show,
            "title": item.findtext("title", "無標題").strip(),
            "url": mp3,
            "guid": item.findtext("guid", "").strip(),
            "duration": dur.text.strip() if dur is not None else "—",
            "pub_date": pub,
            "pub_short": pub_short,
        })
    return show, episodes

def load_episode_list(url):
    """解析 URL，回傳 (show_name, episodes, matched_ep_or_None)"""
    low = url.lower()
    # 直接 MP3
    if re.search(r'\.(mp3|m4a|aac|ogg)(\?|$)', low):
        ep = {"show": "自訂音檔", "title": Path(url.split("?")[0]).stem,
              "url": url, "duration": "—", "pub_short": "—", "pub_date": ""}
        return "自訂音檔", [ep], ep
    # Apple Podcasts
    if "podcasts.apple.com" in low:
        pid, eid = parse_apple_url(url)
        if not pid:
            raise ValueError("無法解析 Apple Podcasts URL")
        rss = get_rss_from_apple_id(pid)
        show, eps = parse_rss(rss)
        # 嘗試比對指定集數
        matched = None
        if eid:
            for ep in eps:
                if eid in ep["guid"] or eid in ep["url"]:
                    matched = ep; break
            if not matched:
                try:
                    api = f"https://itunes.apple.com/lookup?id={eid}&entity=podcastEpisode"
                    with urllib.request.urlopen(urllib.request.Request(api, headers=HEADERS), timeout=10) as r:
                        d = json.loads(r.read())
                    if d.get("results"):
                        t = d["results"][0].get("trackName", "").strip()
                        for ep in eps:
                            if t.lower() in ep["title"].lower() or ep["title"].lower() in t.lower():
                                matched = ep; break
                except Exception:
                    pass
        return show, eps, matched
    # RSS Feed
    show, eps = parse_rss(url)
    if not eps:
        raise ValueError("RSS 中找不到音檔")
    return show, eps, None

def transcribe_assemblyai(mp3_url, api_key):
    import assemblyai as aai
    aai.settings.api_key = api_key
    config = aai.TranscriptionConfig(language_detection=True)
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(mp3_url)
    if transcript.status == aai.TranscriptStatus.error:
        raise ValueError(f"AssemblyAI 轉錄失敗：{transcript.error}")
    return transcript.text or ""

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

def generate_pdf(d, ep):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import ParagraphStyle
    try: pdfmetrics.getFont('STSong-Light')
    except: pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    try: pdfmetrics.getFont('MSung-Light')
    except: pdfmetrics.registerFont(UnicodeCIDFont('MSung-Light'))

    def S(size=11, bold=False, color='#222222', leading=None, indent=0):
        return ParagraphStyle('s',
            fontName='MSung-Light' if bold else 'STSong-Light',
            fontSize=size, leading=leading or size*1.8,
            textColor=colors.HexColor(color), leftIndent=indent)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=22*mm, rightMargin=22*mm, topMargin=22*mm, bottomMargin=22*mm)
    HR  = HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#dddddd'), spaceAfter=8, spaceBefore=6)

    def section(t):
        return [Spacer(1,8), Paragraph(t, S(10, bold=True, color='#555555')),
                HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#eeeeee'), spaceAfter=4, spaceBefore=2)]

    story = []
    story += [Paragraph(d.get('category','OTHER'), S(9, color='#1a73e8')), Spacer(1,4),
              Paragraph(d.get('title', ep['title']), S(17, bold=True, color='#1a1a2e', leading=26)), Spacer(1,6),
              Paragraph(f"{ep['show']}  ·  {ep.get('pub_date','')[:16]}", S(9, color='#888888')), HR]
    story += section('TL;DR') + [Paragraph(d.get('tldr',''), S(11, leading=22))]
    story += section('重點整理')
    for i, pt in enumerate(d.get('key_points',[]),1):
        story.append(Paragraph(f"{i}.  {pt}", S(11, leading=22, indent=4)))
    story += section('分析亮點') + [Paragraph(d.get('insight_strength','—'), S(11, leading=22))]
    story += section('潛在盲點')  + [Paragraph(d.get('insight_caveat','—'),  S(11, leading=22))]
    stocks = d.get('stocks',[])
    if stocks:
        story += section('提及個股')
        for s in stocks:
            stance = s.get('stance','neutral')
            icon = {'bullish':'▲','bearish':'▼','neutral':'→'}.get(stance,'→')
            label_zh = {'bullish':'看多','bearish':'看空','neutral':'中性'}.get(stance,'中性')
            story += [Paragraph(f"{icon} {s['ticker']}  {s['name']}  [{label_zh}]", S(11, bold=True, leading=20)),
                      Paragraph(s.get('reason',''), S(10, color='#555555', leading=18, indent=8)), Spacer(1,4)]
    quotes = d.get('notable_quotes',[])
    if quotes:
        story += section('金句')
        for q in quotes:
            story.append(Paragraph(f'「{q}」', S(11, color='#333333', leading=22, indent=8)))
    if d.get('anchor_prop'):
        story += section('核心辯論命題')
        story.append(Paragraph(f'「{d["anchor_prop"]}」', S(12, bold=True, color='#1a1a2e', leading=24)))
    if d.get('macro_theme'):
        story += [Spacer(1,10), Paragraph(f'總經主題：{d["macro_theme"]}', S(9, color='#888888'))]
    story += [Spacer(1,14),
              HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cccccc'), spaceAfter=4),
              Paragraph('由 Podcast AI 摘要工具生成', S(8, color='#aaaaaa'))]
    doc.build(story)
    return buf.getvalue()

def render_result(d, ep, transcript):
    st.markdown(f'<span class="tag">{d.get("category","")}</span>', unsafe_allow_html=True)
    st.subheader(d.get("title", ep["title"]))
    st.caption(f"{ep['show']}  ·  {ep.get('pub_date','')[:16]}")
    st.markdown('<div class="section-title">TL;DR</div>', unsafe_allow_html=True)
    st.markdown(d.get("tldr",""))
    st.markdown('<div class="section-title">重點整理</div>', unsafe_allow_html=True)
    for pt in d.get("key_points",[]):
        st.markdown(f"- {pt}")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('<div class="section-title">✦ 分析亮點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_strength","—"))
    with col_b:
        st.markdown('<div class="section-title">⚠ 潛在盲點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_caveat","—"))
    stocks = d.get("stocks",[])
    if stocks:
        st.markdown('<div class="section-title">提及個股</div>', unsafe_allow_html=True)
        scols = st.columns(min(len(stocks),4))
        icons = {"bullish":"📈","bearish":"📉","neutral":"➡️"}
        for i, s in enumerate(stocks):
            with scols[i%4]:
                st.markdown(f"**{icons.get(s.get('stance',''),'')}{s['ticker']}**  \n{s['name']}  \n_{s['reason']}_")
    quotes = d.get("notable_quotes",[])
    if quotes:
        st.markdown('<div class="section-title">金句</div>', unsafe_allow_html=True)
        for q in quotes:
            st.info(f"「{q}」")
    if d.get("anchor_prop"):
        st.markdown('<div class="section-title">核心辯論命題</div>', unsafe_allow_html=True)
        st.warning(f"「{d['anchor_prop']}」")
    if d.get("macro_theme"):
        st.caption(f"主題：{d['macro_theme']}")
    st.divider()
    # 下載
    uid = str(id(d))
    report_txt = f"""節目：{ep['show']}\n集數：{ep['title']}\n
【TL;DR】\n{d.get('tldr','')}\n
【重點整理】\n{chr(10).join(f"  {i+1}. {p}" for i,p in enumerate(d.get('key_points',[])))}\n
【分析亮點】\n  {d.get('insight_strength','')}\n
【潛在盲點】\n  {d.get('insight_caveat','')}\n
【提及個股】\n{chr(10).join(f"  {s['ticker']} {s['name']} ({s['stance']}) — {s['reason']}" for s in d.get('stocks',[]))}\n
【核心辯論命題】\n  「{d.get('anchor_prop','')}」\n
【主題】{d.get('macro_theme','')}"""
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇ 摘要 TXT", report_txt,
        file_name=f"{ep['title'][:30]}_摘要.txt", mime="text/plain", key=f"txt_{uid}")
    c2.download_button("⬇ 逐字稿", transcript,
        file_name=f"{ep['title'][:30]}_逐字稿.txt", mime="text/plain", key=f"ts_{uid}")
    try:
        c3.download_button("⬇ PDF", generate_pdf(d, ep),
            file_name=f"{ep['title'][:30]}_摘要.pdf", mime="application/pdf", key=f"pdf_{uid}")
    except Exception as e:
        c3.warning(f"PDF 失敗：{e}")
    with st.expander("查看原始 JSON"): st.json(d)
    with st.expander("查看逐字稿"):
        st.text_area("逐字稿", transcript, height=300, key=f"ta_{uid}")

# ── 側邊欄 ────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 設定")
    api_key = st.text_input("Anthropic API Key", type="password",
        placeholder="sk-ant-...", help="console.anthropic.com/settings/keys")
    assemblyai_key = st.text_input("AssemblyAI API Key", type="password",
        placeholder="your_assemblyai_key", help="app.assemblyai.com（免費 100小時/月）")
    st.divider()
    st.markdown("**費用估算**\n- AssemblyAI：免費 100小時/月\n- Claude：約 NT$1–3 / 集")
    st.divider()
    st.markdown("**支援格式**\n- Apple Podcasts 連結\n- RSS Feed URL\n- 直接 MP3 網址")
    if st.session_state.history:
        st.divider()
        st.markdown("**📚 分析紀錄**")
        for i, rec in enumerate(reversed(st.session_state.history)):
            idx = len(st.session_state.history) - 1 - i
            label = f"🎙 {rec['ep']['show'][:14]}\n{rec['data'].get('title','')[:22]}…\n🕐 {rec['analyzed_at']}"
            if st.button(label, key=f"hist_{idx}", use_container_width=True):
                st.session_state.result = rec
                st.session_state.viewing_history = True
                st.rerun()
        if st.button("🗑 清除所有紀錄", use_container_width=True):
            st.session_state.history = []
            st.session_state.result = None
            st.session_state.viewing_history = False
            st.rerun()

# ── 主畫面 ────────────────────────────────────────────────────────
st.title("🎙 Podcast AI 摘要")
st.caption("輸入 Apple Podcasts 欄目 / RSS / MP3 連結，選集後自動轉錄並生成摘要")

# ── Step 1：輸入連結並載入集數清單 ──────────────────────────────
url_input = st.text_input("Podcast 連結",
    placeholder="https://podcasts.apple.com/tw/podcast/id1234567890",
    key="url_input")

load_col, _ = st.columns([1, 3])
load_btn = load_col.button("🔍 載入集數", use_container_width=True)

if load_btn:
    if not url_input.strip():
        st.error("請輸入 Podcast 連結")
        st.stop()
    with st.spinner("解析中…"):
        try:
            show, eps, matched = load_episode_list(url_input.strip())
            st.session_state.episode_list = eps
            st.session_state.show_name = show
            st.session_state.result = None
            st.session_state.viewing_history = False
            # 若 URL 含指定集數，預設選中
            st.session_state.selected_ep = matched if matched else (eps[0] if eps else None)
            st.rerun()
        except Exception as e:
            st.error(f"載入失敗：{e}")
            st.stop()

# ── Step 2：顯示集數清單讓使用者選擇 ────────────────────────────
if st.session_state.episode_list:
    eps = st.session_state.episode_list
    show = st.session_state.show_name

    st.markdown(f"### 🎙 {show}")
    st.caption(f"共 {len(eps)} 集（最新在前）")

    # 找出目前選中的 index
    sel_ep = st.session_state.selected_ep
    sel_idx = 0
    if sel_ep:
        for i, e in enumerate(eps):
            if e["url"] == sel_ep["url"]:
                sel_idx = i; break

    options = [f"[{i+1}]  {e['pub_short']}  {e['title']}  ({e['duration']})"
               for i, e in enumerate(eps)]
    chosen_idx = st.selectbox("選擇集數", range(len(options)),
        format_func=lambda i: options[i], index=sel_idx,
        label_visibility="collapsed")
    st.session_state.selected_ep = eps[chosen_idx]
    chosen_ep = eps[chosen_idx]

    # 顯示選中集的小卡片
    st.markdown(f"""<div class="ep-card">
        <b>{chosen_ep['title']}</b><br>
        <span style="color:#888;font-size:12px">📅 {chosen_ep['pub_short']} &nbsp;·&nbsp; ⏱ {chosen_ep['duration']}</span>
    </div>""", unsafe_allow_html=True)

    # 分析按鈕
    run_col, _ = st.columns([1, 3])
    run_btn = run_col.button("▶ 開始分析", type="primary", use_container_width=True)

    if run_btn:
        if not api_key:
            st.error("請在左側填入 Anthropic API Key"); st.stop()
        if not assemblyai_key:
            st.error("請在左側填入 AssemblyAI API Key"); st.stop()

        st.session_state.result = None
        st.session_state.viewing_history = False

        with st.status("🎤 AssemblyAI 雲端轉錄中…（約需 1–5 分鐘）", expanded=True) as status:
            try:
                transcript = transcribe_assemblyai(chosen_ep["url"], assemblyai_key)
                st.write(f"✅ 轉錄完成，共 {len(transcript):,} 字")
            except Exception as e:
                status.update(label="轉錄失敗", state="error")
                st.error(f"轉錄失敗：{e}"); st.stop()

            st.write("🤖 Claude 分析摘要中…")
            try:
                data = claude_summarize(transcript, chosen_ep["title"], show, api_key)
                now = datetime.now().strftime("%m/%d %H:%M")
                record = {"data": data, "ep": chosen_ep, "transcript": transcript, "analyzed_at": now}
                st.session_state.result = record
                existing = [r["ep"]["title"] for r in st.session_state.history]
                if chosen_ep["title"] not in existing:
                    st.session_state.history.append(record)
                status.update(label="✅ 分析完成！", state="complete", expanded=False)
            except Exception as e:
                status.update(label="分析失敗", state="error")
                st.error(f"Claude 分析失敗：{e}"); st.stop()

# ── 顯示結果 ──────────────────────────────────────────────────────
if st.session_state.result:
    rec = st.session_state.result
    st.divider()
    if st.session_state.viewing_history:
        st.caption(f"📂 歷史紀錄  ·  分析於 {rec.get('analyzed_at','')}")
    render_result(rec["data"], rec["ep"], rec["transcript"])

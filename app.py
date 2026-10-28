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
              ("selected_ep", None), ("show_name", ""), ("viewing_history", False),
              ("favorites", [])]:
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
    MAX = 20000
    if len(transcript) > MAX:
        transcript = transcript[:MAX] + "\n\n[...節錄...]"
    prompt = f"""你是一位頂尖的財經 Podcast 深度分析師，具備總體經濟、產業研究、股票投資的專業背景。
請根據以下逐字稿，產出一份「專業投資人等級」的深度分析報告，不只是摘要，而是要有獨立的判斷與見解。

節目：{show_name}
集數標題：{ep_title}

=== 逐字稿 ===
{transcript}
=== 結束 ===

請用繁體中文，以 JSON 格式輸出（不要加 markdown 反引號）：

{{
  "title": "一句話點出本集最核心的投資命題或觀點衝突，能引發思考",
  "category": "MARKETS / MACRO / SEMICON / CRYPTO / REALESTATE / OTHER 之一",

  "tldr": "4～6 句話的精華摘要，必須包含：主要論點、數據或事件、結論方向。避免模糊語句。",

  "key_points": [
    "重點1：具體說明主持人的觀點與論據（2～3句）",
    "重點2：具體說明主持人的觀點與論據（2～3句）",
    "重點3：具體說明主持人的觀點與論據（2～3句）",
    "重點4（若有）",
    "重點5（若有）"
  ],

  "deep_analysis": {{
    "core_thesis": "本集核心投資論點是什麼？用2～3句清楚陳述主持人的完整論述邏輯。",
    "supporting_evidence": ["論據1：主持人引用了哪些數據/事件/案例來支持論點", "論據2", "論據3"],
    "counter_risks": ["風險1：若論點成立的前提條件未實現，最大的反向風險是什麼", "風險2", "風險3"],
    "time_horizon": "本集觀點適用的時間維度（短線數週 / 中線數月 / 長線數年）",
    "conviction_level": "HIGH / MEDIUM / LOW — 根據主持人語氣、論據充分程度判斷其信心水位"
  }},

  "actionable_insights": [
    "行動建議1：針對散戶投資人，可以採取的具體行動或關注方向（不是廢話，要有操作性）",
    "行動建議2",
    "行動建議3"
  ],

  "insight_strength": "主持人分析中最值得學習的思維框架或獨特視角（2～3句，說明為何有價值）",
  "insight_caveat": "主持人論點的主要盲點、過度樂觀/悲觀之處、或資訊不足的地方（2～3句，要具體指出）",

  "stocks": [
    {{"ticker": "代碼（若為台股加上.TW，美股直接代碼）", "name": "公司名", "stance": "bullish/bearish/neutral", "reason": "具體說明看多/看空/中性的理由，包含估值或基本面依據（1～2句）"}}
  ],

  "macro_data": {{
    "fed_policy": "若逐字稿提及聯準會政策：目前利率水位、升降息預期、縮表/擴表動向（若無相關內容則填 null）",
    "employment": "若逐字稿提及就業數據：非農就業人數、失業率趨勢、薪資增速等對市場的含義（若無則填 null）",
    "oil": "若逐字稿提及油價：Brent/WTI 走勢、地緣政治影響、對通膨與企業成本的傳導（若無則填 null）",
    "gold": "若逐字稿提及金價：走勢方向、驅動因素（避險/美元/實質利率）、與股市的相關性（若無則填 null）",
    "other_macro": ["其他被提及的重要總經數據或資產價格，例如CPI、PMI、美元指數、銅價、10Y美債殖利率等（若無則回傳空陣列[]）"]
  }},

  "macro_linkage": "本集觀點與當前總體環境（利率/匯率/景氣循環）的關聯性分析（2～3句）",

  "anchor_prop": "本集最具爭議性、最值得深思的核心命題（一句話，要夠犀利）",
  "macro_theme": "本集總經或產業主題（精簡標籤，如：AI資本支出週期、台灣房市結構性問題）",
  "notable_quotes": ["直接引用逐字稿中最有洞見或最具代表性的金句（原文）"]
}}

重要規則：
- stocks 若無明確個股回傳 []；notable_quotes 若無值得引用的金句回傳 []
- macro_data 各欄位：逐字稿有提及才填，真的完全沒提及填 null 或 []，不要捏造數據
- deep_analysis.supporting_evidence、counter_risks、actionable_insights 至少各 2 項
- 所有分析必須忠實反映逐字稿內容，不要捏造主持人未說過的觀點
- 內容要有深度，避免「主持人認為市場很重要」這類廢話
- conviction_level 要根據主持人實際語氣客觀判斷"""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

def generate_pdf(d, ep):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import ParagraphStyle
    import glob, os

    # ── 字型優先順序：
    # 1. repo 內 fonts/NotoSansTC-Regular.ttf（最佳，需手動放入 repo）
    # 2. 系統 CJK 字型（Railway Nix 環境若有安裝）
    # 3. MSung-Light CID 退回（section header 用英文規避亂碼）
    FONT_REG  = 'CJKFont'
    FONT_BOLD = 'CJKFont'
    _reg = set(pdfmetrics.getRegisteredFontNames())

    def _register_cjk():
        # 1. repo 內字型（優先）
        repo_font = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts', 'NotoSansTC-Regular.ttf')
        if os.path.exists(repo_font):
            pdfmetrics.registerFont(TTFont('CJKFont', repo_font))
            return True
        # 2. 系統常見路徑
        patterns = [
            '/usr/share/fonts/**/Noto*CJK*TC*.otf',
            '/usr/share/fonts/**/Noto*CJK*.ttc',
            '/usr/share/fonts/**/WenQuanYi*.ttf',
            '/usr/share/fonts/**/*CJK*.ttf',
        ]
        for pat in patterns:
            hits = glob.glob(pat, recursive=True)
            if hits:
                try:
                    pdfmetrics.registerFont(TTFont('CJKFont', hits[0]))
                    return True
                except Exception:
                    continue
        return False

    use_ttf = 'CJKFont' in _reg or _register_cjk()
    if not use_ttf:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont('MSung-Light'))
            FONT_REG = FONT_BOLD = 'MSung-Light'
        except Exception:
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
            FONT_REG = FONT_BOLD = 'STSong-Light'

    def S(size=12, bold=False, color='#1a1a1a', leading=None, indent=0, space_before=0, space_after=4):
        return ParagraphStyle(
            name=f's_{size}_{bold}_{color}',
            fontName=FONT_BOLD if bold else FONT_REG,
            fontSize=size,
            leading=leading if leading else size * 2.0,
            textColor=colors.HexColor(color),
            leftIndent=indent,
            spaceBefore=space_before,
            spaceAfter=space_after,
            wordWrap='CJK',
        )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=25*mm, rightMargin=25*mm, topMargin=25*mm, bottomMargin=25*mm)

    def HR(thick=0.5, col='#dddddd', before=4, after=8):
        return HRFlowable(width='100%', thickness=thick,
                          color=colors.HexColor(col), spaceBefore=before, spaceAfter=after)

    def section_header(text):
        return [
            Spacer(1, 14),
            Paragraph(text, S(10, bold=True, color='#555555', space_before=0, space_after=2)),
            HR(thick=0.5, col='#e0e0e0', before=2, after=6),
        ]

    story = []

    # ── 標題區 ──
    story.append(Paragraph(d.get('category', 'OTHER'), S(9, color='#1a73e8', space_after=6)))
    story.append(Paragraph(d.get('title', ep['title']), S(18, bold=True, color='#1a1a2e', leading=32, space_after=6)))
    story.append(Paragraph(f"{ep['show']}　·　{ep.get('pub_date','')[:16]}", S(9, color='#888888', space_after=4)))
    story.append(HR(thick=1, col='#cccccc', before=4, after=12))

    # ── TL;DR ──
    story += section_header('TL;DR')
    story.append(Paragraph(d.get('tldr', ''), S(12, leading=26, space_after=6)))

    # ── Key Points ──
    story += section_header('Key Points')
    for i, pt in enumerate(d.get('key_points', []), 1):
        story.append(Paragraph(f"{i}. {pt}", S(12, leading=26, indent=6, space_after=6)))

    # ── Deep Analysis ──
    da = d.get('deep_analysis', {})
    if da:
        story += section_header('Deep Analysis')
        if da.get('core_thesis'):
            story.append(Paragraph('Core Thesis', S(11, bold=True, color='#444444', space_after=2)))
            story.append(Paragraph(da['core_thesis'], S(12, leading=26, indent=6, space_after=8)))

        conv = da.get('conviction_level','')
        horiz = da.get('time_horizon','')
        if conv or horiz:
            meta = []
            if conv:  meta.append(f"Conviction: {conv}")
            if horiz: meta.append(f"Horizon: {horiz}")
            story.append(Paragraph('  |  '.join(meta), S(10, color='#666666', space_after=8)))

        evidences = da.get('supporting_evidence', [])
        if evidences:
            story.append(Paragraph('Supporting Evidence', S(11, bold=True, color='#444444', space_after=2)))
            for e in evidences:
                story.append(Paragraph(f"- {e}", S(11, leading=24, indent=8, space_after=3)))

        risks = da.get('counter_risks', [])
        if risks:
            story.append(Spacer(1, 6))
            story.append(Paragraph('Counter Risks', S(11, bold=True, color='#c0392b', space_after=2)))
            for r in risks:
                story.append(Paragraph(f"- {r}", S(11, leading=24, indent=8, space_after=3)))

    # ── Action Items ──
    actions = d.get('actionable_insights', [])
    if actions:
        story += section_header('Action Items')
        for i, a in enumerate(actions, 1):
            story.append(Paragraph(f"{i}. {a}", S(12, leading=26, indent=6, space_after=6)))

    # ── Strengths / Blind Spots ──
    story += section_header('Strengths')
    story.append(Paragraph(d.get('insight_strength', '-'), S(12, leading=26, space_after=6)))
    story += section_header('Blind Spots')
    story.append(Paragraph(d.get('insight_caveat', '-'), S(12, leading=26, space_after=6)))

    # ── Macro Data ──
    md = d.get('macro_data', {})
    macro_items = [
        ('Fed Policy',  md.get('fed_policy')),
        ('Employment',  md.get('employment')),
        ('Oil Price',   md.get('oil')),
        ('Gold Price',  md.get('gold')),
    ]
    others = md.get('other_macro', [])
    has_md = any(v for _, v in macro_items) or others
    if has_md:
        story += section_header('Macro Data')
        for label, val in macro_items:
            if val:
                story.append(Paragraph(label, S(11, bold=True, color='#444444', space_after=2)))
                story.append(Paragraph(val, S(11, leading=24, indent=8, space_after=6)))
        if others:
            story.append(Paragraph('Other Data Points', S(11, bold=True, color='#444444', space_after=2)))
            for o in others:
                story.append(Paragraph(f"- {o}", S(11, leading=24, indent=8, space_after=3)))

    # ── Macro ──
    if d.get('macro_linkage'):
        story += section_header('Macro Linkage')
        story.append(Paragraph(d['macro_linkage'], S(12, leading=26, space_after=6)))

    # ── Stocks ──
    stocks = d.get('stocks', [])
    if stocks:
        story += section_header('Stocks')
        icons = {'bullish': '[+]', 'bearish': '[-]', 'neutral': '[=]'}
        labels = {'bullish': 'Bullish', 'bearish': 'Bearish', 'neutral': 'Neutral'}
        for s in stocks:
            stance = s.get('stance', 'neutral')
            story.append(Paragraph(
                f"{icons.get(stance,'[=]')} {s['ticker']}  {s['name']}  [{labels.get(stance,'Neutral')}]",
                S(12, leading=24, space_after=2)))
            story.append(Paragraph(s.get('reason', ''), S(11, color='#555555', leading=22, indent=12, space_after=8)))

    # ── Quotes ──
    quotes = d.get('notable_quotes', [])
    if quotes:
        story += section_header('Quotes')
        for q in quotes:
            story.append(Paragraph(f'"{q}"', S(12, color='#333333', leading=26, indent=10, space_after=6)))

    # ── Core Thesis ──
    if d.get('anchor_prop'):
        story += section_header('Core Thesis')
        story.append(Paragraph(f'"{d["anchor_prop"]}"',
                                S(13, bold=True, color='#1a1a2e', leading=28, space_after=6)))

    # ── Theme ──
    if d.get('macro_theme'):
        story.append(Spacer(1, 10))
        story.append(Paragraph(f'Theme: {d["macro_theme"]}', S(9, color='#888888', space_after=4)))

    # ── 頁尾 ──
    story.append(Spacer(1, 20))
    story.append(HR(thick=0.5, col='#cccccc', before=0, after=4))
    story.append(Paragraph('由 Podcast AI 摘要工具生成', S(8, color='#aaaaaa', space_after=0)))

    doc.build(story)
    return buf.getvalue()

def render_result(d, ep, transcript):
    st.markdown(f'<span class="tag">{d.get("category","")}</span>', unsafe_allow_html=True)
    st.subheader(d.get("title", ep["title"]))
    st.caption(f"{ep['show']}  ·  {ep.get('pub_date','')[:16]}")

    # ── TL;DR ──
    st.markdown('<div class="section-title">TL;DR</div>', unsafe_allow_html=True)
    st.markdown(d.get("tldr",""))

    # ── 重點整理 ──
    st.markdown('<div class="section-title">📋 重點整理</div>', unsafe_allow_html=True)
    for pt in d.get("key_points",[]):
        st.markdown(f"- {pt}")

    # ── 深度分析 ──
    da = d.get("deep_analysis", {})
    if da:
        st.markdown('<div class="section-title">🔬 深度分析</div>', unsafe_allow_html=True)
        if da.get("core_thesis"):
            st.markdown(f"**核心論點**\n\n{da['core_thesis']}")

        conviction = da.get("conviction_level","")
        horizon = da.get("time_horizon","")
        if conviction or horizon:
            cv_color = {"HIGH":"🟢","MEDIUM":"🟡","LOW":"🔴"}.get(conviction,"⚪")
            meta_parts = []
            if conviction: meta_parts.append(f"{cv_color} 信心水位：**{conviction}**")
            if horizon:    meta_parts.append(f"⏳ 時間維度：{horizon}")
            st.markdown("　".join(meta_parts))

        ev_col, risk_col = st.columns(2)
        with ev_col:
            evidences = da.get("supporting_evidence",[])
            if evidences:
                st.markdown("**📌 支撐論據**")
                for e in evidences:
                    st.markdown(f"- {e}")
        with risk_col:
            risks = da.get("counter_risks",[])
            if risks:
                st.markdown("**⚡ 反向風險**")
                for r in risks:
                    st.markdown(f"- {r}")

    # ── 可操作建議 ──
    actions = d.get("actionable_insights",[])
    if actions:
        st.markdown('<div class="section-title">💡 可操作建議</div>', unsafe_allow_html=True)
        for i, a in enumerate(actions, 1):
            st.markdown(f"**{i}.** {a}")

    # ── 分析亮點 / 盲點 ──
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown('<div class="section-title">✦ 分析亮點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_strength","—"))
    with col_b:
        st.markdown('<div class="section-title">⚠ 潛在盲點</div>', unsafe_allow_html=True)
        st.markdown(d.get("insight_caveat","—"))

    # ── 財經數據分析 ──
    md = d.get("macro_data", {})
    has_macro_data = md and any([md.get("fed_policy"), md.get("employment"),
                                  md.get("oil"), md.get("gold"),
                                  md.get("other_macro")])
    if has_macro_data:
        st.markdown('<div class="section-title">📈 財經數據解析</div>', unsafe_allow_html=True)
        macro_items = [
            ("🏦 聯準會政策", md.get("fed_policy")),
            ("👷 就業數據",   md.get("employment")),
            ("🛢 油價分析",   md.get("oil")),
            ("🥇 金價分析",   md.get("gold")),
        ]
        for label, val in macro_items:
            if val:
                with st.expander(label, expanded=True):
                    st.markdown(val)
        others = md.get("other_macro", [])
        if others:
            with st.expander("📊 其他總經數據", expanded=True):
                for o in others:
                    st.markdown(f"- {o}")

    # ── 總經連結 ──
    if d.get("macro_linkage"):
        st.markdown('<div class="section-title">🌐 總經連結</div>', unsafe_allow_html=True)
        st.markdown(d.get("macro_linkage"))

    # ── 個股 ──
    stocks = d.get("stocks",[])
    if stocks:
        st.markdown('<div class="section-title">📊 提及個股</div>', unsafe_allow_html=True)
        scols = st.columns(min(len(stocks),3))
        icons = {"bullish":"📈","bearish":"📉","neutral":"➡️"}
        for i, s in enumerate(stocks):
            with scols[i%3]:
                st.markdown(f"**{icons.get(s.get('stance',''),'')}{s['ticker']}**  \n{s['name']}  \n_{s['reason']}_")

    # ── 金句 ──
    quotes = d.get("notable_quotes",[])
    if quotes:
        st.markdown('<div class="section-title">💬 金句</div>', unsafe_allow_html=True)
        for q in quotes:
            st.info(f"「{q}」")

    # ── 核心命題 ──
    if d.get("anchor_prop"):
        st.markdown('<div class="section-title">🎯 核心辯論命題</div>', unsafe_allow_html=True)
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

    # ── 常用欄目收藏 ──────────────────────────────────────────────
    st.divider()
    st.markdown("**⭐ 常用欄目**")
    fav_name = st.text_input("欄目名稱", placeholder="例：股癌、投資老手", key="fav_name_input", label_visibility="collapsed")
    fav_url  = st.text_input("欄目連結", placeholder="Apple Podcasts / RSS 連結", key="fav_url_input", label_visibility="collapsed")
    add_fav_btn = st.button("＋ 加入收藏", use_container_width=True)
    if add_fav_btn:
        name = fav_name.strip()
        url  = fav_url.strip()
        if name and url:
            existing_urls = [f["url"] for f in st.session_state.favorites]
            if url not in existing_urls:
                st.session_state.favorites.append({"name": name, "url": url})
                st.success(f"已加入：{name}")
                st.rerun()
            else:
                st.warning("此連結已在收藏中")
        else:
            st.warning("請填入名稱和連結")

    if st.session_state.favorites:
        for fi, fav in enumerate(st.session_state.favorites):
            fav_col1, fav_col2 = st.columns([5, 1])
            with fav_col1:
                if st.button(f"📻 {fav['name']}", key=f"fav_load_{fi}", use_container_width=True,
                             help=fav['url']):
                    # 點擊直接載入該欄目集數
                    with st.spinner(f"載入 {fav['name']}…"):
                        try:
                            show, eps, matched = load_episode_list(fav['url'])
                            st.session_state.episode_list = eps
                            st.session_state.show_name = show
                            st.session_state.result = None
                            st.session_state.viewing_history = False
                            st.session_state.selected_ep = eps[0] if eps else None
                            st.rerun()
                        except Exception as e:
                            st.error(f"載入失敗：{e}")
            with fav_col2:
                if st.button("✕", key=f"fav_del_{fi}", help="移除收藏"):
                    st.session_state.favorites.pop(fi)
                    st.rerun()

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

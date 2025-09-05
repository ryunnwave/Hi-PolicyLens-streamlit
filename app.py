# -*- coding: utf-8 -*-
"""
RegWatch (Streamlit)
- 대상: ECHA(legislation/news), CBP(trade/press RSS), MOTIE(국문 사이트), BMUV(RSS)
- 기능: 수집 → 요약(Potens API 옵션) → 표/카드/보고서(Markdown) + 다운로드
"""

import os, re, hashlib, io, csv
from datetime import datetime, timezone, timedelta
from typing import List, Dict

import requests, feedparser, pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import streamlit as st

# ---------------------------
# UI 기본 세팅
# ---------------------------
st.set_page_config(page_title="RegWatch – 글로벌 규제 모니터링", layout="wide")
BRAND = "#0f2e69"; ACCENT = "#dc8d32"
st.markdown(f"""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.8/dist/web/static/pretendard.css');
html, body, [class*="css"] {{ font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif; }}
.big-header {{ background: linear-gradient(135deg, {BRAND} 0%, #1a4b8c 100%); color:#fff; padding:18px 22px; border-radius:10px; margin-bottom:12px; }}
.brand-title {{ font-weight:800; font-size:22px; margin-right:10px; }}
.card {{ border:1px solid #e2e8f0; border-left:5px solid transparent; border-radius:12px; padding:16px; margin:12px 0; background:#fff; }}
.card.new {{ border-left-color:{ACCENT}; }}
.card h4 {{ color:{BRAND}; margin:0 0 8px 0; font-size:18px; }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; font-size:11px; font-weight:700; margin-right:6px; }}
.badge.kor {{ background:#f1f5f9; color:#475569; }}
.badge.cat-chem {{ background:#e0f2fe; color:#0277bd; }}
.badge.cat-trade {{ background:#f3e5f5; color:#7b1fa2; }}
.badge.cat-ind {{ background:#e8f5e8; color:#2e7d32; }}
.badge.cat-env {{ background:#fff3e0; color:#f57c00; }}
.badge.status-ann {{ background:#dbeafe; color:#1d4ed8; }}
.badge.status-draft {{ background:#fef3c7; color:#d97706; }}
.badge.status-eff {{ background:#dcfce7; color:#16a34a; }}
.keyword {{ display:inline-block; border:1px solid #e2e8f0; font-size:11px; padding:2px 8px; border-radius:8px; margin-right:6px; margin-top:6px; }}
.meta {{ color:#64748b; font-size:12px; margin-top:6px; }}
</style>
""", unsafe_allow_html=True)

# ---------------------------
# 설정/상수
# ---------------------------
USER_AGENT = "Mozilla/5.0 (compatible; RegWatch/1.0)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
MAX_PER_SOURCE = 60

SOURCES = [
    {"id": "echa",  "name": "ECHA (EU – European Chemicals Agency)",      "type": "html", "url": "https://echa.europa.eu/legislation"},
    {"id": "cbp",   "name": "U.S. Customs and Border Protection (CBP)",    "type": "rss",  "url": "https://www.cbp.gov/rss/trade"},
    {"id": "cbppr", "name": "CBP Press Releases",                          "type": "rss",  "url": "https://www.cbp.gov/rss/press-releases"},
    {"id": "motie", "name": "MOTIE (대한민국 산업통상자원부)",                 "type": "html", "url": "https://www.motie.go.kr"},
    {"id": "bmuv",  "name": "BMUV (독일 환경부)",                           "type": "rss",  "url": "https://www.bundesumweltministerium.de/meldungen.rss"}
]

# Potens 요약 API (선택)
POTENS_API_KEY = st.secrets.get("POTENS_API_KEY", os.environ.get("POTENS_API_KEY", ""))
POTENS_BASE = os.environ.get("POTENS_BASE", "https://api.potens.ai")
POTENS_SUMMARY_PATH = os.environ.get("POTENS_SUMMARY_PATH", "/v1/summarize")

# ---------------------------
# 유틸
# ---------------------------
def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def to_iso(s: str) -> str:
    if not s:
        return datetime.now(timezone.utc).isoformat()
    try:
        d = dtparse.parse(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def days_new(iso: str, days=7) -> bool:
    try:
        d = dtparse.parse(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d) <= timedelta(days=days)
    except Exception:
        return False

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def simple_summary(text: str) -> str:
    t = clean_text(text)
    if len(t) <= 140: return t
    m = re.match(r"^(.{40,280}?[\.!\?])\s+(.{0,280}?[\.!\?])?", t)
    return (m.group(1) + (" " + m.group(2) if m and m.group(2) else "")) if m else t[:280]

def extract_keywords(text: str, topn=5) -> List[str]:
    stop = set(["the","and","for","with","from","that","this","are","was","were","will","have","has","been","on","of","in","to","a","an","by","및","과","에","의","으로"])
    words = re.sub(r"[^a-z0-9가-힣 ]"," ", (text or "").lower()).split()
    freq={}
    for w in words:
        if len(w)<=2 or w in stop: continue
        freq[w]=freq.get(w,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:topn]]

def guess_category(title: str) -> str:
    t=(title or "").lower()
    if re.search(r"(reach|clp|pfas|biocide|chemical|substance|restriction|authorisation)", t): return "화학물질규제"
    if re.search(r"(tariff|duty|quota|fees|rate|ace|import|export|cbp|customs)", t): return "무역정책"
    if re.search(r"(environment|climate|emission|umwelt|환경)", t): return "환경규제"
    if re.search(r"(policy|industry|manufactur|산업|혁신|투자)", t): return "산업정책"
    return "산업정책"

def guess_impact(text: str) -> str:
    t=(text or "").lower()
    if re.search(r"(effective|mandatory|ban|prohibit|enforce|in force|penalty|시행|발효)", t): return "High"
    if re.search(r"(proposal|draft|consultation|comment|plan|roadmap|초안|의견수렴)", t): return "Medium"
    return "Low"

def potens_summarize(text: str, language="ko", max_sentences=3) -> str:
    """포텐스 요약 API (있으면 사용, 없으면 빈 문자열 반환)"""
    if not POTENS_API_KEY:
        return ""
    try:
        url = POTENS_BASE + POTENS_SUMMARY_PATH
        r = requests.post(url, json={
            "text": text, "language": language, "max_sentences": max_sentences
        }, headers={
            "Authorization": f"Bearer {POTENS_API_KEY}",
            "X-API-Key": POTENS_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT
        }, timeout=15)
        if r.status_code >= 400:
            return ""
        data = r.json()
        return (data.get("summary") or data.get("result") or data.get("text") or "").strip()
    except Exception:
        return ""

def normalize(source_id, source_name, title, url, date_iso, summary, country_hint="") -> Dict:
    if not url: return {}
    title = clean_text(title or url)
    date_iso = to_iso(date_iso or "")
    category = guess_category(title)
    impact = guess_impact(title + " " + (summary or ""))
    return {
        "id": md5_hex(url),
        "sourceId": source_id,
        "sourceName": source_name,
        "title": title,
        "url": url,
        "dateIso": date_iso,
        "category": category,
        "summary": clean_text(summary or ""),
        "impact": impact,
        "country": country_hint or {"cbp":"미국","cbppr":"미국","bmuv":"독일","echa":"EU","motie":"대한민국"}.get(source_id,"")
    }

# ---------------------------
# 크롤러들
# ---------------------------
def fetch_rss(source_id, name, feed_url, country=""):
    out=[]
    d = feedparser.parse(feed_url)
    for e in d.entries[:MAX_PER_SOURCE]:
        title = getattr(e,"title",""); link = getattr(e,"link","")
        pub = getattr(e,"published","") or getattr(e,"updated","") or ""
        desc = getattr(e,"summary","") or getattr(e,"description","") or ""
        summ = potens_summarize(f"{title}\n\n{desc}") or simple_summary(f"{title}. {desc}")
        item = normalize(source_id, name, title, link, pub, summ, country)
        if item: out.append(item)
    return out

def fetch_echa_legislation():
    """ECHA: legislation 페이지 우선 → JSON-LD/링크 → 실패 시 news 페이지 폴백"""
    items=[]
    try:
        r = requests.get("https://echa.europa.eu/legislation", headers=HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        # 1) JSON-LD에서 문서/뉴스 캐치
        for script in soup.find_all("script", {"type":"application/ld+json"}):
            try:
                data = script.string
                if not data: continue
                j = st.session_state.get("_tmpjson", None)
                j = None
                j = __import__("json").loads(data)
                arr = j if isinstance(j,list) else [j]
                for n in arr:
                    title = (n.get("headline") or n.get("name") or "").strip()
                    url   = (n.get("url") or (n.get("mainEntityOfPage") or {}).get("@id") or "").strip()
                    if not title or not url: continue
                    date  = n.get("datePublished") or n.get("dateModified") or ""
                    desc  = n.get("description") or ""
                    summ  = potens_summarize(f"{title}\n\n{desc}") or simple_summary(f"{title}. {desc}")
                    items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", title, url, date, summ, "EU"))
            except Exception:
                continue

        # 2) 링크 기반 보조: /legislation/ 또는 /news 포함
        if len(items) < 10:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not re.search(r"/(legislation|news)", href): continue
                href = href if href.startswith("http") else f"https://echa.europa.eu{href}"
                title = clean_text(a.get_text())
                if not title: continue
                summ = potens_summarize(title) or simple_summary(title)
                items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", title, href, "", summ, "EU"))
                if len(items) >= MAX_PER_SOURCE: break
    except Exception:
        pass

    # 3) 폴백: /news 페이지
    if len(items) < 5:
        try:
            r = requests.get("https://echa.europa.eu/news", headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href*="/news"]'):
                href = a.get("href","")
                if not href: continue
                href = href if href.startswith("http") else f"https://echa.europa.eu{href}"
                title = clean_text(a.get_text())
                if not title: continue
                summ = potens_summarize(title) or simple_summary(title)
                items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", title, href, "", summ, "EU"))
                if len(items) >= MAX_PER_SOURCE: break
        except Exception:
            pass

    # dedup by url
    uniq={}
    for it in items: uniq[it["url"]] = it
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_motie_generic():
    """
    MOTIE: 공식 RSS가 없어 기본 페이지와 공지/보도 자료로 추정되는 링크를 긁어옴
    - 동작이 약할 수 있어 포텐스 요약으로 보완
    """
    items=[]
    candidates = [
        "https://www.motie.go.kr",  # 홈(표/리스트에서 a태그 긁기)
        # 필요시 보도자료/공지 메뉴 경로를 추가로 늘릴 수 있음
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            # 표(tr) 우선
            for tr in soup.find_all("tr"):
                a = tr.find("a", href=True)
                if not a: continue
                href=a["href"]
                if not href.startswith("http"): href = "https://www.motie.go.kr" + href
                title = clean_text(a.get_text())
                if not title: continue
                # 날짜 패턴(있을 수도/없을 수도 있음)
                txt = tr.get_text(" ", strip=True)
                m = re.search(r"(\d{4}[.-]\d{2}[.-]\d{2})", txt)
                date = m.group(1).replace(".", "-") if m else ""
                summ = potens_summarize(title) or simple_summary(title)
                items.append(normalize("motie","MOTIE (대한민국 산업통상자원부)", title, href, date, summ, "대한민국"))
                if len(items) >= MAX_PER_SOURCE: break
            if len(items) >= MAX_PER_SOURCE: break

            # 보조: 일반 링크 중 /bbs, /board, /news 단어 포함
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=clean_text(a.get_text())
                if not text or not href: continue
                if not re.search(r"/(bbs|board|news|notice|press)", href, re.I): continue
                if not href.startswith("http"): href = "https://www.motie.go.kr" + href
                summ = potens_summarize(text) or simple_summary(text)
                items.append(normalize("motie","MOTIE (대한민국 산업통상자원부)", text, href, "", summ, "대한민국"))
                if len(items) >= MAX_PER_SOURCE: break
        except Exception:
            continue

    uniq={}
    for it in items: uniq[it["url"]] = it
    return list(uniq.values())[:MAX_PER_SOURCE]

# ---------------------------
# 수집 파이프라인
# ---------------------------
def fetch_all(selected_ids: List[str]) -> List[Dict]:
    out=[]
    for s in SOURCES:
        if s["id"] not in selected_ids: continue
        try:
            if s["type"] == "rss":
                out += fetch_rss(s["id"], s["name"], s["url"])
            elif s["id"] == "echa":
                out += fetch_echa_legislation()
            elif s["id"] == "motie":
                out += fetch_motie_generic()
        except Exception:
            continue
    # dedup by url
    uniq={}
    for it in out: uniq[it["url"]] = it
    return list(uniq.values())

# ---------------------------
# 보고서 생성/다운로드
# ---------------------------
def to_dataframe(items: List[Dict]) -> pd.DataFrame:
    if not items: return pd.DataFrame(columns=["date","title","agency","country","category","impact","url"])
    rows=[]
    for d in items:
        rows.append([d["dateIso"], d["title"], d["sourceName"], d.get("country",""), d["category"], d["impact"], d["url"]])
    df = pd.DataFrame(rows, columns=["date","title","agency","country","category","impact","url"])
    # 최신순
    try:
        df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
    except Exception:
        pass
    return df

def make_markdown_report(df: pd.DataFrame, since_days: int) -> str:
    if df.empty:
        return "# 규제/뉴스 업데이트 보고서\n\n데이터가 없습니다."
    head = f"# 규제/뉴스 업데이트 보고서\n\n- 생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n- 기준: 최근 {since_days}일\n- 총 항목: {len(df)}건\n"
    # 카테고리/기관 요약
    summary_cat = df["category"].value_counts().to_dict()
    summary_ag  = df["agency"].value_counts().to_dict()
    lines = [head, "## 요약(카테고리)",]
    for k,v in summary_cat.items(): lines.append(f"- {k}: {v}건")
    lines.append("\n## 요약(기관)")
    for k,v in summary_ag.items(): lines.append(f"- {k}: {v}건")
    lines.append("\n## 상세 목록")
    for _,r in df.iterrows():
        lines.append(f"- **[{r['title']}]({r['url']})**  \n  - 기관: {r['agency']} | 국가: {r['country']} | 분류: {r['category']} | 영향도: {r['impact']}  \n  - 날짜: {r['date']}")
    return "\n".join(lines)

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8-sig")

# ---------------------------
# UI 본문
# ---------------------------
st.markdown(f"""
<div class="big-header">
  <span class="brand-title">RegWatch</span>
  <span class="subtitle">글로벌 규제 모니터링(보고서)</span>
</div>
""", unsafe_allow_html=True)

left, mid, right = st.columns([2,2,3])
with left:
    sel = st.multiselect("수집 대상", [s["id"] for s in SOURCES],
                         default=[s["id"] for s in SOURCES],
                         format_func=lambda sid: next(s["name"] for s in SOURCES if s["id"]==sid))
with mid:
    since_days = st.slider("최근 N일만 보기", 3, 60, 14)
with right:
    do = st.button("업데이트 실행", use_container_width=True, help="선택한 사이트에서 최신 항목을 수집하고 요약합니다.")

if do or "items" not in st.session_state:
    with st.spinner("수집·요약 중..."):
        items = fetch_all(sel or [s["id"] for s in SOURCES])
        # 포텐스 키가 없으면 simple_summary가 이미 적용됨. (RSS의 desc 또는 제목 위주)
        st.session_state.items = items

items = st.session_state.get("items", [])

# 기간 필터
cut = datetime.now(timezone.utc) - timedelta(days=since_days)
def _in_range(x):
    try:
        return dtparse.parse(x) >= cut
    except Exception:
        return True
items_recent = [d for d in items if _in_range(d["dateIso"])]

# 표/카드/보고서
df = to_dataframe(items_recent)
st.subheader(f"총 {len(df)}건 · 마지막 업데이트 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# 1) 표
st.dataframe(df, use_container_width=True, hide_index=True)

# 2) 카드 뷰
st.markdown("---")
st.subheader("카드 보기")
for d in items_recent:
    is_new = days_new(d["dateIso"], 7)
    kw = extract_keywords(d["title"] + " " + d["summary"])
    cat_class = {"화학물질규제":"cat-chem","무역정책":"cat-trade","산업정책":"cat-ind","환경규제":"cat-env"}.get(d["category"],"")
    status = "EFFECTIVE" if d["impact"]=="High" else ("DRAFT" if d["impact"]=="Medium" else "ANNOUNCED")
    status_class = {"ANNOUNCED":"badge status-ann","DRAFT":"badge status-draft","EFFECTIVE":"badge status-eff"}[status]
    st.markdown(f"<div class='card {'new' if is_new else ''}'>", unsafe_allow_html=True)
    st.markdown(f"<span class='badge {cat_class}'>{d['category']}</span> "
                f"<span class='badge kor'>{d.get('country','')}</span> "
                f"<span class='{status_class}'>{status}</span>", unsafe_allow_html=True)
    st.markdown(f"<h4>{d['title']}</h4>", unsafe_allow_html=True)
    st.write(d["summary"] or "")
    st.markdown(f"<div class='meta'><b>기관</b>·{d['sourceName']} | <b>날짜</b>·{d['dateIso']}</div>", unsafe_allow_html=True)
    if kw:
        st.markdown(" ".join([f"<span class='keyword'>{k}</span>" for k in kw]), unsafe_allow_html=True)
    st.markdown(f"[원문 보기]({d['url']})")
    st.markdown("</div>", unsafe_allow_html=True)

# 3) 보고서(Markdown) + 다운로드
st.markdown("---")
st.subheader("보고서 생성")
md = make_markdown_report(df, since_days)
st.download_button("📄 Markdown 보고서 다운로드", data=md.encode("utf-8"),
                   file_name=f"regwatch_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md",
                   mime="text/markdown")
st.download_button("🧾 CSV 다운로드", data=df_to_csv_bytes(df),
                   file_name=f"regwatch_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                   mime="text/csv")

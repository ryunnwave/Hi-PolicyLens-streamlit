# -*- coding: utf-8 -*-
"""
RegWatch – 카드형 강화요약 (정책/본문 필터 없음 / 재귀 오류 수정판)
- 모든 항목 수집 → 상세 페이지/또는 PDF 본문을 읽어 구조적 요약 생성
- 하이라이트(주요 변경사항), 시행/발효 추정, 영향 범위, 태그 자동화
- 소스: EEA Analysis, CBP RSS, BMUV RSS, MOTIE 루트, Bundesregierung Aktuelles,
        CBP Bulletin(페이지 내 PDF), U.S. DOE Newsroom
"""

import re, io, hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict

import requests, feedparser, pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import streamlit as st

# PDF
try:
    from pypdf import PdfReader
    HAVE_PYPDF = True
except Exception:
    HAVE_PYPDF = False
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
    HAVE_PDFMINER = True
except Exception:
    HAVE_PDFMINER = False

# ----------------------- UI 기본 -----------------------
st.set_page_config(page_title="RegWatch – 강화요약", layout="wide")
BRAND = "#0f2e69"; ACCENT = "#dc8d32"; BG = "#0f2e69"

st.markdown(f"""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.8/dist/web/static/pretendard.css');
:root {{ --brand:{BRAND}; --accent:{ACCENT}; --muted:#8aa0c6; --ink:#0b1a3a; }}
html, body, [class*="css"] {{ font-family:'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif; }}
.main {{ background:#f6f8fb; }}
.hero {{
  background: linear-gradient(135deg, {BG} 0%, #1a4b8c 100%);
  color:#fff; padding:18px 22px; border-radius:14px; margin:6px 0 14px;
  box-shadow:0 6px 20px rgba(0,0,0,.12)
}}
.hero .title {{ font-size:24px; font-weight:900; margin:0 }}
.hero .subtitle {{ opacity:.92; margin-top:6px }}
.badge {{ display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:800; background:#fff; color:var(--brand); margin-left:8px }}
.kpi {{ background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:14px; text-align:center }}
.kpi .num {{ font-weight:900; font-size:22px; color:var(--brand) }}
.kpi .lab {{ color:#64748b; font-size:12px; margin-top:6px }}
hr.sep {{ border:none; border-top:1px solid #e2e8f0; margin:16px 0 }}

.bigcard {{
  position:relative;
  background:#0f2e69; color:#e6eefc;
  border-radius:16px; padding:18px 18px 14px; margin:14px 0;
  box-shadow:0 10px 28px rgba(15,46,105,.18); border:1px solid rgba(255,255,255,.06);
}}
.bigcard h3 {{ color:#ffffff; margin:0 0 10px; font-size:20px; line-height:1.35; letter-spacing:-.2px }}
.chips {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px }}
.chip {{ font-size:11px; font-weight:800; padding:4px 10px; border-radius:999px; background:#0b1a3a; color:#dbe7ff; border:1px solid rgba(255,255,255,.1) }}
.chip.new {{ background:#ffedd5; color:#b45309 }}
.meta {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0 6px }}
.meta .box {{ background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.08); border-radius:10px; padding:8px 10px; font-size:12px }}
.section {{ background:#0b1a3a; border:1px solid rgba(255,255,255,.08); border-radius:12px; padding:10px 12px; margin-top:10px }}
.section h4 {{ margin:0 0 6px; font-size:13px; color:#cfe2ff }}
.tags {{ margin-top:10px }}
.tag {{ display:inline-block; font-size:11px; padding:3px 10px; border-radius:999px; background:#08142c; color:#bcd2ff; margin-right:6px; border:1px solid rgba(255,255,255,.06) }}
.cta {{ margin-top:10px }}
a.btn {{
  display:inline-block; background:#fff; color:#0f2e69; font-weight:800; font-size:13px;
  padding:8px 14px; border-radius:10px; text-decoration:none; border:1px solid #e2e8f0;
}}
small.mono {{ font-family:ui-monospace,Menlo,Consolas,"Courier New",monospace; color:#9fb5db }}
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="hero">
  <div class="title">RegWatch <span class="badge">강화요약</span></div>
  <div class="subtitle">필터 없이 수집 → 상세 본문·PDF를 읽어 카드형 요약을 생성합니다.</div>
</div>
""", unsafe_allow_html=True)

# ----------------------- 소스 정의 -----------------------
USER_AGENT = "Mozilla/5.0 (compatible; RegWatch/2.1; +https://streamlit.io)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
TIMEOUT = 25
MAX_PER_SOURCE = 60

SOURCES = [
    {"id":"eea",   "name":"EEA – Analysis", "type":"html", "url":"https://www.eea.europa.eu/en/analysis"},
    {"id":"cbp",   "name":"CBP – RSS", "type":"rss-multi",
     "urls":["https://www.cbp.gov/rss/trade","https://www.cbp.gov/rss/press-releases"]},
    {"id":"bmuv",  "name":"BMUV – Meldungen RSS", "type":"rss", "url":"https://www.bundesumweltministerium.de/meldungen.rss"},
    {"id":"motie", "name":"MOTIE (산업부) 루트", "type":"html", "url":"https://www.motie.go.kr/"},
    {"id":"bund",  "name":"Bundesregierung – Aktuelles", "type":"html", "url":"https://www.bundesregierung.de/breg-de/aktuelles"},
    {"id":"cbp_bull","name":"CBP – Bulletin/Decisions (PDF)", "type":"html", "url":"https://www.cbp.gov/trade/rulings/bulletin-decisions"},
    {"id":"doe",   "name":"U.S. DOE – Newsroom", "type":"html", "url":"https://www.energy.gov/newsroom"},
]
COUNTRY = {"cbp":"미국","cbp_bull":"미국","bmuv":"독일","motie":"대한민국","eea":"EU","bund":"독일","doe":"미국"}

# ----------------------- 로그 -----------------------
if "logs" not in st.session_state: st.session_state.logs=[]
def log(msg): st.session_state.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def clear_logs(): st.session_state.logs=[]

# ----------------------- 유틸 -----------------------
def md5_hex(s:str)->str: return hashlib.md5(s.encode("utf-8")).hexdigest()
def clean(s:str)->str: return re.sub(r"\s+"," ", (s or "")).strip()
def to_iso(s:str)->str:
    if not s: return datetime.now(timezone.utc).isoformat()
    try:
        d=dtparse.parse(s); d=d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()
def title_from_url(url:str)->str:
    from urllib.parse import urlparse, unquote
    p=urlparse(url); segs=[unquote(x) for x in p.path.split("/") if x]
    if not segs: return p.netloc
    return " / ".join(segs[-3:]).replace("-"," ").title()

def extract_keywords(text:str, topn=6):
    stop=set(["the","and","for","with","from","that","this","are","was","were","will","have","has","been","on","of","in","to","a","an","by",
              "및","과","에","의","으로","대한","관련"])
    words=re.sub(r"[^a-z0-9가-힣 ]"," ", (text or "").lower()).split()
    freq={}
    for w in words:
        if len(w)<=2 or w in stop: continue
        freq[w]=freq.get(w,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda x:x[1], reverse=True)[:topn]]

def guess_category(title:str)->str:
    t=(title or "").lower()
    if re.search(r"(reach|clp|pfas|biocide|chemical|substance|restriction|authorisation)", t): return "화학물질규제"
    if re.search(r"(tariff|duty|quota|rate|import|export|cbp|customs|ruling|bulletin)", t): return "무역정책"
    if re.search(r"(environment|climate|emission|umwelt|환경|energy|전력|에너지)", t): return "환경규제"
    if re.search(r"(policy|industry|manufactur|산업|전략|투자)", t): return "산업정책"
    return "산업정책"

# ----------------------- HTTP -----------------------
@st.cache_data(ttl=1500, show_spinner=False)
def http_get(url:str)->str:
    r=requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status(); return r.text
def http_get_bytes(url:str)->bytes:
    r=requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status(); return r.content
def fetch_with_fallback(url:str)->str:
    try:
        return http_get(url)
    except Exception as ex:
        log(f"직접요청 실패 → 프록시 시도: {url} ({type(ex).__name__})")
        base=re.sub(r"^https?://","", url)
        for prefix in ("https://r.jina.ai/http://","https://r.jina.ai/https://"):
            try:
                txt=http_get(prefix+base)
                if txt and len(txt)>100: return txt
            except Exception: pass
        raise
def fetch_bytes_with_fallback(url:str)->bytes:
    try:
        return http_get_bytes(url)
    except Exception:
        txt=fetch_with_fallback(url)
        return txt.encode("utf-8", errors="ignore")

# ----------------------- PDF -----------------------
def is_pdf_url(url:str)->bool:
    return url.split("?")[0].lower().endswith(".pdf")

def pdf_bytes_to_text(data:bytes, max_pages=14, max_chars=60000)->str:
    if HAVE_PYPDF:
        try:
            reader=PdfReader(io.BytesIO(data))
            parts=[]
            for p in reader.pages[:max_pages]:
                try: parts.append(p.extract_text() or "")
                except Exception: break
            txt=clean(" ".join(parts))
            if len(txt)>80: return txt[:max_chars]
        except Exception: pass
    if HAVE_PDFMINER:
        try:
            txt=pdfminer_extract_text(io.BytesIO(data))
            return clean(txt)[:max_chars]
        except Exception: pass
    return ""

def fetch_pdf_text(url:str)->str:
    data=fetch_bytes_with_fallback(url)
    return pdf_bytes_to_text(data)

def find_pdf_in_html(html:str, base_url:str)->str|None:
    soup=BeautifulSoup(html,"html.parser")
    for a in soup.find_all("a", href=True):
        href=a["href"].strip()
        if not href: continue
        if href.lower().endswith(".pdf"):
            from urllib.parse import urljoin
            return href if href.startswith("http") else urljoin(base_url, href)
    return None

# ----------------------- 본문 추출 & 강화 요약 -----------------------
def extract_page_text(html:str)->str:
    soup=BeautifulSoup(html,"html.parser")
    meta=soup.find("meta", attrs={"name":"description"}) or soup.find("meta", attrs={"property":"og:description"})
    if meta and meta.get("content"): return clean(meta["content"])
    for sel in ["article","main","[role=main]",".content",".article",".post",".richtext",".usa-prose",".body-content"]:
        for tag in soup.select(sel):
            txt=tag.get_text(" ", strip=True)
            if txt and len(txt)>160: return clean(txt)
    ps=soup.find_all("p")
    return clean(" ".join(p.get_text(' ', strip=True) for p in ps[:6]))

DATE_PAT = r"(20\d{2}[-./년 ]\s?(?:0?[1-9]|1[0-2])[-./월 ]\s?(?:0?[1-9]|[12]\d|3[01])(?:일)?)"
HL_WORDS = r"(must|shall|required|require|ban|prohibit|effective|applicab|enforce|submit|report|duty|tariff|quota|amend|revise|extend|deadline|from|until|by|notice|guidance|draft|consultation|개정|시행|발효|의무|제출|보고|금지|연장|의견수렴|초안)"

def split_sents(text:str)->List[str]:
    t=clean(text)
    try: s=re.split(r'(?:(?<=\.)|(?<=!)|(?<=\?)|(?<=다\.)|(?<=요\.))\s+', t)
    except re.error: s=re.split(r'[.!?]\s+', t)
    return [x.strip() for x in s if x.strip()]

def pick_highlights(text: str, topn=5) -> List[str]:
    sents = split_sents(text)
    hits = [s for s in sents if re.search(HL_WORDS, s, re.I)]
    if not hits:
        hits = sents[:topn]
    seen, out = set(), []
    for h in hits:
        key = h.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h[:220])
        if len(out) >= topn:
            break
    return out

def guess_effective_date(text:str)->str:
    m=re.search(r"(effective|applicable|enforce(?:ment)?|시행|발효)[^\.]{0,40}?"+DATE_PAT, text, re.I)
    if not m:
        m=re.search(DATE_PAT, text)
    if not m: return ""
    try: return to_iso(m.group(0).replace("년","-").replace("월","-").replace("일",""))
    except Exception: return ""

def guess_scope(text:str)->str:
    t=text.lower()
    scope=[]
    if re.search(r"importer|exporter|customs|trade|supply|supply chain|import|export|duty|tariff", t): scope.append("수입·무역주체")
    if re.search(r"manufacturer|producer|chemical|factory|industrial", t): scope.append("제조업")
    if re.search(r"energy|fuel|electric|power|renewable|grid", t): scope.append("에너지")
    if re.search(r"retail|consumer|product", t): scope.append("소비재")
    if re.search(r"financial|bank|insurance", t): scope.append("금융")
    return " · ".join(scope) or "일반 기업/기관"

def strengthen_summary(title: str, body: str, power: int = 4) -> Dict[str, str]:
    """요약 강도(power)를 인자로 받아 재귀 없이 길이/하이라이트 개수만 조절"""
    sents = split_sents(body)
    abs_sent_n = min(2 + power, 6)                    # 3~6문장
    abstract = " ".join(sents[:abs_sent_n])[:480] if sents else body[:480]

    hl_topn = min(2 + power, 8)                       # 3~8개
    bullets = pick_highlights(body, topn=hl_topn)
    if not bullets:
        bullets = sents[:min(3, len(sents))]

    eff = guess_effective_date(body)
    scope = guess_scope(body)
    tags = extract_keywords(title + " " + body, topn=6)
    return {"abstract": abstract, "bullets": bullets, "effective": eff, "scope": scope, "tags": tags}

def scrape_detail(url:str)->Dict[str,str]:
    out={"body":"","date":""}
    try:
        if is_pdf_url(url):
            out["body"]=fetch_pdf_text(url); return out
        html=fetch_with_fallback(url)
        body=extract_page_text(html); out["body"]=body
        soup=BeautifulSoup(html,"html.parser")
        dt = soup.find("time") or soup.find("meta", attrs={"property":"article:published_time"}) or soup.find("meta", attrs={"name":"date"})
        if dt:
            out["date"] = dt.get("datetime") or dt.get("content") or dt.get_text(strip=True) or ""
        if len(out["body"])<120:
            pdf=find_pdf_in_html(html, url)
            if pdf: out["body"]=fetch_pdf_text(pdf)
    except Exception as ex:
        log(f"상세 추출 실패: {type(ex).__name__}")
    return out

# ----------------------- 수집기 -----------------------
def normalize(source_id, name, title, url, date_iso, summary)->Dict:
    if not url: return {}
    return {
        "id": md5_hex(url), "sourceId": source_id, "sourceName": name,
        "title": clean(title or url), "url": url, "dateIso": to_iso(date_iso or ""),
        "category": guess_category(title or url), "country": COUNTRY.get(source_id,""),
        "summary": clean(summary or "")
    }

def fetch_rss(feed_url, sid, name):
    items=[]
    try:
        raw=fetch_bytes_with_fallback(feed_url)
        d=feedparser.parse(raw)
        for e in d.entries[:MAX_PER_SOURCE]:
            items.append(normalize(sid,name,
                getattr(e,"title",""), getattr(e,"link",""),
                getattr(e,"published","") or getattr(e,"updated","") or "",
                getattr(e,"summary","") or getattr(e,"description","") or ""))
        log(f"RSS OK [{name}] {len(items)}건")
    except Exception as ex:
        log(f"RSS FAIL [{name}] {type(ex).__name__}: {ex}")
    return items

def fetch_rss_multi(urls, sid, name):
    out=[]
    for u in urls: out += fetch_rss(u, sid, name)
    uniq={}; [uniq.setdefault(i["url"], i) for i in out]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_links_by_contains(url, must_contains:str, sid, name, base=None):
    items=[]
    try:
        html=fetch_with_fallback(url)
        soup=BeautifulSoup(html,"html.parser")
        add=0
        for a in soup.find_all("a", href=True):
            href=a["href"].strip(); text=a.get_text(" ", strip=True)
            if not href or not text: continue
            if must_contains and must_contains not in href: continue
            if not href.startswith("http"):
                from urllib.parse import urljoin
                href=urljoin(base or url, href)
            items.append(normalize(sid,name,text,href,"",text)); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"{name} 링크 {add}건")
    except Exception as ex:
        log(f"{name} FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_motie_root():
    return fetch_links_by_contains("https://www.motie.go.kr/", "", "motie", "MOTIE", base="https://www.motie.go.kr/")

def fetch_all(selected)->List[Dict]:
    out=[]
    for s in SOURCES:
        if s["id"] not in selected: continue
        if s["id"]=="bmuv":
            out += fetch_rss(s["url"], s["id"], s["name"])
        elif s["id"]=="cbp":
            out += fetch_rss_multi(s["urls"], s["id"], s["name"])
        elif s["id"]=="eea":
            out += fetch_links_by_contains(s["url"], "/en/analysis", s["id"], s["name"], base="https://www.eea.europa.eu")
        elif s["id"]=="bund":
            out += fetch_links_by_contains(s["url"], "/breg-de/aktuelles", s["id"], s["name"], base="https://www.bundesregierung.de")
        elif s["id"]=="cbp_bull":
            out += fetch_links_by_contains(s["url"], ".pdf", s["id"], s["name"], base="https://www.cbp.gov")
        elif s["id"]=="doe":
            out += fetch_links_by_contains(s["url"], "/newsroom", s["id"], s["name"], base="https://www.energy.gov")
        elif s["id"]=="motie":
            out += fetch_motie_root()
    uniq={}; [uniq.setdefault(i["url"], i) for i in out]
    log(f"총 수집 {len(uniq)}건")
    return list(uniq.values())

# ----------------------- 컨트롤 -----------------------
c1,c2,c3,c4 = st.columns([2,1.2,1.2,1.6])
with c1:
    selected=st.multiselect("수집 대상", [s["id"] for s in SOURCES],
        default=[s["id"] for s in SOURCES],
        format_func=lambda sid: next(s["name"] for s in SOURCES if s["id"]==sid))
with c2:
    since_days=st.slider("최근 N일", 3, 90, 14)
with c3:
    summary_power=st.select_slider("요약 강도", options=[1,2,3,4,5], value=4,
        help="값이 클수록 요약을 길고/하이라이트 더 많이 생성")
with c4:
    user_terms=st.text_input("추가 키워드(선택, 쉼표구분)", value="")  # 검색에만 사용

a,b,d = st.columns([1,1,2])
with a:
    do=st.button("업데이트 실행", use_container_width=True)
with b:
    if st.button("캐시 초기화", use_container_width=True):
        st.cache_data.clear(); clear_logs(); st.success("HTTP 캐시 비움")
with d:
    show_debug=st.toggle("디버그", value=False)

st.markdown("<hr class='sep'/>", unsafe_allow_html=True)

# ----------------------- 실행 -----------------------
if do or "raw" not in st.session_state:
    clear_logs()
    with st.spinner("수집 중..."):
        st.session_state.raw = fetch_all(selected or [s["id"] for s in SOURCES])

raw = st.session_state.get("raw", [])

# 기간 필터
cut = datetime.now(timezone.utc) - timedelta(days=since_days)
def in_range(iso):
    try: return dtparse.parse(iso) >= cut
    except: return True
items = [d for d in raw if in_range(d["dateIso"])]

# 상세 · 강화요약
processed=[]
with st.spinner("상세 본문/ PDF 요약 생성 중..."):
    for it in items:
        detail = scrape_detail(it["url"])
        body = detail.get("body","")
        if not body or len(body) < 60:
            body = it.get("summary") or title_from_url(it["url"])
        if detail.get("date"): it["dateIso"] = to_iso(detail["date"])
        y = strengthen_summary(it["title"], body, power=summary_power)
        it.update({
            "abs": y["abstract"],
            "bullets": y["bullets"],
            "effective": y["effective"] or "-",
            "scope": y["scope"],
            "tags": y["tags"],
        })
        processed.append(it)

# KPI
k1,k2,k3,k4 = st.columns(4)
k1.markdown(f"<div class='kpi'><div class='num'>{len(raw)}</div><div class='lab'>수집(원본)</div></div>", unsafe_allow_html=True)
k2.markdown(f"<div class='kpi'><div class='num'>{len(items)}</div><div class='lab'>최근 {since_days}일</div></div>", unsafe_allow_html=True)
k3.markdown(f"<div class='kpi'><div class='num'>{len(processed)}</div><div class='lab'>요약 생성 성공</div></div>", unsafe_allow_html=True)
k4.markdown(f"<div class='kpi'><div class='num'>{datetime.now().strftime('%Y-%m-%d %H:%M')}</div><div class='lab'>최종 업데이트</div></div>", unsafe_allow_html=True)

# 검색/카테고리
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
f1,f2 = st.columns([2,1])
with f1: q=st.text_input("검색(제목/요약/기관/국가)", value=user_terms)
with f2: cat=st.selectbox("카테고리", ["전체","화학물질규제","무역정책","산업정책","환경규제"])

data=processed
if q:
    ql=q.lower()
    data=[d for d in data if ql in (d["title"]+" "+d.get("abs","")+" "+d["sourceName"]+" "+d.get("country","")).lower()]
if cat!="전체":
    data=[d for d in data if d["category"]==cat]

# 표 + 다운로드
df = pd.DataFrame([{
    "date":d["dateIso"], "title":d["title"], "agency":d["sourceName"], "country":d.get("country",""),
    "category":d["category"], "effective":d.get("effective",""), "url":d["url"]
} for d in data])
try:
    df["date_dt"]=pd.to_datetime(df["date"], errors="coerce")
    df=df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
except: pass

st.subheader(f"목록 · 총 {len(df)}건")
st.dataframe(df, use_container_width=True, hide_index=True)

# 카드
def days_new(iso:str, days=7):
    try: d=dtparse.parse(iso); d=d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception: return False
    return (datetime.now(timezone.utc)-d)<=timedelta(days=days)

st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
st.subheader("카드 보기")

for d in data:
    is_new = days_new(d["dateIso"], 7)
    chips = f"<span class='chip'>{d['category']}</span>"
    if is_new: chips += "<span class='chip new'>NEW</span>"
    if d.get("country"): chips += f"<span class='chip'>{d['country']}</span>"

    bullets = d.get("bullets") or []
    bl_html = "".join([f"<li>{re.sub(r'<.*?>','',b)}</li>" for b in bullets])

    tags_html = " ".join([f"<span class='tag'>{t}</span>" for t in (d.get('tags') or [])])

    st.markdown("<div class='bigcard'>", unsafe_allow_html=True)
    st.markdown(f"<div class='chips'>{chips}</div>", unsafe_allow_html=True)
    st.markdown(f"<h3>{d['title']}</h3>", unsafe_allow_html=True)
    st.write(d.get("abs",""))

    st.markdown("<div class='section'><h4>⚡ 주요 변경사항</h4><ul>"+bl_html+"</ul></div>", unsafe_allow_html=True)

    eff = d.get("effective","-")
    st.markdown(
        f"<div class='meta'>"
        f"  <div class='box'><b>담당 기관</b><br>{d['sourceName']}</div>"
        f"  <div class='box'><b>시행/발효</b><br>{eff}</div>"
        f"</div>",
        unsafe_allow_html=True
    )
    st.markdown(f"<div class='section'><h4>📌 영향 범위</h4>{d.get('scope','')}</div>", unsafe_allow_html=True)

    if tags_html:
        st.markdown(f"<div class='tags'>{tags_html}</div>", unsafe_allow_html=True)

    st.markdown(f"<div class='cta'><a class='btn' href='{d['url']}' target='_blank'>원문 보기</a> &nbsp; <small class='mono'>{d['dateIso']}</small></div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

# 디버그
if show_debug:
    with st.expander("디버그: 로그/원시데이터"):
        st.json(st.session_state.get("raw", []), expanded=False)
        if st.session_state.logs:
            st.markdown("<small class='mono'>"+"<br/>".join(st.session_state.logs)+"</small>", unsafe_allow_html=True)


# -*- coding: utf-8 -*-
"""
RegWatch – Streamlit (번역 제거판)
- 정책/규제 관련성 높은 항목만 수집/표시 (강한 필터 + 사용자 키워드)
- 상세 페이지를 실제로 열어 본문을 추출/요약 (본문 길이 기준 미달 시 제외)
- 차단 대응: r.jina.ai 프록시 + 텍스트 링크 폴백
- JSON/로그는 디버그에서만 표시(기본 숨김)
"""

import os, re, io, json, hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict
from urllib.parse import urlparse, unquote

import requests, feedparser, pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import streamlit as st

# ----------------------- UI / 스타일 -----------------------
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
hr.sep {{ border:none; border-top:1px solid #e2e8f0; margin:16px 0; }}
small.mono {{ font-family: ui-monospace, Menlo, Consolas, "Courier New", monospace; }}
.warn {{ font-size:12px; color:#92400e; background:#fff7ed; padding:8px 10px; border:1px solid #fed7aa; border-radius:8px; }}
</style>
""", unsafe_allow_html=True)

# ----------------------- 설정 -----------------------
USER_AGENT = "Mozilla/5.0 (compatible; RegWatch/1.4; +https://streamlit.io)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
TIMEOUT = 25
MAX_PER_SOURCE = 60

SOURCES = [
    {"id": "echa",  "name": "ECHA (EU – European Chemicals Agency)", "type": "html", "url": "https://echa.europa.eu/legislation"},
    {"id": "cbp",   "name": "U.S. Customs and Border Protection (CBP)", "type": "rss-multi",
     "urls": ["https://www.cbp.gov/rss/trade", "https://www.cbp.gov/rss/press-releases"]},
    {"id": "motie", "name": "MOTIE (대한민국 산업통상자원부)", "type": "html", "url": "https://www.motie.go.kr/"},
    {"id": "bmuv",  "name": "BMUV (독일 환경부)", "type": "rss", "url": "https://www.bundesumweltministerium.de/meldungen.rss"},
]

# ----------------------- 정책/규제 관련성 판별 -----------------------
policy_terms_en = [
    "regulation","regulatory","law","act","bill","directive","ordinance","decree",
    "guidance","notice","enforcement","compliance","consultation","draft","proposal",
    "tariff","duty","quota","import","export","sanction","ban","restriction","rulemaking"
]
policy_terms_ko = [
    "법","법률","법령","시행령","시행규칙","고시","훈령","지침","지시","예규",
    "입법예고","행정예고","규정","개정","제정","시행","공고","안내","의견수렴","초안","고려안","규제"
]
policy_terms_de = [
    "verordnung","gesetz","richtlinie","bekanntmachung","entwurf","änderung",
    "umsetzung","verbot","durchführung","leitlinie","gesetzgebung"
]
POLICY_TERMS = [t.lower() for t in (policy_terms_en + policy_terms_ko + policy_terms_de)]

def is_policy_like_text(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in POLICY_TERMS)

PATH_HINTS = ["/legislation", "/law", "/directive", "/notice", "/meldungen", "/newsroom", "/press", "/regulations", "/guidance"]

def has_policy_signal(item: dict) -> bool:
    title = item.get("title","")
    summary = item.get("summary","")
    url = (item.get("url","") or "").lower()
    # 1) 제목/요약에서 정책 단어
    if is_policy_like_text(title) or is_policy_like_text(summary):
        return True
    # 2) URL 경로 힌트
    return any(h in url for h in PATH_HINTS)

# ----------------------- 로그 -----------------------
if "logs" not in st.session_state: st.session_state.logs=[]
def log(msg): st.session_state.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def clear_logs(): st.session_state.logs=[]

# ----------------------- 유틸 -----------------------
def md5_hex(s:str)->str: return hashlib.md5(s.encode("utf-8")).hexdigest()

def to_iso(s:str)->str:
    if not s: return datetime.now(timezone.utc).isoformat()
    try:
        d=dtparse.parse(s);  d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def clean_text(s:str)->str: return re.sub(r"\s+"," ", (s or "")).strip()

def split_sentences(text: str):
    t = clean_text(text)
    pattern = r'(?:(?<=\.)|(?<=!)|(?<=\?)|(?<=다\.)|(?<=요\.))\s+'
    try:
        parts = re.split(pattern, t)
    except re.error:
        parts = re.split(r'[.!?]\s+', t)
    return [p for p in parts if p]

def simple_summary(text: str, max_sentences=3, max_len=420) -> str:
    try:
        sents = split_sentences(text)
        out = " ".join(sents[:max_sentences]) if sents else clean_text(text)
        return out[:max_len]
    except Exception:
        return clean_text(text)[:max_len]

def extract_keywords(text: str, topn=5):
    stop=set(["the","and","for","with","from","that","this","are","was","were","will","have","has","been","on","of","in","to","a","an","by","및","과","에","의","으로"])
    words=re.sub(r"[^a-z0-9가-힣 ]"," ", (text or "").lower()).split()
    freq={}
    for w in words:
        if len(w)<=2 or w in stop: continue
        freq[w]=freq.get(w,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda x:x[1], reverse=True)[:topn]]

def guess_category(title:str)->str:
    t=(title or "").lower()
    if re.search(r"(reach|clp|pfas|biocide|chemical|substance|restriction|authorisation)", t): return "화학물질규제"
    if re.search(r"(tariff|duty|quota|fees|rate|ace|import|export|cbp|customs)", t): return "무역정책"
    if re.search(r"(environment|climate|emission|umwelt|환경)", t): return "환경규제"
    if re.search(r"(policy|industry|manufactur|산업|혁신|투자)", t): return "산업정책"
    return "산업정책"

def guess_impact(text:str)->str:
    t=(text or "").lower()
    if re.search(r"(effective|mandatory|ban|prohibit|enforce|in force|penalty|시행|발효)", t): return "High"
    if re.search(r"(proposal|draft|consultation|comment|plan|roadmap|초안|의견수렴)", t): return "Medium"
    return "Low"

def country_of(src:str)->str: return {"cbp":"미국","bmuv":"독일","motie":"대한민국","echa":"EU"}.get(src,"")

def normalize(source_id, source_name, title, url, date_iso, summary)->Dict:
    if not url: return {}
    title=clean_text(title or url)
    return {
        "id": md5_hex(url), "sourceId": source_id, "sourceName": source_name,
        "title": title, "url": url, "dateIso": to_iso(date_iso or ""),
        "category": guess_category(title), "summary": clean_text(summary or ""),
        "impact": guess_impact(title + " " + (summary or "")),
        "country": country_of(source_id)
    }

def title_from_url(url: str) -> str:
    p = urlparse(url); segs = [unquote(s) for s in p.path.split("/") if s]
    if not segs: return p.netloc
    segs = segs[-3:]; t = " ".join(s.replace("-", " ").strip() for s in segs)
    return t.title()

# ----------------------- HTTP + 폴백 -----------------------
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
        base = re.sub(r"^https?://","", url)
        for prefix in ("https://r.jina.ai/http://", "https://r.jina.ai/https://"):
            try:
                txt=http_get(prefix + base)
                if txt and len(txt)>100: return txt
            except Exception as ex2:
                log(f"프록시 실패: {prefix}... ({type(ex2).__name__})")
        raise

def fetch_bytes_with_fallback(url:str)->bytes:
    try:
        return http_get_bytes(url)
    except Exception as ex:
        log(f"직접요청(바이트) 실패 → 프록시 텍스트로 대체: {url} ({type(ex).__name__})")
        txt = fetch_with_fallback(url)
        return txt.encode("utf-8", errors="ignore")

def extract_links_from_text(text: str, domain: str, include: List[str]=None, limit=80) -> List[str]:
    pat = rf'https?://(?:www\.)?{re.escape(domain)}/[^\s\)\"\']+'
    urls = re.findall(pat, text)
    if include:
        urls = [u for u in urls if any(k in u for k in include)]
    seen = {}
    for u in urls:
        if u not in seen:
            seen[u] = True
            if len(seen) >= limit: break
    return list(seen.keys())

# ----------------------- 상세 페이지 본문 추출 -----------------------
def extract_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # 1) 메타 설명
    meta = soup.find("meta", attrs={"name":"description"}) or soup.find("meta", attrs={"property":"og:description"})
    if meta and meta.get("content"): return clean_text(meta["content"])
    # 2) 본문 후보
    candidates = []
    for sel in ["article", "main", "[role=main]", ".content", ".article", ".post", ".text", ".richtext", ".body-content"]:
        for tag in soup.select(sel):
            txt = tag.get_text(" ", strip=True)
            if txt and len(txt) > 120:
                candidates.append(txt)
    if candidates: return max(candidates, key=len)
    # 3) 일반 p 태그 앞부분
    ps = soup.find_all("p")
    txt = " ".join(p.get_text(" ", strip=True) for p in ps[:5])
    return clean_text(txt)

def scrape_detail_summary(url: str) -> Dict[str, str]:
    """상세 페이지를 열어 본문 및 날짜 후보를 추출."""
    out = {"body":"","date":""}
    try:
        html = fetch_with_fallback(url)
        body = extract_page_text(html)
        out["body"] = clean_text(body)
        soup = BeautifulSoup(html, "html.parser")
        # 날짜 후보
        dt_tag = soup.find("time") or soup.find("meta", attrs={"property":"article:published_time"}) \
                 or soup.find("meta", attrs={"name":"date"})
        if dt_tag:
            if dt_tag.name == "meta":
                out["date"] = dt_tag.get("content","")
            else:
                out["date"] = dt_tag.get("datetime","") or dt_tag.get_text(strip=True)
    except Exception as ex:
        log(f"상세 추출 실패: {type(ex).__name__}")
    return out

# ----------------------- 수집기 -----------------------
def fetch_rss_one(source_id, name, feed_url)->List[Dict]:
    out=[]
    try:
        raw=fetch_bytes_with_fallback(feed_url)
        d=feedparser.parse(raw)
        n=0
        for e in d.entries[:MAX_PER_SOURCE]:
            title=getattr(e,"title",""); link=getattr(e,"link","")
            pub=getattr(e,"published","") or getattr(e,"updated","") or ""
            desc=getattr(e,"summary","") or getattr(e,"description","") or ""
            it=normalize(source_id, name, title, link, pub, simple_summary(f"{title}. {desc}"))
            if it: out.append(it); n+=1
        log(f"RSS OK [{name}] {n}건")
    except Exception as ex:
        log(f"RSS FAIL [{name}] {type(ex).__name__}: {ex}")
    return out

def fetch_rss_multi(source_id, name, urls:List[str])->List[Dict]:
    out=[]
    for u in urls: out += fetch_rss_one(source_id, name, u)
    uniq={};  [uniq.setdefault(it["url"], it) for it in out]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_echa_legislation()->List[Dict]:
    items=[]
    try:
        html = fetch_with_fallback("https://echa.europa.eu/legislation")
        soup = BeautifulSoup(html, "html.parser")
        cnt=0
        for s in soup.find_all("script", {"type":"application/ld+json"}):
            try:
                j=json.loads(s.string or "")
                arr=j if isinstance(j,list) else [j]
                for n in arr:
                    title=(n.get("headline") or n.get("name") or "").strip()
                    url=(n.get("url") or (n.get("mainEntityOfPage") or {}).get("@id") or "").strip()
                    if not title or not url: continue
                    date=n.get("datePublished") or n.get("dateModified") or ""
                    desc=n.get("description") or ""
                    items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", title, url, date, simple_summary(desc or title)))
                    cnt+=1
            except: pass
        log(f"ECHA JSON-LD {cnt}건")

        if len(items)<10:
            add=0
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=a.get_text(strip=True)
                if not href or not text: continue
                if not re.search(r"/(legislation|news)", href): continue
                href = href if href.startswith("http") else f"https://echa.europa.eu{href}"
                items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", text, href, "", simple_summary(text))); add+=1
                if len(items)>=MAX_PER_SOURCE: break
            log(f"ECHA 링크 보조 {add}건")
    except Exception as ex:
        log(f"ECHA FAIL {type(ex).__name__}: {ex}")

    if len(items)<5:
        try:
            txt = fetch_with_fallback("https://echa.europa.eu/legislation")
            links = extract_links_from_text(txt, "echa.europa.eu", include=["legislation","news"], limit=50)
            add=0
            for u in links:
                items.append(normalize("echa","ECHA (EU – European Chemicals Agency)", title_from_url(u), u, "", simple_summary(title_from_url(u))))
                add+=1
            log(f"ECHA 텍스트링크 폴백 {add}건")
        except Exception as ex:
            log(f"ECHA TXT FAIL {type(ex).__name__}: {ex}")

    uniq={}; [uniq.setdefault(it["url"], it) for it in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_motie_generic()->List[Dict]:
    items=[]
    try:
        html=fetch_with_fallback("https://www.motie.go.kr/")
        soup=BeautifulSoup(html, "html.parser")

        add1=0
        for tr in soup.find_all("tr"):
            a=tr.find("a", href=True)
            if not a: continue
            href=a["href"]; title=a.get_text(strip=True)
            if not title: continue
            if not href.startswith("http"): href="https://www.motie.go.kr"+href
            txt=tr.get_text(" ", strip=True)
            m=re.search(r"(\d{4}[.\-]\d{2}[.\-]\d{2})", txt); date=m.group(1).replace(".","-") if m else ""
            items.append(normalize("motie","MOTIE (대한민국 산업통상자원부)", title, href, date, simple_summary(title))); add1+=1
            if len(items)>=MAX_PER_SOURCE: break

        add2=0
        if len(items)<10:
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=a.get_text(strip=True)
                if not href or not text: continue
                if not re.search(r"/(bbs|board|news|notice|press)", href, re.I): continue
                if not href.startswith("http"): href="https://www.motie.go.kr"+href
                items.append(normalize("motie","MOTIE (대한민국 산업통상자원부)", text, href, "", simple_summary(text))); add2+=1
                if len(items)>=MAX_PER_SOURCE: break
        log(f"MOTIE 추출 tr:{add1} + link:{add2}")
    except Exception as ex:
        log(f"MOTIE FAIL {type(ex).__name__}: {ex}")

    if len(items)<5:
        try:
            txt = fetch_with_fallback("https://www.motie.go.kr/")
            links = extract_links_from_text(txt, "www.motie.go.kr", include=["bbs","board","news","press","notice"], limit=50)
            add=0
            for u in links:
                items.append(normalize("motie","MOTIE (대한민국 산업통상자원부)", title_from_url(u), u, "", simple_summary(title_from_url(u)))); add+=1
            log(f"MOTIE 텍스트링크 폴백 {add}건")
        except Exception as ex:
            log(f"MOTIE TXT FAIL {type(ex).__name__}: {ex}")

    uniq={}; [uniq.setdefault(it["url"], it) for it in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_cbp_html_fallback()->List[Dict]:
    items=[]
    pages=[("https://www.cbp.gov/newsroom", "U.S. Customs and Border Protection (CBP)"),
           ("https://www.cbp.gov/trade", "U.S. Customs and Border Protection (CBP)")]
    added=0
    for url, name in pages:
        try:
            html=fetch_with_fallback(url)
            soup=BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=a.get_text(strip=True)
                if not href or not text: continue
                if not re.search(r"/newsroom/", href): continue
                if not href.startswith("http"): href="https://www.cbp.gov"+href
                items.append(normalize("cbp", name, text, href, "", simple_summary(text))); added+=1
                if len(items)>=MAX_PER_SOURCE: break
        except Exception as ex:
            log(f"CBP HTML FAIL {type(ex).__name__}: {ex}")

    if len(items)<5:
        try:
            for url,_ in pages:
                txt = fetch_with_fallback(url)
                links = extract_links_from_text(txt, "www.cbp.gov", include=["newsroom"], limit=80)
                for u in links:
                    items.append(normalize("cbp","U.S. Customs and Border Protection (CBP)", title_from_url(u), u, "", simple_summary(title_from_url(u))))
            log(f"CBP 텍스트링크 폴백 {len(items)}건")
        except Exception as ex:
            log(f"CBP TXT FAIL {type(ex).__name__}: {ex}")

    uniq={}; [uniq.setdefault(it["url"], it) for it in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_bmuv_html_fallback()->List[Dict]:
    items=[]
    try:
        html=fetch_with_fallback("https://www.bundesumweltministerium.de/meldungen")
        soup=BeautifulSoup(html, "html.parser")
        add=0
        for a in soup.find_all("a", href=True):
            href=a["href"]; text=a.get_text(strip=True)
            if not href or not text: continue
            if not re.search(r"/meldungen/", href): continue
            if not href.startswith("http"): href="https://www.bundesumweltministerium.de"+href
            items.append(normalize("bmuv","BMUV (독일 환경부)", text, href, "", simple_summary(text))); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"BMUV HTML 폴백 {add}건")
    except Exception as ex:
        log(f"BMUV HTML FAIL {type(ex).__name__}: {ex}")

    if len(items)<5:
        try:
            txt = fetch_with_fallback("https://www.bundesumweltministerium.de/meldungen")
            links = extract_links_from_text(txt, "www.bundesumweltministerium.de", include=["meldungen"], limit=80)
            for u in links:
                items.append(normalize("bmuv","BMUV (독일 환경부)", title_from_url(u), u, "", simple_summary(title_from_url(u))))
            log(f"BMUV 텍스트링크 폴백 {len(items)}건")
        except Exception as ex:
            log(f"BMUV TXT FAIL {type(ex).__name__}: {ex}")

    uniq={}; [uniq.setdefault(it["url"], it) for it in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# ----------------------- 전체 파이프라인 -----------------------
def fetch_all(selected_ids:List[str])->List[Dict]:
    out=[]
    for s in SOURCES:
        if s["id"] not in selected_ids: continue
        try:
            if s["type"]=="rss":
                r=fetch_rss_one(s["id"], s["name"], s["url"])
                out+= (r or fetch_bmuv_html_fallback())
            elif s["type"]=="rss-multi":
                r=fetch_rss_multi(s["id"], s["name"], s["urls"])
                out+= (r or fetch_cbp_html_fallback())
            elif s["id"]=="echa":
                out+=fetch_echa_legislation()
            elif s["id"]=="motie":
                out+=fetch_motie_generic()
        except Exception as ex:
            log(f"PIPE FAIL [{s['name']}]: {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(it["url"], it) for it in out]
    log(f"총 수집 {len(uniq)}건 (후처리 전)")
    return list(uniq.values())

# ----------------------- 보고서/다운로드 -----------------------
def to_dataframe(items:List[Dict])->pd.DataFrame:
    if not items: return pd.DataFrame(columns=["date","title","agency","country","category","impact","url"])
    rows=[[d["dateIso"], d["title"], d["sourceName"], d.get("country",""), d["category"], d["impact"], d["url"]] for d in items]
    df=pd.DataFrame(rows, columns=["date","title","agency","country","category","impact","url"])
    try:
        df["date_dt"]=pd.to_datetime(df["date"], errors="coerce")
        df=df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
    except: pass
    return df

def make_markdown_report(df:pd.DataFrame, since_days:int)->str:
    if df.empty: return "# 규제/뉴스 업데이트 보고서\n\n데이터가 없습니다."
    head=f"# 규제/뉴스 업데이트 보고서\n\n- 생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n- 기준: 최근 {since_days}일\n- 총 항목: {len(df)}건\n"
    lines=[head, "## 요약(카테고리)"]
    for k,v in df["category"].value_counts().to_dict().items(): lines.append(f"- {k}: {v}건")
    lines.append("\n## 요약(기관)")
    for k,v in df["agency"].value_counts().to_dict().items(): lines.append(f"- {k}: {v}건")
    lines.append("\n## 상세 목록")
    for _,r in df.iterrows():
        lines.append(f"- **[{r['title']}]({r['url']})**  \n  - 기관: {r['agency']} | 국가: {r['country']} | 분류: {r['category']} | 영향도: {r['impact']}  \n  - 날짜: {r['date']}")
    return "\n".join(lines)

# ----------------------- UI -----------------------
st.markdown(f"<div class='big-header'><span class='brand-title'>RegWatch</span> 글로벌 규제 모니터링 (정책/규제 전용 · 번역 없음)</div>", unsafe_allow_html=True)

t1,t2,t3=st.columns([2,2,3])
with t1:
    selected=st.multiselect("수집 대상", [s["id"] for s in SOURCES],
        default=[s["id"] for s in SOURCES],
        format_func=lambda sid: next(s["name"] for s in SOURCES if s["id"]==sid))
with t2:
    since_days=st.slider("최근 N일만 보기", 3, 90, 14)
with t3:
    min_body_chars = st.slider("본문 최소 길이(요약 가능 기준)", 80, 800, 200, step=20,
                               help="상세 페이지에서 추출한 본문 길이가 이 값 미만이면 제외합니다.")

a,b,c=st.columns([1,1,1])
with a:
    do=st.button("업데이트 실행", use_container_width=True)
with b:
    if st.button("캐시 초기화", use_container_width=True):
        st.cache_data.clear(); clear_logs(); st.success("HTTP 캐시를 비웠습니다.")
with c:
    show_debug = st.toggle("디버그 모드", value=False)

st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
pol_col1, pol_col2 = st.columns([1,2])
with pol_col1:
    policy_only = st.checkbox("정책/규제 관련 항목만 보기(강한 필터)", value=True)
with pol_col2:
    user_terms = st.text_input("추가 포함 키워드(쉼표로 구분, 선택)", value="PFAS, REACH, CLP")

if do or "items_raw" not in st.session_state:
    clear_logs()
    with st.spinner("수집 중..."):
        st.session_state.items_raw = fetch_all(selected or [s["id"] for s in SOURCES])

items = st.session_state.get("items_raw", [])

# 기간 필터
cut = datetime.now(timezone.utc) - timedelta(days=since_days)
def in_range(iso):
    try: return dtparse.parse(iso) >= cut
    except: return True
items_recent=[d for d in items if in_range(d["dateIso"])]

# 상세 페이지 요약 보강 + 정책/본문 필터
def matches_user_terms(item):
    terms = [t.strip().lower() for t in user_terms.split(",") if t.strip()]
    if not terms: return True
    s = (item.get("title","") + " " + item.get("summary","")).lower()
    return any(t in s for t in terms)

processed=[]
with st.spinner("상세 페이지 분석 및 요약 중..."):
    for it in items_recent:
        # 상세페이지 크롤링
        detail = scrape_detail_summary(it["url"])
        body = detail.get("body","")
        if detail.get("date"):
            it["dateIso"] = to_iso(detail["date"])
        # 본문 길이 기준
        if len(body) < min_body_chars:
            continue
        # 간단 요약
        it["summary"] = simple_summary(body, 3, 420)
        # 정책/규제 필터
        if policy_only and not has_policy_signal(it):
            continue
        # 사용자 키워드
        if not matches_user_terms(it):
            continue
        processed.append(it)

# 검색/카테고리
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
f1,f2=st.columns([2,2])
with f1: q=st.text_input("검색어(제목/요약/기관/국가)")
with f2: cat=st.selectbox("카테고리", ["전체","화학물질규제","무역정책","산업정책","환경규제"], index=0)

data=processed
if q:
    ql=q.lower()
    data=[d for d in data if ql in (d["title"]+" "+d["summary"]+" "+d["sourceName"]+" "+d.get("country","")).lower()]
if cat!="전체":
    data=[d for d in data if d["category"]==cat]

# 표
df = pd.DataFrame([{
    "date": d["dateIso"], "title": d["title"], "agency": d["sourceName"], "country": d.get("country",""),
    "category": d["category"], "impact": d["impact"], "url": d["url"]
} for d in data])
try:
    df["date_dt"]=pd.to_datetime(df["date"], errors="coerce")
    df=df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
except: pass

st.subheader(f"총 {len(df)}건 · 마지막 업데이트 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.dataframe(df, use_container_width=True, hide_index=True)

# 카드
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
st.subheader("카드 보기")
def days_new(iso: str, days=7):
    try: d=dtparse.parse(iso); d = d if d.tzinfo else d.replace(tzinfo=timezone.utc);  return (datetime.now(timezone.utc)-d)<=timedelta(days=days)
    except: return False

for d in data:
    is_new=days_new(d["dateIso"], 7)
    kw=extract_keywords(d["title"]+" "+d["summary"])
    cat_class={"화학물질규제":"cat-chem","무역정책":"cat-trade","산업정책":"cat-ind","환경규제":"cat-env"}.get(d["category"],"")
    status="EFFECTIVE" if d["impact"]=="High" else ("DRAFT" if d["impact"]=="Medium" else "ANNOUNCED")
    status_class={"ANNOUNCED":"badge status-ann","DRAFT":"badge status-draft","EFFECTIVE":"badge status-eff"}[status]

    st.markdown(f"<div class='card {'new' if is_new else ''}'>", unsafe_allow_html=True)
    st.markdown(f"<span class='badge {cat_class}'>{d['category']}</span> <span class='badge kor'>{d.get('country','')}</span> <span class='{status_class}'>{status}</span>", unsafe_allow_html=True)
    st.markdown(f"<h4>{d['title']}</h4>", unsafe_allow_html=True)
    st.write(d.get("summary",""))
    st.markdown(f"<div class='meta'><b>기관</b>·{d['sourceName']} | <b>날짜</b>·{d['dateIso']}</div>", unsafe_allow_html=True)
    if kw: st.markdown(" ".join([f"<span class='keyword'>{k}</span>" for k in kw]), unsafe_allow_html=True)
    st.markdown(f"[원문 보기]({d['url']})")
    st.markdown("</div>", unsafe_allow_html=True)

# 보고서/다운로드
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
st.subheader("보고서 생성")
md = ( "# 규제/뉴스 업데이트 보고서\n\n"
       f"- 생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
       f"- 기준: 최근 {since_days}일\n"
       f"- 총 항목: {len(df)}건\n\n" )
for _,r in df.iterrows():
    md += f"- **[{r['title']}]({r['url']})** — {r['agency']} · {r['country']} · {r['category']} · {r['date']}\n"
st.download_button("📄 Markdown 보고서 다운로드", data=md.encode("utf-8"),
                   file_name=f"regwatch_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md", mime="text/markdown")
st.download_button("🧾 CSV 다운로드", data=df.to_csv(index=False).encode("utf-8-sig"),
                   file_name=f"regwatch_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv")

# 디버그(기본 숨김)
if show_debug:
    with st.expander("디버그: 원시 데이터 / 로그 보기", expanded=False):
        st.json(items, expanded=False)
        if st.session_state.logs:
            st.markdown("<small class='mono'>"+"<br/>".join(st.session_state.logs)+"</small>", unsafe_allow_html=True)


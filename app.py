# -*- coding: utf-8 -*-
"""
RegWatch – 정책/규제/공지 모니터링 (PDF 요약 지원)
- 신규 소스: Bundesregierung Aktuelles, CBP Bulletin(정기 PDF), MOTIE 게시판(페이지 내 PDF 탐지), DOE Newsroom
- 기존 소스 유지: EEA Analysis, CBP RSS, BMUV RSS, MOTIE
- PDF 자동 인지(.pdf URL 또는 페이지 안에 있는 PDF 링크) → 다운로드 → 텍스트 추출 → 간이요약
- 정책/규제 필터 + 본문 최소길이 + 키워드 포함 필터
- 디자인: Pretendard, #0f2e69 / #dc8d32
"""

import os, re, io, json, hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict
import requests, feedparser, pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import streamlit as st

# PDF 라이브러리 (pypdf 우선, 실패 시 pdfminer.six 폴백)
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

# ----------------------- 기본 설정/스타일 -----------------------
st.set_page_config(page_title="RegWatch – 글로벌 규제 모니터링", layout="wide")
BRAND = "#0f2e69"; ACCENT = "#dc8d32"; BG = "#f6f8fb"

st.markdown(f"""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.8/dist/web/static/pretendard.css');
:root {{ --brand:{BRAND}; --accent:{ACCENT}; --muted:#64748b; }}
html, body, [class*="css"] {{ font-family:'Pretendard',-apple-system,BlinkMacSystemFont,system-ui,Roboto,sans-serif; }}
.main {{ background:linear-gradient(180deg,{BG} 0%, #fff 260px); }}
.hero {{ background:linear-gradient(135deg,var(--brand) 0%, #1a4b8c 100%); color:#fff; padding:20px 22px; border-radius:14px; margin:4px 0 14px; box-shadow:0 6px 20px rgba(0,0,0,.12) }}
.hero .title {{ font-weight:900; font-size:24px; margin:0 }}
.hero .subtitle {{ opacity:.9; margin-top:4px }}
.badge {{ display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:800; background:#fff; color:var(--brand); margin-left:8px }}
.pill {{ display:inline-block; padding:3px 10px; border-radius:10px; font-size:11px; font-weight:700; margin-right:6px; border:1px solid #e2e8f0; background:#fff }}
.card {{ border:1px solid #e2e8f0; border-left:5px solid transparent; border-radius:14px; padding:16px 16px 12px; margin:12px 0; background:#fff; transition:.15s; box-shadow:0 3px 12px rgba(0,0,0,.04) }}
.card:hover {{ transform:translateY(-2px); box-shadow:0 10px 28px rgba(0,0,0,.08) }}
.card.new {{ border-left-color:var(--accent) }}
.card h4 {{ color:var(--brand); margin:0 0 8px; font-size:18px; line-height:1.35 }}
.meta {{ color:var(--muted); font-size:12px; margin:8px 0 6px }}
.keyword {{ display:inline-block; border:1px solid #e2e8f0; font-size:11px; padding:2px 8px; border-radius:8px; margin-right:6px; margin-top:6px }}
.kpi {{ background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:14px; text-align:center }}
.kpi .num {{ font-weight:900; font-size:22px; color:var(--brand) }}
.kpi .lab {{ color:var(--muted); font-size:12px; margin-top:6px }}
hr.sep {{ border:none; border-top:1px solid #e2e8f0; margin:16px 0 }}
small.mono {{ font-family:ui-monospace,Menlo,Consolas,"Courier New",monospace }}
</style>
""", unsafe_allow_html=True)

# ----------------------- 데이터 소스 -----------------------
USER_AGENT = "Mozilla/5.0 (compatible; RegWatch/1.6; +https://streamlit.io)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
TIMEOUT = 25
MAX_PER_SOURCE = 60

SOURCES = [
    # 기존
    {"id":"eea",  "name":"EEA (European Environment Agency) – Analysis", "type":"html", "url":"https://www.eea.europa.eu/en/analysis"},
    {"id":"cbp",  "name":"U.S. Customs and Border Protection (CBP) – RSS", "type":"rss-multi",
     "urls":["https://www.cbp.gov/rss/trade","https://www.cbp.gov/rss/press-releases"]},
    {"id":"bmuv", "name":"BMUV (독일 환경부) – Meldungen RSS", "type":"rss", "url":"https://www.bundesumweltministerium.de/meldungen.rss"},
    {"id":"motie","name":"MOTIE (산업통상자원부)", "type":"html", "url":"https://www.motie.go.kr/"},
    # 요청 주신 신규
    {"id":"bund","name":"Bundesregierung – Aktuelles", "type":"html", "url":"https://www.bundesregierung.de/breg-de/aktuelles"},
    {"id":"cbp_bull","name":"CBP – Bulletin & Decisions (PDF)", "type":"html", "url":"https://www.cbp.gov/trade/rulings/bulletin-decisions"},
    {"id":"doe","name":"U.S. Department of Energy – Newsroom", "type":"html", "url":"https://www.energy.gov/newsroom"},
]

# 정책/규제 힌트
policy_terms_en = ["regulation","regulatory","law","act","bill","directive","ordinance","decree","guidance","notice","enforcement","compliance","consultation","draft","proposal","tariff","duty","quota","import","export","sanction","ban","restriction","rulemaking"]
policy_terms_ko = ["법","법률","법령","시행령","시행규칙","고시","훈령","지침","예규","입법예고","행정예고","규정","개정","제정","시행","공고","의견수렴","초안","규제"]
policy_terms_de = ["verordnung","gesetz","richtlinie","bekanntmachung","entwurf","änderung","umsetzung","verbot","durchführung","leitlinie","gesetzgebung"]
POLICY_TERMS = [t.lower() for t in (policy_terms_en+policy_terms_ko+policy_terms_de)]
PATH_HINTS = ["/law","/directive","/notice","/meldungen","/newsroom","/press","/regulations","/guidance","/en/analysis","/breg-de/aktuelles","/trade/rulings"]

# 로깅
if "logs" not in st.session_state: st.session_state.logs=[]
def log(msg): st.session_state.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def clear_logs(): st.session_state.logs=[]

# 유틸
def md5_hex(s:str)->str: return hashlib.md5(s.encode("utf-8")).hexdigest()
def clean_text(s:str)->str: return re.sub(r"\s+"," ", (s or "")).strip()
def to_iso(s:str)->str:
    if not s: return datetime.now(timezone.utc).isoformat()
    try:
        d=dtparse.parse(s); d=d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()
def title_from_url(url:str)->str:
    from urllib.parse import urlparse, unquote
    p=urlparse(url); segs=[unquote(s) for s in p.path.split("/") if s]
    if not segs: return p.netloc
    segs=segs[-3:]; return " / ".join(s.replace("-"," ").title() for s in segs)
def split_sentences(text:str):
    t=clean_text(text)
    try: parts=re.split(r'(?:(?<=\.)|(?<=!)|(?<=\?)|(?<=다\.)|(?<=요\.))\s+', t)
    except re.error: parts=re.split(r'[.!?]\s+', t)
    return [p for p in parts if p]
def simple_summary(text:str, max_sentences=3, max_len=480)->str:
    sents=split_sentences(text); out=" ".join(sents[:max_sentences]) if sents else clean_text(text)
    return out[:max_len]
def extract_keywords(text:str, topn=6):
    stop=set(["the","and","for","with","from","that","this","are","was","were","will","have","has","been","on","of","in","to","a","an","by","및","과","에","의","으로","대한","관련"])
    words=re.sub(r"[^a-z0-9가-힣 ]"," ", (text or "").lower()).split()
    freq={}
    for w in words:
        if len(w)<=2 or w in stop: continue
        freq[w]=freq.get(w,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda x:x[1], reverse=True)[:topn]]
def guess_category(title:str)->str:
    t=(title or "").lower()
    if re.search(r"(reach|clp|pfas|biocide|chemical|substance|restriction|authorisation)", t): return "화학물질규제"
    if re.search(r"(tariff|duty|quota|fee|rate|import|export|cbp|customs|ruling|bulletin)", t): return "무역정책"
    if re.search(r"(environment|climate|emission|umwelt|환경|energy|전력|에너지)", t): return "환경규제"
    if re.search(r"(policy|industry|manufactur|산업|전략|투자)", t): return "산업정책"
    return "산업정책"
def guess_impact(text:str)->str:
    t=(text or "").lower()
    if re.search(r"(effective|mandatory|ban|prohibit|enforce|in force|penalty|시행|발효)", t): return "High"
    if re.search(r"(proposal|draft|consultation|comment|plan|roadmap|초안|의견수렴)", t): return "Medium"
    return "Low"
def country_of(src:str)->str: return {"cbp":"미국","cbp_bull":"미국","bmuv":"독일","motie":"대한민국","eea":"EU","bund":"독일","doe":"미국"}.get(src,"")
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
def is_policy_like_text(text:str)->bool:
    t=(text or "").lower()
    return any(k in t for k in POLICY_TERMS)
def has_policy_signal(item:dict)->bool:
    title=item.get("title",""); summary=item.get("summary",""); url=(item.get("url","") or "").lower()
    if is_policy_like_text(title) or is_policy_like_text(summary): return True
    return any(h in url for h in PATH_HINTS)

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

# ----------------------- PDF 처리 -----------------------
def is_pdf_url(url:str)->bool:
    base=url.split("?")[0].lower()
    return base.endswith(".pdf")

def pdf_bytes_to_text(data:bytes, max_pages:int=12, max_chars:int=40000)->str:
    # 1) pypdf 시도
    if HAVE_PYPDF:
        try:
            reader = PdfReader(io.BytesIO(data))
            parts=[]
            for i,page in enumerate(reader.pages[:max_pages]):
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    break
            txt=clean_text(" ".join(parts))
            if len(txt)>80:  # 어느정도 텍스트가 나오면 채택
                return txt[:max_chars]
        except Exception:
            pass
    # 2) pdfminer 폴백
    if HAVE_PDFMINER:
        try:
            txt = pdfminer_extract_text(io.BytesIO(data))  # maxpages 옵션 미제공, 대신 잘라줌
            return clean_text(txt)[:max_chars]
        except Exception:
            pass
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
            if not href.startswith("http"):
                # 상대경로 보정
                from urllib.parse import urljoin
                href=urljoin(base_url, href)
            return href
    return None

# ----------------------- 상세 페이지 본문/요약 -----------------------
def extract_page_text(html:str)->str:
    soup=BeautifulSoup(html,"html.parser")
    meta = soup.find("meta", attrs={"name":"description"}) or soup.find("meta", attrs={"property":"og:description"})
    if meta and meta.get("content"): return clean_text(meta["content"])
    candidates=[]
    for sel in ["article","main","[role=main]",".content",".article",".post",".text",".richtext",".body-content",".usa-prose"]:
        for tag in soup.select(sel):
            txt=tag.get_text(" ", strip=True)
            if txt and len(txt)>120: candidates.append(txt)
    if candidates: return max(candidates, key=len)
    ps=soup.find_all("p")
    return clean_text(" ".join(p.get_text(" ", strip=True) for p in ps[:6]))

def scrape_detail_summary(url:str)->Dict[str,str]:
    out={"body":"","date":""}
    try:
        if is_pdf_url(url):
            out["body"]=fetch_pdf_text(url); return out
        html=fetch_with_fallback(url)
        body=extract_page_text(html); out["body"]=clean_text(body)
        # 날짜 추출
        soup=BeautifulSoup(html,"html.parser")
        dt_tag = soup.find("time") or soup.find("meta", attrs={"property":"article:published_time"}) or soup.find("meta", attrs={"name":"date"})
        if dt_tag:
            out["date"] = (dt_tag.get("datetime") or dt_tag.get("content") or dt_tag.get_text(strip=True) or "")
        # 본문이 짧으면 페이지 내 PDF 탐색 → 요약
        if len(out["body"])<160:
            pdf_url=find_pdf_in_html(html, url)
            if pdf_url:
                out["body"]=fetch_pdf_text(pdf_url)
    except Exception as ex:
        log(f"상세 추출 실패: {type(ex).__name__}")
    return out

# ----------------------- 수집기 -----------------------
def fetch_rss_one(source_id,name,feed_url)->List[Dict]:
    out=[]
    try:
        raw=fetch_bytes_with_fallback(feed_url)
        d=feedparser.parse(raw); n=0
        for e in d.entries[:MAX_PER_SOURCE]:
            title=getattr(e,"title",""); link=getattr(e,"link","")
            pub=getattr(e,"published","") or getattr(e,"updated","") or ""
            desc=getattr(e,"summary","") or getattr(e,"description","") or ""
            it=normalize(source_id,name,title,link,pub,simple_summary(f"{title}. {desc}"))
            if it: out.append(it); n+=1
        log(f"RSS OK [{name}] {n}건")
    except Exception as ex:
        log(f"RSS FAIL [{name}] {type(ex).__name__}: {ex}")
    return out

def fetch_rss_multi(source_id,name,urls:List[str])->List[Dict]:
    out=[]
    for u in urls: out+=fetch_rss_one(source_id,name,u)
    uniq={}; [uniq.setdefault(i["url"], i) for i in out]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_bmuv_html_fallback()->List[Dict]:
    items=[]
    try:
        html=fetch_with_fallback("https://www.bundesumweltministerium.de/meldungen")
        soup=BeautifulSoup(html,"html.parser"); add=0
        for a in soup.find_all("a", href=True):
            href=a["href"]; text=a.get_text(strip=True)
            if not href or not text: continue
            if "/meldungen/" not in href: continue
            if not href.startswith("http"): href="https://www.bundesumweltministerium.de"+href
            items.append(normalize("bmuv","BMUV (독일 환경부)",text,href,"",simple_summary(text))); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"BMUV HTML 폴백 {add}건")
    except Exception as ex:
        log(f"BMUV HTML FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_cbp_html_fallback()->List[Dict]:
    items=[]
    for url in ["https://www.cbp.gov/newsroom","https://www.cbp.gov/trade"]:
        try:
            html=fetch_with_fallback(url)
            soup=BeautifulSoup(html,"html.parser")
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=a.get_text(strip=True)
                if not href or not text: continue
                if "/newsroom/" not in href: continue
                if not href.startswith("http"): href="https://www.cbp.gov"+href
                items.append(normalize("cbp","U.S. Customs and Border Protection (CBP) – Newsroom",text,href,"",simple_summary(text)))
                if len(items)>=MAX_PER_SOURCE: break
        except Exception as ex:
            log(f"CBP HTML FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

def fetch_motie_generic()->List[Dict]:
    items=[]
    try:
        html=fetch_with_fallback("https://www.motie.go.kr/")
        soup=BeautifulSoup(html,"html.parser"); add1=0
        for tr in soup.find_all("tr"):
            a=tr.find("a", href=True)
            if not a: continue
            href=a["href"]; title=a.get_text(strip=True)
            if not title: continue
            if not href.startswith("http"): href="https://www.motie.go.kr"+href
            txt=tr.get_text(" ", strip=True)
            m=re.search(r"(\d{4}[.\-]\d{2}[.\-]\d{2})", txt); date=m.group(1).replace(".","-") if m else ""
            items.append(normalize("motie","MOTIE (산업통상자원부)",title,href,date,simple_summary(title))); add1+=1
            if len(items)>=MAX_PER_SOURCE: break
        # 링크 폴백
        if len(items)<8:
            add2=0
            for a in soup.find_all("a", href=True):
                href=a["href"]; text=a.get_text(strip=True)
                if not href or not text: continue
                if not re.search(r"/(bbs|board|news|notice|press)", href, re.I): continue
                if not href.startswith("http"): href="https://www.motie.go.kr"+href
                items.append(normalize("motie","MOTIE (산업통상자원부)",text,href,"",simple_summary(text))); add2+=1
                if len(items)>=MAX_PER_SOURCE: break
            log(f"MOTIE 추가 link:{add2}")
        log(f"MOTIE 추출 tr:{add1}")
    except Exception as ex:
        log(f"MOTIE FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# 신규: Bundesregierung Aktuelles
def fetch_bund_aktuelles()->List[Dict]:
    items=[]
    url="https://www.bundesregierung.de/breg-de/aktuelles"
    try:
        html=fetch_with_fallback(url)
        soup=BeautifulSoup(html,"html.parser"); add=0
        for a in soup.find_all("a", href=True):
            href=a["href"]; text=a.get_text(" ", strip=True)
            if not href or not text: continue
            if "/breg-de/aktuelles" not in href: continue
            if not href.startswith("http"): href="https://www.bundesregierung.de"+href
            items.append(normalize("bund","Bundesregierung – Aktuelles",text,href,"",text)); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"BUND 링크 {add}건")
    except Exception as ex:
        log(f"BUND FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# 신규: CBP Bulletin(페이지 안에 PDF들)
def fetch_cbp_bulletins()->List[Dict]:
    items=[]; url="https://www.cbp.gov/trade/rulings/bulletin-decisions"
    try:
        html=fetch_with_fallback(url)
        soup=BeautifulSoup(html,"html.parser"); add=0
        for a in soup.find_all("a", href=True):
            href=a["href"].strip(); text=a.get_text(" ", strip=True)
            if not href or not text: continue
            if not href.lower().endswith(".pdf"): continue
            if not href.startswith("http"): href="https://www.cbp.gov"+href
            items.append(normalize("cbp_bull","CBP – Bulletin & Decisions (PDF)", text, href, "", text)); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"CBP Bulletin PDF {add}건")
    except Exception as ex:
        log(f"CBP BULLETIN FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# 신규: DOE Newsroom
def fetch_doe_newsroom()->List[Dict]:
    items=[]; url="https://www.energy.gov/newsroom"
    try:
        html=fetch_with_fallback(url)
        soup=BeautifulSoup(html,"html.parser"); add=0
        for a in soup.find_all("a", href=True):
            href=a["href"].strip(); text=a.get_text(" ", strip=True)
            if not href or not text: continue
            if "/newsroom" not in href: continue
            if not href.startswith("http"): href="https://www.energy.gov"+href
            items.append(normalize("doe","U.S. Department of Energy – Newsroom", text, href, "", text)); add+=1
            if len(items)>=MAX_PER_SOURCE: break
        log(f"DOE 링크 {add}건")
    except Exception as ex:
        log(f"DOE FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# 기존: EEA Analysis
def fetch_eea_analysis()->List[Dict]:
    items=[]; url="https://www.eea.europa.eu/en/analysis"
    try:
        html=fetch_with_fallback(url); soup=BeautifulSoup(html,"html.parser")
        for art in soup.find_all("article"):
            a=art.find("a", href=True); 
            if not a: continue
            href=a["href"].strip(); title=a.get_text(" ", strip=True)
            if "/en/analysis" not in href: continue
            if not href.startswith("http"): href="https://www.eea.europa.eu"+href
            items.append(normalize("eea","EEA (European Environment Agency) – Analysis", title, href, "", title))
            if len(items)>=MAX_PER_SOURCE: break
        if len(items)<10:
            for a in soup.find_all("a", href=True):
                href=a["href"].strip(); text=a.get_text(" ", strip=True)
                if "/en/analysis" not in href: continue
                if not href.startswith("http"): href="https://www.eea.europa.eu"+href
                items.append(normalize("eea","EEA (European Environment Agency) – Analysis", text, href, "", text))
                if len(items)>=MAX_PER_SOURCE: break
        log(f"EEA 링크 수집 {len(items)}건")
    except Exception as ex:
        log(f"EEA FAIL {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in items]
    return list(uniq.values())[:MAX_PER_SOURCE]

# ----------------------- 전체 파이프라인 -----------------------
def fetch_all(selected_ids:List[str])->List[Dict]:
    out=[]
    for s in SOURCES:
        if s["id"] not in selected_ids: continue
        try:
            if s["type"]=="rss":
                r=fetch_rss_one(s["id"], s["name"], s["url"])
                out += (r or fetch_bmuv_html_fallback())
            elif s["type"]=="rss-multi":
                r=fetch_rss_multi(s["id"], s["name"], s["urls"])
                out += (r or fetch_cbp_html_fallback())
            elif s["id"]=="motie":
                out += fetch_motie_generic()
            elif s["id"]=="eea":
                out += fetch_eea_analysis()
            elif s["id"]=="bund":
                out += fetch_bund_aktuelles()
            elif s["id"]=="cbp_bull":
                out += fetch_cbp_bulletins()
            elif s["id"]=="doe":
                out += fetch_doe_newsroom()
        except Exception as ex:
            log(f"PIPE FAIL [{s['name']}]: {type(ex).__name__}: {ex}")
    uniq={}; [uniq.setdefault(i["url"], i) for i in out]
    log(f"총 수집 {len(uniq)}건 (후처리 전)")
    return list(uniq.values())

# ----------------------- UI -----------------------
st.markdown(f"""
<div class="hero">
  <div class="title">RegWatch <span class="badge">정책·규제 전용</span></div>
  <div class="subtitle">목록/피드/페이지 내 PDF에서 정책·규제 관련 업데이트만 수집하고 본문(또는 PDF)을 읽어 요약합니다.</div>
</div>
""", unsafe_allow_html=True)

c1,c2,c3,c4 = st.columns([2,1.2,1.2,1.6])
with c1:
    selected=st.multiselect("수집 대상 (기본 전체 선택)", [s["id"] for s in SOURCES],
        default=[s["id"] for s in SOURCES],
        format_func=lambda sid: next(s["name"] for s in SOURCES if s["id"]==sid))
with c2:
    since_days=st.slider("최근 N일", 3, 90, 14)
with c3:
    min_body_chars=st.slider("본문 최소 길이", 80, 1000, 220, step=20, help="추출한 본문이 이 길이 미만이면 제외")
with c4:
    user_terms=st.text_input("추가 포함 키워드(쉼표로 구분)", value="PFAS, REACH, CLP, draft, 개정")

a,b,d = st.columns([1,1,2])
with a:
    do=st.button("업데이트 실행", use_container_width=True)
with b:
    if st.button("캐시 초기화", use_container_width=True):
        st.cache_data.clear(); clear_logs(); st.success("HTTP 캐시를 비웠습니다.")
with d:
    show_debug=st.toggle("디버그 모드", value=False)

st.markdown("<hr class='sep'/>", unsafe_allow_html=True)

# 실행
if do or "items_raw" not in st.session_state:
    clear_logs()
    with st.spinner("수집 중..."):
        st.session_state.items_raw=fetch_all(selected or [s["id"] for s in SOURCES])

items=st.session_state.get("items_raw", [])

# 기간 필터
cut=datetime.now(timezone.utc)-timedelta(days=since_days)
def in_range(iso):
    try: return dtparse.parse(iso)>=cut
    except: return True
items_recent=[d for d in items if in_range(d["dateIso"])]

# 상세페이지/ PDF 요약 + 정책 필터
def matches_user_terms(item):
    terms=[t.strip().lower() for t in user_terms.split(",") if t.strip()]
    if not terms: return True
    s=(item.get("title","")+" "+item.get("summary","")).lower()
    return any(t in s for t in terms)

processed=[]
with st.spinner("상세 페이지 분석 · (필요시 PDF 요약) 중..."):
    for it in items_recent:
        detail=scrape_detail_summary(it["url"])
        body=detail.get("body","")
        if detail.get("date"): it["dateIso"]=to_iso(detail["date"])
        if len(body)<min_body_chars: continue
        it["summary"]=simple_summary(body,3,480)
        if not has_policy_signal(it): continue
        if not matches_user_terms(it): continue
        processed.append(it)

# KPI
k1,k2,k3,k4 = st.columns(4)
k1.markdown(f"<div class='kpi'><div class='num'>{len(items)}</div><div class='lab'>수집(원본)</div></div>", unsafe_allow_html=True)
k2.markdown(f"<div class='kpi'><div class='num'>{len(items_recent)}</div><div class='lab'>최근 {since_days}일</div></div>", unsafe_allow_html=True)
k3.markdown(f"<div class='kpi'><div class='num'>{len(processed)}</div><div class='lab'>정책/본문 필터 후</div></div>", unsafe_allow_html=True)
k4.markdown(f"<div class='kpi'><div class='num'>{datetime.now().strftime('%Y-%m-%d %H:%M')}</div><div class='lab'>최종 업데이트</div></div>", unsafe_allow_html=True)

# 검색/카테고리
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
f1,f2 = st.columns([2,1])
with f1: q=st.text_input("검색(제목/요약/기관/국가)")
with f2: cat=st.selectbox("카테고리", ["전체","화학물질규제","무역정책","산업정책","환경규제"], index=0)

data=processed
if q:
    ql=q.lower()
    data=[d for d in data if ql in (d["title"]+" "+d["summary"]+" "+d["sourceName"]+" "+d.get("country","")).lower()]
if cat!="전체":
    data=[d for d in data if d["category"]==cat]

# 표 & 다운로드
import io as _io
def to_csv_bytes(df:pd.DataFrame)->bytes:
    buf=_io.StringIO(); df.to_csv(buf, index=False); return buf.getvalue().encode("utf-8-sig")

df=pd.DataFrame([{
    "date":d["dateIso"], "title":d["title"], "agency":d["sourceName"], "country":d.get("country",""),
    "category":d["category"], "impact":d["impact"], "url":d["url"]
} for d in data])
try:
    df["date_dt"]=pd.to_datetime(df["date"], errors="coerce")
    df=df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
except: pass

st.subheader(f"목록 · 총 {len(df)}건")
st.dataframe(df, use_container_width=True, hide_index=True)
st.download_button("🧾 CSV 다운로드", data=to_csv_bytes(df),
                   file_name=f"regwatch_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv")

# 카드
st.markdown("<hr class='sep'/>", unsafe_allow_html=True)
st.subheader("카드 보기")
def extract_top_keywords(items:List[Dict], topn=10):
    pool=" ".join([(i.get("title","")+" "+i.get("summary","")) for i in items])
    return extract_keywords(pool, topn)
topk=extract_top_keywords(data,10)
if topk:
    st.caption("Top 키워드")
    st.markdown(" ".join([f"<span class='pill'>{k}</span>" for k in topk]), unsafe_allow_html=True)

def days_new(iso:str, days=7):
    try:
        d=dtparse.parse(iso); d=d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc)-d)<=timedelta(days=days)
    except: return False

for d in data:
    is_new=days_new(d["dateIso"],7)
    kw=extract_keywords(d["title"]+" "+d["summary"])
    st.markdown(f"<div class='card {'new' if is_new else ''}'>", unsafe_allow_html=True)
    st.markdown(f"<h4>{d['title']}</h4>", unsafe_allow_html=True)
    st.markdown(f"<div class='meta'>기관·{d['sourceName']} | 국가·{d.get('country','')} | 분류·<span class='pill'>{d['category']}</span> | 날짜·{d['dateIso']}</div>", unsafe_allow_html=True)
    st.write(d.get("summary",""))
    if kw:
        st.markdown(" ".join([f"<span class='keyword'>{k}</span>" for k in kw]), unsafe_allow_html=True)
    st.markdown(f"[원문/파일 열기]({d['url']})")
    st.markdown("</div>", unsafe_allow_html=True)

# 디버그
if show_debug:
    with st.expander("디버그: 원시 데이터 / 로그", expanded=False):
        st.json(st.session_state.get("items_raw", []), expanded=False)
        if st.session_state.logs:
            st.markdown("<small class='mono'>"+"<br/>".join(st.session_state.logs)+"</small>", unsafe_allow_html=True)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SAMCO Construction Opportunity Watcher — real, live site watcher.

Every number shown in dashboard.html comes from an actual HTTP check performed by
this script. Nothing is invented: if a source cannot be read it is reported as
failed/limited with the real error, and it is never counted as watched.

Usage
  python Watcher.py --once                 one real check cycle, then write dashboard
  python Watcher.py --loop --interval 60   keep checking every N minutes
  python Watcher.py --once --render        include headless-Chrome pass for JS-heavy sites

Outputs
  dashboard.html      live dashboard (from template.html, filled with real data)
  tenders.json        real tender/authority records extracted this run
  state/state.json    per-source memory (hashes, deltas) + run history
  logs/watcher.log    append-only run log
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # see fetch(): insecure retry is recorded per-source, not logged

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
TEMPLATE_FILE = ROOT / "template.html"
DASHBOARD_FILE = ROOT / "dashboard.html"
TENDERS_FILE = ROOT / "tenders.json"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = ROOT / "logs" / "watcher.log"
WEB_DIR = ROOT / "vercel"
WEB_DATA_FILE = WEB_DIR / "data" / "latest.json"
WEB_INDEX_FILE = WEB_DIR / "index.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 SAMCO-OpportunityWatcher/2.0")
HEADERS = {"User-Agent": UA, "Accept-Language": "en,ar;q=0.8,fr;q=0.6",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
TIMEOUT = 30
WORKERS = 8
SCORING_NOTE = ("attention = deadline proximity (50/40/30/20/10 for <=3/<=7/<=14/<=30/later days) "
                "+ source verification (Official 20, Direct corporate/International 15, other 5) "
                "+ novelty (new record +20, changed on source +10), capped at 100. "
                "All inputs are real fields from the source response.")

MONTHS = {"يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5, "يونيو": 6,
          "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10,
          "نوفمبر": 11, "ديسمبر": 12,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
          "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def norm_text(s):
    return re.sub(r"\s+", " ", s or "").strip()


def h12(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def ar_norm(s):
    return (s or "").replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")


def is_junk_row(text):
    """True for table rows that are portal chrome, not content: dropdown option
    lists, bare column labels, pagination sequences."""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith((":::", "---", "===", "***")):
        return True
    if not re.search(r"[A-Za-z\u0600-\u06FF]", t):
        return True
    if len(t) <= 40 and t.endswith((":", "\uff1a")):
        return True
    return False


# ------------------------------------------------------- work-type classifier
WORK_TYPE_NOTE = ("Work type is keyword-estimated from each record's real title/category text, "
                  "not from any invented field (no source publishes a contract value). Rules in order: "
                  "maintenance/repair words -> Small project, unless a construction verb is present; "
                  "construction verbs (construction / EPC / design-build / انشاء) -> Mega project; "
                  "supply/delivery/purchase words (supply, delivery, توريد، شراء، تأمين) -> Procurement; "
                  "named major infrastructure (desalination, treatment/power plant, network, pipeline, "
                  "bridge, highway, roads, airport, محطة معالجة، شبكات، الطرق، جسر) -> Mega project; "
                  "anything else -> Other works.")

WT_SMALL = re.compile(
    r"(maintenance|repair|renovat|rehabilitat|refurbish|remodel|"
    r"cleaning works|paint(?:ing)? works|fencing works|resurfac|upgrad|"
    r"صيانة|إصلاح|اصلاح|ترميم|تأهيل|تاهيل|تجديد|تنظيف|دهان)", re.I)

WT_MEGA_VERB = re.compile(
    r"(construction|design\s*(?:and|&|-)?\s*(?:build|construct)|build\s*own\s*operate|\bepc\b|"
    r"إنشاء|انشاء|تشييد|تصميم وبناء|تصميم وتنفيذ وتشغيل)", re.I)

WT_MEGA_NOUN = re.compile(
    r"(desalination|(?:water|sewage|treatment|power)\s*(?:treatment\s*)?plant|pumping station|substation|"
    r"transmission line|interchange|expressway|highway|motorway|bridge|tunnel|airport|harbou?r|"
    r"refinery|pipelines?|\bnetworks?\b|infrastructure|metro\b|railway|flood|drainage|reservoir|"
    r"\bdam\b|\broads?\b|intersections?|محطة معالجة|محطة تحلية|محطة كهرباء|محطة ضخ|شبكة|شبكات|جسر|أنفاق|انفاق|"
    r"الطرق|الطريق|طرق سريع|تقاطعات|مطار|ميناء|مصفاة|خطوط أنابيب|خطوط انابيب|البنية التحتية|بنية تحتية)", re.I)

WT_PROC = re.compile(
    r"(supply|supplying|delivery|purchase|procurement|provision of|"
    r"توريد|شراء|تأمين|تامين)", re.I)


def classify_work_type(title, category=""):
    """Transparent keyword heuristic over the record's real title/category text."""
    t = norm_text(ar_norm(" ".join([title or "", category or ""])))
    if not t:
        return "Other works"
    small, mverb = WT_SMALL.search(t), WT_MEGA_VERB.search(t)
    if small and not mverb:
        return "Small project"
    if mverb:
        return "Mega project"
    if WT_PROC.search(t):
        return "Procurement"
    if WT_MEGA_NOUN.search(t):
        return "Mega project"
    return "Other works"


def parse_date(s):
    """Real date strings from the sources -> ISO date, else None."""
    s = norm_text(s)
    if not s:
        return None
    m = re.search(r"(?<![\d/])(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?![\d/])", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+([^\s,]+),?\s+(\d{4})", s)  # 1 أكتوبر, 2026
    if m:
        d, mon, y = int(m.group(1)), ar_norm(m.group(2)), int(m.group(3))
        mo = MONTHS.get(mon) or MONTHS.get(m.group(2))
        if mo:
            try:
                return datetime(y, mo, d).date().isoformat()
            except ValueError:
                return None
    m = re.search(r"([^\s,\d]+),?\s+(\d{1,2}),?\s+(\d{4})", s)  # أكتوبر 1, 2026 / October 1, 2026
    if m:
        mon = MONTHS.get(ar_norm(m.group(1))) or MONTHS.get(m.group(1).lower())
        if mon:
            d, y = int(m.group(2)), int(m.group(3))
            try:
                return datetime(y, mon, d).date().isoformat()
            except ValueError:
                return None
    m = re.search(r"(?<![\d/])(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?![\d/])", s)  # 27/09/2026
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    m = re.search(r"(?<![\d-])(\d{1,2})-([A-Za-z]{3})-(\d{2,4})(?![\d])", s)  # 28-APR-26
    if m:
        d, mon, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = MONTHS.get(mon)
        if mo:
            if y < 100:
                y += 2000
            try:
                return datetime(y, mo, d).date().isoformat()
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- robots + fetch
_robots = {}


def robots_ok(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    if host in _robots:
        return _robots[host]
    verdict = True
    try:
        r = requests.get("%s://%s/robots.txt" % (parts.scheme, host), headers=HEADERS, timeout=10)
        if r.status_code == 200 and "user-agent" in r.text.lower():
            import urllib.robotparser as rp
            parser = rp.RobotFileParser()
            parser.parse(r.text.splitlines())
            verdict = parser.can_fetch(UA, url)
    except Exception:
        verdict = True  # unreachable robots.txt: fail-open, and that is visible in the log
    _robots[host] = verdict
    return verdict


def fetch(url, timeout=TIMEOUT):
    """One real HTTP GET. Returns a dict with honest outcome, never raises."""
    out = {"ok": False, "status": None, "bytes": None, "elapsed_ms": None, "text": "",
           "final_url": url, "error": None, "error_kind": None, "tls_insecure": False,
           "attempts": []}
    for insecure in (False, True):
        t0 = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout,
                             allow_redirects=True, verify=not insecure)
            out.update(ok=r.status_code < 400, status=r.status_code, bytes=len(r.content),
                       elapsed_ms=int((time.time() - t0) * 1000), text=r.text,
                       final_url=str(r.url), tls_insecure=insecure)
            if "login" in str(r.url).lower() and "login" not in url.lower():
                out["error"] = "redirected to a login page (%s)" % str(r.url)[:120]
                out["error_kind"] = "redirected_to_login"
                out["ok"] = False
            if not out["ok"] and not out["error"]:
                out["error"] = "HTTP %s" % r.status_code
                out["error_kind"] = "http_error"
            out["attempts"].append({"url": url, "status": r.status_code, "insecure": insecure})
            return out
        except requests.exceptions.SSLError as e:
            if insecure:
                out.update(error="TLS certificate error (%s)" % type(e).__name__, error_kind="tls_error")
            else:
                out["attempts"].append({"url": url, "status": None, "insecure": False, "error": "SSLError"})
                continue  # retry once without verification, recorded honestly
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as e:
            out.update(error="timeout after %ss (%s)" % (timeout, type(e).__name__), error_kind="timeout")
            out["attempts"].append({"url": url, "status": None, "error": type(e).__name__})
            return out
        except requests.exceptions.ConnectionError as e:
            kind = "dns_error" if "NameResolution" in str(e) or "getaddrinfo" in str(e) else "conn_error"
            out.update(error="%s: %s" % (kind, str(e)[:140]), error_kind=kind)
            out["attempts"].append({"url": url, "status": None, "error": kind})
            return out
        except Exception as e:
            out.update(error="%s: %s" % (type(e).__name__, str(e)[:140]), error_kind="conn_error")
            out["attempts"].append({"url": url, "status": None, "error": type(e).__name__})
            return out
    return out


def page_title(html):
    try:
        t = BeautifulSoup(html, "lxml").title
        return norm_text(t.get_text() if t else "")[:120]
    except Exception:
        return ""


def abs_url(base, href):
    return urllib.parse.urljoin(base, href or "")


# ---------------------------------------------------------------- extractors
def tender_record(**kw):
    rec = {"id": None, "country": "", "region": "", "city": "", "sector": "", "work_type": "", "category": "",
           "title": "", "issuer": "", "reference": "", "deadline": None, "published": None,
           "remaining_days": None, "status": "", "urgency": "", "attention_score": 0,
           "verification_status": "Official", "source_type": "Official / Direct",
           "source_name": "", "source_url": "", "how_found": "", "evidence_http": None,
           "evidence_rows": 0, "extracted_at": now_iso(), "is_new": False, "is_updated": False,
           "first_seen": now_iso(), "last_seen": now_iso(), "next_action": ""}
    rec.update(kw)
    if not rec.get("work_type"):
        rec["work_type"] = classify_work_type(rec.get("title", ""), rec.get("category", ""))
    return rec


def ex_etimad(src):
    api = src["api_url"]
    h = dict(HEADERS)
    h.update({"X-Requested-With": "XMLHttpRequest",
              "Referer": "https://tenders.etimad.sa/Tender/AllTendersForVisitor",
              "Accept": "application/json, text/javascript, */*; q=0.01"})
    records, rows, errs, last = [], 0, [], None
    for page in (1, 2):
        params = {"PageNumber": page, "PageSize": 50, "PublishDateId": "", "Sort": "",
                  "SortDirection": "asc", "TenderActivityId": "", "TenderAreasIdString": "",
                  "TenderCategory": "", "TenderSubActivityId": "", "TenderTypeId": "",
                  "agency": "", "ConditionaBookletRange": ""}
        try:
            t0 = time.time()
            r = requests.get(api, params=params, headers=h, timeout=TIMEOUT)
            last = {"ok": r.status_code == 200, "status": r.status_code, "bytes": len(r.content),
                    "elapsed_ms": int((time.time() - t0) * 1000), "text": r.text[:400],
                    "final_url": api, "error": None if r.status_code == 200 else "HTTP %s" % r.status_code,
                    "error_kind": None if r.status_code == 200 else "http_error",
                    "tls_insecure": False, "attempts": [{"url": api, "status": r.status_code}]}
            data = r.json().get("data") or []
        except Exception as e:
            errs.append("%s: %s" % (type(e).__name__, str(e)[:100]))
            break
        for it in data:
            deadline = norm_text(str(it.get("lastOfferPresentationDate") or ""))[:10] or None
            records.append(tender_record(
                id="SA-ETM-%s" % it.get("tenderId"),
                country="Saudi Arabia", city=norm_text(it.get("branchName") or ""),
                category=norm_text(it.get("tenderTypeName") or ""),
                title=norm_text(it.get("tenderName") or ""),
                issuer=norm_text(it.get("agencyName") or ""),
                reference=str(it.get("referenceNumber") or it.get("tenderNumber") or ""),
                deadline=deadline, published=norm_text(str(it.get("submitionDate") or ""))[:10] or None,
                remaining_days=(it.get("remainingDays") if deadline else None),
                source_id=src["id"], evidence_http=200, evidence_rows=len(data),
                source_name=src["source_name"], source_url=(
                    "https://tenders.etimad.sa/Tender/Details?STenderId="
                    + urllib.parse.quote(str(it.get("tenderIdString") or ""))),
                how_found="etimad_api_json", verification_status="Official",
                source_type="Official portal API"))
        rows += len(data)
    if errs and last is None:
        return None, 0, {"error": "; ".join(errs), "error_kind": "conn_error"}
    notes = " / ".join(errs)
    if notes and last is not None:
        last["error"] = notes
    return records, rows, last


COL_TITLE = re.compile(r"(عنوان|اسم العطاء|الوصف|بيان|موضوع|description|title|tender name|project|subject)", re.I)
COL_REF = re.compile(r"(رقم العطاء|رقم المناقصة|رقم الممارسة|الرقم|tender (number|no)|reference|ref\.?\s*(no)?$|\bref\b)", re.I)
COL_DEADLINE = re.compile(r"(تاريخ (الايداع|الإيداع|الاقفال|الإقفال|الاغلاق|الإغلاق|التسليم)|الموعد النهائي|آخر موعد|اخر موعد|"
                          r"bid submission|closing date|deadline|submission date|due date|last date)", re.I)
COL_DEADLINE_STRONG = re.compile(r"(الايداع|الإيداع|الاقفال|الإقفال|الاغلاق|الإغلاق|النهائي|submission|closing|deadline|due date)", re.I)
COL_DEADLINE_WEAK = re.compile(r"(last date|آخر موعد|اخر موعد)", re.I)
COL_DEADLINE_ANTI = re.compile(r"(purchase|شراء)", re.I)
COL_PUBLISHED = re.compile(r"(تاريخ (الطرح|الإصدار|الاصدار|النشر|الاعلان|الإعلان)|floated|publish|issued|announcement|start date|opening date)", re.I)
COL_IRRELEVANT = re.compile(r"(النوع|الحالة|الفئة|المرفقات|type|status|category|fee|eligibility|attachments)", re.I)


def _col_map(header_cells):
    m = {}
    dl = []
    for i, t in enumerate(header_cells):
        if i not in m and COL_TITLE.search(t) and not COL_IRRELEVANT.search(t):
            m["title"] = i
        if i not in m and COL_REF.search(t) and not COL_DEADLINE.search(t):
            m["ref"] = i
        if COL_DEADLINE.search(t):
            score = 0
            if COL_DEADLINE_STRONG.search(t):
                score += 3
            if COL_DEADLINE_WEAK.search(t):
                score += 1
            if COL_DEADLINE_ANTI.search(t):
                score -= 2
            dl.append((score, i))
        if i not in m and COL_PUBLISHED.search(t):
            m["published"] = i
    if dl:
        m["deadline"] = max(dl)[1]
    return m


def ex_table_generic(src, html, base_url, link_text=None):
    """Row reading for real government tables (GTD, NOC, Ashghal, NWS style).
    Column meaning is taken from the source table's own header row; nothing is assumed."""
    soup = BeautifulSoup(html, "lxml")
    records, seen = [], set()
    hdr_words = (r"(العنوان|اسم العطاء|الموعد النهائي|رقم العطاء|رقم المناقصة|عنوان المناقصة|"
                 r"تاريخ الإصدار|تاريخ الإغلاق|تاريخ الطرح|تاريخ الإقفال|تاريخ النشر|النوع|الحالة|"
                 r"S\.?\s*No|Tender Number|Description|Eligibility|Tender fee|Status|"
                 r"Floated Date|Last Date|Closing Date|Submission Date|Tender Title|Tender No)")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        cmap, hdr_idx = {}, -1
        for idx, tr in enumerate(rows[:3]):
            cells = [norm_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 3 and sum(1 for t in cells if re.search(hdr_words, t, re.I)) >= 2:
                cmap, hdr_idx = _col_map(cells), idx
                break
        for tr in rows[hdr_idx + 1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [norm_text(c.get_text(" ", strip=True)) for c in cells]
            if sum(1 for t in texts if re.search(hdr_words, t, re.I)) >= 2:
                continue  # repeated header
            if cmap.get("title") is not None and cmap["title"] < len(texts):
                title = texts[cmap["title"]]
            else:
                title = max(texts[1:], key=len) if len(texts) > 1 else texts[0]
            title_clean = re.sub(r"^(العنوان|الموعد النهائي|الوصف)\s*:\s*", "", title).strip()
            if len(title_clean) < 12:
                continue
            if is_junk_row(title_clean):
                continue
            dates, ref = [], ""
            if cmap.get("published") is not None and cmap["published"] < len(texts):
                d = parse_date(texts[cmap["published"]])
                if d:
                    dates.append(d)
            if cmap.get("deadline") is not None and cmap["deadline"] < len(texts):
                d = parse_date(texts[cmap["deadline"]])
                if d:
                    dates.append(d)
            if cmap.get("ref") is not None and cmap["ref"] < len(cells):
                ref = texts[cmap["ref"]][:60]
            if not cmap:
                for t in texts:
                    ts = t.strip()
                    if re.match(r"^\S{1,40}/\d{4}$", ts):
                        ref = ref or ts
                        continue
                    d = parse_date(ts)
                    if d:
                        dates.append(d)
            link = ""
            if link_text:
                for a in tr.find_all("a", href=True):
                    if link_text in norm_text(a.get_text(" ")):
                        link = abs_url(base_url, a["href"])
                        break
            if not link:
                for a in tr.find_all("a", href=True):
                    if a["href"].lower().split("?")[0].endswith(".pdf"):
                        link = abs_url(base_url, a["href"])
                        break
            if not link:
                for a in tr.find_all("a", href=True):
                    if not a["href"].lower().startswith(("#", "javascript")):
                        link = abs_url(base_url, a["href"])
                        break
            key = (title_clean[:60], dates[0] if dates else "")
            if key in seen:
                continue
            seen.add(key)
            published = dates[0] if len(dates) > 1 else None
            deadline = dates[1] if len(dates) > 1 else (dates[0] if dates else None)
            records.append(tender_record(
                id="%s-%s" % (src["id"][:14].upper(), h12(key[0] + (key[1] or ""))),
                country=src["country"], title=title_clean,
                issuer=src.get("client_name") or src["source_name"],
                reference=ref, published=published, deadline=deadline,
                source_name=src["source_name"], source_url=link or base_url,
                how_found=src["extractor"], verification_status=src.get("verification") or "Official",
                source_type=src.get("source_type") or "Official / Direct"))
    return records


def harvest_listing(src, html, base_url):
    """Honest listing harvest for sources without a dedicated extractor: counts real
    rows/links that look like procurement notices. These stay 'listing items' for
    change detection; they are not promoted to tender records and never scored."""
    soup = BeautifulSoup(html, "lxml")
    kw = re.compile(r"(tender|bid|rfp|rfq|prequalif|procure|expression of interest|"
                    r"مناقص|عطاء|عطاأ|منافس|استدراج|تأهيل|تاهيل|اعلان|إعلان|طرح)", re.I)
    items = []
    for a in soup.find_all("a", href=True):
        txt = norm_text(a.get_text(" ", strip=True))
        if len(txt) < 12 or not kw.search(txt):
            continue
        items.append({"text": txt[:160], "url": abs_url(base_url, a["href"])})
    rows = 0
    for t in soup.find_all("table"):
        rows += sum(1 for tr in t.find_all("tr") if len(tr.find_all(["td", "th"])) >= 2)
    return items, rows


# ---------------------------------------------------------------- render pass
def render_pages(urls):
    """Headless-Chrome pass for JS-heavy sources. Returns {url: {html,title}|{_render_error}}."""
    out = {}
    if not urls:
        return out
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception as e:
        return {u: {"_render_error": "selenium unavailable: %s" % e} for u in urls}
    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
              "--window-size=1500,2600", "--blink-settings=imagesEnabled=false",
              "--disable-extensions", "--log-level=3"):
        opts.add_argument(a)
    opts.add_argument("--user-agent=" + UA)
    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(50)
    except Exception as e:
        return {u: {"_render_error": "chrome start failed: %s" % str(e)[:120]} for u in urls}
    for u in urls:
        try:
            driver.get(u)
            time.sleep(4)
            out[u] = {"html": driver.page_source, "title": driver.title}
        except Exception as e:
            out[u] = {"_render_error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    try:
        driver.quit()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- probe one source
def probe_source(src, prev_src_state, render_results, use_render):
    res = {"id": src["id"], "country": src.get("country") or "", "client_name": src.get("client_name") or "",
           "source_name": src.get("source_name") or "", "source_class": src.get("source_class") or "",
           "coverage_status": src.get("coverage_status") or "", "url": src.get("url") or "",
           "listing_url": src.get("listing_url") or src.get("url") or "",
           "verification": src.get("verification") or "", "notes": src.get("notes") or "",
           "http_status": None, "elapsed_ms": None, "bytes": None, "page_title": "",
           "content_hash": None, "rows_found": 0, "error": None, "health_state": "not_checked",
           "watched_last_turn": False, "last_checked": now_iso(), "changed_last_run": False,
           "last_changed_at": (prev_src_state or {}).get("last_changed_at"),
           "tls_insecure": False, "render_used": False, "extractor": src.get("extractor") or "generic"}
    target = src.get("listing_url") or src.get("url")
    if not target:
        res["error"] = "no URL in registry"
        return res, [], []
    if not robots_ok(target):
        res["health_state"] = "blocked_robots"
        res["error"] = "robots.txt disallows this watcher for %s" % urllib.parse.urlsplit(target).netloc
        return res, [], []

    r, records, listing_items = None, [], []
    if src.get("extractor") == "etimad_json" and src.get("api_url"):
        recs, rows, api_res = ex_etimad(src)
        if api_res is None:
            r = {"ok": False, "error": (api_res or {}).get("error") or "api unreachable",
                 "error_kind": "conn_error", "attempts": [], "status": None}
        else:
            r = api_res
            records = recs or []
            res["rows_found"] = rows
            if r.get("error"):
                res["error"] = r["error"]
    if r is None:
        r = fetch(target)
        for fb in (src.get("fallback_urls") or []):
            if r["ok"]:
                break
            r2 = fetch(fb)
            r2["attempts"] = r["attempts"] + r2.get("attempts", [])
            r = r2
        if r.get("ok"):
            html, final = r["text"], r["final_url"]
            res["page_title"] = page_title(html)
            ex = src.get("extractor") or "generic"
            if "gtd" in ex:
                records = ex_table_generic(src, html, final, link_text="التفاصيل")
                res["rows_found"] = len(records)
            elif "table" in ex:
                records = ex_table_generic(src, html, final)
                res["rows_found"] = len(records)
            else:
                listing_items, table_rows = harvest_listing(src, html, final)
                res["rows_found"] = max(len(listing_items), table_rows)
    if r is None:
        r = {"ok": False, "error": "no attempt recorded", "error_kind": "conn_error", "attempts": []}
    res["http_status"] = r.get("status")
    res["elapsed_ms"] = r.get("elapsed_ms")
    res["bytes"] = r.get("bytes")
    res["tls_insecure"] = bool(r.get("tls_insecure"))
    if r.get("error"):
        res["error"] = r["error"]

    # render pass for JS/session-heavy sources when the static read produced no tender records
    if r.get("ok") and src.get("render") and not records:
        if use_render and target in render_results:
            rr = render_results[target]
            res["render_used"] = True
            if rr.get("_render_error"):
                res["error"] = "render failed: %s" % rr["_render_error"]
            else:
                res["page_title"] = res["page_title"] or norm_text(rr.get("title") or "")
                html = rr.get("html") or ""
                res["content_hash"] = h12(norm_text(BeautifulSoup(html, "lxml").get_text(" ")))
                ex = src.get("extractor") or "generic"
                if "gtd" in ex:
                    records = ex_table_generic(src, html, target, link_text="التفاصيل")
                elif "table" in ex:
                    records = ex_table_generic(src, html, target)
                else:
                    listing_items, table_rows = harvest_listing(src, html, target)
                    res["rows_found"] = max(len(listing_items), table_rows)
                # promotion guard: a rendered row is only promoted if it carries at
                # least one real parsed date or a reference number, never a bare label
                records = [t for t in records
                           if t.get("deadline") or t.get("published") or t.get("reference")]
                if records:
                    res["rows_found"] = len(records)
        else:
            res["health_state"] = "js_required"
            res["error"] = ("listing is rendered by JavaScript/session; static read returned no rows. "
                            "Run Watcher.py with --render to include the headless-Chrome pass.")

    if res["content_hash"] is None and r.get("ok") and r.get("text"):
        try:
            res["content_hash"] = h12(norm_text(BeautifulSoup(r["text"], "lxml").get_text(" ")))
        except Exception:
            res["content_hash"] = h12(r["text"])

    if res["health_state"] == "not_checked":
        if not r.get("ok"):
            k = r.get("error_kind") or "conn_error"
            if k in ("timeout", "tls_error", "dns_error", "conn_error"):
                res["health_state"] = k
            elif k == "redirected_to_login":
                res["health_state"] = "redirected_to_login"
            else:
                res["health_state"] = "http_%s" % res["http_status"]
            if res["tls_insecure"]:
                res["health_state"] = "watched_no_rows" if res["rows_found"] else "tls_error"
                res["error"] = (res["error"] or "") + " [retried without TLS verification]"
        elif res["rows_found"] > 0 or records:
            res["health_state"] = "watched_ok"
        else:
            res["health_state"] = "watched_no_rows"
        res["watched_last_turn"] = bool(r.get("ok"))

    prev_hash = (prev_src_state or {}).get("content_hash")
    if prev_hash and res["content_hash"] and prev_hash != res["content_hash"]:
        res["changed_last_run"] = True
        res["last_changed_at"] = now_iso()
    elif res["content_hash"] and not prev_hash:
        res["last_changed_at"] = res["last_changed_at"] or now_iso()
    for t in records:
        t["source_id"] = src["id"]
        t["evidence_http"] = t.get("evidence_http") or res.get("http_status")
        t["evidence_rows"] = res.get("rows_found") or len(records)
    for it in listing_items:
        it["source_id"] = src["id"]
    return res, records, listing_items


# ---------------------------------------------------------------- scoring + assembly
def apply_scoring(t, today, baseline):
    d = t.get("remaining_days")
    if d is None and t.get("deadline"):
        try:
            d = (datetime.fromisoformat(t["deadline"]).date() - today).days
        except Exception:
            d = None
    t["remaining_days"] = d
    s = 0
    if d is not None:
        if d >= 0:
            s += 50 if d <= 3 else 40 if d <= 7 else 30 if d <= 14 else 20 if d <= 30 else 10
        t["status"] = "Open" if d >= 0 else "Closed"
        t["urgency"] = ("Closed" if d < 0 else "Critical" if d <= 3 else "High" if d <= 7
                        else "Medium" if d <= 21 else "Normal")
    else:
        t["status"] = "Open"
        t["urgency"] = "Unknown"
    v = t.get("verification_status") or ""
    s += 20 if v == "Official" else 15 if v in ("Direct corporate", "International") else 5
    if not baseline:
        if t.get("is_new"):
            s += 20
        if t.get("is_updated"):
            s += 10
    t["attention_score"] = max(0, min(100, s))
    if t["status"] == "Closed":
        t["next_action"] = "Deadline passed — archive or monitor for re-tender"
    elif d is not None and d <= 3:
        t["next_action"] = "Deadline imminent — confirm submission/participation now"
    elif t.get("is_new") and not baseline:
        t["next_action"] = "New this run — open source, assess eligibility, register interest"
    elif t.get("is_updated") and not baseline:
        t["next_action"] = "Changed on source — re-open and diff scope/deadline"
    elif d is None:
        t["next_action"] = "No deadline published on source — open and verify directly"
    else:
        t["next_action"] = "Monitor — no change on source this run"
    return t


def run_cycle(args, state):
    started = now_iso()
    t0 = time.time()
    run_no = (state.get("last_run") or {}).get("run_no", 0) + 1
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    src_list = sources["sources"]
    log("run #%d start — probing %d sources (render=%s)" % (run_no, len(src_list), args.render))

    render_results = {}
    if args.render:
        render_urls = [s.get("listing_url") or s.get("url") for s in src_list
                       if s.get("render") and (s.get("listing_url") or s.get("url"))]
        render_urls = [u for u in render_urls if robots_ok(u)]
        if args.render_limit:
            render_urls = render_urls[:args.render_limit]
        log("render pass: %d pages via headless Chrome" % len(render_urls))
        render_results = render_pages(render_urls)

    prev_sources = state.get("sources") or {}
    health, all_records, all_items = [], [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(probe_source, s, prev_sources.get(s["id"]), render_results, args.render): s
                for s in src_list}
        for f in as_completed(futs):
            s = futs[f]
            try:
                res, recs, items = f.result()
            except Exception:
                res = {"id": s["id"], "country": s.get("country") or "", "client_name": s.get("client_name") or "",
                       "source_name": s.get("source_name") or "", "source_class": s.get("source_class") or "",
                       "coverage_status": s.get("coverage_status") or "", "url": s.get("url") or "",
                       "http_status": None, "elapsed_ms": None, "bytes": None, "page_title": "",
                       "content_hash": None, "rows_found": 0, "health_state": "conn_error",
                       "watched_last_turn": False, "last_checked": now_iso(), "changed_last_run": False,
                       "error": traceback.format_exc(limit=1)[:200], "notes": s.get("notes") or ""}
                recs, items = [], []
            health.append(res)
            all_records += recs
            all_items += items

    health.sort(key=lambda h: (h["country"], h["client_name"]))
    ok = sum(1 for h in health if h["watched_last_turn"])
    log("probing done in %.1fs — watched OK %d / %d" % (time.time() - t0, ok, len(health)))

    # tender delta memory: novelty is only claimed when a previous run observed the source
    today = datetime.now(timezone.utc).date()
    baseline = run_no == 1
    mem = state.get("tenders") or {}
    seen_ids = set()
    for t in all_records:
        fp = h12("|".join([t.get("title") or "", str(t.get("deadline") or ""), t.get("issuer") or ""]))
        old = mem.get(t["id"])
        seen_ids.add(t["id"])
        if old:
            t["first_seen"] = old.get("first_seen") or t["first_seen"]
            if old.get("fp") != fp and not baseline:
                t["is_updated"] = True
        elif not baseline:
            t["is_new"] = True
        mem[t["id"]] = {"fp": fp, "first_seen": t["first_seen"], "last_seen": now_iso(),
                        "deadline": t.get("deadline"), "title": t.get("title"),
                        "country": t.get("country"), "source_name": t.get("source_name")}
        apply_scoring(t, today, baseline)
    all_records.sort(key=lambda t: -t["attention_score"])

    new_n = sum(1 for t in all_records if t["is_new"])
    upd_n = sum(1 for t in all_records if t["is_updated"])
    duration = round(time.time() - t0, 1)

    run_rec = {"run_no": run_no, "started": started, "finished": now_iso(), "duration_s": duration,
               "mode": ("loop+render" if args.loop and args.render else "loop" if args.loop
                        else "once+render" if args.render else "once"),
               "sources_total": len(health), "sources_ok": ok, "sources_failed": len(health) - ok,
               "records_total": len(all_records), "records_new": new_n, "records_changed": upd_n,
               "baseline": baseline, "listing_items_tracked": len(all_items)}
    runs = (state.get("runs") or []) + [run_rec]

    next_check = None
    if args.loop:
        next_check = (datetime.now(timezone.utc) + timedelta(minutes=args.interval)).isoformat()
    watch = {"last_checked": started, "next_check": next_check, "interval_minutes": args.interval,
             "loop_active": bool(args.loop), "mode": run_rec["mode"], "run_no": run_no,
             "duration_s": duration, "sources_total": len(health), "sources_ok": ok,
             "sources_failed": len(health) - ok, "records_total": len(all_records),
             "records_new": new_n, "records_changed": upd_n, "baseline": baseline}

    src_mem = {}
    for h in health:
        prev = prev_sources.get(h["id"]) or {}
        src_mem[h["id"]] = {"content_hash": h.get("content_hash") or prev.get("content_hash"),
                            "last_changed_at": h.get("last_changed_at") or prev.get("last_changed_at"),
                            "last_status": h.get("http_status"), "last_rows": h.get("rows_found"),
                            "health_state": h.get("health_state"), "last_checked": h.get("last_checked")}

    data = {"generated_at": now_iso(), "watch": watch, "scoring_note": SCORING_NOTE,
            "work_type_note": WORK_TYPE_NOTE,
            "tenders": all_records, "health": health, "gcc": sources.get("gcc") or [],
            "africa": sources.get("africa") or [], "regions": sources.get("regions") or {},
            "runs": runs[-200:], "authorities_without_url": sources.get("authorities_without_url") or [],
            "listing_items": all_items[:400]}

    tpl = TEMPLATE_FILE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    DASHBOARD_FILE.write_text(tpl.replace("__DATA_JSON__", payload, 1), encoding="utf-8")
    if WEB_DIR.exists():
        WEB_INDEX_FILE.write_text(tpl.replace("__DATA_JSON__", payload, 1), encoding="utf-8")
        write_json(WEB_DATA_FILE, data)
    write_json(TENDERS_FILE, {"generated_at": data["generated_at"], "watch": watch,
                              "tenders": all_records, "health": health, "listing_items": all_items[:400]})
    new_state = {"last_run": run_rec, "sources": src_mem, "tenders": mem, "runs": runs[-500:]}
    write_json(STATE_FILE, new_state)
    log("run #%d done — records %d (new %d, changed %d), dashboard written%s, %s"
        % (run_no, len(all_records), new_n, upd_n,
           " + vercel bundle" if WEB_DIR.exists() else "", DASHBOARD_FILE.name))
    return new_state


def main():
    ap = argparse.ArgumentParser(description="SAMCO Construction Opportunity Watcher (real checks only)")
    ap.add_argument("--once", action="store_true", help="run a single real check cycle")
    ap.add_argument("--loop", action="store_true", help="keep running cycles")
    ap.add_argument("--interval", type=int, default=60, help="minutes between cycles in --loop mode")
    ap.add_argument("--render", action="store_true", help="headless-Chrome pass for JS/session sites")
    ap.add_argument("--render-limit", type=int, default=0, help="max pages rendered per cycle (0 = all render-flagged sources)")
    args = ap.parse_args()

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log("state file unreadable (%s) — starting fresh" % e)

    if args.loop:
        log("loop mode — every %d min (Ctrl+C to stop)" % args.interval)
        while True:
            try:
                state = run_cycle(args, state)
            except KeyboardInterrupt:
                log("loop stopped by user")
                return
            except Exception:
                log("cycle error:\n" + traceback.format_exc())
            try:
                time.sleep(max(30, args.interval * 60))
            except KeyboardInterrupt:
                log("loop stopped by user")
                return
    else:
        run_cycle(args, state)


if __name__ == "__main__":
    main()

"""Discovery sources. Each returns candidate leads ranked best-first; the pipeline keeps the new ones.

A lead is a dict: name, domain, website, description, location, team_size, source,
signal (why now), signal_url, context (text for the LLM), contacts (optional).
"""
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

import llm

http = httpx.Client(
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"},
    timeout=15, follow_redirects=True,
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")
FREEMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "protonmail.com", "icloud.com"}
JUNIOR_RE = re.compile(r"\b(intern(ship)?s?|junior|new[- ]grads?|entry[- ]level|graduates?|early[- ]career|"
                       r"freshers?|trainee|sde[- ]?1|0-[12] years?)\b", re.I)
DEV_RE = re.compile(r"\b(engineer|developer|software|sde|backend|front-?end|full-?stack|programmer|devops|"
                    r"machine learning)\b", re.I)
SENIOR_RE =re.compile(r"\b(senior|sr|staff|principal|lead|head|director|manager|vp|architect)\b", re.I)
# hosts that are job boards / link tools / app hosting, never the company's own site
NOT_COMPANY_RE = re.compile(
    r"(^|\.)(lever\.co|greenhouse\.io|ashbyhq\.com|workable\.com|linkedin\.com|notion\.site|notion\.so|google\.com|"
    r"forms\.gle|bit\.ly|github\.com|github\.io|vercel\.app|wellfound\.com|angel\.co|ycombinator\.com|"
    r"workatastartup\.com|gem\.com|rippling\.com|breezy\.hr|bamboohr\.com|smartrecruiters\.com|teamtailor\.com|"
    r"recruitee\.com|myworkdayjobs\.com|x\.com|twitter\.com)$")
INDIA_OK_RE = re.compile(r"india|worldwide|anywhere|global|asia|apac|remote", re.I)


def text_of(markup):
    return BeautifulSoup(markup or "", "html.parser").get_text(" ", strip=True)


def domain_of(url):
    if not url:
        return ""
    host = urlparse(url if "//" in url else "//" + url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def keyword_hits(text, profile):
    t = text.lower()
    return sum(1 for k in profile.get("keywords", [])
               if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", t))


NAME_SUFFIX_RE = re.compile(r"\b(labs?|ai|technologies|technology|tech|inc|ltd|llc|pvt|limited)\b\.?", re.I)
HINT_STOP = {"startup", "company", "platform", "based", "powered", "for", "and", "the", "of", "to", "in", "an",
             "with", "firm", "brand", "new", "that", "its", "their", "from"}


def resolve_domain(name, hint=""):
    """Company name -> website domain.

    Without a hint: Clearbit's free autocomplete, exact name matches only.
    With a hint (what the company does): also tries name.ai/.com/... and keeps the first candidate whose
    homepage actually talks about the hinted things, so a same-named company is not picked by mistake.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    candidates = []
    try:
        suggestions = http.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}).json()
        candidates = [s["domain"] for s in suggestions if norm(s["name"]) == norm(name)]
    except Exception:
        pass
    words = [w for w in dict.fromkeys(re.findall(r"[a-z]{2,}", hint.lower())) if w not in HINT_STOP]
    if not words:
        return candidates[0] if candidates else ""

    core = norm(NAME_SUFFIX_RE.sub("", name))
    for slug in dict.fromkeys([norm(name), core]):
        if len(slug) >= 4:
            candidates += [slug + tld for tld in (".ai", ".com", ".io", ".in", ".co")]
    if os.getenv("SERPER_API_KEY"):
        candidates += [d for d in (domain_of(r["url"]) for r in search(f"{name} {hint} official website", 5))
                       if core and core in norm(d)]
    candidates = list(dict.fromkeys(candidates))

    def talks_about_it(domain):
        try:
            r = http.get(f"https://{domain}", timeout=6)
            page = text_of(r.text).lower() if r.status_code == 200 else ""
        except Exception:
            return False
        return sum(bool(re.search(rf"\b{re.escape(w)}\b", page)) for w in words) >= min(2, len(words))

    with ThreadPoolExecutor(8) as pool:
        for domain, good in zip(candidates, pool.map(talks_about_it, candidates)):
            if good:
                return domain
    return ""


def search(query, n=10, recent=False):
    """Web search. Serper (Google) when SERPER_API_KEY is set, else best-effort free search."""
    try:
        key = os.getenv("SERPER_API_KEY")
        if key:
            body = {"q": query, "num": n, **({"tbs": "qdr:m"} if recent else {})}
            r = http.post("https://google.serper.dev/search", json=body, headers={"X-API-KEY": key})
            r.raise_for_status()
            return [{"title": o.get("title", ""), "url": o.get("link", ""), "snippet": o.get("snippet", "")}
                    for o in r.json().get("organic", [])]
        from ddgs import DDGS
        res = DDGS().text(query, max_results=n, **({"timelimit": "m"} if recent else {}))
        return [{"title": x["title"], "url": x["href"], "snippet": x["body"]} for x in res]
    except Exception:
        return []


# --- YC directory: companies flagged as hiring ---------------------------------------------

def yc(profile, log):
    data = http.get("https://yc-oss.github.io/api/companies/hiring.json").json()
    ranked = []
    for c in data:
        where = " ".join(c.get("regions") or []) + " " + (c.get("all_locations") or "")
        if c.get("status") != "Active" or (c.get("team_size") or 0) > 300 or not INDIA_OK_RE.search(where):
            continue
        text = f'{c["one_liner"]} {c["long_description"]} {" ".join(c.get("tags") or [])}'
        score = keyword_hits(text, profile) + (3 if "india" in where.lower() else 0)
        ranked.append((score, c.get("launched_at") or 0, c, text))
    ranked.sort(key=lambda t: t[:2], reverse=True)
    return [{
        "name": c["name"], "domain": domain_of(c["website"]), "website": c["website"],
        "description": c["one_liner"], "location": c.get("all_locations"), "team_size": c.get("team_size"),
        "source": "yc", "signal": f'YC {c["batch"]}, marked as hiring', "signal_url": c["url"],
        "context": text[:1500],
    } for _, _, c, text in ranked]


# --- Hacker News "Who is hiring?" ----------------------------------------------------------

def hn(profile, log):
    stories = http.get("https://hn.algolia.com/api/v1/search_by_date",
                       params={"tags": "story,author_whoishiring", "hitsPerPage": 6}).json()["hits"]
    story = next(s for s in stories if s["title"].startswith("Ask HN: Who is hiring"))
    hits = http.get("https://hn.algolia.com/api/v1/search_by_date",
                    params={"tags": f'comment,story_{story["objectID"]}', "hitsPerPage": 1000}).json()["hits"]
    ranked = []
    for h in hits:
        if str(h.get("parent_id")) != str(story["objectID"]):
            continue  # replies, not job posts
        raw = html.unescape(h.get("comment_text") or "")  # hrefs arrive entity-encoded
        text = text_of(raw)
        head = text_of(raw.split("<p>")[0])
        if "|" not in head or not INDIA_OK_RE.search(text):
            continue
        junior = bool(JUNIOR_RE.search(text))
        emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
        sites = [d for d in map(domain_of, re.findall(r'href="([^"]+)"', raw)) if not NOT_COMPANY_RE.search(d)]
        domain = next((e.split("@")[1].lower() for e in emails if e.split("@")[1].lower() not in FREEMAIL), "") \
            or (sites[0] if sites else "")
        name = re.sub(r"\s*\(.*?\)|https?://\S+", "", head.split("|")[0]).strip(" -:")[:60]
        ranked.append((junior * 3 + keyword_hits(text, profile), {
            "name": name, "domain": domain, "website": f"https://{domain}" if domain else None,
            "description": head[:300], "location": None, "team_size": None,
            "source": "hn", "signal": f'Posted in HN "{story["title"][8:]}"',
            "signal_url": f'https://news.ycombinator.com/item?id={h["objectID"]}',
            "context": text[:1500],
            "contacts": [{"name": "", "title": "Email in hiring post", "email": e, "conf": "found", "src": "post"}
                         for e in emails[:2]],
        }))
    ranked.sort(key=lambda t: t[0], reverse=True)
    leads = [lead for _, lead in ranked[:40]]
    for lead in leads:
        if not lead["domain"]:
            lead["domain"] = resolve_domain(lead["name"])
            lead["website"] = f'https://{lead["domain"]}' if lead["domain"] else None
    return leads


# --- Funding news (Google News RSS) -> LLM extraction --------------------------------------

FUND_RE = re.compile(r"\b(raises?|raised|secures?|bags|lands|closes|nets)\b.*(\$|₹|€|rs\.?|crore|cr\b|mn\b|million|"
                     r"seed|series [abc]|funding|round)", re.I)
NEWS_QUERIES = [
    "startup raises seed OR pre-seed OR \"Series A\" funding india when:7d",
    "startup raises \"pre-seed\" OR \"seed\" OR \"Series A\" when:7d",
]


def funding(profile, log):
    entries = {}
    for query in NEWS_QUERIES:
        r = http.get("https://news.google.com/rss/search",
                     params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        for e in feedparser.parse(r.content).entries:
            if FUND_RE.search(e.title):
                entries.setdefault(e.title, e)
    items = list(entries.values())[:40]
    if not items:
        return []
    out = llm.chat_json(
        "From these news headlines, extract startups that just raised an early-stage round (pre-seed to Series B). "
        "Skip VC funds raising money, acquisitions, IPOs, market reports and late-stage companies. "
        "List each company once. Use only what the headline says. "
        'Return JSON: {"items":[{"i":<headline number>,"company":"<name>","what":"<what they do, from the headline>",'
        '"raised":"<amount and round>"}]}',
        "\n".join(f"{i}. {e.title}" for i, e in enumerate(items)),
        max_tokens=2500, log=log,
    )
    leads = []
    for it in out.get("items", []):
        try:
            e = items[int(it["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        name = str(it.get("company", "")).strip()
        if not name:
            continue
        domain = resolve_domain(name, hint=str(it.get("what") or ""))
        leads.append({
            "name": name, "domain": domain, "website": f"https://{domain}" if domain else None,
            "description": it.get("what"), "location": None, "team_size": None,
            "source": "funding", "signal": f'Raised {it.get("raised", "funding")} ({e.get("published", "")[5:16]})',
            "signal_url": e.link, "context": e.title,
        })
    return leads


# --- Remote job boards (Remotive, RemoteOK) ------------------------------------------------

def jobs(profile, log):
    posts = []  # (company, title, location, url, description)
    try:
        for j in http.get("https://remotive.com/api/remote-jobs", params={"category": "software-dev"}).json()["jobs"]:
            posts.append((j["company_name"], j["title"], j.get("candidate_required_location", ""),
                          j["url"], text_of(j.get("description"))))
    except Exception as e:
        log(f"  Remotive failed: {e}")
    try:
        for j in http.get("https://remoteok.com/api", timeout=40).json()[1:]:
            if set(j.get("tags") or []) & {"dev", "engineer", "backend", "frontend", "full stack", "software"}:
                posts.append((j["company"], j["position"], j.get("location", ""), j["url"], text_of(j.get("description"))))
    except Exception as e:
        log(f"  RemoteOK failed: {e}")

    ranked = []
    for company, title, location, url, desc in posts:
        if not DEV_RE.search(title) or SENIOR_RE.search(title) or (location and not INDIA_OK_RE.search(location)):
            continue
        score = bool(JUNIOR_RE.search(title)) * 3 + keyword_hits(f"{title} {desc}", profile)
        ranked.append((score, {
            "name": company, "description": f"Hiring: {title}", "location": location or "Remote", "team_size": None,
            "source": "jobs", "signal": f"Open remote role: {title}", "signal_url": url,
            "context": f"{title}. {desc[:1300]}",
        }))
    ranked.sort(key=lambda t: t[0], reverse=True)
    leads = [lead for _, lead in ranked[:30]]
    for lead in leads:
        lead["domain"] = resolve_domain(lead["name"])
        lead["website"] = f'https://{lead["domain"]}' if lead["domain"] else None
    return leads


# --- LinkedIn / X hiring posts (via web search) -> LLM extraction --------------------------

def social(profile, log):
    roles = (profile.get("target_roles") or ["software engineer"])[:3]
    queries = [f'site:linkedin.com/posts "hiring" "{r}" (fresher OR intern OR "0-1 years") (resume OR email)'
               for r in roles]
    queries.append(f'(site:x.com OR site:twitter.com) "hiring" "{roles[0]}" (intern OR fresher OR "new grad") startup')
    results = {}
    for query in queries:
        for r in search(query, 10, recent=True):
            results.setdefault(r["url"], r)
    results = list(results.values())[:30]
    if not results:
        log("  No search results. Set SERPER_API_KEY in .env for reliable LinkedIn/X post search.")
        return []
    out = llm.chat_json(
        "These are search results for social posts about hiring. Extract only posts where a specific startup or "
        "company is hiring developers (interns, freshers or junior roles preferred). Skip job aggregators, staffing "
        "agencies, courses and posts by job seekers. Use only what the text says; leave a field empty if absent. "
        'Return JSON: {"items":[{"i":<result number>,"company":"<name>","role":"<role>",'
        '"poster":"<person who posted>","email":"<email in the text>"}]}',
        "\n".join(f'{i}. {r["title"]} :: {r["snippet"]}' for i, r in enumerate(results)),
        max_tokens=2500, log=log,
    )
    leads = []
    for it in out.get("items", []):
        try:
            r = results[int(it["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        name = str(it.get("company", "")).strip()
        if not name:
            continue
        text = f'{r["title"]} {r["snippet"]}'
        email = str(it.get("email") or "").strip()
        if email.lower() not in text.lower():
            email = ""  # never trust an email the model did not copy from the text
        edomain = email.split("@")[1].lower() if email else ""
        domain = edomain if edomain and edomain not in FREEMAIL else resolve_domain(name)
        leads.append({
            "name": name, "domain": domain, "website": f"https://{domain}" if domain else None,
            "description": None, "location": None, "team_size": None,
            "source": "social", "signal": f'Hiring post: {it.get("role") or "developer"}', "signal_url": r["url"],
            "context": text[:1500],
            "contacts": [{"name": it.get("poster") or "", "title": "Email in hiring post", "email": email,
                          "conf": "found", "src": "post"}] if email else [],
        })
    return leads


SOURCES = {"yc": yc, "hn": hn, "funding": funding, "jobs": jobs, "social": social}

"""Find who to email at a company, and pick the best recipient for its size.

Sources, in order: emails already in the hiring post, the company website, YC founder
listings, Apollo (if APOLLO_API_KEY is set), a LinkedIn search for founders, and finally a
first-name@domain guess (labelled "guess") when the domain accepts mail.
"""
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import dns.resolver

from sources import EMAIL_RE, http, search, text_of

HIRING_INBOX = {"careers", "career", "jobs", "job", "hr", "hiring", "talent", "recruiting", "recruitment",
                "people", "join", "work", "founders", "founder"}
GENERAL_INBOX = {"hello", "hi", "hey", "contact", "info", "team"}
FOUNDER_RE = re.compile(r"founder|ceo|chief exec|cto|chief tech|owner", re.I)
ENG_RE = re.compile(r"cto|engineering|chief tech|technology", re.I)
HR_RE = re.compile(r"\bhr\b|human|talent|recruit|people|careers|hiring", re.I)

_apollo_off = False  # set once Apollo rejects the key/plan, so we stop calling it


def has_mx(domain):
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:
        return False


def from_website(domain):
    """Scrape a few standard pages for same-domain emails. Returns (contacts, homepage text)."""
    def fetch(path):
        try:
            r = http.get(f"https://{domain}{path}", timeout=8)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    with ThreadPoolExecutor(6) as pool:
        pages = list(pool.map(fetch, ["", "/about", "/team", "/careers", "/contact", "/jobs"]))
    found = {}
    for page in pages:
        for email in EMAIL_RE.findall(html.unescape(page)):
            email = email.lower().strip(".")
            local, _, host = email.partition("@")
            if host != domain or email in found:
                continue
            if local in HIRING_INBOX:
                found[email] = {"name": "", "title": "Careers / hiring inbox", "email": email, "conf": "found", "src": "website"}
            elif local in GENERAL_INBOX:
                found[email] = {"name": "", "title": "General inbox", "email": email, "conf": "found", "src": "website"}
            elif re.fullmatch(r"[a-z]+(\.[a-z]+)?", local) and local not in {
                    "support", "sales", "press", "privacy", "legal", "security", "noreply", "abuse", "billing",
                    "help", "media", "partners", "admin", "webmaster", "example", "name", "email", "you"}:
                found[email] = {"name": local.split(".")[0].title(), "title": "Listed on website", "email": email,
                                "conf": "found", "src": "website"}
    return list(found.values()), text_of(pages[0])[:1200]


def yc_founders(yc_url):
    """Founder names and titles from a YC company page."""
    try:
        m = re.search(r'data-page="([^"]+)"', http.get(yc_url).text)
        founders = json.loads(html.unescape(m.group(1)))["props"]["company"]["founders"]
        return [{"name": f["full_name"], "title": f.get("title") or "Founder"} for f in founders if f.get("is_active", True)]
    except Exception:
        return []


def linkedin_founders(company):
    """Founder names from LinkedIn result titles like 'Jane Doe - Co-Founder - Acme | LinkedIn'."""
    people = []
    for r in search(f'"{company}" founder OR CEO OR CTO site:linkedin.com/in', 6):
        m = re.match(r"^([^-–|]{3,40}?)\s*[-–]\s*(.+?)(\s*\|\s*LinkedIn)?$", r["title"])
        if m and "linkedin.com/in" in r["url"] and company.lower() in (r["title"] + r["snippet"]).lower() \
                and FOUNDER_RE.search(m.group(2)):
            people.append({"name": m.group(1).strip(), "title": m.group(2).strip()[:60]})
    return people[:3]


def apollo(domain, want_hr, log):
    """One enriched contact from Apollo (search is free; revealing the email costs 1 credit)."""
    global _apollo_off
    key = os.getenv("APOLLO_API_KEY")
    if not key or _apollo_off:
        return []
    headers = {"x-api-key": key, "accept": "application/json"}
    params = [("q_organization_domains_list[]", domain), ("per_page", 5)]
    params += [("person_titles[]", t) for t in ("recruiter", "talent acquisition", "human resources", "people operations")] \
        if want_hr else [("person_seniorities[]", s) for s in ("founder", "owner", "c_suite")]
    try:
        r = http.post("https://api.apollo.io/api/v1/mixed_people/api_search", params=params, headers=headers)
        if r.status_code in (401, 403):
            _apollo_off = True
            log(f"  Apollo refused the request ({r.status_code}): {r.text[:160]}. Skipping Apollo for this run.")
            return []
        r.raise_for_status()
        people = [p for p in r.json().get("people", []) if p.get("has_email", True)]
        if not people:
            return []
        r = http.post("https://api.apollo.io/api/v1/people/match", params={"id": people[0]["id"]}, headers=headers)
        r.raise_for_status()
        p = r.json().get("person") or {}
        if not p.get("email") or "email_not_unlocked" in p["email"]:
            return []
        return [{"name": p.get("name") or "", "title": p.get("title") or "", "email": p["email"],
                 "conf": "verified" if p.get("email_status") == "verified" else "found", "src": "apollo"}]
    except Exception as e:
        log(f"  Apollo error: {e}")
        return []


def rank(contact, size):
    """Higher is better. Small company -> founders; mid -> engineering leads; large -> HR."""
    title = contact.get("title") or ""
    founder, eng, hr = FOUNDER_RE.search(title), ENG_RE.search(title), HR_RE.search(title)
    if size <= 30:
        role = 3 if founder else 2 if eng else 1 if hr else 0
    elif size <= 200:
        role = 3 if eng else 2 if (founder or hr) else 0
    else:
        role = 3 if hr else 1 if eng else 0
    conf = {"verified": 3, "found": 2, "guess": 1}[contact["conf"]]
    invited = 6 if contact.get("src") == "post" else 0  # they asked for emails at this address
    return role * 2 + conf + invited


def find(lead, log):
    """Returns (contacts sorted best-first, extra context from the homepage)."""
    domain, size = lead.get("domain"), lead.get("team_size") or 15  # unknown size: assume small startup
    contacts = list(lead.get("contacts") or [])
    if not domain:
        return contacts, ""
    site_contacts, homepage = from_website(domain)
    contacts += site_contacts
    contacts += apollo(domain, want_hr=size > 200, log=log)

    if size <= 200 and not any(c["conf"] == "verified" for c in contacts):
        people = yc_founders(lead["signal_url"]) if lead.get("source") == "yc" else linkedin_founders(lead["name"])
        if people and has_mx(domain):
            for p in people:
                first = re.sub(r"[^a-z]", "", p["name"].split()[0].lower())
                if first:
                    contacts.append({**p, "email": f"{first}@{domain}", "conf": "guess", "src": "pattern"})

    unique = {}
    for c in sorted(contacts, key=lambda c: rank(c, size), reverse=True):
        unique.setdefault(c["email"].lower(), c)
    return list(unique.values()), homepage

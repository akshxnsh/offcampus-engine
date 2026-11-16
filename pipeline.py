"""The workflow: discover -> score -> find contacts -> write emails -> push to Gmail drafts.

Runs in a background thread; the UI polls `state` for progress.
"""
import json
import threading
import time

import contacts
import db
import gmail
import llm
from sources import SOURCES

STAGES = ["discover", "score", "contacts", "draft", "push"]
RESUME_PATH = db.DATA / "resume.pdf"
DEFAULT_PREFS = {"threshold": 55, "per_source": 10, "notes": ""}

state = {"running": False, "stage": "", "log": []}


def log(msg):
    state["log"] = (state["log"] + [time.strftime("%H:%M:%S  ") + msg])[-400:]


def prefs():
    return {**DEFAULT_PREFS, **(db.get_setting("prefs") or {})}


# --- profile -----------------------------------------------------------------------------

PROFILE_PROMPT = """Turn this resume into a compact profile used to match the candidate with startups and to write
outreach emails. Use only facts in the resume. Return JSON:
{"name": "", "email": "", "phone": "", "linkedin": "", "github": "", "portfolio": "",
 "education": "<degree, college, graduation year>",
 "headline": "<one line: what kind of developer they are>",
 "target_roles": ["<3-4 job titles they fit, e.g. backend developer, SDE intern>"],
 "skills": ["<top 12 skills>"],
 "keywords": ["<15-20 lowercase single words or short terms for matching companies: tech, domains, e.g. python, llm, fintech>"],
 "highlights": [{"title": "<project or job>", "what": "<one sentence: what was built and the result, keep any numbers>",
                 "tech": ["..."]}]}
Include at most 5 highlights, strongest first."""


def build_profile(resume_text):
    profile = llm.chat_json(PROFILE_PROMPT, resume_text[:9000], model=llm.SMART, max_tokens=3000, temperature=0.1)
    db.set_setting("profile", profile)
    return profile


# --- stages ------------------------------------------------------------------------------

def discover(profile, sources):
    cap = prefs()["per_source"]
    for name in sources:
        log(f"Discover: {name}")
        try:
            candidates = SOURCES[name](profile, log)
        except llm.BudgetExceeded:
            raise
        except Exception as e:
            log(f"  {name} failed: {e}")
            continue
        added = 0
        for lead in candidates:
            if added >= cap:
                break
            added += db.add_lead(lead)
        log(f"  {added} new leads (from {len(candidates)} candidates)")


SCORE_PROMPT = """You screen companies for a job seeker who is a fresher (new graduate) developer looking for
internships or entry-level roles, based in India (open to India-based or remote roles).

Candidate: {profile}

For each company give "fit" 0-100: how worthwhile a cold email is. High when the company is a real startup, its work
overlaps the candidate's skills, it is small or recently funded or actively hiring (so it can take a junior), and the
role could be done from India. Low for staffing agencies, job seekers' own posts, non-software businesses, roles
restricted to other countries, or senior-only hiring with no overlap.
Also give "role": the most plausible role to ask for, and "reason": one short sentence.
Return JSON: {{"items":[{{"id":<id>,"fit":<0-100>,"role":"","reason":""}}]}}"""


def score(profile):
    todo = db.leads("status='new'")
    threshold = prefs()["threshold"]
    brief = json.dumps({k: profile.get(k) for k in ("headline", "education", "target_roles", "skills")})
    for i in range(0, len(todo), 8):
        batch = todo[i:i + 8]
        log(f"Score: {i + len(batch)}/{len(todo)}")
        listing = "\n".join(
            f'id {l["id"]}: {l["name"]} | {(l["description"] or "")[:200]} | location: {l["location"] or "?"} | '
            f'team: {l["team_size"] or "?"} | signal: {l["signal"]} | {(l["context"] or "")[:250]}' for l in batch)
        out = llm.chat_json(SCORE_PROMPT.format(profile=brief), listing, max_tokens=2000, log=log)
        by_id = {l["id"]: l for l in batch}
        for it in out.get("items", []):
            try:
                lead_id, fit = int(it["id"]), int(it["fit"])
            except (KeyError, ValueError, TypeError):
                continue
            if lead_id in by_id:
                db.update_lead(lead_id, fit_score=fit, fit_reason=it.get("reason"), role=it.get("role"),
                               status="scored" if fit >= threshold else "lowfit")


def find_contacts(profile):
    todo = db.leads("status='scored'")
    for i, lead in enumerate(todo, 1):
        log(f"Contacts {i}/{len(todo)}: {lead['name']}")
        found, homepage = contacts.find(lead, log)
        fields = {"contacts": found, "status": "ready" if found else "nocontact"}
        if homepage:
            fields["context"] = f'{lead["context"] or ""}\n\nFrom their website: {homepage}'
        if found:
            best = found[0]
            fields.update(to_name=best["name"], to_title=best["title"], to_email=best["email"], to_conf=best["conf"])
            log(f"  -> {best['email']} ({best['conf']})")
        db.update_lead(lead["id"], **fields)


DRAFT_PROMPT = """You write cold emails for a fresher developer asking startups for an off-campus opportunity. Each
email must read as if one person wrote it for this one company, not a template.

Rules:
- Body is 80-120 words. Plain text, short paragraphs. No bullet points, no bold, no emojis, no em dashes.
- First line: a specific, concrete observation about THIS company taken from the company facts (what they build, a
  recent raise, a launch, their hiring post). Never open with "I hope this email finds you well", "My name is",
  or flattery like "I am impressed by".
- Then one or two sentences with the candidate's single most relevant proof: the project or experience that maps
  to what this company does, with a concrete number if the profile has one. Do not list skills or summarise the resume.
- Then one sentence on what the candidate could help with there, tied to the company's actual work.
- The ask: low pressure and specific. Open to an internship or entry-level role (name the role), happy to do a
  small trial task or a 15-minute call. Say the resume is attached.
- Sign off with the candidate's name, then one line of contact details (phone, LinkedIn, GitHub) that exist in the profile.
- Use ONLY facts from the candidate profile and the company facts. Never invent metrics, mutual connections,
  having used their product, or news.
- Greeting: "Hi <first name>," when a recipient name is given, otherwise "Hi <Company> team,".
- To a founder/CEO/CTO write as a builder to a builder. To HR or a careers inbox be a little more formal and name the role clearly.
- Subject: under 8 words, specific to this company, natural case, not clickbait, not "Application for ...".
- Banned words: passionate, synergy, leverage, esteemed, delve, thrilled, excited.
Return JSON: {"subject": "", "body": ""}"""


def write_draft(lead, profile):
    facts = {
        "candidate": profile,
        "company": {"name": lead["name"], "what_they_do": lead["description"], "why_now": lead["signal"],
                    "location": lead["location"], "team_size": lead["team_size"],
                    "more": (lead["context"] or "")[:2000]},
        "recipient": {"name": lead["to_name"], "title": lead["to_title"]},
        "role_to_ask_for": lead["role"],
        "extra_instructions_from_candidate": prefs()["notes"],
    }
    out = llm.chat_json(DRAFT_PROMPT, json.dumps(facts), model=llm.SMART, max_tokens=1500, temperature=0.7, log=log)
    db.update_lead(lead["id"], subject=out["subject"], body=out["body"], status="drafted")
    return out


def draft(profile):
    todo = db.leads("status='ready'")
    for i, lead in enumerate(todo, 1):
        log(f"Write {i}/{len(todo)}: {lead['name']}")
        write_draft(lead, profile)


def push_one(lead):
    gmail.push_draft(lead["to_email"], lead["subject"], lead["body"], RESUME_PATH,
                     db.get_setting("resume_name") or "Resume.pdf")
    db.update_lead(lead["id"], status="pushed")


def push(profile):
    todo = db.leads("status='drafted'")
    for i, lead in enumerate(todo, 1):
        log(f"Gmail draft {i}/{len(todo)}: {lead['name']} -> {lead['to_email']}")
        push_one(lead)


# --- runner ------------------------------------------------------------------------------

def start(stages, sources):
    if state["running"]:
        return False
    profile = db.get_setting("profile")
    if not profile:
        raise RuntimeError("Upload your resume first")

    def work():
        try:
            for stage in STAGES:
                if stage in stages:
                    state["stage"] = stage
                    if stage == "discover":
                        discover(profile, sources)
                    else:
                        {"score": score, "contacts": find_contacts, "draft": draft, "push": push}[stage](profile)
            log("Done.")
        except llm.BudgetExceeded as e:
            log(f"Stopped: {e}. Remaining leads will continue on the next run.")
        except Exception as e:
            log(f"Stopped on error: {e}")
        finally:
            state.update(running=False, stage="")

    state.update(running=True, log=[])
    threading.Thread(target=work, daemon=True).start()
    return True

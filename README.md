# OffCampus Engine

Finds startups that are likely to hire a fresher developer, works out who to email there,
writes a short personalised email for each one, and saves it to your Gmail Drafts with your
resume attached. You review and press Send in Gmail. Nothing is ever sent automatically.

## Setup (once)

1. Copy `.env.example` to `.env` and fill in:
   - `GROQ_API_KEY` from https://console.groq.com/keys
   - `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` for the account you send outreach from
     (create the app password at https://myaccount.google.com/apppasswords; 2-Step Verification must be on)
   - optional `APOLLO_API_KEY` (verified founder / HR emails) and `SERPER_API_KEY` (LinkedIn / X post search)
2. Double-click `run.bat` (or run `.venv\Scripts\python -m uvicorn app:app --port 8765`) and open http://localhost:8765
3. In **Profile & setup**, upload your resume PDF. Review the generated profile; emails only use facts from it.

## Each run

Open **Run**, pick sources and steps, press **Start run**. Then open **Leads**, click a lead to
read or edit its email, switch recipient if needed, and press **Save to Gmail drafts**.

| Step | What happens | Groq use |
|---|---|---|
| Find new startups | Pulls candidates from the sources below, keeps ones not seen before | 1 small call each for funding news and social posts |
| Score fit | Rates each company 0-100 against your profile, 8 companies per call | fast model |
| Find who to email | Hiring-post emails, company website, YC founder pages, Apollo, then `first@domain` guesses | none |
| Write the emails | One 80-120 word email per company | smart model, about 2K tokens each |
| Save drafts to Gmail | IMAP append to Drafts with the resume attached | none |

## Sources

- **yc**: Y Combinator companies flagged as hiring, India-based or remote-friendly
- **hn**: the current Hacker News "Who is hiring?" thread (often includes a direct email)
- **funding**: startups that raised pre-seed to Series B in the last 7 days (Google News)
- **jobs**: developer roles on Remotive and RemoteOK open to India / worldwide
- **social**: LinkedIn and X hiring posts found through web search (needs `SERPER_API_KEY` to be reliable)

## Who gets the email

Up to 30 people: founder / CEO / CTO. 31-200: engineering lead, then founder or HR. Larger: HR / talent.
An address published in a hiring post always wins. Every recipient carries a confidence label:
`verified` (Apollo), `found` (published somewhere), or `guess` (name pattern, may bounce).

## Groq limits

Defaults assume the free tier: 30 requests/min, 8K tokens/min, 200K tokens/day per model. The app
waits out per-minute limits and stops a run when a model reaches `GROQ_DAILY_TOKENS` (default 180K);
unfinished leads continue on the next run. The header shows tokens used in the last 24 hours.

## Files

`app.py` web API · `pipeline.py` the workflow and prompts · `sources.py` discovery · `contacts.py` email finding ·
`llm.py` Groq client · `gmail.py` draft upload · `db.py` SQLite · `static/index.html` UI · `data/` your local database and resume (not committed)

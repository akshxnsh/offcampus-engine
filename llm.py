"""Groq chat client: JSON output, 429 back-off, and a rolling 24h token budget per model."""
import json
import os
import time

import httpx

import db

SMART = os.getenv("GROQ_MODEL_SMART", "openai/gpt-oss-120b")  # resume parsing + email writing
FAST = os.getenv("GROQ_MODEL_FAST", "openai/gpt-oss-20b")     # scoring + extraction
DAILY_TOKENS = int(os.getenv("GROQ_DAILY_TOKENS", "180000"))  # per model; free tier is 200K/day

URL = "https://api.groq.com/openai/v1/chat/completions"


class BudgetExceeded(Exception):
    pass


def used_today(model):
    rows = db.q("SELECT COALESCE(SUM(tokens),0) t FROM usage WHERE model=? AND ts>?",
                (model, time.time() - 86400))
    return rows[0]["t"]


def chat_json(system, user, model=FAST, max_tokens=1500, temperature=0.3, log=print):
    """One chat call that must return a JSON object. Retries on rate limits."""
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is missing in .env")
    if used_today(model) >= DAILY_TOKENS:
        raise BudgetExceeded(f"Daily token budget reached for {model}")

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"  # reasoning tokens count against the budget

    for attempt in range(6):
        r = httpx.post(URL, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=90)
        if r.status_code == 429:
            wait = min(float(r.headers.get("retry-after", 20)), 90) + 1
            log(f"  Groq rate limit, waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.status_code == 400 and "json_validate_failed" in r.text and attempt < 2:
            continue  # model produced invalid JSON; try again
        r.raise_for_status()
        data = r.json()
        db.x("INSERT INTO usage VALUES(?,?,?)", (time.time(), model, data["usage"]["total_tokens"]))
        return json.loads(data["choices"][0]["message"]["content"])
    raise RuntimeError("Groq kept rate-limiting; try again later")

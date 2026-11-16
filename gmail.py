"""Save an email with the resume attached into Gmail's Drafts folder over IMAP (app password)."""
import imaplib
import os
import time
from email.message import EmailMessage
from pathlib import Path


def push_draft(to, subject, body, resume_path=None, resume_name="Resume.pdf"):
    address, password = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are missing in .env")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = address, to, subject
    msg.set_content(body)
    if resume_path and Path(resume_path).exists():
        msg.add_attachment(Path(resume_path).read_bytes(), maintype="application", subtype="pdf", filename=resume_name)

    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(address, password.replace(" ", ""))
        # the Drafts folder name is localised; find it by its \Drafts flag
        _, boxes = imap.list()
        drafts = next(b.decode().split(' "/" ')[-1] for b in boxes if b"\\Drafts" in b)
        status, detail = imap.append(drafts, "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise RuntimeError(f"Gmail rejected the draft: {detail}")

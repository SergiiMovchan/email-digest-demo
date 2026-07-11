#!/usr/bin/env python3
"""
Email Morning Digest Bot
Reads Gmail + IMAP mailboxes from yesterday, categorizes them with Claude,
and sends a per-account summary to Telegram.
"""

import os
import imaplib
import email
import json
import pickle
import base64
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import anthropic
import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load .env file if it exists
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ALLOWED_CHAT_IDS = {int(os.environ["TELEGRAM_CHAT_ID"])}

CONFIG = {
    "telegram": {
        "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
    },
    "anthropic": {
        "api_key": os.environ["ANTHROPIC_API_KEY"],
    },
    "gmail": [
        {"email": "sales@example.com"},
    ],
    "imap": [
        {
            "email": "orders@example.com",
            "password": os.environ["IMAP_PASSWORD_1"],
            "imap_server": "imap.example.com",
        },
        {
            "email": "support@example.com",
            "password": os.environ["IMAP_PASSWORD_2"],
            "imap_server": "imap.example.com",
        },
    ],
}

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ─────────────────────────────────────────────
# DATE RANGE
# ─────────────────────────────────────────────
def get_date_range():
    """Return (date_from, date_to, label) depending on weekday.
    Monday → analyze Sat+Sun. Other days → yesterday only."""
    today = datetime.now()
    if today.weekday() == 0:  # Monday
        date_from = today - timedelta(days=2)  # Saturday
        date_to = today
        label = f"{date_from.strftime('%d.%m')}–{(today - timedelta(days=1)).strftime('%d.%m.%Y')} (Sat–Sun)"
    else:
        date_from = today - timedelta(days=1)
        date_to = today
        label = date_from.strftime("%d.%m.%Y")
    return date_from, date_to, label


# ─────────────────────────────────────────────
# GMAIL
# ─────────────────────────────────────────────
def get_gmail_credentials(email_addr: str) -> Credentials:
    token_file = f"gmail_token_{email_addr.split('@')[0]}.pickle"
    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)
    return creds


def fetch_gmail_emails(account: dict, date_from: datetime, date_to: datetime) -> list[dict]:
    print(f"📬 Reading Gmail: {account['email']}")
    emails = []
    try:
        creds = get_gmail_credentials(account["email"])
        service = build("gmail", "v1", credentials=creds)
        from_str = date_from.strftime("%Y/%m/%d")
        to_str = date_to.strftime("%Y/%m/%d")
        query = f"after:{from_str} before:{to_str} in:inbox"
        results = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
        messages = results.get("messages", [])
        for msg in messages:
            msg_data = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
            headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
            subject = decode_mime_str(headers.get("Subject", "(no subject)"))
            sender = headers.get("From", "")
            body = extract_gmail_body(msg_data["payload"])
            emails.append({
                "account": account["email"],
                "source": "Gmail",
                "subject": subject,
                "sender": sender,
                "body": body[:300],
            })
    except Exception as e:
        print(f"  ❌ Error: {e}")
    print(f"  ✅ Found {len(emails)} emails")
    return emails


def extract_gmail_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""


# ─────────────────────────────────────────────
# IMAP
# ─────────────────────────────────────────────
def fetch_imap_emails(account: dict, date_from: datetime, date_to: datetime) -> list[dict]:
    print(f"📬 Reading IMAP: {account['email']}")
    emails = []
    try:
        mail = imaplib.IMAP4_SSL(account["imap_server"], 993)
        mail.login(account["email"], account["password"])
        mail.select("INBOX")
        from_str = date_from.strftime("%d-%b-%Y")
        to_str = date_to.strftime("%d-%b-%Y")
        _, message_ids = mail.search(None, f'(SINCE "{from_str}" BEFORE "{to_str}")')
        for msg_id in message_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime_str(msg.get("Subject", "(no subject)"))
            sender = msg.get("From", "")
            body = extract_imap_body(msg)
            emails.append({
                "account": account["email"],
                "source": "IMAP",
                "subject": subject,
                "sender": sender,
                "body": body[:300],
            })
        mail.logout()
    except Exception as e:
        print(f"  ❌ Error: {e}")
    print(f"  ✅ Found {len(emails)} emails")
    return emails


def extract_imap_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


def decode_mime_str(s: str) -> str:
    try:
        parts = decode_header(s)
        result = []
        for part, enc in parts:
            if isinstance(part, bytes):
                result.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                result.append(part)
        return "".join(result)
    except Exception:
        return s


# ─────────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────────
def analyze_emails_with_claude(emails: list[dict], date_label: str) -> str:
    """Analyze emails and return formatted digest text."""
    print(f"\n🤖 Analyzing {len(emails)} emails with Claude...")
    if not emails:
        return ""

    client = anthropic.Anthropic(api_key=CONFIG["anthropic"]["api_key"])

    # Token-efficient: send only sender + subject, never the body.
    emails_text = "\n".join([
        f"[{i+1}] From: {e['sender']} | Subject: {e['subject']}"
        for i, e in enumerate(emails)
    ])

    account = emails[0]["account"]
    total = len(emails)

    prompt = f"""Analyze {total} emails from the mailbox {account} for {date_label}.

EMAILS:
{emails_text}

Write a short digest in exactly this format:

From: [comma-separated company/sender names, NOT email addresses, max 10 names + "and others"]

Summary:
• [count] emails — [group description]
(max 7 lines, group similar items together)

⚠️ Needs attention:
• [specific issue — what it is and what to do]
(only what matters: debts, pricing violations, cancellations, deadlines, documents to sign)

Rules:
- Do NOT use Markdown: no **, *, #, __
- Do NOT add a heading at the start
- Do NOT include email numbers in brackets
- Show senders as names (e.g. Acme, Globex, Initech), not as email addresses
- Include concrete amounts and order numbers where relevant
- Max 1200 characters"""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    print(f"  ✅ Done ({len(text)} chars)")
    return text


# ─────────────────────────────────────────────
# FORMATTING
# ─────────────────────────────────────────────
def safe(text: str) -> str:
    """Remove chars that break Telegram Markdown."""
    if not text:
        return ""
    return text.replace("*", "").replace("_", " ").replace("`", "").replace("[", "").replace("]", "")


def format_account_digest(account_email: str, emails: list, analysis: str, date_label: str) -> list[str]:
    header = f"📧 {account_email}\n📅 {date_label} | Total: {len(emails)} emails\n"
    msg = header + "\n" + analysis
    if len(msg) > 4000:
        msg = msg[:3950] + "\n... truncated"
    return [msg]


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(messages: list[str]) -> None:
    token = CONFIG["telegram"]["bot_token"]
    chat_id = CONFIG["telegram"]["chat_id"]

    # Whitelist check — only send to allowed chats
    if int(chat_id) not in ALLOWED_CHAT_IDS:
        print(f"  🚫 Blocked: chat_id {chat_id} is not in the whitelist. Send cancelled.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for msg in messages:
        if not msg.strip():
            continue
        try:
            resp = httpx.post(url, json={"chat_id": chat_id, "text": msg})
            if not resp.json().get("ok"):
                print(f"  ⚠️ Telegram error: {resp.json()}")
        except Exception as e:
            print(f"  ❌ Send error: {e}")
    print("  ✅ Sent!")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"🌅 Email Morning Digest — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print("=" * 60)

    date_from, date_to, date_label = get_date_range()
    print(f"📅 Period: {date_label}")

    all_emails = []
    for account in CONFIG["gmail"]:
        all_emails.extend(fetch_gmail_emails(account, date_from, date_to))
    for account in CONFIG["imap"]:
        all_emails.extend(fetch_imap_emails(account, date_from, date_to))

    print(f"\n📊 Total: {len(all_emails)} emails")

    if not all_emails:
        send_telegram([f"📭 No new emails for {date_label}"])
        return

    # Group by account
    accounts: dict = {}
    for em in all_emails:
        acc = em["account"]
        if acc not in accounts:
            accounts[acc] = []
        accounts[acc].append(em)

    for account_email, emails in accounts.items():
        print(f"\n{'='*40}\n📬 {account_email} ({len(emails)} emails)")
        analysis = analyze_emails_with_claude(emails, date_label)
        messages = format_account_digest(account_email, emails, analysis, date_label)
        send_telegram(messages)

    print("\n✅ Done!")


if __name__ == "__main__":
    main()

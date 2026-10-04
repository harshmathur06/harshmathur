import asyncio
import hashlib
import json
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "https://www.naukri.com/"
PROFILE_DIR = Path(os.getenv("NAUKRI_PROFILE_DIR", ".naukri-browser"))
STATE_FILE = Path(os.getenv("NINVITE_STATE_FILE", "data/seen_invites.json"))
DEBUG_DIR = Path("data/debug")
RECIPIENT = os.getenv("NINVITE_EMAIL_TO", "Harsh.nid@gmail.com")


@dataclass
class Invite:
    key: str
    title: str
    company: str
    location: str
    date: str
    recruiter: str
    details: str
    source_url: str


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def invite_key(*parts: str) -> str:
    raw = "|".join(norm(p).lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()).get("keys", []))
    except Exception:
        return set()


def save_seen(keys: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "keys": sorted(keys)
    }, indent=2))


async def click_text(page, candidates):
    for candidate in candidates:
        for loc in [
            page.get_by_text(candidate, exact=True),
            page.get_by_text(re.compile(re.escape(candidate), re.I)),
        ]:
            if await loc.count():
                try:
                    await loc.first.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    return candidate
                except Exception:
                    pass
    return None


async def wait_for_login(page):
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    print("Naukri opened. Complete login/MFA in the browser if requested.")

    for _ in range(120):
        text = norm(await page.locator("body").inner_text(timeout=5000))
        lower = text.lower()
        logged_in = any(x in lower for x in [
            "my naukri", "my profile", "naukri profile",
            "job recommendations", "jobs"
        ])
        if logged_in and len(text) > 200:
            return
        await asyncio.sleep(2)

    raise RuntimeError("Timed out waiting for a logged-in Naukri session.")


async def open_ninvite(page):
    await click_text(page, ["Jobs", "Jobs & Responses"])

    clicked = await click_text(page, [
        "NInvite", "NVite", "N Invites",
        "NInvite list", "Invites", "Recruiter Invites"
    ])
    if clicked:
        return

    for locator in [
        page.locator("a").filter(has_text=re.compile(r"n.?vite|invite", re.I)),
        page.locator("button").filter(has_text=re.compile(r"n.?vite|invite", re.I)),
    ]:
        if await locator.count():
            try:
                await locator.first.click(timeout=5000)
                await page.wait_for_timeout(1500)
                return
            except Exception:
                pass

    raise RuntimeError(
        "Could not locate NInvite in the current Naukri UI. "
        "Run python agent.py --discover and inspect data/debug/."
    )


async def discover(page):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
    (DEBUG_DIR / "page.html").write_text(await page.content(), encoding="utf-8")

    print("\n--- LINKS ---")
    for x in await page.locator("a").all_inner_texts():
        x = norm(x)
        if x:
            print(x)

    print("\n--- BUTTONS ---")
    for x in await page.locator("button").all_inner_texts():
        x = norm(x)
        if x:
            print(x)

    print(f"\nDebug files saved under {DEBUG_DIR}/")


async def extract_invites(page) -> list[Invite]:
    body_text = norm(await page.locator("body").inner_text())
    if not re.search(r"n.?vite|invite", body_text, re.I):
        raise RuntimeError("NInvite page opened, but invitation text was not found.")

    candidates = page.locator(
        "article, li, tr, [role='row'], [role='listitem'], "
        ".card, [class*='card'], [class*='invite'], [class*='nvit']"
    )

    rows = []
    for i in range(min(await candidates.count(), 300)):
        try:
            txt = norm(await candidates.nth(i).inner_text(timeout=1500))
        except Exception:
            continue
        if 20 <= len(txt) <= 2500 and re.search(r"n.?vite|invite|recruit|job", txt, re.I):
            rows.append(txt)

    unique = []
    seen_text = set()
    for txt in rows:
        if txt.lower() not in seen_text:
            seen_text.add(txt.lower())
            unique.append(txt)

    invites = []
    for txt in unique:
        lines = [norm(x) for x in txt.splitlines() if norm(x)]
        title = lines[0] if lines else "NInvite"
        company = next((x for x in lines[1:] if "company" in x.lower()), "")
        location = next((x for x in lines[1:] if "location" in x.lower()), "")
        date = next(
            (x for x in lines if re.search(
                r"\b(today|yesterday|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
                x, re.I
            )), ""
        )
        recruiter = next(
            (x for x in lines if "recruit" in x.lower() or "hiring" in x.lower()),
            ""
        )

        invites.append(Invite(
            key=invite_key(title, company, location, date, txt),
            title=title[:300],
            company=company[:300],
            location=location[:300],
            date=date[:200],
            recruiter=recruiter[:300],
            details=txt[:2500],
            source_url=page.url,
        ))

    return invites


def send_email(invites: list[Invite]):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    if invites:
        subject = f"Naukri: {len(invites)} new NInvite(s)"
        body = ["New Naukri NInvite(s) found:", ""]
        for i, inv in enumerate(invites, 1):
            body += [
                f"{i}. {inv.title}",
                f"Company: {inv.company or 'Not detected'}",
                f"Location: {inv.location or 'Not detected'}",
                f"Date: {inv.date or 'Not detected'}",
                f"Recruiter: {inv.recruiter or 'Not detected'}",
                f"Details: {inv.details}",
                f"Link: {inv.source_url}",
                "",
            ]
    else:
        subject = "Naukri NInvite check: No new NInvite"
        body = ["No new NInvite was found during this check."]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = RECIPIENT
    msg.set_content("\n".join(body))

    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


async def main(discover_only=False):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await wait_for_login(page)

            if discover_only:
                await discover(page)
                return

            await open_ninvite(page)
            await page.wait_for_timeout(2000)

            invites = await extract_invites(page)
            seen = load_seen()
            new_invites = [x for x in invites if x.key not in seen]

            print(f"Found {len(invites)} invitation candidate(s); {len(new_invites)} new.")

            send_email(new_invites)

            seen.update(x.key for x in invites)
            save_seen(seen)
            print("Notification sent and state updated.")
        finally:
            await context.close()


if __name__ == "__main__":
    import sys
    asyncio.run(main("--discover" in sys.argv))

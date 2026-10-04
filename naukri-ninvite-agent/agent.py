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
STATE_FILE = Path(os.getenv("NAUKRI_JOBS_STATE_FILE", os.getenv("NINVITE_STATE_FILE", "data/seen_jobs.json")))
DEBUG_DIR = Path("data/debug")
RECIPIENT = os.getenv("NINVITE_EMAIL_TO", "Harsh.nid@gmail.com")


@dataclass
class Job:
    key: str
    title: str
    company: str
    location: str
    date: str
    details: str
    source_url: str


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def job_key(*parts: str) -> str:
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


async def open_jobs(page):
    await page.wait_for_timeout(2500)

    clicked = await click_text(page, [
        "Jobs",
        "Jobs & Responses",
        "My Jobs",
        "Job Recommendations",
    ])

    if clicked:
        print(f"Opened Naukri section: {clicked}")
        await page.wait_for_timeout(2500)
        return

    # Fallback: find a visible navigation link/button containing Jobs.
    for locator in [
        page.locator("a").filter(has_text=re.compile(r"^\s*jobs\s*$|jobs", re.I)),
        page.locator("button").filter(has_text=re.compile(r"^\s*jobs\s*$|jobs", re.I)),
        page.locator("[role='button']").filter(has_text=re.compile(r"jobs", re.I)),
    ]:
        count = await locator.count()
        for i in range(min(count, 15)):
            try:
                await locator.nth(i).click(timeout=4000)
                await page.wait_for_timeout(2500)
                return
            except Exception:
                pass

    await discover(page)
    raise RuntimeError(
        "Could not locate the Jobs option in your authenticated Naukri UI. "
        "Debug files were saved under data/debug/."
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


def parse_job_text(txt: str) -> tuple[str, str, str, str]:
    lines = [norm(x) for x in txt.splitlines() if norm(x)]

    title = lines[0] if lines else "Naukri Job"

    # Common Naukri card pattern: title, company, location, experience/salary,
    # then metadata. Keep extraction deliberately conservative.
    company = ""
    location = ""
    date = ""

    for line in lines[1:]:
        low = line.lower()
        if not company and (
            "company" in low
            or "technologies" in low
            or "limited" in low
            or "private" in low
        ):
            company = line
        if not location and (
            "bangalore" in low
            or "bengaluru" in low
            or "hyderabad" in low
            or "pune" in low
            or "mumbai" in low
            or "delhi" in low
            or "noida" in low
            or "gurgaon" in low
            or "remote" in low
            or "india" in low
        ):
            location = line
        if not date and re.search(
            r"\b(today|yesterday|\d+\s*days?\s*ago|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
            line,
            re.I,
        ):
            date = line

    return title[:300], company[:300], location[:300], date[:200]


async def extract_jobs(page) -> list[Job]:
    body_text = norm(await page.locator("body").inner_text())

    # Job cards vary across Naukri releases. Start with likely card/list containers
    # and retain only reasonably sized blocks containing job-like information.
    candidates = page.locator(
        "article, li, tr, [role='row'], [role='listitem'], "
        ".jobTuple, .job-tuple, .srpTuple, [class*='jobTuple'], "
        "[class*='job-tuple'], [class*='jobCard'], [class*='job-card'], "
        "[class*='joblist'], [class*='job-list']"
    )

    rows = []
    for i in range(min(await candidates.count(), 500)):
        try:
            txt = norm(await candidates.nth(i).inner_text(timeout=1500))
        except Exception:
            continue

        if not (30 <= len(txt) <= 3000):
            continue

        # Avoid nav/header/footer noise. Job cards usually contain at least
        # one job-oriented signal.
        if re.search(
            r"\b(experience|yrs?|salary|lpa|apply|job description|skills|location|"
            r"remote|work from home|posted|days? ago)\b",
            txt,
            re.I,
        ):
            rows.append(txt)

    unique = []
    seen_text = set()
    for txt in rows:
        normalized = txt.lower()
        if normalized not in seen_text:
            seen_text.add(normalized)
            unique.append(txt)

    jobs = []
    for txt in unique:
        title, company, location, date = parse_job_text(txt)
        if title.lower() in {"jobs", "job recommendations", "search jobs"}:
            continue

        jobs.append(Job(
            key=job_key(title, company, location, date, txt),
            title=title,
            company=company,
            location=location,
            date=date,
            details=txt[:3000],
            source_url=page.url,
        ))

    # If the page contains no detectable cards, save the authenticated page
    # rather than silently reporting zero jobs.
    if not jobs:
        await discover(page)
        raise RuntimeError(
            "The Jobs tab opened, but no job cards could be detected. "
            "Debug files were saved under data/debug/ so the selectors can be refined."
        )

    return jobs


def send_email(jobs: list[Job]):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    if jobs:
        subject = f"Naukri: {len(jobs)} new job(s)"
        body = ["New job listing(s) found in the Naukri Jobs tab:", ""]
        for i, job in enumerate(jobs, 1):
            body += [
                f"{i}. {job.title}",
                f"Company: {job.company or 'Not detected'}",
                f"Location: {job.location or 'Not detected'}",
                f"Posted: {job.date or 'Not detected'}",
                f"Details: {job.details}",
                f"Jobs page: {job.source_url}",
                "",
            ]
    else:
        subject = "Naukri Jobs check: No new jobs"
        body = ["No new jobs were found in the Naukri Jobs tab during this check."]

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

            await open_jobs(page)
            await page.wait_for_timeout(2000)

            jobs = await extract_jobs(page)
            seen = load_seen()
            new_jobs = [x for x in jobs if x.key not in seen]

            print(f"Found {len(jobs)} job listing(s); {len(new_jobs)} new.")

            # Only update state after email succeeds, so a failed notification
            # does not cause jobs to be lost on the next run.
            send_email(new_jobs)

            seen.update(x.key for x in jobs)
            save_seen(seen)
            print("Notification sent and state updated.")
        finally:
            await context.close()


if __name__ == "__main__":
    import sys
    asyncio.run(main("--discover" in sys.argv))

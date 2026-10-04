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
STATE_FILE = Path(
    os.getenv(
        "NAUKRI_JOBS_STATE_FILE",
        os.getenv("NINVITE_STATE_FILE", "data/seen_jobs.json"),
    )
)
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
    STATE_FILE.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "keys": sorted(keys),
            },
            indent=2,
        )
    )


async def click_text(page, candidates, exact=True):
    for candidate in candidates:
        locators = [
            page.get_by_text(candidate, exact=exact),
            page.get_by_role("link", name=re.compile(re.escape(candidate), re.I)),
            page.get_by_role("button", name=re.compile(re.escape(candidate), re.I)),
        ]
        for loc in locators:
            try:
                if await loc.count():
                    await loc.first.click(timeout=5000)
                    await page.wait_for_timeout(1800)
                    return candidate
            except Exception:
                pass
    return None


async def is_logged_in(page) -> bool:
    url = page.url.lower()
    if "/mnjuser/" in url:
        return True

    body = norm(await page.locator("body").inner_text(timeout=5000))
    lower = body.lower()

    # Public Naukri pages show a Login control. Authenticated pages show the
    # Jobs navigation/profile UI instead.
    login_count = await page.get_by_text("Login", exact=True).count()
    authenticated_signals = [
        "my profile",
        "recommended jobs for you",
        "profile performance",
        "application status",
        "saved jobs",
        "my home",
    ]
    return login_count == 0 and any(x in lower for x in authenticated_signals)


async def login_if_needed(page):
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)

    if await is_logged_in(page):
        print("Naukri session already logged in.")
        return

    # Follow the requested flow: open Naukri -> click Login.
    login = page.get_by_role("button", name=re.compile(r"^login$", re.I))
    if not await login.count():
        login = page.get_by_text("Login", exact=True)

    if not await login.count():
        await discover(page)
        raise RuntimeError(
            "Naukri opened but the Login control could not be found. "
            "Debug files were saved under data/debug/."
        )

    print("Clicking Login. Complete username/password, OTP and CAPTCHA manually if requested.")
    await login.first.click(timeout=10000)
    await page.wait_for_timeout(2000)

    # Never handle credentials, OTP or CAPTCHA automatically. Wait for the
    # user to complete authentication in the visible browser.
    for _ in range(180):
        if await is_logged_in(page):
            print("Naukri login successful.")
            return
        await asyncio.sleep(2)

    raise RuntimeError(
        "Timed out waiting for Naukri login. Complete the login/MFA in the browser "
        "and run the agent again."
    )


async def open_jobs_and_recommended(page):
    # The authenticated Naukri UI shown by the user has a Jobs menu that opens
    # a dropdown containing 'Recommended jobs', 'NVites', etc. We only use
    # Recommended jobs; NVites are intentionally ignored.
    await page.wait_for_timeout(1500)

    clicked = await click_text(page, ["Jobs"])
    if not clicked:
        # Fallback to a visible nav link/button named Jobs.
        loc = page.locator("a, button, [role='button']").filter(
            has_text=re.compile(r"^\s*Jobs\s*$", re.I)
        )
        if await loc.count():
            await loc.first.click(timeout=7000)
            await page.wait_for_timeout(1500)
        else:
            await discover(page)
            raise RuntimeError(
                "Could not click the Jobs option after login. "
                "Debug files were saved under data/debug/."
            )

    print("Clicked Jobs.")

    # Screenshot shows this dropdown:
    # Recommended jobs
    # NVites
    # Application status
    # Saved jobs
    recommended = await click_text(page, ["Recommended jobs"])
    if not recommended:
        # Try the exact URL only as a fallback after the user-visible click
        # failed. This keeps the normal workflow click-driven.
        try:
            await page.goto(
                "https://www.naukri.com/mnjuser/recommendedjobs",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2500)
        except Exception:
            await discover(page)
            raise RuntimeError(
                "Jobs menu opened but Recommended jobs could not be selected. "
                "Debug files were saved under data/debug/."
            )

    print("Opened Recommended jobs.")
    await page.wait_for_timeout(2500)

    if "/recommendedjobs" not in page.url.lower():
        # Some UI versions change the URL late. Give the page a little time.
        await page.wait_for_timeout(2500)

    if "recommendedjobs" not in page.url.lower():
        await discover(page)
        raise RuntimeError(
            "The Recommended jobs page did not open. Debug files were saved under data/debug/."
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


async def collect_job_blocks(page):
    # The Recommended Jobs screen contains individual clickable job titles.
    # Collect their nearest card-like ancestor so we capture all visible
    # metadata (company, experience, salary, location, posted date, skills).
    anchors = page.locator(
        "a[href*='job-listings'], a[href*='/job/'], a[href*='jobId=']"
    )
    count = await anchors.count()

    blocks = []
    seen_urls = set()

    for i in range(min(count, 500)):
        a = anchors.nth(i)
        try:
            href = await a.get_attribute("href")
            title = norm(await a.inner_text(timeout=1500))
        except Exception:
            continue

        if not href or not title:
            continue

        absolute = href if href.startswith("http") else "https://www.naukri.com" + href
        if absolute in seen_urls:
            continue
        seen_urls.add(absolute)

        # Walk upward to find a useful card-sized container.
        block_text = ""
        for level in range(2, 8):
            try:
                candidate = a.locator("/" + "/.." * level)
                txt = norm(await candidate.inner_text(timeout=1000))
                if 80 <= len(txt) <= 3500:
                    block_text = txt
                    break
            except Exception:
                pass

        if not block_text:
            try:
                block_text = norm(await a.locator("xpath=..").inner_text(timeout=1000))
            except Exception:
                continue

        if len(block_text) < 40:
            continue

        blocks.append((title, block_text, absolute))

    return blocks


async def extract_jobs(page) -> list[Job]:
    # First pass on the current page.
    blocks = await collect_job_blocks(page)

    # Naukri may lazy-load more recommendations while scrolling. Scroll in
    # controlled increments until the number of job links stops increasing.
    stable_rounds = 0
    previous_count = len(blocks)

    for _ in range(12):
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(1200)
        new_blocks = await collect_job_blocks(page)

        if len(new_blocks) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            blocks = new_blocks
            previous_count = len(new_blocks)

        if stable_rounds >= 2:
            break

    # Deduplicate by job URL.
    unique = {}
    for title, txt, url in blocks:
        unique[url] = (title, txt, url)

    jobs = []
    for title, txt, url in unique.values():
        lines = [norm(x) for x in txt.splitlines() if norm(x)]
        company = ""
        location = ""
        date = ""

        for line in lines[1:]:
            low = line.lower()

            if not company and (
                "company" in low
                or "technologies" in low
                or "systems" in low
                or "solutions" in low
                or "limited" in low
                or "private" in low
                or "group" in low
            ):
                company = line

            if not location and re.search(
                r"\b(bangalore|bengaluru|hyderabad|pune|mumbai|delhi|noida|"
                r"gurgaon|gurugram|chennai|remote|india|hybrid)\b",
                line,
                re.I,
            ):
                location = line

            if not date and re.search(
                r"\b(today|yesterday|\d+\s*days?\s*ago|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
                line,
                re.I,
            ):
                date = line

        jobs.append(
            Job(
                key=job_key(url, title, txt),
                title=title[:300],
                company=company[:300],
                location=location[:300],
                date=date[:200],
                details=txt[:3000],
                source_url=url,
            )
        )

    if not jobs:
        await discover(page)
        raise RuntimeError(
            "Recommended jobs opened, but no job listing links/cards were detected. "
            "Debug files were saved under data/debug/."
        )

    return jobs


def send_email(jobs: list[Job]):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    if jobs:
        subject = f"Naukri: {len(jobs)} job(s) in Recommended Jobs"
        body = [
            f"Naukri Recommended Jobs: {len(jobs)} job(s) found.",
            "",
        ]

        for i, job in enumerate(jobs, 1):
            body += [
                f"{i}. {job.title}",
                f"Company: {job.company or 'Not detected'}",
                f"Location: {job.location or 'Not detected'}",
                f"Posted: {job.date or 'Not detected'}",
                f"Details: {job.details}",
                f"Job link: {job.source_url}",
                "",
                "-" * 80,
                "",
            ]
    else:
        subject = "Naukri Jobs check: No jobs found"
        body = ["No jobs were found in Naukri Recommended Jobs during this check."]

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
            await login_if_needed(page)

            if discover_only:
                await discover(page)
                return

            await open_jobs_and_recommended(page)

            jobs = await extract_jobs(page)
            seen = load_seen()
            new_jobs = [x for x in jobs if x.key not in seen]

            print(f"Found {len(jobs)} job listing(s); {len(new_jobs)} new.")

            # Send the current complete list on the first run. On later runs,
            # send only jobs not previously seen.
            send_email(new_jobs if seen else jobs)

            seen.update(x.key for x in jobs)
            save_seen(seen)
            print("Email notification sent and job state updated.")
        finally:
            await context.close()


if __name__ == "__main__":
    import sys

    asyncio.run(main("--discover" in sys.argv))

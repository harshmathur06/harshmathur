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
from typing import Optional

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


def load_seen() -> set:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()).get("keys", []))
    except Exception:
        return set()


def save_seen(keys: set) -> None:
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
    await page.wait_for_timeout(1500)

    clicked = await click_text(page, ["Jobs"])
    if not clicked:
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

    recommended = await click_text(page, ["Recommended jobs"])
    if not recommended:
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


def looks_like_job_card(text: str) -> bool:
    low = text.lower()

    has_posted = bool(
        re.search(
            r"\b(today|yesterday|\d+\s*days?\s*ago|\d+\s*day\s*ago)\b",
            low,
        )
    )
    has_experience = bool(
        re.search(r"\b\d+\s*[-–]\s*\d+\s*yrs?\b|\byrs?\b", low)
    )
    has_salary = bool(
        re.search(r"\b(lpa|lakhs?|lacs?|pa)\b|₹|rs\.?\s*\d", low)
    )
    has_job_signal = bool(
        re.search(
            r"\b(product manager|program manager|business analyst|software|"
            r"engineer|developer|consultant|analyst|manager|architect|designer|"
            r"sales|marketing|finance|hr)\b",
            low,
        )
    )

    return len(text) >= 80 and has_posted and (
        has_experience or has_salary or has_job_signal
    )


async def collect_job_blocks(page):
    candidates = page.locator(
        "article, li, [role='listitem'], [role='article'], "
        "div[class*='job'], div[class*='Job'], div[class*='card'], div[class*='Card'], "
        "div[class*='tuple'], div[class*='Tuple']"
    )

    count = await candidates.count()
    blocks = []
    seen = set()

    for i in range(min(count, 1200)):
        try:
            txt = norm(await candidates.nth(i).inner_text(timeout=1000))
        except Exception:
            continue

        if not looks_like_job_card(txt):
            continue

        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)

        source_url = page.url
        try:
            link = candidates.nth(i).locator("a").first
            if await link.count():
                href = await link.get_attribute("href")
                if href:
                    source_url = (
                        href if href.startswith("http")
                        else "https://www.naukri.com" + href
                    )
        except Exception:
            pass

        blocks.append((txt, source_url))

    if not blocks:
        all_divs = page.locator("div")
        count = await all_divs.count()
        for i in range(min(count, 2500)):
            try:
                txt = norm(await all_divs.nth(i).inner_text(timeout=700))
            except Exception:
                continue

            if not (80 <= len(txt) <= 1600) or not looks_like_job_card(txt):
                continue

            key = txt.lower()
            if key in seen:
                continue
            seen.add(key)

            source_url = page.url
            try:
                links = all_divs.nth(i).locator("a")
                if await links.count():
                    href = await links.first.get_attribute("href")
                    if href:
                        source_url = (
                            href if href.startswith("http")
                            else "https://www.naukri.com" + href
                        )
            except Exception:
                pass

            blocks.append((txt, source_url))

    return blocks


def parse_job_card(txt: str, source_url: str) -> Optional[Job]:
    lines = [norm(x) for x in txt.splitlines() if norm(x)]
    if not lines:
        return None

    ignored = {"hide", "save", "apply", "view all"}
    title = next((x for x in lines if x.lower() not in ignored), lines[0])

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
            or "posted by" in low
        ):
            company = re.sub(r"^posted by\s*", "", line, flags=re.I)

        if not location and re.search(
            r"\b(bangalore|bengaluru|hyderabad|pune|mumbai|delhi|noida|"
            r"gurgaon|gurugram|chennai|remote|india|hybrid)\b",
            line,
            re.I,
        ):
            location = line

        if not date and re.search(
            r"\b(today|yesterday|\d+\s*days?\s*ago|\d+\s*day\s*ago|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b",
            line,
            re.I,
        ):
            date = line

    return Job(
        key=job_key(source_url, title, txt),
        title=title[:300],
        company=company[:300],
        location=location[:300],
        date=date[:200],
        details=txt[:3000],
        source_url=source_url,
    )


async def extract_jobs(page) -> list:
    all_blocks = []

    for _ in range(12):
        blocks = await collect_job_blocks(page)
        all_blocks.extend(blocks)
        await page.mouse.wheel(0, 850)
        await page.wait_for_timeout(1200)

    unique = {}
    for txt, url in all_blocks:
        key = (norm(txt).lower(), url)
        unique[key] = (txt, url)

    jobs = []
    for txt, url in unique.values():
        job = parse_job_card(txt, url)
        if job:
            jobs.append(job)

    deduped = {}
    for job in jobs:
        deduped[job.key] = job

    jobs = list(deduped.values())

    if not jobs:
        await discover(page)
        raise RuntimeError(
            "Recommended jobs opened, but no job cards were detected. "
            "The agent now captures debug files for selector refinement."
        )

    return jobs


def send_email(jobs: list):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    if jobs:
        subject = f"Naukri: {len(jobs)} job(s) in Recommended Jobs"
        body = [f"Naukri Recommended Jobs: {len(jobs)} job(s) found.", ""]

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

            send_email(new_jobs if seen else jobs)

            seen.update(x.key for x in jobs)
            save_seen(seen)
            print("Email notification sent and job state updated.")
        finally:
            await context.close()


if __name__ == "__main__":
    import sys

    asyncio.run(main("--discover" in sys.argv))

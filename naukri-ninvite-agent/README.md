# Naukri NInvite Agent

A browser agent that opens Naukri, uses a persistent browser profile, navigates to NInvite, detects invitations not seen before, and emails a notification.

## Security

- Never put your Naukri password, OTP, cookies, or Gmail App Password into GitHub.
- The first run is human-assisted: the browser opens and you complete Naukri login/MFA yourself.
- The browser profile is stored locally and ignored by Git.
- Naukri's authenticated UI can change, so the first discovery run captures the live page structure.

## Mac setup

    cd naukri-ninvite-agent
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python -m playwright install chromium

Create a local .env from .env.example.

First discovery run:

    python agent.py --discover

The browser opens. Complete Naukri login. Diagnostic files are saved under data/debug/.

Normal run:

    python agent.py

## Gmail

Use a Gmail App Password if the sending account has 2-Step Verification enabled. Do not use your normal Gmail password.

## Behavior

- Opens Naukri.
- Reuses a local browser profile.
- Finds Jobs / Jobs & Responses and NInvite/NVite.
- Extracts visible invitation rows/cards.
- Creates a local fingerprint for each invitation.
- Emails only invitations not seen before.
- Sends a one-line "No new NInvite" email when there are no new invitations.
- Saves seen state only after successful email delivery.

## Next hardening step

After the first discovery run, the live DOM can be refined to exact selectors from your account. This avoids relying on unstable private CSS classes.

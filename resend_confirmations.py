"""
resend_confirmations.py — One-time script to send confirmation emails
to families whose 2026-2027 registration is Complete but never received
the confirmation email.

Usage:
    Set environment variables then run:
    MAIL_PASSWORD=<brevo_api_key> MAIL_SENDER=<from_email> python3 resend_confirmations.py

    Or on Railway shell:
    python3 resend_confirmations.py
"""
import os, json, urllib.request, time

# ── Brevo email helper (same as the app) ──────────────────────────────────────
def send_email(to_addr, subject, text_body, html_body):
    api_key = os.environ.get('MAIL_PASSWORD', '')
    sender  = os.environ.get('MAIL_SENDER', '')
    if not api_key or not sender:
        print(f'  ❌  Missing MAIL_PASSWORD or MAIL_SENDER env vars')
        return False
    try:
        payload = json.dumps({
            "sender":      {"name": "Monmouth Fidelity Chinese School", "email": sender},
            "to":          [{"email": to_addr}],
            "subject":     subject,
            "textContent": text_body,
            "htmlContent": html_body
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.brevo.com/v3/smtp/email',
            data=payload,
            headers={'Content-Type': 'application/json', 'api-key': api_key},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f'  ✅  Sent to {to_addr} (status {resp.status})')
        return True
    except Exception as e:
        print(f'  ❌  Failed to send to {to_addr}: {e}')
        return False

# ── Families from the CSV (2026-2027 Complete Registration) ───────────────────
FAMILIES = [
    {"email": "huynhsusie@gmail.com",        "first": "Susie",     "last": "Ao"},
    {"email": "samuelmbourne@gmail.com",      "first": "Sam",       "last": "Bourne"},
    {"email": "seafoodhoi@gmail.com",         "first": "Gloria",    "last": "Chan"},
    {"email": "rchen69412@gmail.com",         "first": "Roger",     "last": "Chen"},
    {"email": "christine.l.cheuk@gmail.com",  "first": "Christine", "last": "Cheuk"},
    {"email": "alexleonardoching@gmail.com",  "first": "Alex",      "last": "Ching"},
    {"email": "laura.fong@gmail.com",         "first": "Laura",     "last": "Fong"},
    {"email": "charity94@gmail.com",          "first": "Richard",   "last": "Fong"},
    {"email": "moniefung@gmail.com",          "first": "Hoi",       "last": "Fung"},
    {"email": "mingwha@gmail.com",            "first": "Vivian",    "last": "Ha"},
    {"email": "sammi1981@gmail.com",          "first": "Sammi",     "last": "Ho"},
    {"email": "jason_thorpe@hotmail.com",     "first": "Thorpe",    "last": "Jason"},
    {"email": "freebluemind@gmail.com",       "first": "Angela",    "last": "Kwok"},
    {"email": "pat@patlee.org",               "first": "Patrick",   "last": "Lee"},
    {"email": "dayton@gmail.com",             "first": "Dayton",    "last": "Leong"},
    {"email": "ada_sukping_li@yahoo.com",     "first": "Ada",       "last": "Li"},
    {"email": "boningli@gmail.com",           "first": "Boning",    "last": "Li"},
    {"email": "lizziellz.me@gmail.com",       "first": "Lizzie",    "last": "Li"},
    {"email": "mei.w.li@gmail.com",           "first": "Mei",       "last": "Li"},
    {"email": "jenjifer@gmail.com",           "first": "Jennifer",  "last": "Lin"},
    {"email": "melchanly@gmail.com",          "first": "Bill",      "last": "Ly"},
    {"email": "yip217@gmail.com",             "first": "Susanne",   "last": "Molnar"},
    {"email": "nguyenhoangchip@gmail.com",    "first": "Anh",       "last": "Nguyen"},
    {"email": "Shirley.w.nunez@gmail.com",    "first": "Shirley",   "last": "Nunez"},
    {"email": "quonrebecca@gmail.com",        "first": "Rebecca",   "last": "Quon"},
    {"email": "cannyx482@yahoo.com",          "first": "Donnie",    "last": "Sam"},
    {"email": "jonathan.b.sy@gmail.com",      "first": "Jonathan",  "last": "Sy"},
    {"email": "azndrrgn@yahoo.com",           "first": "Jeff",      "last": "Sze"},
    {"email": "joannestse@gmail.com",         "first": "Joanne",    "last": "Tse"},
    {"email": "taryn.tsien@gmail.com",        "first": "Taryn",     "last": "Tsien"},
    {"email": "megan.tsoi@gmail.com",         "first": "Megan",     "last": "Tsoi"},
    {"email": "For.wyattlau@gmail.com",       "first": "Mary",      "last": "Tsui"},
    {"email": "jennifer.s.wu@gmail.com",      "first": "Jennifer",  "last": "Wu-Chiu"},
    {"email": "eileenyu@gmail.com",           "first": "Eileen",    "last": "Yu"},
    {"email": "maryq912@gmail.com",           "first": "Mary",      "last": "Zhang"},
]

PERIOD = "2026-2027"

def make_email(first, last):
    subject = "MFCS — Payment Confirmed & Registration Complete"
    text_body = (
        f"Dear {first} {last},\n\n"
        f"Your payment has been confirmed and your registration "
        f"for {PERIOD} is now complete.\n\n"
        f"Thank you for registering with Monmouth Fidelity Chinese School.\n\n"
        f"Monmouth Fidelity Chinese School"
    )
    html_body = (
        f"<p>Dear {first} {last},</p>"
        f"<p>Your payment has been confirmed and your registration for "
        f"<strong>{PERIOD}</strong> is now complete.</p>"
        f"<p>Thank you for registering with Monmouth Fidelity Chinese School. "
        f"We look forward to seeing your family!</p>"
        f"<p>Monmouth Fidelity Chinese School</p>"
    )
    return subject, text_body, html_body

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"Sending confirmation emails for {PERIOD}")
    print(f"Total families: {len(FAMILIES)}")
    print(f"Sender: {os.environ.get('MAIL_SENDER', '(not set)')}")
    print("-" * 50)

    sent = 0; failed = 0
    for f in FAMILIES:
        print(f"{f['last']}, {f['first']} <{f['email']}>")
        subject, text_body, html_body = make_email(f['first'].strip(), f['last'].strip())
        if send_email(f['email'].strip(), subject, text_body, html_body):
            sent += 1
        else:
            failed += 1
        time.sleep(0.3)  # avoid Brevo rate limit

    print("-" * 50)
    print(f"Done. Sent: {sent}  Failed: {failed}")

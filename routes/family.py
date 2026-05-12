"""
routes/family.py
Family-facing routes. Queries legacy DB tables directly.

Fee calculation rules (per student):
  age >= 18 : culture class fee only
  age <  18 : period.tuition + culture class fee
  Language class selection — no extra fee, included in tuition (minors only)

Family-level fees added once per registration:
  + period.registration_fee   (one-time per family per period)
  + period.pa_assignment_deposit  (refundable PA duty deposit)
"""
import os
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date

from flask import (Blueprint, render_template, redirect, url_for,
                   request, flash)
from flask_login import login_user, logout_user, login_required, current_user
import bcrypt

from address_helpers import address_is_valid

from db import get_db_connection
from models import Family

def _send_email(to_addr, subject, text_body, html_body):
    """Send an email via Brevo API (avoids SMTP port blocking on Railway)."""
    import urllib.request, json
    try:
        api_key = os.environ.get('MAIL_PASSWORD', '')
        sender  = os.environ.get('MAIL_SENDER', '')

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
            headers={
                'Content-Type': 'application/json',
                'api-key': api_key
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f'EMAIL DEBUG: sent to {to_addr}, status={resp.status}', flush=True)
        return True
    except Exception as e:
        print(f'EMAIL ERROR: {e}', flush=True)
        return False


family_bp = Blueprint('family', __name__)


# ── helpers ────────────────────────────────────────────────────────────────────

def _hash(plain):
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()

def _check(plain, stored):
    if not plain or not stored:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), stored.encode())
    except Exception:
        pass
    return plain == stored

def _today_eastern():
    """Return today's date in US Eastern time (avoids UTC off-by-one at night)."""
    try:
        import pytz
        from datetime import datetime as dt
        return dt.now(pytz.timezone('America/New_York')).date()
    except ImportError:
        from datetime import datetime as dt, timezone, timedelta
        return dt.now(timezone(timedelta(hours=-4))).date()  # approximate EDT
    """Return age in years from a date/datetime object, or None.
    Returns None for missing/sentinel dates (None, 2999-01-01)."""
    if not birthday:
        return None
    today = _today_eastern()
    try:
        bd = birthday if isinstance(birthday, date) else birthday.date()
        # Treat legacy sentinel 2999-01-01 as unknown
        if bd.year >= 2999:
            return None
        return today.year - bd.year - (
            (today.month, today.day) < (bd.month, bd.day)
        )
    except Exception:
        return None

def _is_adult(student):
    # Use explicit is_adult flag if set, otherwise fall back to birthday
    if student.get('is_adult') is not None and student.get('is_adult') != '':
        return bool(int(student.get('is_adult', 0)))
    return _age(student.get('birthday')) is not None and \
           _age(student.get('birthday')) >= 18


def _validate_phone(val):
    """Return error string if phone is invalid, else None.
    Optional field — blank/'?' is allowed. If provided, must have 7-15 digits."""
    if not val or val in ('', '?'): return None
    import re as _re
    digits = _re.sub(r'\D', '', val)
    if len(digits) < 7:
        return 'Phone number must contain at least 7 digits.'
    if len(digits) > 15:
        return 'Phone number is too long (max 15 digits).'
    return None

def _calc_student_fee(student, period, cult_fee, cult_fee2=0, tuition_override=None,
                      cult_discount=0, cult_discount2=0):
    """
    Per-student fee:
      adult (is_adult=1) → culture fees only (up to 2 classes)
        - if mfcs_affiliation='Y' → apply class-level discount per culture class
      minor (is_adult=0)  → tuition + culture fees (up to 2 classes)
    tuition_override: use this instead of period.tuition (for grandfathered rate)
    cult_discount/cult_discount2: per-class MFCS discount amount
    """
    if tuition_override is not None:
        tuition = float(tuition_override)
    else:
        tuition = float(period.get('tuition') or 0)

    # Apply MFCS affiliation discount to culture fees for affiliated adults
    mfcs = student.get('mfcs_affiliation') == 'Y'
    cf1 = max(0, float(cult_fee  or 0) - (float(cult_discount  or 0) if mfcs else 0))
    cf2 = max(0, float(cult_fee2 or 0) - (float(cult_discount2 or 0) if mfcs else 0))
    culture = cf1 + cf2

    if _is_adult(student):
        return culture
    else:
        return tuition + culture


def _effective_tuition(period, family_id, conn):
    """
    Determine the effective tuition for a family registering for a period.
    Rules:
      1. If family_record has tuition_override set → use that
      2. If no grandfathered_tuition set → use period.tuition
      3. Returning family + today <= grandfathered_deadline → grandfathered_tuition
      4. Otherwise → period.tuition
    """
    g_tuition   = period.get('grandfathered_tuition')
    g_deadline  = period.get('grandfathered_deadline')
    std_tuition = float(period.get('tuition') or 0)
    gran_tuition = float(g_tuition) if g_tuition else std_tuition

    # Check for manual finance override first
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT tuition_override FROM family_record WHERE fid=%s AND pid=%s",
            (family_id, period['id'])
        )
        fpr = cur.fetchone()
        if fpr and fpr.get('tuition_override') == 'standard':
            return std_tuition, 'standard (finance override)'
        if fpr and fpr.get('tuition_override') == 'grandfathered':
            return gran_tuition, 'grandfathered (finance override)'
    except Exception:
        pass

    if not g_tuition:
        return std_tuition, 'standard'

    # Check if returning family
    cur.execute(
        "SELECT COUNT(*) AS n FROM family_record "
        "WHERE fid=%s AND pid != %s",
        (family_id, period['id'])
    )
    row = cur.fetchone()
    is_existing = (row['n'] > 0) if row else False

    if not is_existing:
        return std_tuition, 'standard (new family)'

    # Returning family — check grandfathered deadline
    today = _today_eastern()
    if g_deadline:
        try:
            if isinstance(g_deadline, str):
                from datetime import datetime as dt
                deadline = dt.strptime(g_deadline, '%Y-%m-%d').date()
            elif hasattr(g_deadline, 'date'):
                deadline = g_deadline.date()  # datetime → date
            else:
                deadline = g_deadline  # already a date
            if today <= deadline:
                return gran_tuition, 'grandfathered'
        except Exception as e:
            print(f"_effective_tuition deadline error: {e} g_deadline={g_deadline!r}", flush=True)

    return std_tuition, 'standard (past deadline)'

def _is_late(period):
    """Return True if today is past the payment deadline."""
    deadline = period.get('deadline')
    if not deadline:
        return False
    try:
        if isinstance(deadline, str):
            from datetime import datetime as dt
            deadline_date = dt.strptime(deadline, '%Y-%m-%d').date()
        elif hasattr(deadline, 'year'):
            deadline_date = deadline if isinstance(deadline, date) else deadline.date()
        else:
            return False
        return _today_eastern() > deadline_date
    except Exception as e:
        print(f'_is_late error: {e}', flush=True)
        return False


def _is_returning_family(family_id, current_pid, conn):
    """Return True if the family has a family_record in any previous period."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT COUNT(*) AS n FROM family_record WHERE fid=%s AND pid != %s",
        (family_id, current_pid)
    )
    row = cur.fetchone()
    return (row['n'] > 0) if row else False


def _should_charge_late_fee(period, family_id, current_pid, total_paid, late_fee_waived, conn,
                            first_payment_date=None):
    """
    Returns (charge_late, minor_late_fee) where:
      charge_late     = True if late fee should be applied
      minor_late_fee  = per-minor late fee amount
    Rules:
      1. New family → no late fee
      2. Returning family + first_payment_date IS NULL → late fee
      3. Returning family + first_payment_date <= deadline → no late fee
      4. Returning family + first_payment_date > deadline → late fee
      5. Finance waived → no late fee
    """
    if late_fee_waived:
        return False, 0.0
    if not _is_late(period):
        return False, 0.0
    if conn is None:
        return False, 0.0
    if not _is_returning_family(family_id, current_pid, conn):
        return False, 0.0

    # Get payment deadline
    deadline = period.get('deadline')
    if not deadline:
        return True, float(period.get('late_fee') or 0)

    try:
        if isinstance(deadline, str):
            from datetime import datetime as dt
            deadline_date = dt.strptime(deadline, '%Y-%m-%d').date()
        else:
            deadline_date = deadline if isinstance(deadline, date) else deadline.date()

        if first_payment_date:
            if isinstance(first_payment_date, str):
                from datetime import datetime as dt2
                pd = dt2.strptime(first_payment_date, '%Y-%m-%d').date()
            elif hasattr(first_payment_date, 'year'):
                pd = first_payment_date if isinstance(first_payment_date, date) else first_payment_date.date()
            else:
                pd = None
            if pd and pd <= deadline_date:
                return False, 0.0  # Paid on time
        # No payment date or paid late
        return True, float(period.get('late_fee') or 0)
    except Exception:
        return True, float(period.get('late_fee') or 0)


def _calc_total_family_fee(student_subtotal, period, minor_count=0, late_fee=0.0):
    """
    Add once-per-family fees:
      + registration_fee        (only if family has minors)
      + pa_assignment_deposit   (only if family has minors)
      + late_fee                (per minor, for returning families past payment deadline)
      - period.discount per minor beyond the first 2 (multi-kid discount)
    Adult-only families are exempt from registration fee and PA deposit.
    """
    reg_fee           = float(period.get('registration_fee') or 0) if minor_count > 0 else 0.0
    pa_fee            = float(period.get('pa_assignment_deposit') or 0) if minor_count > 0 else 0.0
    per_kid_discount  = float(period.get('discount') or 0)
    additional_kids   = max(0, minor_count - 2)
    multi_kid_discount = additional_kids * per_kid_discount
    return student_subtotal + reg_fee + pa_fee + late_fee - multi_kid_discount


# ── login / logout ─────────────────────────────────────────────────────────────

@family_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('family.dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM family WHERE LOWER(primary_email) = %s", (email,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            pw_hash = row.get('password_hash', '')
            pw_plain = row.get('password', '')
            stored = pw_hash if pw_hash and pw_hash != '?' else pw_plain
            if _check(password, stored):
                # Block login if email not verified
                if row.get('email_verified') == 0:
                    flash('Please verify your email address before logging in. '
                          'Check your inbox (and spam folder) for the verification link.', 'warning')
                    return render_template('family/login.html', show_resend=True, email=email)
                login_user(Family(row), remember=remember)
                # Redirect to profile if address not yet verified
                if not Family(row).address_verified:
                    flash('Please update your home address — it is now required.', 'warning')
                    return redirect(url_for('family.profile'))
                return redirect(url_for('family.dashboard'))
        flash('Incorrect email or password. Please try again.', 'error')
    return render_template('family/login.html')


@family_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('family.login'))


# ── forgot / reset password ────────────────────────────────────────────────────

@family_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        print(f'FORGOT_PW DEBUG: received request for email={email}', flush=True)
        conn  = get_db_connection()
        cur   = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM family WHERE LOWER(primary_email) = %s", (email,))
        row = cur.fetchone()
        print(f'FORGOT_PW DEBUG: row found={row is not None}', flush=True)
        if row:
            token      = secrets.token_urlsafe(32)
            expires_at = datetime.now() + timedelta(hours=2)
            cur.execute(
                "INSERT INTO password_reset_tokens (family_id, token, expires_at) "
                "VALUES (%s, %s, %s)", (row['id'], token, expires_at)
            )
            conn.commit()
            reset_url = url_for('family.reset_password', token=token, _external=True)
            _send_email(
                to_addr=email,
                subject='MFCS — Password Reset Request',
                text_body=(
                    f"Hello,\n\nWe received a request to reset your password "
                    f"for your MFCS family account.\n\n"
                    f"Click the link below to reset your password (valid for 2 hours):\n"
                    f"{reset_url}\n\n"
                    f"If you did not request a password reset, please ignore this email.\n\n"
                    f"Monmouth Fidelity Chinese School"
                ),
                html_body=(
                    f"<p>Hello,</p>"
                    f"<p>We received a request to reset your password for your MFCS family account.</p>"
                    f"<p style='text-align:center;margin:24px 0'>"
                    f"<a href='{reset_url}' style='background:#c0392b;color:#fff;padding:12px 28px;"
                    f"border-radius:6px;text-decoration:none;font-weight:bold'>Reset My Password</a></p>"
                    f"<p>Or copy this link: <a href='{reset_url}'>{reset_url}</a></p>"
                    f"<p>If you did not request this, please ignore this email.</p>"
                    f"<p>Monmouth Fidelity Chinese School</p>"
                )
            )
        conn.close()
        flash('If that email is on file you will receive a reset link shortly.', 'info')
        return redirect(url_for('family.login'))
    return render_template('family/forgot_password.html')


@family_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM password_reset_tokens "
        "WHERE token = %s AND used = 0 AND expires_at > NOW()", (token,)
    )
    record = cur.fetchone()
    if not record:
        conn.close()
        flash('This reset link is invalid or has expired.', 'error')
        return redirect(url_for('family.forgot_password'))
    if request.method == 'POST':
        pw1 = request.form.get('password', '')
        pw2 = request.form.get('confirm_password', '')
        if pw1 != pw2 or len(pw1) < 8:
            flash('Passwords must match and be at least 8 characters.', 'error')
        else:
            cur.execute("UPDATE family SET password_hash = %s WHERE id = %s",
                        (_hash(pw1), record['family_id']))
            cur.execute("UPDATE password_reset_tokens SET used = 1 WHERE id = %s",
                        (record['id'],))
            conn.commit()
            conn.close()
            flash('Password updated. Please log in.', 'success')
            return redirect(url_for('family.login'))
    conn.close()
    return render_template('family/reset_password.html', token=token)




# ── self registration (new family account) ────────────────────────────────────

@family_bp.route('/register-account', methods=['GET', 'POST'])
def register_account():
    """Allow new families to create their own account."""
    if current_user.is_authenticated:
        return redirect(url_for('family.dashboard'))
    if request.method == 'POST':
        f      = request.form
        email  = f.get('primary_email','').strip().lower()
        pw     = f.get('password','')
        pw2    = f.get('confirm_password','')
        phone  = f.get('primary_phone','').strip()
        first  = f.get('first_name_0','').strip()
        last   = f.get('last_name_0','').strip()
        field_errors = {}
        if not first:   field_errors['first_name_0']   = 'First name is required.'
        if not last:    field_errors['last_name_0']    = 'Last name is required.'
        if not email:   field_errors['primary_email']  = 'Email is required.'
        if not phone:   field_errors['primary_phone']  = 'Phone is required.'
        if not pw:      field_errors['password']       = 'Password is required.'
        elif len(pw) < 8: field_errors['password']     = 'Password must be at least 8 characters.'
        elif pw != pw2: field_errors['confirm_password']= 'Passwords do not match.'
        street = f.get('street_address', '').strip()
        city   = f.get('city', '').strip()
        state  = f.get('state', '').strip()
        zip_   = f.get('zip', '').strip()
        if not address_is_valid(street, city, state, zip_):
            field_errors['street_address'] = 'A valid street address, city, state, and 5-digit ZIP are required.'
        if field_errors:
            return render_template('family/register_account.html',
                                   field_errors=field_errors, form=f)
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM family WHERE LOWER(primary_email)=%s",(email,))
        if cur.fetchone():
            conn.close()
            return render_template('family/register_account.html',
                                   field_errors={'primary_email': 'An account with that email already exists.'},
                                   form=f)

        # Generate email verification token
        verify_token = secrets.token_urlsafe(32)

        cur2 = conn.cursor()
        cur2.execute("""INSERT INTO family
            (primary_email,password,password_hash,
             last_name_0,first_name_0,last_name_1,first_name_1,
             primary_phone,street_address,city,state,zip,
             address_verified,email_verified,email_verify_token)
            VALUES(%s,'',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,%s)""",
            (email,_hash(pw),last,first,
             f.get('last_name_1', '?'),f.get('first_name_1', '?'),
             phone, street, city, state, zip_, verify_token))
        conn.commit()

        # Send verification email
        verify_url = url_for('family.verify_email', token=verify_token, _external=True)
        _send_email(
            to_addr=email,
            subject='MFCS — Please verify your email address',
            text_body=(
                f"Dear {first} {last},\n\n"
                f"Thank you for creating an account with Monmouth Fidelity Chinese School.\n\n"
                f"Please click the link below to verify your email address:\n"
                f"{verify_url}\n\n"
                f"This link will expire in 24 hours.\n\n"
                f"If you did not create this account, please ignore this email.\n\n"
                f"NOTE: If you do not see this email, please check your spam or junk folder.\n\n"
                f"Monmouth Fidelity Chinese School"
            ),
            html_body=(
                f"<p>Dear {first} {last},</p>"
                f"<p>Thank you for creating an account with Monmouth Fidelity Chinese School.</p>"
                f"<p>Please click the button below to verify your email address:</p>"
                f"<p style='text-align:center;margin:24px 0'>"
                f"<a href='{verify_url}' style='background:#c0392b;color:#fff;padding:12px 28px;"
                f"border-radius:6px;text-decoration:none;font-weight:bold'>Verify My Email</a></p>"
                f"<p>Or copy and paste this link: <a href='{verify_url}'>{verify_url}</a></p>"
                f"<p>This link will expire in 24 hours.</p>"
                f"<p style='color:#e74c3c'><strong>NOTE:</strong> If you do not see this email "
                f"in your inbox, please check your <strong>spam or junk folder</strong>.</p>"
                f"<p>Monmouth Fidelity Chinese School</p>"
            )
        )
        conn.close()
        return render_template('family/verify_email_sent.html', email=email)
    return render_template('family/register_account.html')
@family_bp.route('/verify-email/<token>')
def verify_email(token):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM family WHERE email_verify_token=%s", (token,))
    row = cur.fetchone()
    if not row:
        conn.close()
        flash('This verification link is invalid or has already been used.', 'error')
        return redirect(url_for('family.login'))
    cur2 = conn.cursor()
    cur2.execute(
        "UPDATE family SET email_verified=1, email_verify_token=NULL WHERE id=%s",
        (row['id'],)
    )
    conn.commit(); conn.close()
    login_user(Family(row))
    flash('Your email has been verified! Welcome to MFCS.', 'success')
    return redirect(url_for('family.dashboard'))


@family_bp.route('/resend-verification', methods=['GET', 'POST'])
def resend_verification():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn  = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM family WHERE LOWER(primary_email)=%s AND email_verified=0",
            (email,)
        )
        row = cur.fetchone()
        if row:
            verify_token = secrets.token_urlsafe(32)
            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE family SET email_verify_token=%s WHERE id=%s",
                (verify_token, row['id'])
            )
            conn.commit()
            verify_url = url_for('family.verify_email', token=verify_token, _external=True)
            _send_email(
                to_addr=email,
                subject='MFCS — Email Verification (Resent)',
                text_body=(
                    f"Dear {row['first_name_0']} {row['last_name_0']},\n\n"
                    f"Here is your new email verification link:\n{verify_url}\n\n"
                    f"NOTE: If you do not see this email, please check your spam or junk folder.\n\n"
                    f"Monmouth Fidelity Chinese School"
                ),
                html_body=(
                    f"<p>Dear {row['first_name_0']} {row['last_name_0']},</p>"
                    f"<p>Here is your new email verification link:</p>"
                    f"<p style='text-align:center;margin:24px 0'>"
                    f"<a href='{verify_url}' style='background:#c0392b;color:#fff;padding:12px 28px;"
                    f"border-radius:6px;text-decoration:none;font-weight:bold'>Verify My Email</a></p>"
                    f"<p style='color:#e74c3c'><strong>NOTE:</strong> If you do not see this email "
                    f"in your inbox, please check your <strong>spam or junk folder</strong>.</p>"
                    f"<p>Monmouth Fidelity Chinese School</p>"
                )
            )
        conn.close()
        flash('If that email has a pending verification, a new link has been sent.', 'info')
        return redirect(url_for('family.login'))
    return render_template('family/resend_verification.html')



@family_bp.route('/dashboard')
@login_required
def dashboard():
    from models import Family
    if isinstance(current_user, Family) and not current_user.address_verified:
        flash('Please update your home address before continuing.', 'warning')
        return redirect(url_for('family.profile'))
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM student WHERE fid = %s ORDER BY last_name, first_name",
        (current_user.id,)
    )
    students = cur.fetchall()
    cur.execute("SELECT * FROM period ORDER BY id DESC LIMIT 1")
    period = cur.fetchone()
    fpr = None
    if period:
        cur.execute(
            "SELECT * FROM family_record WHERE fid = %s AND pid = %s",
            (current_user.id, period['id'])
        )
        fpr = cur.fetchone()
    registrations = []
    if period and students:
        cur.execute(
            """
            SELECT sr.sid,
                   s.first_name, s.last_name,
                   lc.name AS lang_class_name,
                   cc.name AS cult_class_name
            FROM   student_record sr
            JOIN   student s ON s.id = sr.sid
            LEFT   JOIN class_group_record lc ON lc.id = sr.lcgrid
            LEFT   JOIN class_group_record cc ON cc.id = sr.ccgrid
            WHERE  s.fid = %s AND sr.pid = %s
            """,
            (current_user.id, period['id'])
        )
        registrations = cur.fetchall()
    conn.close()
    return render_template('family/dashboard.html',
                           students=students, period=period,
                           fpr=fpr, registrations=registrations)


# ── family profile ─────────────────────────────────────────────────────────────

@family_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        f      = request.form
        npw    = f.get('new_password', '')
        cpw    = f.get('confirm_password', '')
        street = f.get('street_address', '').strip()
        city   = f.get('city', '').strip()
        state  = f.get('state', '').strip()
        zip_   = f.get('zip', '').strip()

        if not address_is_valid(street, city, state, zip_):
            flash('A valid street address, city, state, and 5-digit ZIP code are required.', 'error')
            return redirect(url_for('family.profile'))

        conn = get_db_connection()
        cur  = conn.cursor()
        if npw:
            if npw != cpw or len(npw) < 8:
                flash('Passwords must match and be at least 8 characters.', 'error')
                conn.close()
                return redirect(url_for('family.profile'))
            cur.execute(
                "UPDATE family SET "
                "first_name_0=%s, last_name_0=%s, "
                "first_name_1=%s, last_name_1=%s, "
                "primary_phone=%s, secondary_phone=%s, secondary_email=%s, "
                "street_address=%s, city=%s, state=%s, zip=%s, "
                "address_verified=1, "
                "password_hash=%s WHERE id=%s",
                (f.get('first_name_0',''), f.get('last_name_0',''),
                 f.get('first_name_1', '?'), f.get('last_name_1', '?'),
                 f.get('primary_phone',''), f.get('secondary_phone',''),
                 f.get('secondary_email',''),
                 street, city, state, zip_,
                 _hash(npw), current_user.id)
            )
        else:
            cur.execute(
                "UPDATE family SET "
                "first_name_0=%s, last_name_0=%s, "
                "first_name_1=%s, last_name_1=%s, "
                "primary_phone=%s, secondary_phone=%s, secondary_email=%s, "
                "street_address=%s, city=%s, state=%s, zip=%s, "
                "address_verified=1 "
                "WHERE id=%s",
                (f.get('first_name_0',''), f.get('last_name_0',''),
                 f.get('first_name_1', '?'), f.get('last_name_1', '?'),
                 f.get('primary_phone',''), f.get('secondary_phone',''),
                 f.get('secondary_email',''),
                 street, city, state, zip_,
                 current_user.id)
            )
        conn.commit()
        conn.close()
        flash('Profile updated.', 'success')
        return redirect(url_for('family.profile'))
    return render_template('family/profile.html', family=current_user)


# ── student management ─────────────────────────────────────────────────────────

@family_bp.route('/students')
@login_required
def students():
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM student WHERE fid = %s ORDER BY last_name, first_name",
        (current_user.id,)
    )
    students = cur.fetchall()
    # Get current period for the Register button
    cur.execute("SELECT * FROM period ORDER BY id DESC LIMIT 1")
    current_period = cur.fetchone()
    has_registration = False
    if current_period:
        cur.execute(
            "SELECT id FROM family_record WHERE fid=%s AND pid=%s",
            (current_user.id, current_period['id'])
        )
        has_registration = cur.fetchone() is not None
    conn.close()
    return render_template('family/students.html',
                           students=students,
                           current_period=current_period,
                           has_registration=has_registration)


@family_bp.route('/students/new', methods=['GET', 'POST'])
@login_required
def new_student():
    if request.method == 'POST':
        f     = request.form
        first = f.get('first_name', '').strip()
        last  = f.get('last_name', '').strip()
        if not first or not last:
            flash('First name and last name are required.', 'error')
            return render_template('family/student_form.html',
                                   student=None, action='new', family=current_user)
        media_consent = f.get('media_consent')
        media_consent = int(media_consent) if media_consent in ('0','1') else None
        is_adult_val = 1 if f.get('is_adult') == '1' else 0
        mfcs_aff = f.get('mfcs_affiliation') if is_adult_val else None
        conn = get_db_connection()
        cur  = conn.cursor()
        bday = f.get('birthday', '').strip() or '2999-01-01'
        email_val = f.get('email', '').strip()
        if not email_val or email_val == '?':
            email_val = ''
        try:
            cur.execute(
                """
                INSERT INTO student
                  (fid, last_name, first_name, chinese_name,
                   gender, birthday, phone, email,
                   ec_last_name, ec_first_name, ec_phone,
                   special_note, media_consent, is_adult, mfcs_affiliation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (current_user.id, last, first,
                 f.get('chinese_name', '?'), f.get('gender', '?'),
                 bday, f.get('phone', '?'), email_val,
                 f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
                 f.get('ec_phone', '?'), f.get('special_note', ''),
                 media_consent, is_adult_val, mfcs_aff)
            )
            conn.commit()
            conn.close()
            flash(f'{first} {last} has been added.', 'success')
            return redirect(url_for('family.students'))
        except Exception as e:
            conn.close()
            if '1062' in str(e) or 'Duplicate' in str(e):
                flash('A student with that email already exists. Please use a different email or leave it blank.', 'error')
            else:
                flash(f'Error adding student: {e}', 'error')
            return render_template('family/student_form.html', student=None, action='new',
                                   family=current_user)
    return render_template('family/student_form.html', student=None, action='new',
                           family=current_user)


@family_bp.route('/students/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM student WHERE id = %s AND fid = %s",
        (student_id, current_user.id)
    )
    student = cur.fetchone()
    if not student:
        conn.close()
        flash('Student not found.', 'error')
        return redirect(url_for('family.students'))
    if request.method == 'POST':
        f     = request.form
        first = f.get('first_name', '').strip()
        last  = f.get('last_name', '').strip()
        if not first or not last:
            flash('First name and last name are required.', 'error')
            return render_template('family/student_form.html', student=student, action='edit',
                                   family=current_user)
        phone_err2 = _validate_phone(f.get('phone','').strip())
        ec_phone_err2 = _validate_phone(f.get('ec_phone','').strip())
        if phone_err2 or ec_phone_err2:
            flash(phone_err2 or ec_phone_err2, 'error')
            return render_template('family/student_form.html', student=student, action='edit',
                                   family=current_user)
        bday = f.get('birthday', '').strip() or '2999-01-01'
        media_consent = f.get('media_consent')
        media_consent = int(media_consent) if media_consent in ('0','1') else None
        is_adult_val = 1 if f.get('is_adult') == '1' else 0
        mfcs_aff = f.get('mfcs_affiliation') if is_adult_val else None
        email_val = f.get('email', '').strip()
        if not email_val or email_val == '?':
            existing_email = student.get('email', '') or ''
            if existing_email and existing_email != '?' and not existing_email.endswith('@placeholder.invalid'):
                email_val = existing_email
            else:
                email_val = ''
        cur2 = conn.cursor()
        cur2.execute(
                """
                UPDATE student SET
                  last_name=%s, first_name=%s, chinese_name=%s,
                  gender=%s, birthday=%s, phone=%s, email=%s,
                  ec_last_name=%s, ec_first_name=%s, ec_phone=%s,
                  special_note=%s, media_consent=%s,
                  is_adult=%s, mfcs_affiliation=%s
                WHERE id=%s AND fid=%s
                """,
                (last, first,
                 f.get('chinese_name', '?'), f.get('gender', '?'),
                 bday, f.get('phone', '?'), email_val,
                 f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
                 f.get('ec_phone', '?'), f.get('special_note', ''),
                 media_consent, is_adult_val, mfcs_aff,
                 student_id, current_user.id)
            )
        conn.commit()
        conn.close()
        flash(f'{first} {last} has been updated.', 'success')
        return redirect(url_for('family.students'))
    conn.close()
    return render_template('family/student_form.html',
                           student=student, action='edit', family=current_user)


# ── class registration ─────────────────────────────────────────────────────────

@family_bp.route('/register/<int:period_id>')
@login_required
def register_classes(period_id):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM period WHERE id = %s", (period_id,))
    period = cur.fetchone()
    if not period:
        conn.close()
        flash('Registration period not found.', 'error')
        return redirect(url_for('family.dashboard'))

    cur.execute(
        "SELECT * FROM student WHERE fid = %s ORDER BY last_name, first_name",
        (current_user.id,)
    )
    students = cur.fetchall()
    if not students:
        conn.close()
        flash('Please add your students before registering for classes.', 'info')
        return redirect(url_for('family.students'))

    cur.execute(
        "SELECT * FROM class_group_record "
        "WHERE pid = %s AND type = 'language' ORDER BY name", (period_id,)
    )
    lang_classes = cur.fetchall()

    # Load all culture classes
    cur.execute(
        "SELECT * FROM class_group_record "
        "WHERE pid = %s AND type = 'culture' ORDER BY name", (period_id,)
    )
    all_cult_classes = cur.fetchall()

    # Per-student culture class lists
    cult_classes_all          = all_cult_classes  # for JS fee lookup only
    cult_classes_adult        = [c for c in all_cult_classes if c.get('adult_only', 0)]
    cult_classes_adult_second = [c for c in cult_classes_adult if c.get('allow_as_second', 1)]
    cult_classes_minor        = [c for c in all_cult_classes if not c.get('adult_only', 0)]
    cult_classes_second_minor = [c for c in all_cult_classes
                                 if not c.get('adult_only', 0) and c.get('allow_as_second', 1)]

    existing = {}
    for s in students:
        cur.execute(
            "SELECT * FROM student_record WHERE sid = %s AND pid = %s",
            (s['id'], period_id)
        )
        row = cur.fetchone()
        if row:
            existing[s['id']] = row

    conn.close()

    # Tag each student with adult flag — use is_adult column, fall back to birthday
    for s in students:
        s['_is_adult'] = _is_adult(s)

    # Calculate effective tuition for this family (grandfathered or standard)
    conn2 = get_db_connection()
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn2)

    is_late_flag = _is_late(period)
    # Get current payment status for late fee check — reuse conn2
    try:
        cur2 = conn2.cursor(dictionary=True)
        cur2.execute(
            "SELECT total_paid, late_fee_waived, first_payment_date FROM family_record WHERE fid=%s AND pid=%s",
            (current_user.id, period_id)
        )
        fpr_row = cur2.fetchone()
        total_paid_so_far  = float((fpr_row or {}).get('total_paid') or 0)
        late_fee_waived    = bool((fpr_row or {}).get('late_fee_waived', 0))
        first_payment_date = (fpr_row or {}).get('first_payment_date')
    except Exception:
        try:
            cur2 = conn2.cursor(dictionary=True)
            cur2.execute(
                "SELECT total_paid FROM family_record WHERE fid=%s AND pid=%s",
                (current_user.id, period_id)
            )
            fpr_row = cur2.fetchone()
            total_paid_so_far = float((fpr_row or {}).get('total_paid') or 0)
        except Exception:
            total_paid_so_far = 0.0
        late_fee_waived    = False
        first_payment_date = None

    conn2.close()

    conn2 = get_db_connection()
    charge_late, per_minor_late = _should_charge_late_fee(
        period, current_user.id, period_id,
        total_paid_so_far, late_fee_waived, conn2,
        first_payment_date=first_payment_date
    )
    conn2.close()
    late_fee_amount = per_minor_late if charge_late else 0.0
    return render_template('family/register.html',
                           period=period,
                           students=students,
                           lang_classes=lang_classes,
                           cult_classes_all=cult_classes_all,
                           cult_classes_adult=cult_classes_adult,
                           cult_classes_adult_second=cult_classes_adult_second,
                           cult_classes_minor=cult_classes_minor,
                           cult_classes_second_minor=cult_classes_second_minor,
                           existing=existing,
                           eff_tuition=eff_tuition,
                           tuition_type=tuition_type,
                           is_late=is_late_flag,
                           late_fee_amount=late_fee_amount,
                           now_date=_today_eastern().strftime('%Y-%m-%d'))


@family_bp.route('/register/<int:period_id>/submit', methods=['POST'])
@login_required
def submit_registration(period_id):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM period WHERE id = %s", (period_id,))
    period = cur.fetchone()

    cur.execute("SELECT * FROM student WHERE fid = %s", (current_user.id,))
    students = {s['id']: s for s in cur.fetchall()}

    cur.execute(
        "SELECT id, fee, discount FROM class_group_record "
        "WHERE pid = %s AND type = 'culture'", (period_id,)
    )
    cult_rows = cur.fetchall()
    cult_fee_map      = {r['id']: float(r['fee'] or 0)      for r in cult_rows}
    cult_discount_map = {r['id']: float(r['discount'] or 0) for r in cult_rows}

    # Determine effective tuition (standard vs grandfathered)
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn)
    student_subtotal = 0.0
    minor_count = 0  # track minors for multi-kid discount

    # Validate that at least one student has selected something
    any_selection = any(
        request.form.get(f'lang_{sid}') or
        request.form.get(f'cult_{sid}') or
        request.form.get(f'cult2_{sid}')
        for sid in students
    )
    if not any_selection:
        conn.close()
        flash('Please select at least one class for a student before submitting.', 'error')
        return redirect(url_for('family.register_classes', period_id=period_id))

    for sid, student in students.items():
        # Adults have no language class
        lid  = request.form.get(f'lang_{sid}')
        cid  = request.form.get(f'cult_{sid}')
        cid2 = request.form.get(f'cult2_{sid}')
        lid  = int(lid)  if lid  and lid  != '0' else None
        cid  = int(cid)  if cid  and cid  != '0' else None
        cid2 = int(cid2) if cid2 and cid2 != '0' else None

        # Adults cannot select language class
        if _is_adult(student):
            lid = None

        # Block cult2 if cult1 not selected
        if cid2 and not cid:
            cid2 = None
        # Prevent selecting the same culture class twice
        if cid2 and cid2 == cid:
            cid2 = None

        cult_fee_amount  = cult_fee_map.get(cid, 0)  if cid  else 0
        cult_fee_amount2 = cult_fee_map.get(cid2, 0) if cid2 else 0
        cult_disc1 = cult_discount_map.get(cid, 0)  if cid  else 0
        cult_disc2 = cult_discount_map.get(cid2, 0) if cid2 else 0

        # Skip or clear student if nothing selected
        if not lid and not cid and not cid2:
            # If they had a previous record, clear it
            cur.execute(
                "UPDATE student_record SET lcgrid=NULL, ccgrid=NULL, ccgrid2=NULL "
                "WHERE sid=%s AND pid=%s",
                (sid, period_id)
            )
            continue

        student_fee = _calc_student_fee(student, period, cult_fee_amount,
                                        cult_fee_amount2, eff_tuition,
                                        cult_disc1, cult_disc2)
        student_subtotal += student_fee
        if not _is_adult(student):
            minor_count += 1

        cur.execute(
            "SELECT id FROM student_record WHERE sid = %s AND pid = %s",
            (sid, period_id)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE student_record SET lcgrid=%s, ccgrid=%s, ccgrid2=%s WHERE id=%s",
                (lid, cid, cid2, existing['id'])
            )
        elif lid or cid or cid2:
            try:
                cur.execute(
                    "INSERT INTO student_record (sid, pid, lcgrid, ccgrid, ccgrid2) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (sid, period_id, lid, cid, cid2)
                )
            except Exception as e:
                if '1062' not in str(e) and 'Duplicate' not in str(e):
                    raise

    # Determine late fee: per-minor, returning families only, unpaid past deadline
    charge_late, per_minor_late = _should_charge_late_fee(
        period, current_user.id, period_id, 0, False, conn
    )
    late_fee_total = minor_count * per_minor_late if charge_late else 0.0

    # Total = student fees + registration fee + PA deposit + late fee - multi-kid discount
    total_due = _calc_total_family_fee(student_subtotal, period, minor_count, late_fee_total)

    cur.execute(
        "SELECT id, total_due AS old_total, reg_status FROM family_record "
        "WHERE fid = %s AND pid = %s",
        (current_user.id, period_id)
    )
    fpr = cur.fetchone()
    if fpr:
        old_total    = float(fpr.get('old_total') or fpr.get('total_due') or 0)
        old_status   = fpr['reg_status'] or 'Pending'
        fee_changed  = abs(total_due - old_total) > 0.01
        # If fee changed AND was already Complete, revert to Pending and flag it
        if fee_changed and old_status == 'Complete Registration':
            new_status = 'Pending'
            note_suffix = (f' [Fee updated from ${old_total:.2f} to ${total_due:.2f}'
                           f' — please review payment]')
            cur.execute(
                "SELECT description FROM family_record WHERE id=%s",(fpr['id'],))
            existing_note = (cur.fetchone() or {}).get('description','') or ''
            new_note = (existing_note + note_suffix)[:9999]
            cur.execute(
                "UPDATE family_record SET total_due=%s, reg_status=%s, "
                "description=%s, last_update=NOW() WHERE id=%s",
                (total_due, new_status, new_note, fpr['id'])
            )
            flash('Registration updated. Fee changed — status set back to Pending '
                  f'(was ${old_total:.2f}, now ${total_due:.2f}).', 'warning')
        else:
            # Fee unchanged or was already Pending — keep status
            new_status = old_status if not fee_changed else 'Pending'
            cur.execute(
                "UPDATE family_record SET total_due=%s, reg_status=%s, "
                "last_update=NOW() WHERE id=%s",
                (total_due, new_status, fpr['id'])
            )
            if fee_changed:
                flash('Registration updated. Fee changed — awaiting payment confirmation.', 'info')
            else:
                flash('Registration saved!', 'success')
    else:
        cur.execute(
            "INSERT INTO family_record "
            "(fid, pid, total_due, reg_status, reg_time, last_update) "
            "VALUES (%s, %s, %s, 'Pending', NOW(), NOW())",
            (current_user.id, period_id, total_due)
        )
        flash('Registration saved!', 'success')

    conn.commit()
    conn.close()

    # Send registration confirmation email
    try:
        email_conn = get_db_connection()
        email_cur  = email_conn.cursor(dictionary=True)
        email_cur.execute("""
            SELECT sr.*, s.first_name, s.last_name, s.birthday,
                   s.is_adult, s.mfcs_affiliation,
                   lc.name AS lang_class,
                   cc.name  AS cult_class,  cc.fee  AS cult_fee,  cc.discount  AS cult_disc,
                   cc2.name AS cult_class2, cc2.fee AS cult_fee2, cc2.discount AS cult_disc2
            FROM student_record sr
            JOIN student s ON s.id = sr.sid
            LEFT JOIN class_group_record lc  ON lc.id  = sr.lcgrid
            LEFT JOIN class_group_record cc  ON cc.id  = sr.ccgrid
            LEFT JOIN class_group_record cc2 ON cc2.id = sr.ccgrid2
            WHERE sr.pid = %s AND s.fid = %s
            AND (sr.lcgrid IS NOT NULL OR sr.ccgrid IS NOT NULL OR sr.ccgrid2 IS NOT NULL)
        """, (period_id, current_user.id))
        reg_rows = email_cur.fetchall()
        email_conn.close()

        # Build per-student receipt lines
        text_rows = ''
        html_rows = ''
        # Get effective tuition and returning family status
        email_conn2 = get_db_connection()
        email_eff_tuition, email_tuition_type = _effective_tuition(period, current_user.id, email_conn2)
        is_returning = _is_returning_family(current_user.id, period_id, email_conn2)
        email_conn2.close()

        payment_deadline = period.get('deadline', '')
        if isinstance(payment_deadline, date):
            payment_deadline_str = payment_deadline.strftime('%B %d, %Y')
        elif payment_deadline:
            try:
                from datetime import datetime as dt2
                payment_deadline_str = dt2.strptime(str(payment_deadline), '%Y-%m-%d').strftime('%B %d, %Y')
            except Exception:
                payment_deadline_str = str(payment_deadline)
        else:
            payment_deadline_str = ''

        late_fee_warning_text = ''
        late_fee_warning_html = ''
        if is_returning and payment_deadline_str:
            per_minor = float(period.get('late_fee') or 0)
            late_fee_warning_text = (
                f"\n⚠ IMPORTANT — PAYMENT DEADLINE: {payment_deadline_str}\n"
                f"As a returning family, a late payment fee of ${per_minor:.2f} per minor student\n"
                f"will be applied if payment is not received by {payment_deadline_str}.\n"
                f"If paying by cheque, the postmark date will be used to determine timeliness.\n"
            )
            late_fee_warning_html = (
                f"<div style='background:#fff3cd;border:1px solid #ffc107;padding:12px;"
                f"border-radius:6px;margin:16px 0'>"
                f"<strong>⚠ Payment Deadline: {payment_deadline_str}</strong><br>"
                f"As a returning family, a late payment fee of <strong>${per_minor:.2f} per minor student</strong> "
                f"will be applied if payment is not received by <strong>{payment_deadline_str}</strong>.<br>"
                f"If paying by cheque, the <strong>postmark date</strong> will be used to determine timeliness."
                f"</div>"
            )

        for r in reg_rows:
            name     = f"{r['last_name']}, {r['first_name']}"
            is_adult = _is_adult(r)
            mfcs     = r.get('mfcs_affiliation') == 'Y'
            tuit     = 0.0 if is_adult else email_eff_tuition
            disc1    = float(r.get('cult_disc')  or 0) if mfcs else 0
            disc2    = float(r.get('cult_disc2') or 0) if mfcs else 0
            cf1      = max(0, float(r['cult_fee']  or 0) - disc1)
            cf2      = max(0, float(r['cult_fee2'] or 0) - disc2)
            st_fee   = tuit + cf1 + cf2
            lang     = r['lang_class']  or '—'
            cult     = r['cult_class']  or '—'
            cult2    = r['cult_class2'] or '—'

            disc1_note = f" [discounted from ${float(r['cult_fee'] or 0):.2f}]" if disc1 else ""
            disc2_note = f" [discounted from ${float(r['cult_fee2'] or 0):.2f}]" if disc2 else ""
            text_rows += (
                f"  {name}\n"
                f"    Language:  {lang}\n"
                f"    Culture 1: {cult}{f' (${cf1:.2f})' if cf1 else ''}{disc1_note}\n"
                f"    Culture 2: {cult2}{f' (${cf2:.2f})' if cf2 else ''}{disc2_note}\n"
                f"    Tuition:   {'N/A (adult)' if is_adult else f'${tuit:.2f}'}\n"
                f"    Subtotal:  ${st_fee:.2f}\n\n"
            )
            html_rows += (
                f"<tr>"
                f"<td><strong>{name}</strong></td>"
                f"<td>{lang}</td>"
                f"<td>{cult}{'<br><small>$'+f'{cf1:.2f}'+'</small>' if cf1 else ''}</td>"
                f"<td>{cult2}{'<br><small>$'+f'{cf2:.2f}'+'</small>' if cf2 else ''}</td>"
                f"<td style='text-align:right'>{'N/A' if is_adult else '$'+f'{tuit:.2f}'}</td>"
                f"<td style='text-align:right'>${st_fee:.2f}</td>"
                f"</tr>"
            )

        # Fee summary lines
        minor_cnt    = sum(1 for r in reg_rows if not _is_adult(r))
        discount_per = float(period.get('discount') or 0)
        extra_kids   = max(0, minor_cnt - 2)
        disc_amt     = extra_kids * discount_per
        reg_fee_email = float(period.get('registration_fee') or 0) if minor_cnt > 0 else 0.0
        pa_dep_email  = float(period.get('pa_assignment_deposit') or 0) if minor_cnt > 0 else 0.0

        text_summary = ''
        html_summary = ''
        if minor_cnt > 0:
            text_summary += (
                f"  Registration fee:          ${reg_fee_email:.2f}\n"
                f"  PA Assignment Duty deposit: ${pa_dep_email:.2f} (refundable)\n"
            )
            html_summary += (
                f"<tr><td colspan='5'>Registration fee</td>"
                f"<td style='text-align:right'>${reg_fee_email:.2f}</td></tr>"
                f"<tr><td colspan='5'>PA Assignment Duty deposit (refundable)</td>"
                f"<td style='text-align:right'>${pa_dep_email:.2f}</td></tr>"
            )
        if late_fee_total > 0:
            text_summary += f"  Late payment fee ({minor_cnt} minor{'s' if minor_cnt>1 else ''} × ${float(period.get('late_fee') or 0):.2f}): ${late_fee_total:.2f}\n"
            html_summary += (
                f"<tr><td colspan='5' style='color:red'>Late payment fee "
                f"({minor_cnt} minor{'s' if minor_cnt>1 else ''} × ${float(period.get('late_fee') or 0):.2f})</td>"
                f"<td style='text-align:right;color:red'>${late_fee_total:.2f}</td></tr>"
            )
        if disc_amt > 0:
            text_summary += f"  Multi-child discount ({extra_kids} child{'ren' if extra_kids>1 else ''}): -${disc_amt:.2f}\n"
            html_summary += (f"<tr><td colspan='5' style='color:green'>Multi-child discount "
                             f"({extra_kids} child{'ren' if extra_kids>1 else ''} × ${discount_per:.2f})</td>"
                             f"<td style='text-align:right;color:green'>-${disc_amt:.2f}</td></tr>")

        # Build payment instructions from period settings
        check_title = period.get('check_title','') or 'Monmouth Fidelity Chinese School'
        attention   = period.get('attention','')   or ''
        addr        = period.get('street_address','') or ''
        city        = period.get('city','')  or ''
        state_      = period.get('state','') or ''
        zip_        = period.get('zip','')   or ''

        pay_text = (
            f"\nPAYMENT INSTRUCTIONS\n"
            f"{'='*50}\n"
            f"Option 1 — Pay in Person\n"
            f"  Bring cash or cheque to the school office.\n\n"
            f"Option 2 — Pay by Mail\n"
            f"  Make cheque payable to: {check_title}\n"
        )
        if attention:
            pay_text += f"  Attention: {attention}\n"
        if addr:
            pay_text += f"  Mail to: {addr}, {city}, {state_} {zip_}\n"
        pay_text += "  Please include your family name on the cheque.\n"

        pay_html = (
            f"<h3 style='border-bottom:2px solid #c0392b;padding-bottom:6px'>Payment Instructions</h3>"
            f"<table cellpadding='10' cellspacing='0' style='width:100%;border:1px solid #ddd'>"
            f"<tr style='background:#f9f9f9;vertical-align:top'>"
            f"<td style='width:50%;border-right:1px solid #ddd'>"
            f"<strong>Option 1 — Pay in Person</strong><br><br>"
            f"Bring cash or cheque to the school office."
            f"</td>"
            f"<td style='width:50%;padding-left:16px'>"
            f"<strong>Option 2 — Pay by Mail</strong><br><br>"
            f"Make cheque payable to:<br><strong>{check_title}</strong><br>"
            + (f"Attention: {attention}<br>" if attention else "")
            + (f"<br>{addr}<br>{city}, {state_} {zip_}" if addr else "")
            + f"<br><br><em>Please include your family name on the cheque.</em>"
            f"</td></tr></table>"
        )

        _send_email(
            to_addr=current_user.primary_email,
            subject=f'MFCS — Registration Received (Pending Payment) — {period["name"]}',
            text_body=(
                f"Dear {current_user.first_name_0} {current_user.last_name_0},\n\n"
                f"Your class registration for {period['name']} has been received.\n\n"
                f"{'='*50}\n"
                f"REGISTRATION RECEIPT\n"
                f"{'='*50}\n\n"
                f"{text_rows}"
                f"{text_summary}"
                f"  {'─'*30}\n"
                f"  TOTAL DUE:                 ${total_due:.2f}\n"
                f"  NOTE: The total above reflects fees at time of registration.\n"
                f"  Please log in to your account to view the most current amount due,\n"
                f"  as a late payment fee may apply if payment is received after the deadline.\n\n"
                f"{pay_text}\n"
                f"{late_fee_warning_text}"
                f"{'='*50}\n\n"
                f"STATUS: PENDING\n"
                f"Your registration will not be finalized until payment is received.\n"
                f"Once confirmed, you will receive a separate payment confirmation email.\n\n"
                f"Questions? Contact us at languagedeputydir@mfcsnj.org\n\n"
                f"Monmouth Fidelity Chinese School"
            ),
            html_body=(
                f"<p>Dear {current_user.first_name_0} {current_user.last_name_0},</p>"
                f"<p>Your class registration for <strong>{period['name']}</strong> has been received.</p>"
                f"<h3 style='border-bottom:2px solid #c0392b;padding-bottom:6px'>Registration Receipt</h3>"
                + (f"<p style='color:green'>✦ Returning family tuition rate applied: ${email_eff_tuition:.2f}</p>" if email_tuition_type == 'grandfathered' else "")
                + f"<table border='1' cellpadding='8' cellspacing='0' style='border-collapse:collapse;width:100%'>"
                f"<tr style='background:#f0f0f0'>"
                f"<th>Student</th><th>Language</th><th>Culture 1</th>"
                f"<th>Culture 2</th><th>Tuition</th><th>Subtotal</th></tr>"
                f"{html_rows}"
                f"<tr style='background:#f9f9f9'>{html_summary}</tr>"
                f"<tr style='background:#e8f5e9;font-weight:bold'>"
                f"<td colspan='5'><strong>TOTAL DUE</strong></td>"
                f"<td style='text-align:right'><strong>${total_due:.2f}</strong></td></tr>"
                f"</table>"
                f"<p style='color:#666;font-size:0.9em;margin-top:8px'>"
                f"⚠ The total above reflects fees at the time of registration. "
                f"Please <a href='https://register.mfcsnj.org'>log in to your account</a> "
                f"to view the most current amount due, as a late payment fee may apply "
                f"if payment is received after the payment deadline.</p><br>"
                f"{pay_html}"
                f"{late_fee_warning_html}"
                f"<div style='background:#fff3cd;border:1px solid #ffc107;padding:12px;"
                f"border-radius:6px;margin:16px 0'>"
                f"<strong>⚠ Status: PENDING PAYMENT</strong><br>"
                f"Your registration will not be finalized until payment is received by our finance team. "
                f"Once your payment has been confirmed, you will receive a separate confirmation email.</div>"
                f"<p>Questions? Contact us at "
                f"<a href='mailto:languagedeputydir@mfcsnj.org'>languagedeputydir@mfcsnj.org</a></p>"
                f"<p>Monmouth Fidelity Chinese School</p>"
            )
        )
    except Exception as e:
        print(f'Confirmation email error: {e}', flush=True)

    return redirect(url_for('family.fee_summary', period_id=period_id))


# ── fee summary ────────────────────────────────────────────────────────────────

@family_bp.route('/fees/<int:period_id>')
@login_required
def fee_summary(period_id):
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM period WHERE id = %s", (period_id,))
    period = cur.fetchone()

    cur.execute(
        "SELECT * FROM family_record WHERE fid = %s AND pid = %s",
        (current_user.id, period_id)
    )
    fpr = cur.fetchone()

    cur.execute(\
        """
        SELECT s.first_name, s.last_name, s.birthday, s.is_adult, s.mfcs_affiliation,
               lc.name  AS lang_class_name,
               cc.name  AS cult_class_name,  cc.fee  AS cult_fee,  cc.discount  AS cult_disc,
               cc2.name AS cult_class2_name, cc2.fee AS cult_fee2, cc2.discount AS cult_disc2
        FROM   student_record sr
        JOIN   student s   ON s.id = sr.sid
        LEFT   JOIN class_group_record lc  ON lc.id  = sr.lcgrid
        LEFT   JOIN class_group_record cc  ON cc.id  = sr.ccgrid
        LEFT   JOIN class_group_record cc2 ON cc2.id = sr.ccgrid2
        WHERE  s.fid = %s AND sr.pid = %s
        AND   (sr.lcgrid IS NOT NULL OR sr.ccgrid IS NOT NULL OR sr.ccgrid2 IS NOT NULL)
        ORDER  BY s.last_name, s.first_name
        """,
        (current_user.id, period_id)
    )
    raw_rows = cur.fetchall()
    reg_fee  = float(period.get('registration_fee') or 0) if period else 0
    pa_fee   = float(period.get('pa_assignment_deposit') or 0) if period else 0
    # Will be recalculated after minor_count is known below

    # Determine effective tuition for this family
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn) if period else (0, 'standard')
    conn.close()

    rows = []
    student_subtotal = 0.0
    minor_count = 0
    for r in raw_rows:
        adult = _is_adult(r)
        mfcs  = r.get('mfcs_affiliation') == 'Y'

        cf1_raw = float(r.get('cult_fee')  or 0)
        cf2_raw = float(r.get('cult_fee2') or 0)
        disc1   = float(r.get('cult_disc')  or 0) if mfcs else 0
        disc2   = float(r.get('cult_disc2') or 0) if mfcs else 0
        cf1 = max(0, cf1_raw - disc1)
        cf2 = max(0, cf2_raw - disc2)
        total_cult = cf1 + cf2

        if adult:
            student_fee = total_cult
            parts = []
            if cf1: parts.append(f'Culture ${cf1:.2f}' + (' (discounted)' if disc1 else ''))
            if cf2: parts.append(f'Culture 2 ${cf2:.2f}' + (' (discounted)' if disc2 else ''))
            fee_note = ' + '.join(parts) if parts else 'Adult — no fee'
        else:
            student_fee = eff_tuition + total_cult
            tuition_label = f'Tuition ${eff_tuition:.2f}'
            if tuition_type == 'grandfathered':
                tuition_label += ' ✦'
            parts = [tuition_label]
            if cf1: parts.append(f'Culture ${cf1:.2f}')
            if cf2: parts.append(f'Culture 2 ${cf2:.2f}')
            fee_note = ' + '.join(parts)
            minor_count += 1

        student_subtotal += student_fee
        rows.append({**r,
                     'student_fee': student_fee,
                     'fee_note':    fee_note,
                     'is_adult':    adult})

    # Multi-kid discount
    # Apply adult-only exemption to reg fee and PA deposit
    reg_fee  = float(period.get('registration_fee') or 0) if (period and minor_count > 0) else 0.0
    pa_fee   = float(period.get('pa_assignment_deposit') or 0) if (period and minor_count > 0) else 0.0
    discount_per = float(period.get('discount') or 0) if period else 0
    extra_kids   = max(0, minor_count - 2)
    multi_disc   = extra_kids * discount_per

    # Late fee: per minor, returning families, based on first payment date vs deadline
    late_fee_waived    = bool((fpr or {}).get('late_fee_waived', 0))
    total_paid_so_far  = float((fpr or {}).get('total_paid') or 0)
    first_payment_date = (fpr or {}).get('first_payment_date')
    conn3 = get_db_connection()
    charge_late, per_minor_late = _should_charge_late_fee(
        period, current_user.id, period_id,
        total_paid_so_far, late_fee_waived, conn3,
        first_payment_date=first_payment_date
    )
    conn3.close()
    late_fee    = minor_count * per_minor_late if charge_late else 0.0
    grand_total = student_subtotal + reg_fee + pa_fee + late_fee - multi_disc

    # Auto-update total_due if recalculated total differs from stored (e.g. late fee kicked in)
    if fpr and abs(grand_total - float(fpr.get('total_due') or 0)) > 0.01:
        conn4 = get_db_connection()
        cur4  = conn4.cursor()
        cur4.execute(
            "UPDATE family_record SET total_due=%s, last_update=NOW() WHERE id=%s",
            (grand_total, fpr['id'])
        )
        conn4.commit()
        conn4.close()
        # Update fpr in memory so Payment Status section shows correct value
        fpr = dict(fpr)
        fpr['total_due'] = grand_total

    return render_template('family/fee_summary.html',
                           period=period, fpr=fpr,
                           rows=rows,
                           student_subtotal=student_subtotal,
                           reg_fee=reg_fee,
                           pa_fee=pa_fee,
                           late_fee=late_fee,
                           late_fee_per_minor=per_minor_late,
                           minor_count=minor_count,
                           multi_disc=multi_disc,
                           extra_kids=extra_kids,
                           discount_per=discount_per,
                           grand_total=grand_total,
                           tuition_type=tuition_type,
                           eff_tuition=eff_tuition)

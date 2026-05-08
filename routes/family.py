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

def _age(birthday):
    """Return age in years from a date/datetime object, or None.
    Returns None for missing/sentinel dates (None, 2999-01-01)."""
    if not birthday:
        return None
    today = date.today()
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

def _calc_student_fee(student, period, cult_fee, cult_fee2=0, tuition_override=None):
    """
    Per-student fee:
      adult (>=18) → culture fees only (up to 2 classes)
      minor (<18)  → tuition + culture fees (up to 2 classes)
    tuition_override: use this instead of period.tuition (for grandfathered rate)
    """
    if tuition_override is not None:
        tuition = float(tuition_override)
    else:
        tuition = float(period.get('tuition') or 0)
    culture = float(cult_fee or 0) + float(cult_fee2 or 0)
    if _is_adult(student):
        return culture
    else:
        return tuition + culture


def _effective_tuition(period, family_id, conn):
    """
    Determine the effective tuition for a family registering for a period.
    Rules:
      - If no grandfathered_tuition set → use period.tuition
      - If family has a previous family_record in ANY period → existing family
        - If today <= grandfathered_deadline → use grandfathered_tuition
        - Else → use period.tuition
      - New family (no prior records) → always use period.tuition
    """
    g_tuition  = period.get('grandfathered_tuition')
    g_deadline = period.get('grandfathered_deadline')
    std_tuition = float(period.get('tuition') or 0)

    if not g_tuition:
        return std_tuition, 'standard'

    # Check if this family has registered in any PREVIOUS period
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT COUNT(*) AS n FROM family_record "
        "WHERE fid=%s AND pid != %s",
        (family_id, period['id'])
    )
    row = cur.fetchone()
    is_existing = (row['n'] > 0) if row else False

    if not is_existing:
        return std_tuition, 'standard (new family)'

    # Existing family — check deadline
    today = date.today()
    if g_deadline:
        try:
            if isinstance(g_deadline, str):
                from datetime import datetime as dt
                deadline = dt.strptime(g_deadline, '%Y-%m-%d').date()
            else:
                deadline = g_deadline
            if today <= deadline:
                return float(g_tuition), 'grandfathered'
        except Exception:
            pass

    return std_tuition, 'standard (past deadline)' 

def _calc_total_family_fee(student_subtotal, period, minor_count=0):
    """
    Add once-per-family fees:
      + registration_fee
      + pa_assignment_deposit  (refundable, but still collected up front)
      - period.discount per minor beyond the first 2 (multi-kid discount)
    """
    reg_fee           = float(period.get('registration_fee') or 0)
    pa_fee            = float(period.get('pa_assignment_deposit') or 0)
    per_kid_discount  = float(period.get('discount') or 0)
    additional_kids   = max(0, minor_count - 2)
    multi_kid_discount = additional_kids * per_kid_discount
    return student_subtotal + reg_fee + pa_fee - multi_kid_discount


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
        conn = get_db_connection()
        cur  = conn.cursor()
        bday = f.get('birthday', '').strip() or '2999-01-01'
        cur.execute(
            """
            INSERT INTO student
              (fid, last_name, first_name, chinese_name,
               gender, birthday, phone, email,
               ec_last_name, ec_first_name, ec_phone,
               special_note, media_consent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (current_user.id, last, first,
             f.get('chinese_name', '?'), f.get('gender', '?'),
             bday, f.get('phone', '?'), f.get('email', '?'),
             f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
             f.get('ec_phone', '?'), f.get('special_note', ''),
             media_consent)
        )
        conn.commit()
        conn.close()
        flash(f'{first} {last} has been added.', 'success')
        return redirect(url_for('family.students'))
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
        cur2 = conn.cursor()
        cur2.execute(
                """
                UPDATE student SET
                  last_name=%s, first_name=%s, chinese_name=%s,
                  gender=%s, birthday=%s, phone=%s, email=%s,
                  ec_last_name=%s, ec_first_name=%s, ec_phone=%s,
                  special_note=%s, media_consent=%s
                WHERE id=%s AND fid=%s
                """,
                (last, first,
                 f.get('chinese_name', '?'), f.get('gender', '?'),
                 bday, f.get('phone', '?'), f.get('email', '?'),
                 f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
                 f.get('ec_phone', '?'), f.get('special_note', ''),
                 media_consent, student_id, current_user.id)
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

    # Per-student culture class lists (filter adult_only based on age)
    # cult_classes_by_student built in template using _is_adult flag on student
    cult_classes = all_cult_classes          # used for non-age-filtered contexts
    cult_classes_second = [c for c in all_cult_classes if c.get('allow_as_second', 1)]
    cult_classes_all = all_cult_classes      # full list for adults
    cult_classes_minor = [c for c in all_cult_classes if not c.get('adult_only', 0)]
    cult_classes_second_minor = [c for c in cult_classes_second if not c.get('adult_only', 0)]

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

    # Tag each student with adult flag for template logic
    for s in students:
        s['_is_adult'] = _is_adult(s)

    return render_template('family/register.html',
                           period=period,
                           students=students,
                           lang_classes=lang_classes,
                           cult_classes_all=cult_classes_all,
                           cult_classes_minor=cult_classes_minor,
                           cult_classes_second_minor=cult_classes_second_minor,
                           cult_classes_second=cult_classes_second,
                           existing=existing)


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
        "SELECT id, fee FROM class_group_record "
        "WHERE pid = %s AND type = 'culture'", (period_id,)
    )
    cult_fee_map = {r['id']: float(r['fee']) for r in cur.fetchall()}

    # Determine effective tuition (standard vs grandfathered)
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn)
    student_subtotal = 0.0
    minor_count = 0  # track minors for multi-kid discount

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
        student_fee = _calc_student_fee(student, period, cult_fee_amount,
                                        cult_fee_amount2, eff_tuition)
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

    # Total = student fees + registration fee + PA deposit - multi-kid discount
    total_due = _calc_total_family_fee(student_subtotal, period, minor_count)

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
        cur2 = get_db_connection().cursor(dictionary=True)
        cur2.execute("""
            SELECT sr.*, s.first_name, s.last_name,
                   lc.name AS lang_class, cc.name AS cult_class, cc2.name AS cult_class2
            FROM student_record sr
            JOIN student s ON s.id = sr.sid
            LEFT JOIN class_group_record lc  ON lc.id  = sr.lcgrid
            LEFT JOIN class_group_record cc  ON cc.id  = sr.ccgrid
            LEFT JOIN class_group_record cc2 ON cc2.id = sr.ccgrid2
            WHERE sr.pid = %s AND s.fid = %s
        """, (period_id, current_user.id))
        reg_rows = cur2.fetchall()

        student_lines_text = ''
        student_lines_html = ''
        for r in reg_rows:
            name = f"{r['last_name']}, {r['first_name']}"
            lang = r['lang_class'] or '—'
            cult = r['cult_class'] or '—'
            cult2 = r['cult_class2'] or '—'
            student_lines_text += f"  • {name}: Language: {lang} | Culture 1: {cult} | Culture 2: {cult2}\n"
            student_lines_html += (f"<tr><td>{name}</td><td>{lang}</td>"
                                   f"<td>{cult}</td><td>{cult2}</td></tr>")

        _send_email(
            to_addr=current_user.primary_email,
            subject=f'MFCS — Registration Confirmation ({period["name"]})',
            text_body=(
                f"Dear {current_user.first_name_0} {current_user.last_name_0},\n\n"
                f"Thank you! Your registration for {period['name']} has been saved.\n\n"
                f"Students registered:\n{student_lines_text}\n"
                f"Total Due: ${total_due:.2f}\n\n"
                f"Please log in to your account to view your fee summary and payment details.\n\n"
                f"Monmouth Fidelity Chinese School"
            ),
            html_body=(
                f"<p>Dear {current_user.first_name_0} {current_user.last_name_0},</p>"
                f"<p>Thank you! Your registration for <strong>{period['name']}</strong> has been saved.</p>"
                f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                f"<tr style='background:#f0f0f0'><th>Student</th><th>Language</th>"
                f"<th>Culture 1</th><th>Culture 2</th></tr>"
                f"{student_lines_html}"
                f"</table>"
                f"<p><strong>Total Due: ${total_due:.2f}</strong></p>"
                f"<p>Please log in to your account to view your fee summary and payment details.</p>"
                f"<p>Monmouth Fidelity Chinese School</p>"
            )
        )
        cur2.close()
    except Exception as e:
        print(f'Confirmation email error: {e}')

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

    cur.execute(
        """
        SELECT s.first_name, s.last_name, s.birthday,
               lc.name  AS lang_class_name,
               cc.name  AS cult_class_name,
               cc.fee   AS cult_fee,
               cc2.name AS cult_class2_name,
               cc2.fee  AS cult_fee2
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
    # conn stays open until after _effective_tuition call
    reg_fee  = float(period.get('registration_fee') or 0) if period else 0
    pa_fee   = float(period.get('pa_assignment_deposit') or 0) if period else 0

    # Determine effective tuition for this family
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn) if period else (0, 'standard')
    conn.close()

    rows = []
    student_subtotal = 0.0
    for r in raw_rows:
        age       = _age(r.get('birthday'))
        adult     = age is not None and age >= 18
        cult_fee  = float(r.get('cult_fee')  or 0)
        cult_fee2 = float(r.get('cult_fee2') or 0)
        total_cult = cult_fee + cult_fee2

        if adult:
            student_fee = total_cult
            fee_note    = 'Adult — culture fee'
            if cult_fee:  fee_note += f' ${cult_fee:.2f}'
            if cult_fee2: fee_note += f' + ${cult_fee2:.2f}'
        else:
            student_fee = eff_tuition + total_cult
            tuition_label = f'Tuition ${eff_tuition:.2f}'
            if tuition_type == 'grandfathered':
                tuition_label += ' ✦'
            parts = [tuition_label]
            if cult_fee:  parts.append(f'Culture ${cult_fee:.2f}')
            if cult_fee2: parts.append(f'Culture 2 ${cult_fee2:.2f}')
            fee_note = ' + '.join(parts)

        student_subtotal += student_fee
        rows.append({**r,
                     'student_fee': student_fee,
                     'fee_note':    fee_note,
                     'age':         age,
                     'is_adult':    adult})

    grand_total = student_subtotal + reg_fee + pa_fee

    return render_template('family/fee_summary.html',
                           period=period, fpr=fpr,
                           rows=rows,
                           student_subtotal=student_subtotal,
                           reg_fee=reg_fee,
                           pa_fee=pa_fee,
                           grand_total=grand_total,
                           tuition_type=tuition_type,
                           eff_tuition=eff_tuition)

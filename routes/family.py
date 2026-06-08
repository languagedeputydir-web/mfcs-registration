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
import secrets
from datetime import datetime, timedelta, date

from flask import (Blueprint, render_template, redirect, url_for,
                   request, flash)
from flask_login import login_user, logout_user, login_required, current_user
import bcrypt

from db import get_db_connection
from models import Family

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

def _calc_total_family_fee(student_subtotal, period):
    """
    Add once-per-family fees:
      + registration_fee
      + pa_assignment_deposit  (refundable, but still collected up front)
    """
    reg_fee = float(period.get('registration_fee') or 0)
    pa_fee  = float(period.get('pa_assignment_deposit') or 0)
    return student_subtotal + reg_fee + pa_fee


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
            stored = row.get('password_hash') or row.get('password', '')
            if _check(password, stored):
                login_user(Family(row), remember=remember)
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
        conn  = get_db_connection()
        cur   = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id FROM family WHERE LOWER(primary_email) = %s", (email,)
        )
        row = cur.fetchone()
        if row:
            token      = secrets.token_urlsafe(32)
            expires_at = datetime.now() + timedelta(hours=2)
            cur.execute(
                "INSERT INTO password_reset_tokens (family_id, token, expires_at) "
                "VALUES (%s, %s, %s)", (row['id'], token, expires_at)
            )
            conn.commit()
            # TODO: send_email(email, url_for('family.reset_password', token=token, _external=True))
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
        cur2 = conn.cursor()
        cur2.execute("""INSERT INTO family
            (primary_email,password,password_hash,
             last_name_0,first_name_0,last_name_1,first_name_1,
             primary_phone,street_address,city,state,zip)
            VALUES(%s,'',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (email,_hash(pw),last,first,
             f.get('last_name_1', '?'),f.get('first_name_1', '?'),
             phone,
             f.get('street_address', '?'),f.get('city', '?'),
             f.get('state', '?'),f.get('zip', '?')))
        conn.commit()
        # Log them in right away
        cur.execute("SELECT * FROM family WHERE LOWER(primary_email)=%s",(email,))
        row = cur.fetchone(); conn.close()
        if not row:
            flash('Account created. Please log in.', 'success')
            return redirect(url_for('family.login'))
        login_user(Family(row))
        flash('Welcome! Your account has been created.', 'success')
        return redirect(url_for('family.dashboard'))
    return render_template('family/register_account.html')
# ── dashboard ──────────────────────────────────────────────────────────────────

@family_bp.route('/dashboard')
@login_required
def dashboard():
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
        f   = request.form
        npw = f.get('new_password', '')
        cpw = f.get('confirm_password', '')
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
                "password_hash=%s WHERE id=%s",
                (f.get('first_name_0',''), f.get('last_name_0',''),
                 f.get('first_name_1', '?'), f.get('last_name_1', '?'),
                 f.get('primary_phone',''), f.get('secondary_phone',''),
                 f.get('secondary_email',''),
                 f.get('street_address', '?'), f.get('city', '?'),
                 f.get('state', '?'), f.get('zip', '?'),
                 _hash(npw), current_user.id)
            )
        else:
            cur.execute(
                "UPDATE family SET "
                "first_name_0=%s, last_name_0=%s, "
                "first_name_1=%s, last_name_1=%s, "
                "primary_phone=%s, secondary_phone=%s, secondary_email=%s, "
                "street_address=%s, city=%s, state=%s, zip=%s "
                "WHERE id=%s",
                (f.get('first_name_0',''), f.get('last_name_0',''),
                 f.get('first_name_1', '?'), f.get('last_name_1', '?'),
                 f.get('primary_phone',''), f.get('secondary_phone',''),
                 f.get('secondary_email',''),
                 f.get('street_address', '?'), f.get('city', '?'),
                 f.get('state', '?'), f.get('zip', '?'),
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
                                   student=None, action='new')
        conn = get_db_connection()
        cur  = conn.cursor()
        bday = f.get('birthday', '').strip() or '2999-01-01'
        cur.execute(
            """
            INSERT INTO student
              (fid, last_name, first_name, chinese_name,
               gender, birthday, phone, email,
               ec_last_name, ec_first_name, ec_phone,
               special_note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (current_user.id, last, first,
             f.get('chinese_name', '?'), f.get('gender', '?'),
             bday, f.get('phone', '?'), f.get('email', '?'),
             f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
             f.get('ec_phone', '?'), f.get('special_note', ''))
        )
        conn.commit()
        conn.close()
        flash(f'{first} {last} has been added.', 'success')
        return redirect(url_for('family.students'))
    return render_template('family/student_form.html', student=None, action='new')


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
            return render_template('family/student_form.html', student=student, action='edit')
        phone_err2 = _validate_phone(f.get('phone','').strip())
        ec_phone_err2 = _validate_phone(f.get('ec_phone','').strip())
        if phone_err2 or ec_phone_err2:
            flash(phone_err2 or ec_phone_err2, 'error')
            return render_template('family/student_form.html', student=student, action='edit')
        bday = f.get('birthday', '').strip() or '2999-01-01'
        cur2 = conn.cursor()
        cur2.execute(
                """
                UPDATE student SET
                  last_name=%s, first_name=%s, chinese_name=%s,
                  gender=%s, birthday=%s, phone=%s, email=%s,
                  ec_last_name=%s, ec_first_name=%s, ec_phone=%s,
                  special_note=%s
                WHERE id=%s AND fid=%s
                """,
                (last, first,
                 f.get('chinese_name', '?'), f.get('gender', '?'),
                 bday, f.get('phone', '?'), f.get('email', '?'),
                 f.get('ec_last_name', '?'), f.get('ec_first_name', '?'),
                 f.get('ec_phone', '?'), f.get('special_note', ''),
                 student_id, current_user.id)
            )
        conn.commit()
        conn.close()
        flash(f'{first} {last} has been updated.', 'success')
        return redirect(url_for('family.students'))
    conn.close()
    return render_template('family/student_form.html',
                           student=student, action='edit')


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
    students_raw = cur.fetchall()
    if not students_raw:
        conn.close()
        flash('Please add your students before registering for classes.', 'info')
        return redirect(url_for('family.students'))
    # Add _is_adult flag to each student for template/JS use
    students = []
    for s in students_raw:
        s = dict(s)
        s['_is_adult'] = _is_adult(s)
        students.append(s)

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

    # Build a fee map for JS live calculator: {class_id: fee}
    cult_fee_map = {str(c['id']): float(c.get('fee') or 0) for c in all_cult_classes}

    # Determine effective tuition for this family (grandfathered vs standard)
    conn2 = get_db_connection()
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn2)
    # Check if existing registration implies grandfathered rate
    fpr_check = None
    cur2 = conn2.cursor(dictionary=True)
    cur2.execute("SELECT total_due FROM family_record WHERE fid=%s AND pid=%s",
                 (current_user.id, period_id))
    fpr_check = cur2.fetchone()
    conn2.close()
    if fpr_check and fpr_check.get('total_due') and period.get('grandfathered_tuition'):
        saved = float(fpr_check['total_due'])
        gf  = float(period['grandfathered_tuition'])
        std = float(period.get('tuition') or 0)
        # Rough back-calc: if implied tuition is near grandfathered, use it
        student_count = len([s for s in students if not _is_adult(s)])
        cult_total_approx = 0.0
        if student_count > 0:
            reg = float(period.get('registration_fee') or 0)
            pa  = float(period.get('pa_assignment_deposit') or 0)
            implied = (saved - pa - reg) / max(student_count, 1)
            if abs(implied - gf) <= 30:   # within $30 of grandfathered
                eff_tuition = gf
                tuition_type = 'grandfathered'

    return render_template('family/register.html',
                           period=period,
                           students=students,
                           lang_classes=lang_classes,
                           cult_classes_all=cult_classes_all,
                           cult_classes_minor=cult_classes_minor,
                           cult_classes_second_minor=cult_classes_second_minor,
                           cult_classes_second=cult_classes_second,
                           existing=existing,
                           cult_fee_map=cult_fee_map,
                           eff_tuition=eff_tuition,
                           tuition_type=tuition_type)


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

    # Determine effective tuition.
    # If the family already has a saved registration, use the OLD student_records
    # to back-calculate the tuition rate actually used — preserving grandfathered
    # rate even after the deadline and even when class selections change.
    cur.execute(
        "SELECT id, total_due, reg_status, description FROM family_record "
        "WHERE fid=%s AND pid=%s", (current_user.id, period_id)
    )
    _existing_fpr = cur.fetchone()
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn)

    if _existing_fpr and _existing_fpr.get('total_due') and float(_existing_fpr.get('total_due') or 0) > 0.01:
        _gf  = period.get('grandfathered_tuition')
        _std = float(period.get('tuition') or 0)
        if _gf:
            # Fetch OLD student_records with birthday for accurate minor count.
            # Using OLD records (not current family list) handles the case where a
            # new member is added after the deadline — saved total reflects fewer students.
            cur.execute("""SELECT s.birthday,
                COALESCE(cc.fee,0) AS cult_fee, COALESCE(cc2.fee,0) AS cult_fee2
                FROM student_record sr JOIN student s ON s.id=sr.sid
                LEFT JOIN class_group_record cc  ON cc.id=sr.ccgrid
                LEFT JOIN class_group_record cc2 ON cc2.id=sr.ccgrid2
                WHERE s.fid=%s AND sr.pid=%s
                AND (sr.lcgrid IS NOT NULL OR sr.ccgrid IS NOT NULL
                     OR sr.ccgrid2 IS NOT NULL)""",
                (current_user.id, period_id))
            _old_rows   = cur.fetchall()
            _old_cult_t = sum(float(r.get('cult_fee') or 0) + float(r.get('cult_fee2') or 0)
                              for r in _old_rows)
            _old_minor_c = sum(1 for r in _old_rows if not _is_adult(r))
            _reg   = float(period.get('registration_fee') or 0)
            _pa    = float(period.get('pa_assignment_deposit') or 0)
            _saved = float(_existing_fpr['total_due'])
            if _old_minor_c > 0:
                _implied = (_saved - _old_cult_t - _reg - _pa) / _old_minor_c
                if abs(_implied - float(_gf)) <= 1.0:
                    eff_tuition  = float(_gf)
                    tuition_type = 'grandfathered'
                elif abs(_implied - _std) <= 1.0:
                    eff_tuition  = _std
                    tuition_type = 'standard'
                # else: unusual amount — keep _effective_tuition result

    student_subtotal = 0.0

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
            cur.execute(
                "INSERT INTO student_record (sid, pid, lcgrid, ccgrid, ccgrid2) "
                "VALUES (%s, %s, %s, %s, %s)",
                (sid, period_id, lid, cid, cid2)
            )

    # Total = student fees + registration fee + PA deposit
    total_due = _calc_total_family_fee(student_subtotal, period)

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

    # Determine effective tuition.
    # If registration already exists, back-calculate the real tuition from the
    # stored total_due — so grandfathered rates don't flip to full after deadline.
    eff_tuition, tuition_type = _effective_tuition(period, current_user.id, conn) if period else (0, 'standard')
    conn.close()

    saved_total = float(fpr['total_due']) if fpr and fpr.get('total_due') else None

    # Back-calculate eff_tuition from saved_total so the per-student breakdown
    # shows the rate actually used, not today's live-calculated rate.
    if saved_total is not None and period and raw_rows:
        total_cult_all = sum(
            float(r.get('cult_fee') or 0) + float(r.get('cult_fee2') or 0)
            for r in raw_rows
        )
        minor_count = sum(
            1 for r in raw_rows
            if not (_age(r.get('birthday')) is not None and _age(r.get('birthday')) >= 18)
        )
        if minor_count > 0:
            implied = (saved_total - total_cult_all - reg_fee - pa_fee) / minor_count
            std = float(period.get('tuition') or 0)
            gf  = float(period.get('grandfathered_tuition') or std)
            if abs(implied - gf) <= 1.0:
                eff_tuition  = gf
                tuition_type = 'grandfathered'
            elif abs(implied - std) <= 1.0:
                eff_tuition  = std
                tuition_type = 'standard'
            else:
                eff_tuition  = max(0, implied)  # unusual stored amount — use as-is
                tuition_type = 'standard'

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
            if cult_fee:  fee_note += f' ${cult_fee:,.0f}'
            if cult_fee2: fee_note += f' + ${cult_fee2:,.0f}'
        else:
            student_fee = eff_tuition + total_cult
            tuition_label = f'Tuition ${eff_tuition:,.0f}'
            if tuition_type == 'grandfathered':
                tuition_label += ' ✦'
            parts = [tuition_label]
            if cult_fee:  parts.append(f'Culture ${cult_fee:,.0f}')
            if cult_fee2: parts.append(f'Culture 2 ${cult_fee2:,.0f}')
            fee_note = ' + '.join(parts)

        student_subtotal += student_fee
        rows.append({**r,
                     'student_fee': student_fee,
                     'fee_note':    fee_note,
                     'age':         age,
                     'is_adult':    adult})

    # grand_total: use saved DB value as authoritative when available
    grand_total = saved_total if saved_total is not None else student_subtotal + reg_fee + pa_fee

    return render_template('family/fee_summary.html',
                           period=period, fpr=fpr,
                           rows=rows,
                           student_subtotal=student_subtotal,
                           reg_fee=reg_fee,
                           pa_fee=pa_fee,
                           grand_total=grand_total,
                           tuition_type=tuition_type,
                           eff_tuition=eff_tuition,
                           saved_total=saved_total)

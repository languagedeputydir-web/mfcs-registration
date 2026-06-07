"""
routes/admin.py  — full admin portal
Roles: admin | finance | language | culture
"""
import csv, io
from functools import wraps
from flask import (Blueprint, render_template, redirect, url_for,
                   request, flash, Response)
from flask_login import login_user, logout_user, login_required, current_user
import bcrypt
from db import get_db_connection
from models import Admin

admin_bp = Blueprint('admin', __name__)

# ── helpers ────────────────────────────────────────────────────────────────────

def _hash(p): return bcrypt.hashpw(p.encode(), bcrypt.gensalt(12)).decode()
def _check(p, h):
    try: return bcrypt.checkpw(p.encode(), h.encode())
    except: return False

def admin_required(f):
    @wraps(f)
    def d(*a,**k):
        if not current_user.is_authenticated or not isinstance(current_user, Admin):
            return redirect(url_for('admin.login'))
        return f(*a,**k)
    return d

def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def d(*a,**k):
            if not current_user.is_authenticated or not isinstance(current_user, Admin):
                return redirect(url_for('admin.login'))
            if current_user.role not in roles:
                flash('You do not have permission to access that page.', 'error')
                return redirect(url_for('admin.dashboard'))
            return f(*a,**k)
        return d
    return decorator

def _cur_period(cur):
    cur.execute("SELECT * FROM period ORDER BY id DESC LIMIT 1")
    return cur.fetchone()

def _periods_list(cur):
    cur.execute("SELECT id, name FROM period ORDER BY id DESC")
    return cur.fetchall()


def _recalc_family_record(cur, fid, pid):
    """Recalculate total_due for a family in a period based on current
    student_record selections. If total changed and status was Complete,
    reverts to Pending and appends a note.
    Returns (new_total, status_changed_to_pending)."""
    cur.execute("SELECT * FROM period WHERE id=%s", (pid,))
    period = cur.fetchone()
    if not period:
        return 0.0, False

    from routes.family import _is_adult, _calc_student_fee, _calc_total_family_fee
    cur.execute("""SELECT s.birthday, sr.lcgrid, sr.ccgrid, sr.ccgrid2,
        COALESCE(cc.fee,0) AS cult_fee, COALESCE(cc2.fee,0) AS cult_fee2
        FROM student s
        JOIN student_record sr ON sr.sid=s.id
        LEFT JOIN class_group_record cc  ON cc.id=sr.ccgrid
        LEFT JOIN class_group_record cc2 ON cc2.id=sr.ccgrid2
        WHERE s.fid=%s AND sr.pid=%s""", (fid, pid))
    rows = cur.fetchall()

    student_subtotal = 0.0
    for r in rows:
        student_subtotal += _calc_student_fee(
            {'birthday': r['birthday']}, period,
            float(r['cult_fee'] or 0), float(r['cult_fee2'] or 0)
        )
    new_total = _calc_total_family_fee(student_subtotal, period)

    cur.execute(
        "SELECT id, total_due, reg_status, description FROM family_record "
        "WHERE fid=%s AND pid=%s", (fid, pid)
    )
    fpr = cur.fetchone()
    if not fpr:
        return new_total, False

    old_total   = float(fpr['total_due'] or 0)
    old_status  = fpr['reg_status'] or 'Pending'
    fee_changed = abs(new_total - old_total) > 0.01

    if not fee_changed:
        cur.execute("UPDATE family_record SET total_due=%s, last_update=NOW() "
                    "WHERE id=%s", (new_total, fpr['id']))
        return new_total, False

    if old_status == 'Complete Registration':
        note = ((fpr['description'] or '') +
                f' [Fee updated ${old_total:.2f}→${new_total:.2f} by admin — review payment]')[:9999]
        cur.execute(
            "UPDATE family_record SET total_due=%s, reg_status='Pending', "
            "description=%s, last_update=NOW() WHERE id=%s",
            (new_total, note, fpr['id'])
        )
        return new_total, True
    else:
        cur.execute("UPDATE family_record SET total_due=%s, last_update=NOW() "
                    "WHERE id=%s", (new_total, fpr['id']))
        return new_total, False

# ── login / logout ─────────────────────────────────────────────────────────────

@admin_bp.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated and isinstance(current_user, Admin):
        return redirect(url_for('admin.dashboard'))
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM admins WHERE username=%s AND is_active=1", (u,))
        row = cur.fetchone(); conn.close()
        if row and _check(p, row['password_hash']):
            login_user(Admin(row))
            return redirect(url_for('admin.dashboard'))
        flash('Incorrect username or password.', 'error')
    return render_template('admin/login.html')

@admin_bp.route('/logout')
@admin_required
def logout():
    logout_user()
    return redirect(url_for('admin.login'))

# ── dashboard ──────────────────────────────────────────────────────────────────

@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur)
    registered_students = 0
    minor_students      = 0
    adult_students      = 0
    total_collected = 0.0
    unpaid_balance  = 0.0
    unpaid_families = 0
    fee_changed_count = 0
    if period:
        cur.execute("""SELECT COUNT(DISTINCT sr.sid) AS n
            FROM student_record sr WHERE sr.pid=%s""",(period['id'],))
        registered_students = cur.fetchone()['n']
        # Adult = registered for at least one adult-only culture class
        # Minor = all others
        cur.execute("""SELECT COUNT(DISTINCT sr.sid) AS n
            FROM student_record sr
            WHERE sr.pid=%s
            AND (
                EXISTS (SELECT 1 FROM class_group_record cc
                        WHERE cc.id=sr.ccgrid AND cc.adult_only=1)
             OR EXISTS (SELECT 1 FROM class_group_record cc2
                        WHERE cc2.id=sr.ccgrid2 AND cc2.adult_only=1)
            )""", (period['id'],))
        adult_students = int(cur.fetchone()['n'])
        minor_students = registered_students - adult_students
        cur.execute("""SELECT COALESCE(SUM(total_paid),0) AS tot
            FROM family_record WHERE pid=%s""",(period['id'],))
        total_collected = float(cur.fetchone()['tot'])
        cur.execute("""SELECT COALESCE(SUM(total_due - total_paid - COALESCE(adjustment,0)),0) AS bal,
            COUNT(*) AS n
            FROM family_record
            WHERE pid=%s AND (total_due - total_paid - COALESCE(adjustment,0)) > 0.01""",
            (period['id'],))
        row = cur.fetchone()
        unpaid_balance  = float(row['bal'])
        unpaid_families = int(row['n'])
        cur.execute("""SELECT COUNT(*) AS n FROM family_record
            WHERE pid=%s AND reg_status='Pending'
            AND description LIKE '%Fee updated%'""",(period['id'],))
        fee_changed_count = cur.fetchone()['n']
    conn.close()
    return render_template('admin/dashboard.html',
        period=period,
        registered_students=registered_students,
        minor_students=minor_students,
        adult_students=adult_students,
        total_collected=total_collected,
        unpaid_balance=unpaid_balance,
        unpaid_families=unpaid_families,
        fee_changed_count=fee_changed_count)

# ══ PERIODS ════════════════════════════════════════════════════════════════════

@admin_bp.route('/periods')
@roles_required('admin')
def periods():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM period ORDER BY id DESC")
    rows = cur.fetchall(); conn.close()
    return render_template('admin/periods.html', periods=rows)

@admin_bp.route('/periods/new', methods=['GET','POST'])
@roles_required('admin')
def new_period():
    if request.method == 'POST':
        f = request.form; name = f.get('name','').strip()
        if not name: flash('Name required.','error'); return render_template('admin/period_form.html',period=None)
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM period WHERE name=%s",(name,))
        if cur.fetchone(): conn.close(); flash('Name already exists.','error'); return render_template('admin/period_form.html',period=None)
        g_tuition  = f.get('grandfathered_tuition','').strip() or None
        g_deadline = f.get('grandfathered_deadline','').strip() or None
        cur.execute("""INSERT INTO period (name,tuition,grandfathered_tuition,grandfathered_deadline,
            registration_fee,pa_fee,pa_assignment_deposit,
            discount,late_fee,deadline,street_address,city,state,zip,
            check_title,attention,description,status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')""",
            (name,f.get('tuition','0'),g_tuition,g_deadline,
             f.get('registration_fee','0'),
             f.get('pa_fee','0'),f.get('pa_assignment_deposit','0'),
             f.get('discount','0'),f.get('late_fee','0'),
             f.get('deadline','').strip() or None,
             f.get('street_address','?'),f.get('city','?'),f.get('state','?'),f.get('zip','?'),
             f.get('check_title','?'),f.get('attention','?'),f.get('description','?')))
        conn.commit(); conn.close()
        flash(f'School year "{name}" created.','success')
        return redirect(url_for('admin.periods'))
    return render_template('admin/period_form.html', period=None)

@admin_bp.route('/periods/<int:pid>/edit', methods=['GET','POST'])
@roles_required('admin')
def edit_period(pid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); period = cur.fetchone()
    if not period: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.periods'))
    if request.method == 'POST':
        f = request.form
        g_tuition2  = f.get('grandfathered_tuition','').strip() or None
        g_deadline2 = f.get('grandfathered_deadline','').strip() or None
        cur.execute("""UPDATE period SET name=%s,tuition=%s,
            grandfathered_tuition=%s,grandfathered_deadline=%s,
            registration_fee=%s,pa_fee=%s,pa_assignment_deposit=%s,
            discount=%s,late_fee=%s,deadline=%s,
            street_address=%s,city=%s,state=%s,zip=%s,
            check_title=%s,attention=%s,description=%s WHERE id=%s""",
            (f.get('name',''),f.get('tuition','0'),g_tuition2,g_deadline2,
             f.get('registration_fee','0'),
             f.get('pa_fee','0'),f.get('pa_assignment_deposit','0'),
             f.get('discount','0'),f.get('late_fee','0'),
             f.get('deadline','').strip() or None,
             f.get('street_address','?'),f.get('city','?'),f.get('state','?'),f.get('zip','?'),
             f.get('check_title','?'),f.get('attention','?'),f.get('description','?'),pid))
        conn.commit(); conn.close(); flash('Period updated.','success')
        return redirect(url_for('admin.periods'))
    conn.close()
    return render_template('admin/period_form.html', period=period)

# ══ USERS ══════════════════════════════════════════════════════════════════════

@admin_bp.route('/users')
@roles_required('admin')
def users():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins ORDER BY role,username")
    rows = cur.fetchall(); conn.close()
    return render_template('admin/users.html', users=rows)

@admin_bp.route('/users/new', methods=['GET','POST'])
@roles_required('admin')
def new_user():
    if request.method == 'POST':
        f = request.form
        u = f.get('username','').strip().lower()
        p = f.get('password',''); r = f.get('role','finance')
        d = f.get('display_name','').strip()
        if not u or not p: flash('Username and password required.','error'); return render_template('admin/user_form.html',user=None)
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM admins WHERE username=%s",(u,))
        if cur.fetchone(): conn.close(); flash('Username taken.','error'); return render_template('admin/user_form.html',user=None)
        cur.execute("INSERT INTO admins (username,password_hash,display_name,role) VALUES(%s,%s,%s,%s)",(u,_hash(p),d,r))
        conn.commit(); conn.close(); flash(f'User "{u}" ({r}) created.','success')
        return redirect(url_for('admin.users'))
    return render_template('admin/user_form.html', user=None)

@admin_bp.route('/users/<int:uid>/edit', methods=['GET','POST'])
@roles_required('admin')
def edit_user(uid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins WHERE id=%s",(uid,)); user = cur.fetchone()
    if not user: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.users'))
    if request.method == 'POST':
        f = request.form; d = f.get('display_name','').strip()
        r = f.get('role',user['role']); active = 1 if f.get('is_active') else 0
        np = f.get('password','').strip()
        if np:
            cur.execute("UPDATE admins SET display_name=%s,role=%s,is_active=%s,password_hash=%s WHERE id=%s",(d,r,active,_hash(np),uid))
        else:
            cur.execute("UPDATE admins SET display_name=%s,role=%s,is_active=%s WHERE id=%s",(d,r,active,uid))
        conn.commit(); conn.close(); flash('User updated.','success')
        return redirect(url_for('admin.users'))
    conn.close()
    return render_template('admin/user_form.html', user=user)

# ══ TEACHER RECORD (teachers / TAs / SIs) ════════════════════════════════════
# Uses legacy table: teacher_record
# type values: 'Teacher', 'TA', 'SI'

@admin_bp.route('/staff')
@roles_required('admin','language','culture')
def staff_list():
    """Teachers and TAs."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    stype = request.args.get('type', 'all')
    teachers = []; sel_period = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel_period = cur.fetchone()
        q = "SELECT * FROM teacher_record WHERE pid=%s"
        params = [pid]
        if stype != 'all':
            q += " AND type=%s"; params.append(stype)
        cur.execute(q + " ORDER BY type,last_name,first_name", params)
        teachers = cur.fetchall()
    conn.close()
    return render_template('admin/staff_list.html',
        teachers=teachers, periods_list=plist,
        selected_period=sel_period, current_period=period,
        stype=stype, pid=pid)


@admin_bp.route('/facilities')
@roles_required('admin','language','culture')
def facility_list():
    """Facilities / rooms."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    facilities = []; sel_period = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel_period = cur.fetchone()
        cur.execute("SELECT * FROM facility_record WHERE pid=%s ORDER BY name",(pid,))
        facilities = cur.fetchall()
    conn.close()
    return render_template('admin/facility_list.html',
        facilities=facilities, periods_list=plist,
        selected_period=sel_period, current_period=period, pid=pid)

@admin_bp.route('/staff/teacher/new', methods=['GET','POST'])
@roles_required('admin','language','culture')
def new_teacher():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form
        pid = f.get('pid'); stype = f.get('type','Teacher')
        last = f.get('last_name','').strip(); first = f.get('first_name','').strip()
        phone = f.get('phone', '?').strip()
        if not pid or not last or not first or not phone:
            flash('Period, name and phone are required.','error')
            conn.close()
            return render_template('admin/teacher_form.html', teacher=None, periods_list=plist)
        import re as _re
        digits = _re.sub(r'\D', '', phone)
        if len(digits) < 7:
            flash('Phone number must contain at least 7 digits.','error')
            conn.close()
            return render_template('admin/teacher_form.html', teacher=None, periods_list=plist)
        # ssn is part of the legacy unique key 'trkey' (pid, ssn)
        # Use a unique placeholder so the constraint is never violated
        import uuid as _uuid
        ssn_val = f.get('ssn','').strip() or _uuid.uuid4().hex[:12]
        cur.execute("""INSERT INTO teacher_record
            (pid,type,last_name,first_name,chinese_name,gender,phone,email,
             street_address,city,state,zip,ssn,description,status,last_update)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',NOW())""",
            (pid,stype,last,first,
             f.get('chinese_name','?'),f.get('gender','?'),phone,
             f.get('email','?'),f.get('street_address','?'),
             f.get('city','?'),f.get('state','?'),f.get('zip','?'),
             ssn_val,f.get('description','?')))
        conn.commit(); conn.close()
        flash(f'{stype} {first} {last} added.','success')
        return redirect(url_for('admin.staff_list', pid=pid))
    conn.close()
    return render_template('admin/teacher_form.html', teacher=None, periods_list=plist)

@admin_bp.route('/staff/teacher/<int:tid>/edit', methods=['GET','POST'])
@roles_required('admin','language','culture')
def edit_teacher(tid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM teacher_record WHERE id=%s",(tid,)); t = cur.fetchone()
    if not t: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.staff_list'))
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form
        phone = f.get('phone', '?').strip()
        if not phone:
            flash('Phone is required.','error'); conn.close()
            return render_template('admin/teacher_form.html', teacher=t, periods_list=plist)
        cur.execute("""UPDATE teacher_record SET type=%s,last_name=%s,first_name=%s,
            chinese_name=%s,gender=%s,phone=%s,email=%s,
            street_address=%s,city=%s,state=%s,zip=%s,
            description=%s,last_update=NOW() WHERE id=%s""",
            (f.get('type','Teacher'),f.get('last_name',''),f.get('first_name',''),
             f.get('chinese_name','?'),f.get('gender','?'),phone,
             f.get('email','?'),f.get('street_address','?'),
             f.get('city','?'),f.get('state','?'),f.get('zip','?'),
             f.get('description','?'),tid))
        conn.commit(); conn.close(); flash('Teacher/TA updated.','success')
        return redirect(url_for('admin.staff_list', pid=t['pid']))
    conn.close()
    return render_template('admin/teacher_form.html', teacher=t, periods_list=plist)

# ══ FACILITY RECORD ═══════════════════════════════════════════════════════════
# Uses legacy table: facility_record

@admin_bp.route('/staff/facility/new', methods=['GET','POST'])
@roles_required('admin','language','culture')
def new_facility():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form
        pid = f.get('pid'); name = f.get('name','').strip()
        if not pid or not name:
            flash('Period and name are required.','error'); conn.close()
            return render_template('admin/facility_form.html', facility=None, periods_list=plist)
        cur.execute("""INSERT INTO facility_record
            (pid,name,chinese_name,max_capacity,description,status,last_update)
            VALUES(%s,%s,%s,%s,%s,'active',NOW())""",
            (pid,name,f.get('chinese_name','?'),
             f.get('max_capacity','-1'),f.get('description','?')))
        conn.commit(); conn.close()
        flash(f'Facility "{name}" added.','success')
        return redirect(url_for('admin.facility_list', pid=pid))
    conn.close()
    return render_template('admin/facility_form.html', facility=None, periods_list=plist)

@admin_bp.route('/staff/facility/<int:fid>/edit', methods=['GET','POST'])
@roles_required('admin','language','culture')
def edit_facility(fid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM facility_record WHERE id=%s",(fid,)); facility = cur.fetchone()
    if not facility: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.staff_list'))
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form
        cur.execute("""UPDATE facility_record SET name=%s,chinese_name=%s,
            max_capacity=%s,description=%s,last_update=NOW() WHERE id=%s""",
            (f.get('name',''),f.get('chinese_name','?'),
             f.get('max_capacity','-1'),f.get('description','?'),fid))
        conn.commit(); conn.close(); flash('Facility updated.','success')
        return redirect(url_for('admin.facility_list', pid=facility['pid']))
    conn.close()
    return render_template('admin/facility_form.html', facility=facility, periods_list=plist)

# ══ CLASS ASSIGNMENTS ═════════════════════════════════════════════════════════
# Uses legacy tables: class_record, class_assignment, teacher_record,
#                     facility_record, student_record
#
# class_assignment columns:
#   crid    → class_record.id       (the section)
#   trid    → teacher_record.id     (teacher or TA)
#   facrid  → facility_record.id    (facility/room)
#   frid    → family_record.id      (not used here)
#   srid    → student_record.id     (student enrollment)
#
# Assignments live at the section (class_record) level, not class group level.

@admin_bp.route('/classes/<int:cgrid>/assignments')
@roles_required('admin','language','culture')
def class_assignments(cgrid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s",(cgrid,)); cls = cur.fetchone()
    if not cls or 'pid' not in cls: conn.close(); flash('Class not found.','error'); return redirect(url_for('admin.dashboard'))

    # Sections for this class group
    cur.execute("SELECT * FROM class_record WHERE cgrid=%s ORDER BY name",(cgrid,))
    sections = cur.fetchall()

    # For each section, get its assignments
    section_data = []
    for sec in sections:
        # Assigned teachers/TAs
        cur.execute("""SELECT ca.*, tr.first_name, tr.last_name, tr.type AS teacher_type
            FROM class_assignment ca
            JOIN teacher_record tr ON tr.id=ca.trid
            WHERE ca.crid=%s AND ca.trid IS NOT NULL
            ORDER BY tr.type, tr.last_name""",(sec['id'],))
        teachers = cur.fetchall()

        # Assigned facility
        cur.execute("""SELECT ca.*, fr.name AS facility_name, fr.max_capacity
            FROM class_assignment ca
            JOIN facility_record fr ON fr.id=ca.facrid
            WHERE ca.crid=%s AND ca.facrid IS NOT NULL
            LIMIT 1""",(sec['id'],))
        facility = cur.fetchone()

        # Students already assigned to this section
        cur.execute("""SELECT ca.srid, s.first_name, s.last_name, s.chinese_name
            FROM class_assignment ca
            JOIN student_record sr ON sr.id=ca.srid
            JOIN student s ON s.id=sr.sid
            WHERE ca.crid=%s AND ca.srid IS NOT NULL
            ORDER BY s.last_name, s.first_name""",(sec['id'],))
        enrolled = cur.fetchall()
        enrolled_srids = {r['srid'] for r in enrolled}

        # Students registered for this class group but not yet in this section
        # (language: lcgrid=cgrid, culture: ccgrid=cgrid OR ccgrid2=cgrid)
        if cls['type'] == 'language':
            cur.execute("""SELECT sr.id AS srid, s.first_name, s.last_name, s.chinese_name
                FROM student_record sr JOIN student s ON s.id=sr.sid
                WHERE sr.pid=%s AND sr.lcgrid=%s
                ORDER BY s.last_name, s.first_name""",(cls['pid'], cgrid))
        else:
            cur.execute("""SELECT sr.id AS srid, s.first_name, s.last_name, s.chinese_name
                FROM student_record sr JOIN student s ON s.id=sr.sid
                WHERE sr.pid=%s AND (sr.ccgrid=%s OR sr.ccgrid2=%s)
                ORDER BY s.last_name, s.first_name""",(cls['pid'], cgrid, cgrid))
        all_registered = cur.fetchall()
        # Filter out already-assigned ones
        available = [r for r in all_registered if r['srid'] not in enrolled_srids]

        section_data.append({
            'section': sec,
            'teachers': teachers,
            'facility': facility,
            'enrolled': enrolled,
            'registered_students': available,
        })

    # Available teachers/TAs for this period
    cur.execute("""SELECT * FROM teacher_record WHERE pid=%s
        ORDER BY type,last_name,first_name""",(cls['pid'],))
    all_teachers = cur.fetchall()

    # Available facilities for this period
    cur.execute("SELECT * FROM facility_record WHERE pid=%s ORDER BY name",(cls['pid'],))
    all_facilities = cur.fetchall()

    conn.close()
    back_url = url_for('admin.language_classes', pid=cls['pid']) if cls['type']=='language'                else url_for('admin.culture_classes', pid=cls['pid'])
    return render_template('admin/class_assignments.html',
        cls=cls, sections=section_data,
        all_teachers=all_teachers, all_facilities=all_facilities,
        back_url=back_url)

@admin_bp.route('/classes/<int:cgrid>/sections/new', methods=['POST'])
@roles_required('admin','language','culture')
def new_section(cgrid):
    """Add a new section (class_record) under a class group."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s",(cgrid,)); cls = cur.fetchone()
    name = request.form.get('section_name','').strip()
    if cls and name:
        try:
            cur.execute("""INSERT INTO class_record (cgrid,name,chinese_name,min_size,max_size,description,status,last_update)
                VALUES(%s,%s,%s,%s,%s,%s,'active',NOW())""",
                (cgrid,name,request.form.get('chinese_name','?'),
                 request.form.get('min_size',1),request.form.get('max_size',50),
                 request.form.get('description','?')))
            conn.commit(); flash(f'Section "{name}" added.','success')
        except Exception as e:
            conn.rollback(); flash(f'Could not add section: {e}','error')
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))

@admin_bp.route('/sections/<int:crid>/assign-teacher', methods=['POST'])
@roles_required('admin','language','culture')
def assign_teacher(crid):
    """Assign a TEACHER to a section. Teachers: max 1 language + 1 culture per year."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""SELECT cr.*, cgr.pid, cgr.type AS class_type
        FROM class_record cr JOIN class_group_record cgr ON cgr.id=cr.cgrid
        WHERE cr.id=%s""",(crid,))
    sec = cur.fetchone()
    trid  = request.form.get('trid')
    cgrid = request.form.get('cgrid')
    if sec and trid:
        trid = int(trid)
        # Check teacher is actually type 'Teacher'
        cur.execute("SELECT type FROM teacher_record WHERE id=%s",(trid,))
        tr = cur.fetchone()
        if tr and tr['type'] != 'Teacher':
            flash('Selected staff is not a Teacher. Use the TA slot instead.','error')
            conn.close()
            return redirect(url_for('admin.class_assignments', cgrid=cgrid))
        # Enforce: teacher can only have 1 class of each type per period
        cur.execute("""SELECT COUNT(*) AS n FROM class_assignment ca
            JOIN class_record cr2 ON cr2.id=ca.crid
            JOIN class_group_record cgr2 ON cgr2.id=cr2.cgrid
            WHERE ca.trid=%s AND cgr2.pid=%s AND cgr2.type=%s""",
            (trid, sec['pid'], sec['class_type']))
        row = cur.fetchone()
        if row and row['n'] >= 1:
            flash(f'This teacher is already assigned to a {sec["class_type"]} class this year.','error')
            conn.close()
            return redirect(url_for('admin.class_assignments', cgrid=cgrid))
        try:
            cur.execute("INSERT INTO class_assignment (crid,trid) VALUES(%s,%s)",(crid,trid))
            conn.commit(); flash('Teacher assigned.','success')
        except: conn.rollback(); flash('Already assigned to this section.','warning')
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))


@admin_bp.route('/sections/<int:crid>/assign-ta', methods=['POST'])
@roles_required('admin','language','culture')
def assign_ta(crid):
    """Assign a TA to a section. TAs: max 1 language + 1 culture per year."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""SELECT cr.*, cgr.pid, cgr.type AS class_type
        FROM class_record cr JOIN class_group_record cgr ON cgr.id=cr.cgrid
        WHERE cr.id=%s""",(crid,))
    sec = cur.fetchone()
    trid  = request.form.get('trid')
    cgrid = request.form.get('cgrid')
    if sec and trid:
        trid = int(trid)
        cur.execute("SELECT type FROM teacher_record WHERE id=%s",(trid,))
        tr = cur.fetchone()
        if tr and tr['type'] != 'TA':
            flash('Selected staff is not a TA. Use the Teacher slot instead.','error')
            conn.close()
            return redirect(url_for('admin.class_assignments', cgrid=cgrid))
        # Enforce: TA can only have 1 class of each type per period
        cur.execute("""SELECT COUNT(*) AS n FROM class_assignment ca
            JOIN class_record cr2 ON cr2.id=ca.crid
            JOIN class_group_record cgr2 ON cgr2.id=cr2.cgrid
            WHERE ca.trid=%s AND cgr2.pid=%s AND cgr2.type=%s""",
            (trid, sec['pid'], sec['class_type']))
        row = cur.fetchone()
        if row and row['n'] >= 1:
            flash(f'This TA is already assigned to a {sec["class_type"]} class this year.','error')
            conn.close()
            return redirect(url_for('admin.class_assignments', cgrid=cgrid))
        try:
            cur.execute("INSERT INTO class_assignment (crid,trid) VALUES(%s,%s)",(crid,trid))
            conn.commit(); flash('TA assigned.','success')
        except: conn.rollback(); flash('Already assigned to this section.','warning')
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))

@admin_bp.route('/sections/<int:crid>/assign-facility', methods=['POST'])
@roles_required('admin','language','culture')
def assign_facility(crid):
    """Assign a facility to a section (replaces existing)."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    facrid = request.form.get('facrid')
    cgrid  = request.form.get('cgrid')
    if facrid:
        # Remove old facility assignment for this section first
        cur.execute("DELETE FROM class_assignment WHERE crid=%s AND facrid IS NOT NULL AND trid IS NULL AND srid IS NULL",(crid,))
        cur.execute("INSERT INTO class_assignment (crid,facrid) VALUES(%s,%s)",(crid,int(facrid)))
        conn.commit(); flash('Facility assigned.','success')
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))

@admin_bp.route('/sections/<int:crid>/remove-assignment', methods=['POST'])
@roles_required('admin','language','culture')
def remove_assignment(crid):
    """Remove a teacher or facility assignment row."""
    conn = get_db_connection(); cur = conn.cursor()
    trid   = request.form.get('trid')
    facrid = request.form.get('facrid')
    cgrid  = request.form.get('cgrid')
    if trid:
        cur.execute("DELETE FROM class_assignment WHERE crid=%s AND trid=%s",(crid,int(trid)))
    elif facrid:
        cur.execute("DELETE FROM class_assignment WHERE crid=%s AND facrid=%s",(crid,int(facrid)))
    conn.commit(); conn.close()
    flash('Assignment removed.','success')
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))


@admin_bp.route('/sections/<int:crid>/assign-student', methods=['POST'])
@roles_required('admin','language','culture')
def assign_student(crid):
    """Assign a student to a section.
    Rules: 0-1 language sections, 0-2 culture sections per student per period."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    srid  = request.form.get('srid')
    cgrid = request.form.get('cgrid')
    if srid:
        srid = int(srid)
        # Get class type for this section
        cur.execute("""SELECT cgr.type AS class_type, cgr.pid
            FROM class_record cr JOIN class_group_record cgr ON cgr.id=cr.cgrid
            WHERE cr.id=%s""",(crid,))
        sec = cur.fetchone()
        if sec:
            class_type = sec['class_type']
            # Count existing section assignments of this type for this student
            cur.execute("""SELECT COUNT(*) AS n FROM class_assignment ca
                JOIN class_record cr2 ON cr2.id=ca.crid
                JOIN class_group_record cgr2 ON cgr2.id=cr2.cgrid
                WHERE ca.srid=%s AND cgr2.pid=%s AND cgr2.type=%s""",
                (srid, sec['pid'], class_type))
            row = cur.fetchone()
            limit = 1 if class_type == 'language' else 2
            if row and row['n'] >= limit:
                flash(f'Student already assigned to the maximum {limit} {class_type} section(s) this year.','error')
                conn.close()
                return redirect(url_for('admin.class_assignments', cgrid=cgrid))
        try:
            cur.execute("INSERT INTO class_assignment (crid,srid) VALUES(%s,%s)",(crid,srid))
            # Recalculate family total after class change
            cur.execute("SELECT sid FROM student_record WHERE id=%s",(srid,))
            sr = cur.fetchone()
            if sr:
                cur.execute("SELECT fid FROM student WHERE id=%s",(sr['sid'],))
                stu = cur.fetchone()
                if stu and sec:
                    new_total, reverted = _recalc_family_record(cur, stu['fid'], sec['pid'])
                    if reverted:
                        flash(f'Student assigned. Fee changed to ${new_total:.2f} — '
                              f'registration set back to Pending.','warning')
                    else:
                        flash('Student assigned to section.','success')
                else:
                    flash('Student assigned to section.','success')
            else:
                flash('Student assigned to section.','success')
            conn.commit()
        except Exception as ex:
            conn.rollback()
            flash('Student already assigned to this section.','warning')
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))

@admin_bp.route('/sections/<int:crid>/remove-student', methods=['POST'])
@roles_required('admin','language','culture')
def remove_student(crid):
    """Remove a student assignment from a section and recalculate family total."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    srid  = request.form.get('srid')
    cgrid = request.form.get('cgrid')
    if srid:
        srid = int(srid)
        # Capture family/period info before deleting
        cur.execute("""SELECT s.fid, cgr.pid
            FROM class_assignment ca
            JOIN student_record sr ON sr.id=ca.srid
            JOIN student s ON s.id=sr.sid
            JOIN class_record cr ON cr.id=ca.crid
            JOIN class_group_record cgr ON cgr.id=cr.cgrid
            WHERE ca.crid=%s AND ca.srid=%s""", (crid, srid))
        ca_info = cur.fetchone()
        cur2 = conn.cursor()
        cur2.execute("DELETE FROM class_assignment WHERE crid=%s AND srid=%s", (crid, srid))
        if ca_info:
            new_total, reverted = _recalc_family_record(cur, ca_info['fid'], ca_info['pid'])
            if reverted:
                flash(f'Student removed. Fee changed to ${new_total:.2f} — '
                      f'registration set back to Pending.', 'warning')
            else:
                flash('Student removed from section.', 'success')
        else:
            flash('Student removed from section.', 'success')
        conn.commit()
    conn.close()
    return redirect(url_for('admin.class_assignments', cgrid=cgrid))


@admin_bp.route('/language/<int:cid>/delete', methods=['POST'])
@roles_required('admin','language')
def delete_language_class(cid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s AND type='language'",(cid,))
    cls = cur.fetchone()
    if not cls: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.language_classes'))
    pid = cls['pid']
    cur.execute("DELETE FROM class_assignment WHERE crid IN (SELECT id FROM class_record WHERE cgrid=%s)",(cid,))
    cur.execute("DELETE FROM class_record WHERE cgrid=%s",(cid,))
    cur.execute("DELETE FROM class_group_record WHERE id=%s",(cid,))
    conn.commit(); conn.close()
    flash(f'Language class "{cls["name"]}" removed.','success')
    return redirect(url_for('admin.language_classes', pid=pid))


@admin_bp.route('/culture/<int:cid>/delete', methods=['POST'])
@roles_required('admin','culture')
def delete_culture_class(cid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s AND type='culture'",(cid,))
    cls = cur.fetchone()
    if not cls: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.culture_classes'))
    pid = cls['pid']
    cur.execute("DELETE FROM class_assignment WHERE crid IN (SELECT id FROM class_record WHERE cgrid=%s)",(cid,))
    cur.execute("DELETE FROM class_record WHERE cgrid=%s",(cid,))
    cur.execute("DELETE FROM class_group_record WHERE id=%s",(cid,))
    conn.commit(); conn.close()
    flash(f'Culture class "{cls["name"]}" removed.','success')
    return redirect(url_for('admin.culture_classes', pid=pid))

# ══ LANGUAGE CLASSES ══════════════════════════════════════════════════════════

@admin_bp.route('/language')
@roles_required('admin','language')
def language_classes():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    classes = []; sel = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel = cur.fetchone()
        cur.execute("""SELECT cgr.*,
            (SELECT COUNT(DISTINCT ca.trid)
             FROM class_record cr2 JOIN class_assignment ca ON ca.crid=cr2.id
             JOIN teacher_record tr ON tr.id=ca.trid
             WHERE cr2.cgrid=cgr.id AND tr.type='Teacher') AS teacher_count,
            (SELECT GROUP_CONCAT(DISTINCT CONCAT(tr2.last_name,', ',tr2.first_name) ORDER BY tr2.last_name SEPARATOR '; ')
             FROM class_record cr3 JOIN class_assignment ca2 ON ca2.crid=cr3.id
             JOIN teacher_record tr2 ON tr2.id=ca2.trid
             WHERE cr3.cgrid=cgr.id AND tr2.type='Teacher') AS teachers,
            (SELECT GROUP_CONCAT(DISTINCT CONCAT(tr3.last_name,', ',tr3.first_name) ORDER BY tr3.last_name SEPARATOR '; ')
             FROM class_record cr4 JOIN class_assignment ca3 ON ca3.crid=cr4.id
             JOIN teacher_record tr3 ON tr3.id=ca3.trid
             WHERE cr4.cgrid=cgr.id AND tr3.type='TA') AS tas,
            (SELECT COUNT(DISTINCT ca4.srid)
             FROM class_record cr5 JOIN class_assignment ca4 ON ca4.crid=cr5.id
             WHERE cr5.cgrid=cgr.id AND ca4.srid IS NOT NULL) AS student_count,
            (SELECT GROUP_CONCAT(DISTINCT fr2.name ORDER BY fr2.name SEPARATOR '; ')
             FROM class_record cr6 JOIN class_assignment ca5 ON ca5.crid=cr6.id
             JOIN facility_record fr2 ON fr2.id=ca5.facrid
             WHERE cr6.cgrid=cgr.id AND ca5.facrid IS NOT NULL) AS facilities
            FROM class_group_record cgr
            WHERE cgr.pid=%s AND cgr.type='language' ORDER BY cgr.name""",(pid,))
        classes = cur.fetchall()
    conn.close()
    return render_template('admin/language_classes.html',
        classes=classes, periods_list=plist, selected_period=sel, current_period=period)

@admin_bp.route('/language/new', methods=['GET','POST'])
@roles_required('admin','language')
def new_language_class():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form; pid = f.get('pid'); name = f.get('name','').strip()
        if not pid or not name:
            flash('Period and name required.','error'); conn.close()
            return render_template('admin/class_form.html',cls=None,class_type='language',periods_list=plist)
        cur.execute("""INSERT INTO class_group_record
            (pid,name,chinese_name,type,fee,misc_fee,late_fee,discount,min_size,max_size,description,status,allow_as_second)
            VALUES(%s,%s,%s,'language',0,0,0,0,%s,%s,%s,'active',0)""",
            (pid,name,f.get('chinese_name','?'),f.get('min_size','1'),f.get('max_size','50'),f.get('description','?')))
        cgrid = cur.lastrowid
        cur.execute("""INSERT INTO class_record (cgrid,name,chinese_name,min_size,max_size,description,status,last_update)
            VALUES(%s,'Section 1','?',1,50,'?','active',NOW())""",(cgrid,))
        conn.commit(); conn.close()
        flash(f'Language class "{name}" added.','success')
        return redirect(url_for('admin.language_classes', pid=pid))
    conn.close()
    return render_template('admin/class_form.html',cls=None,class_type='language',periods_list=plist)

@admin_bp.route('/language/<int:cid>/edit', methods=['GET','POST'])
@roles_required('admin','language')
def edit_language_class(cid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s AND type='language'",(cid,)); cls = cur.fetchone()
    if not cls: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.language_classes'))
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form
        cur.execute("""UPDATE class_group_record SET pid=%s,name=%s,chinese_name=%s,
            fee=0,misc_fee=0,late_fee=0,discount=0,min_size=%s,max_size=%s,description=%s WHERE id=%s""",
            (f.get('pid'),f.get('name',''),f.get('chinese_name', '?'),
             f.get('min_size','1'),f.get('max_size','50'),f.get('description', '?'),cid))
        conn.commit(); conn.close(); flash('Language class updated.','success')
        return redirect(url_for('admin.language_classes', pid=cls['pid']))
    conn.close()
    return render_template('admin/class_form.html',cls=cls,class_type='language',periods_list=plist)

# ══ CULTURE CLASSES ══════════════════════════════════════════════════════════

@admin_bp.route('/culture')
@roles_required('admin','culture')
def culture_classes():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    classes = []; sel = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel = cur.fetchone()
        cur.execute("""SELECT cgr.*,
            (SELECT COUNT(DISTINCT ca.trid)
             FROM class_record cr2 JOIN class_assignment ca ON ca.crid=cr2.id
             JOIN teacher_record tr ON tr.id=ca.trid
             WHERE cr2.cgrid=cgr.id AND tr.type='Teacher') AS teacher_count,
            (SELECT GROUP_CONCAT(DISTINCT CONCAT(tr2.last_name,', ',tr2.first_name) ORDER BY tr2.last_name SEPARATOR '; ')
             FROM class_record cr3 JOIN class_assignment ca2 ON ca2.crid=cr3.id
             JOIN teacher_record tr2 ON tr2.id=ca2.trid
             WHERE cr3.cgrid=cgr.id AND tr2.type='Teacher') AS teachers,
            (SELECT GROUP_CONCAT(DISTINCT CONCAT(tr3.last_name,', ',tr3.first_name) ORDER BY tr3.last_name SEPARATOR '; ')
             FROM class_record cr4 JOIN class_assignment ca3 ON ca3.crid=cr4.id
             JOIN teacher_record tr3 ON tr3.id=ca3.trid
             WHERE cr4.cgrid=cgr.id AND tr3.type='TA') AS tas,
            (SELECT COUNT(DISTINCT ca4.srid)
             FROM class_record cr5 JOIN class_assignment ca4 ON ca4.crid=cr5.id
             WHERE cr5.cgrid=cgr.id AND ca4.srid IS NOT NULL) AS student_count,
            (SELECT GROUP_CONCAT(DISTINCT fr2.name ORDER BY fr2.name SEPARATOR '; ')
             FROM class_record cr6 JOIN class_assignment ca5 ON ca5.crid=cr6.id
             JOIN facility_record fr2 ON fr2.id=ca5.facrid
             WHERE cr6.cgrid=cgr.id AND ca5.facrid IS NOT NULL) AS facilities
            FROM class_group_record cgr
            WHERE cgr.pid=%s AND cgr.type='culture' ORDER BY cgr.name""",(pid,))
        classes = cur.fetchall()
    conn.close()
    return render_template('admin/culture_classes.html',
        classes=classes, periods_list=plist, selected_period=sel, current_period=period)

@admin_bp.route('/culture/new', methods=['GET','POST'])
@roles_required('admin','culture')
def new_culture_class():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form; pid = f.get('pid'); name = f.get('name','').strip()
        if not pid or not name:
            flash('Period and name required.','error'); conn.close()
            return render_template('admin/class_form.html',cls=None,class_type='culture',periods_list=plist)
        allow2 = 1 if f.get('allow_as_second') else 0
        adult_only = 1 if f.get('adult_only') else 0
        cur.execute("""INSERT INTO class_group_record
            (pid,name,chinese_name,type,fee,misc_fee,late_fee,discount,min_size,max_size,description,status,allow_as_second,adult_only)
            VALUES(%s,%s,%s,'culture',%s,0,%s,%s,%s,%s,%s,'active',%s,%s)""",
            (pid,name,f.get('chinese_name','?'),f.get('fee','0'),
             f.get('late_fee','0'),f.get('discount','0'),
             f.get('min_size','1'),f.get('max_size','50'),f.get('description','?'),allow2,adult_only))
        cgrid2 = cur.lastrowid
        cur.execute("""INSERT INTO class_record (cgrid,name,chinese_name,min_size,max_size,description,status,last_update)
            VALUES(%s,'Section 1','?',1,50,'?','active',NOW())""",(cgrid2,))
        conn.commit(); conn.close()
        flash(f'Culture class "{name}" added.','success')
        return redirect(url_for('admin.culture_classes', pid=pid))
    conn.close()
    return render_template('admin/class_form.html',cls=None,class_type='culture',periods_list=plist)

@admin_bp.route('/culture/<int:cid>/edit', methods=['GET','POST'])
@roles_required('admin','culture')
def edit_culture_class(cid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM class_group_record WHERE id=%s AND type='culture'",(cid,)); cls = cur.fetchone()
    if not cls: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.culture_classes'))
    plist = _periods_list(cur)
    if request.method == 'POST':
        f = request.form; allow2 = 1 if f.get('allow_as_second') else 0
        adult_only2 = 1 if f.get('adult_only') else 0
        cur.execute("""UPDATE class_group_record SET pid=%s,name=%s,chinese_name=%s,
            fee=%s,misc_fee=0,late_fee=%s,discount=%s,min_size=%s,max_size=%s,
            description=%s,allow_as_second=%s,adult_only=%s WHERE id=%s""",
            (f.get('pid'),f.get('name',''),f.get('chinese_name','?'),
             f.get('fee','0'),f.get('late_fee','0'),f.get('discount','0'),
             f.get('min_size','1'),f.get('max_size','50'),f.get('description','?'),
             allow2,adult_only2,cid))
        conn.commit(); conn.close(); flash('Culture class updated.','success')
        return redirect(url_for('admin.culture_classes', pid=cls['pid']))
    conn.close()
    return render_template('admin/class_form.html',cls=cls,class_type='culture',periods_list=plist)

# ══ FINANCE ════════════════════════════════════════════════════════════════════

@admin_bp.route('/finance')
@roles_required('admin','finance')
def finance():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    status = request.args.get('status','all')
    sel = None; regs = []
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel = cur.fetchone()
        sc = ''
        if status=='complete': sc = "AND fr.reg_status='Complete Registration'"
        elif status=='pending': sc = "AND fr.reg_status='Pending'"
        cur.execute(f"""SELECT f.id AS family_id,f.last_name_0,f.first_name_0,
            f.primary_email,f.primary_phone,
            fr.id AS fpr_id,fr.total_due,fr.total_paid,fr.adjustment,
            fr.reg_status,fr.description,fr.last_update,
            COUNT(s.id) AS student_count
            FROM family_record fr JOIN family f ON f.id=fr.fid
            LEFT JOIN student s ON s.fid=f.id
            WHERE fr.pid=%s {sc} GROUP BY fr.id
            ORDER BY f.last_name_0,f.first_name_0""",(pid,))
        regs = cur.fetchall()
        for r in regs:
            r['balance'] = float(r['total_due'] or 0)-float(r['total_paid'] or 0)-float(r['adjustment'] or 0)
    conn.close()
    return render_template('admin/finance.html',
        periods_list=plist, selected_period=sel, registrations=regs,
        status_filter=status, current_period=period, pid=pid)

@admin_bp.route('/finance/update', methods=['POST'])
@roles_required('admin','finance')
def finance_update():
    fpr_id = request.form.get('fpr_id')
    total_paid = request.form.get('total_paid','').strip()
    note = request.form.get('description','').strip()
    mark_done = request.form.get('mark_complete')
    pid = request.form.get('pid')
    if not fpr_id: flash('No record.','error'); return redirect(url_for('admin.finance'))
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM family_record WHERE id=%s",(fpr_id,)); fpr = cur.fetchone()
    if not fpr: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.finance',pid=pid))
    updates = ["description=%s","last_update=NOW()"]; params = [note]
    if total_paid:
        try: updates.append("total_paid=%s"); params.append(float(total_paid))
        except: flash('Invalid amount.','error'); conn.close(); return redirect(url_for('admin.finance',pid=pid))
    new_status = request.form.get('reg_status','').strip()
    if new_status in ('Complete Registration','Pending'):
        updates.append("reg_status=%s"); params.append(new_status)
    params.append(int(fpr_id))
    cur.execute(f"UPDATE family_record SET {', '.join(updates)} WHERE id=%s", params)
    conn.commit(); conn.close(); flash('Payment updated.','success')
    return redirect(url_for('admin.finance', pid=pid))

# ══ FAMILIES ══════════════════════════════════════════════════════════════════

@admin_bp.route('/families')
@roles_required('admin','finance')
def families():
    q    = request.args.get('q','').strip()
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur)
    plist  = _periods_list(cur)
    # pid='' means "All Families"; default to current period
    pid_raw = request.args.get('pid', str(period['id']) if period else '')
    pid = pid_raw if pid_raw else None
    sel_period = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,))
        sel_period = cur.fetchone()

    if pid:
        # Only families registered for this school year (INNER JOIN on family_record)
        filter_pid = int(pid)
        if q:
            like = f"%{q}%"
            cur.execute("""SELECT f.*,COUNT(DISTINCT s.id) AS student_count,
                fr.reg_status, fr.total_due, fr.total_paid, fr.adjustment
                FROM family_record fr
                JOIN family f ON f.id=fr.fid
                LEFT JOIN student s ON s.fid=f.id
                WHERE fr.pid=%s
                AND (f.last_name_0 LIKE %s OR f.first_name_0 LIKE %s
                     OR f.primary_email LIKE %s)
                GROUP BY f.id, fr.id ORDER BY f.last_name_0,f.first_name_0""",
                (filter_pid,like,like,like))
        else:
            cur.execute("""SELECT f.*,COUNT(DISTINCT s.id) AS student_count,
                fr.reg_status, fr.total_due, fr.total_paid, fr.adjustment
                FROM family_record fr
                JOIN family f ON f.id=fr.fid
                LEFT JOIN student s ON s.fid=f.id
                WHERE fr.pid=%s
                GROUP BY f.id, fr.id ORDER BY f.last_name_0,f.first_name_0""",
                (filter_pid,))
    else:
        # No year selected — show all families without fee info
        if q:
            like = f"%{q}%"
            cur.execute("""SELECT f.*,COUNT(DISTINCT s.id) AS student_count,
                NULL AS reg_status, NULL AS total_due,
                NULL AS total_paid, NULL AS adjustment
                FROM family f LEFT JOIN student s ON s.fid=f.id
                WHERE f.last_name_0 LIKE %s OR f.first_name_0 LIKE %s
                OR f.primary_email LIKE %s
                GROUP BY f.id ORDER BY f.last_name_0,f.first_name_0""",
                (like,like,like))
        else:
            cur.execute("""SELECT f.*,COUNT(DISTINCT s.id) AS student_count,
                NULL AS reg_status, NULL AS total_due,
                NULL AS total_paid, NULL AS adjustment
                FROM family f LEFT JOIN student s ON s.fid=f.id
                GROUP BY f.id ORDER BY f.last_name_0,f.first_name_0""")
    rows = cur.fetchall(); conn.close()
    return render_template('admin/families.html',
        families=rows, period=period, sel_period=sel_period,
        periods_list=plist, q=q, pid=pid)

@admin_bp.route('/families/<int:fid>')
@roles_required('admin','finance','language','culture')
def family_detail(fid):
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM family WHERE id=%s",(fid,)); family = cur.fetchone()
    if not family: conn.close(); flash('Not found.','error'); return redirect(url_for('admin.families'))
    cur.execute("SELECT * FROM student WHERE fid=%s ORDER BY last_name,first_name",(fid,)); students = cur.fetchall()
    cur.execute("""SELECT fr.id AS fpr_id, fr.fid, fr.pid,
        fr.total_due, fr.total_paid, fr.adjustment,
        fr.reg_status, fr.description AS payment_note,
        fr.last_update AS fr_last_update,
        p.name AS period_name
        FROM family_record fr
        JOIN period p ON p.id=fr.pid
        WHERE fr.fid=%s ORDER BY p.id DESC""",(fid,)); family_records = cur.fetchall()
    cur.execute("""SELECT
        sr.id AS sr_id, sr.sid, sr.pid, sr.lcgrid, sr.ccgrid, sr.ccgrid2,
        s.first_name, s.last_name, s.birthday,
        p.name AS period_name, p.tuition AS period_tuition,
        lc.name AS lang_class_name,
        cc.name  AS cult_class_name,  COALESCE(cc.fee,0)  AS cult_fee,
        cc2.name AS cult_class2_name, COALESCE(cc2.fee,0) AS cult_fee2
        FROM student_record sr
        JOIN student s ON s.id=sr.sid
        JOIN period p ON p.id=sr.pid
        LEFT JOIN class_group_record lc  ON lc.id=sr.lcgrid
        LEFT JOIN class_group_record cc  ON cc.id=sr.ccgrid
        LEFT JOIN class_group_record cc2 ON cc2.id=sr.ccgrid2
        WHERE s.fid=%s ORDER BY p.id DESC, s.last_name""",(fid,)); student_records = cur.fetchall()
    period = _cur_period(cur); current_fpr = None
    if period:
        cur.execute("SELECT * FROM family_record WHERE fid=%s AND pid=%s",(fid,period['id'])); current_fpr = cur.fetchone()
    conn.close()
    # Only keep student_records where at least one class is selected
    student_records = [sr for sr in student_records
                       if sr.get('lcgrid') or sr.get('ccgrid') or sr.get('ccgrid2')]
    # Calculate per-student fee for display.
    # Use a fresh connection — original conn was already closed above.
    from routes.family import _age, _is_adult, _calc_student_fee

    conn2 = get_db_connection(); cur2 = conn2.cursor(dictionary=True)
    tuition_by_pid = {}
    for fr in family_records:
        pid_key = fr['pid']
        stored_total = float(fr.get('total_due') or 0)
        cur2.execute("SELECT * FROM period WHERE id=%s", (pid_key,))
        p = cur2.fetchone()
        if not p:
            tuition_by_pid[pid_key] = 0.0
            continue
        pid_students = [sr for sr in student_records
                        if sr.get('pid') == pid_key]
        minor_count = sum(1 for sr in pid_students
                         if not _is_adult({'birthday': sr.get('birthday')}))
        cult_total = sum(
            float(sr.get('cult_fee', 0) or 0) + float(sr.get('cult_fee2', 0) or 0)
            for sr in pid_students
        )
        reg_fee = float(p.get('registration_fee') or 0)
        pa_fee  = float(p.get('pa_assignment_deposit') or 0)
        if minor_count > 0 and stored_total > 0:
            implied_tuition = (stored_total - cult_total - reg_fee - pa_fee) / minor_count
            std = float(p.get('tuition') or 0)
            gf  = float(p.get('grandfathered_tuition') or std)
            if abs(implied_tuition - gf) < 1.0:
                tuition_by_pid[pid_key] = gf
            elif abs(implied_tuition - std) < 1.0:
                tuition_by_pid[pid_key] = std
            else:
                tuition_by_pid[pid_key] = implied_tuition
        else:
            tuition_by_pid[pid_key] = float(p.get('tuition') or 0)
    conn2.close()

    for sr in student_records:
        student_dict = {'birthday': sr.get('birthday')}
        eff_t = tuition_by_pid.get(sr.get('pid'), float(sr.get('period_tuition', 0) or 0))
        period_dict  = {'tuition': eff_t,
                        'registration_fee': 0, 'pa_assignment_deposit': 0}
        cult_fee  = float(sr.get('cult_fee',  0) or 0)
        cult_fee2 = float(sr.get('cult_fee2', 0) or 0)
        sr['display_fee'] = _calc_student_fee(student_dict, period_dict, cult_fee, cult_fee2)
    return render_template('admin/family_detail.html',
        family=family,students=students,family_records=family_records,
        student_records=student_records,period=period,current_fpr=current_fpr)

@admin_bp.route('/families/new', methods=['GET','POST'])
@roles_required('admin','finance')
def new_family():
    if request.method == 'POST':
        f = request.form; email = f.get('primary_email','').strip().lower()
        pw = f.get('password',''); phone = f.get('primary_phone','').strip()
        if not email or not pw or not phone:
            flash('Email, phone and password are required.','error')
            return render_template('admin/new_family.html')
        conn = get_db_connection(); cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM family WHERE LOWER(primary_email)=%s",(email,))
        if cur.fetchone(): conn.close(); flash('Email already exists.','error'); return render_template('admin/new_family.html')
        cur.execute("""INSERT INTO family
            (primary_email,password,password_hash,last_name_0,first_name_0,
             last_name_1,first_name_1,primary_phone,street_address,city,state,zip)
            VALUES(%s,'',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (email,_hash(pw),f.get('last_name_0',''),f.get('first_name_0',''),
             f.get('last_name_1', '?'),f.get('first_name_1', '?'),phone,
             f.get('street_address', '?'),f.get('city', '?'),f.get('state', '?'),f.get('zip', '?')))
        conn.commit(); new_id = cur.lastrowid; conn.close()
        flash('Family created.','success')
        return redirect(url_for('admin.family_detail', fid=new_id))
    return render_template('admin/new_family.html')

@admin_bp.route('/payment/update', methods=['POST'])
@roles_required('admin','finance')
def update_payment():
    fpr_id=request.form.get('fpr_id'); family_id=request.form.get('family_id')
    reg_status=request.form.get('reg_status','').strip()
    total_paid=request.form.get('total_paid','').strip()
    note=request.form.get('description','').strip()
    conn=get_db_connection(); cur=conn.cursor()
    updates=["last_update=NOW()"]; params=[]
    if reg_status: updates.append("reg_status=%s"); params.append(reg_status)
    if total_paid:
        try: updates.append("total_paid=%s"); params.append(float(total_paid))
        except: pass
    updates.append("description=%s"); params.append(note)
    params.append(int(fpr_id))
    cur.execute(f"UPDATE family_record SET {', '.join(updates)} WHERE id=%s",params)
    pid = request.form.get('pid','')
    conn.commit(); conn.close(); flash('Payment updated.','success')
    return redirect(url_for('admin.finance', pid=pid) if pid else url_for('admin.finance'))

# ══ STUDENTS ══════════════════════════════════════════════════════════════════


@admin_bp.route('/students/<int:student_id>/edit', methods=['GET','POST'])
@roles_required('admin','language','culture','finance')
def edit_student(student_id):
    """Allow admin/language/culture users to edit student info esp. special_note."""
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM student WHERE id=%s",(student_id,)); student = cur.fetchone()
    if not student: conn.close(); flash('Student not found.','error'); return redirect(url_for('admin.students'))
    if request.method == 'POST':
        f = request.form
        cur.execute("""UPDATE student SET
                first_name=%s, last_name=%s, chinese_name=%s,
                gender=%s, birthday=%s, phone=%s, email=%s,
                ec_first_name=%s, ec_last_name=%s, ec_phone=%s,
                special_note=%s WHERE id=%s""",
                (f.get('first_name',''), f.get('last_name',''),
                 f.get('chinese_name','?'), f.get('gender','?'),
                 f.get('birthday','').strip() or '2999-01-01',
                 f.get('phone','?'), f.get('email','?'),
                 f.get('ec_first_name','?'), f.get('ec_last_name','?'),
                 f.get('ec_phone','?'), f.get('special_note',''), student_id))
        conn.commit(); conn.close()
        flash('Student updated.','success')
        # Preserve filter params so user returns to same filtered view
        pid        = request.form.get('_pid','')
        lang_filter= request.form.get('_lang','')
        cult_filter= request.form.get('_cult','')
        return redirect(url_for('admin.students',
            pid=pid or None, lang=lang_filter or None, cult=cult_filter or None))
    conn.close()
    return render_template('admin/edit_student.html', student=student,
        pid=request.args.get('pid',''),
        lang=request.args.get('lang',''),
        cult=request.args.get('cult',''))

@admin_bp.route('/students')
@admin_required
def students():
    conn = get_db_connection(); cur = conn.cursor(dictionary=True)
    period = _cur_period(cur); plist = _periods_list(cur)
    pid = request.args.get('pid', period['id'] if period else None)
    lang_filter = request.args.get('lang','')
    cult_filter = request.args.get('cult','')
    lang_classes = []; cult_classes = []; rows = []
    sel = None
    if pid:
        cur.execute("SELECT * FROM period WHERE id=%s",(pid,)); sel = cur.fetchone()
        cur.execute("SELECT id,name FROM class_group_record WHERE pid=%s AND type='language' ORDER BY name",(pid,))
        lang_classes = cur.fetchall()
        cur.execute("SELECT id,name FROM class_group_record WHERE pid=%s AND type='culture' ORDER BY name",(pid,))
        cult_classes = cur.fetchall()
        where = ["sr.pid=%s"]; params = [pid]
        if lang_filter: where.append("sr.lcgrid=%s"); params.append(int(lang_filter))
        if cult_filter: where.append("(sr.ccgrid=%s OR sr.ccgrid2=%s)"); params.extend([int(cult_filter),int(cult_filter)])
        cur.execute(f"""SELECT s.*,f.last_name_0 AS family_last,f.first_name_0 AS family_first,
            f.primary_email AS family_email,f.id AS family_id,
            lc.name AS lang_class,cc.name AS cult_class,cc2.name AS cult_class2
            FROM student_record sr
            JOIN student s ON s.id=sr.sid JOIN family f ON f.id=s.fid
            LEFT JOIN class_group_record lc ON lc.id=sr.lcgrid
            LEFT JOIN class_group_record cc ON cc.id=sr.ccgrid
            LEFT JOIN class_group_record cc2 ON cc2.id=sr.ccgrid2
            WHERE {' AND '.join(where)} ORDER BY s.last_name,s.first_name""", params)
        rows = cur.fetchall()
    conn.close()
    return render_template('admin/students.html',
        students=rows, period=sel, periods_list=plist,
        lang_classes=lang_classes, cult_classes=cult_classes,
        lang_filter=lang_filter, cult_filter=cult_filter, pid=pid)

# ══ CSV EXPORTS ════════════════════════════════════════════════════════════════

@admin_bp.route('/export/families')
@roles_required('admin','finance')
def export_families():
    conn=get_db_connection(); cur=conn.cursor(dictionary=True)
    period=_cur_period(cur)
    pid=request.args.get('pid',period['id'] if period else 0)
    status=request.args.get('status','all')
    sc=''
    if status=='complete': sc="AND fr.reg_status='Complete Registration'"
    elif status=='pending': sc="AND fr.reg_status='Pending'"
    cur.execute(f"""SELECT f.id,f.last_name_0,f.first_name_0,f.primary_email,
        f.primary_phone,f.street_address,f.city,f.state,f.zip,
        COUNT(DISTINCT s.id) AS student_count,
        fr.reg_status,fr.total_due,fr.total_paid,fr.adjustment,fr.description AS payment_note
        FROM family f LEFT JOIN student s ON s.fid=f.id
        LEFT JOIN family_record fr ON fr.fid=f.id AND fr.pid=%s
        WHERE 1=1 {sc} GROUP BY f.id ORDER BY f.last_name_0,f.first_name_0""",(pid,))
    rows=cur.fetchall(); conn.close()
    output=io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    if rows:
        w=csv.DictWriter(output,fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    return Response(output.getvalue(),mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename=mfcs_families.csv'})

@admin_bp.route('/export/students')
@roles_required('admin','finance')
def export_students():
    conn=get_db_connection(); cur=conn.cursor(dictionary=True)
    period=_cur_period(cur)
    pid         = request.args.get('pid', period['id'] if period else 0)
    lang_filter = request.args.get('lang','')
    cult_filter = request.args.get('cult','')
    extra = []; params = [pid]
    if lang_filter: extra.append("AND sr.lcgrid=%s"); params.append(int(lang_filter))
    if cult_filter: extra.append("AND (sr.ccgrid=%s OR sr.ccgrid2=%s)"); params.extend([int(cult_filter),int(cult_filter)])
    params.append(pid)  # for family_record join
    cur.execute(f"""SELECT s.id,s.last_name,s.first_name,s.chinese_name,s.gender,s.birthday,
        s.special_note,
        f.last_name_0 AS family_last,f.first_name_0 AS family_first,
        f.primary_email,f.primary_phone,
        lc.name AS language_class,cc.name AS culture_class,cc2.name AS culture_class2,
        fr.reg_status,fr.total_due,fr.total_paid,fr.description AS payment_note
        FROM student_record sr
        JOIN student s ON s.id=sr.sid JOIN family f ON f.id=s.fid
        LEFT JOIN class_group_record lc ON lc.id=sr.lcgrid
        LEFT JOIN class_group_record cc ON cc.id=sr.ccgrid
        LEFT JOIN class_group_record cc2 ON cc2.id=sr.ccgrid2
        LEFT JOIN family_record fr ON fr.fid=s.fid AND fr.pid=%s
        WHERE sr.pid=%s {' '.join(extra)}
        ORDER BY s.last_name,s.first_name""", params)
    rows=cur.fetchall(); conn.close()
    output=io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    if rows:
        w=csv.DictWriter(output,fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    return Response(output.getvalue(),mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename=mfcs_students.csv'})

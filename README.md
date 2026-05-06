# MFCS Registration System

Flask-based registration portal for Monmouth Fidelity Chinese School.  
Connects directly to the existing `legacyregdb2` MySQL database — no migration needed.

---

## Project structure

```
mfcs_registration/
├── app.py                  Flask factory + login manager
├── db.py                   MySQL connection (Railway URL or env vars)
├── models.py               Family / Admin user classes
├── hash_password.py        Password utility (bcrypt hash + bulk migration)
├── admins_schema.sql       ONE-TIME SQL: adds 3 things to your existing DB
├── requirements.txt
├── Procfile                Railway deployment
├── railway.toml
├── routes/
│   ├── family.py           Family-facing routes
│   └── admin.py            Admin/staff routes
└── templates/
    ├── base.html
    ├── family/             login, dashboard, profile, register, fee_summary, …
    └── admin/              login, dashboard, families, family_detail, students, …
```

---

## Tables used (all pre-existing in legacyregdb2)

| Table | Used for |
|---|---|
| `family` | household login, contact info |
| `student` | children (fid → family.id) |
| `period` | school years, fees |
| `class_group_record` | class offerings per period (language / culture) |
| `class_record` | sections within a class group |
| `student_record` | per-student class assignments (lcgrid, ccgrid) |
| `family_record` | per-family registration + payment record |

**New tables added by admins_schema.sql:**

| Table | Purpose |
|---|---|
| `admins` | staff login accounts |
| `family.password_hash` | bcrypt column added to existing family table |
| `password_reset_tokens` | forgot-password flow |

---

## First-time setup (local)

### 1. Run the schema additions

```bash
mysql -u root legacyregdb2 < admins_schema.sql
```

This adds the `admins` table, `family.password_hash` column, and `password_reset_tokens` table.  
All existing data is untouched.

### 2. Create your first admin account

```bash
# Generate a bcrypt hash
python hash_password.py hash "yourpassword"
```

Copy the printed hash (starts with `$2b$`), then:

```sql
INSERT INTO admins (username, password_hash, display_name, role)
VALUES ('admin', '$2b$12$...paste_hash_here...', 'Administrator', 'admin');
```

Or run it as a one-liner:
```bash
HASH=$(python hash_password.py hash "yourpassword")
mysql -u root legacyregdb2 -e \
  "INSERT INTO admins (username, password_hash, display_name, role) VALUES ('admin', '$HASH', 'Administrator', 'admin');"
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the app locally

```bash
export DATABASE_URL="mysql://root:@127.0.0.1:3306/legacyregdb2"
export SECRET_KEY="any-local-dev-key"
python app.py
```

| URL | What it is |
|---|---|
| http://localhost:5000/login | Family login |
| http://localhost:5000/admin/login | Staff / admin login |

---

## Password handling

Legacy families have plaintext passwords in `family.password`.  
The login route tries **bcrypt first**, then falls back to **plaintext** automatically.

To migrate all families to bcrypt in one shot (run before go-live):

```bash
export DATABASE_URL="mysql://root:@127.0.0.1:3306/legacyregdb2"
python hash_password.py migrate_families
```

This reads `family.password`, hashes each one into `family.password_hash`, and leaves the original column untouched. After migration, plaintext fallback still works as a safety net.

---

## Railway deployment

### Environment variables to set in Railway:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Auto-provided by Railway MySQL plugin |
| `SECRET_KEY` | Any long random string (keep secret) |

### Deploy steps:

1. Push code to GitHub
2. Connect repo to Railway
3. Railway will detect `Procfile` and build automatically
4. Run `admins_schema.sql` against the Railway MySQL (one time)
5. Create first admin account against Railway MySQL

---

## Development notes

- **Current period** is always the `period` row with the highest `id`
- **Family login** uses `primary_email` (case-insensitive)
- **Admin login** uses `username` from the `admins` table
- The `family_record.description` column holds payment notes (admin-editable)
- Culture classes with `fee = 0` are included (some are free/included in tuition)

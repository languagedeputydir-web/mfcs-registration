"""
app.py — MFCS Registration System
Queries the legacy DB tables directly (no migration needed).
"""
import os
from flask import Flask
from flask_login import LoginManager
from db import get_db_connection
from models import Family, Admin

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-change-me-in-prod')

    # ── Flask-Login ────────────────────────────────────────────────────────────
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'family.login'
    login_manager.login_message = 'Please log in to continue.'

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import request, redirect, url_for
        # If trying to access admin pages, redirect to admin login
        if request.path.startswith('/admin'):
            return redirect(url_for('admin.login'))
        return redirect(url_for('family.login'))

    @login_manager.user_loader
    def load_user(user_id):
        """
        user_id format:
            "f:<id>"  → family row  (legacy table: family)
            "a:<id>"  → admin row   (new table: admins)
        """
        try:
            prefix, uid = user_id.split(':', 1)
        except ValueError:
            return None

        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True)

        if prefix == 'f':
            cur.execute("SELECT * FROM family WHERE id = %s", (uid,))
            row = cur.fetchone()
            conn.close()
            return Family(row) if row else None

        elif prefix == 'a':
            cur.execute("SELECT * FROM admins WHERE id = %s", (uid,))
            row = cur.fetchone()
            conn.close()
            return Admin(row) if row else None

        conn.close()
        return None

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from routes.family import family_bp
    from routes.admin  import admin_bp

    app.register_blueprint(family_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # ── Custom Jinja filters ───────────────────────────────────────────────────
    @app.template_filter('currency')
    def currency_filter(value):
        """Format as $X,XXX (no decimal, includes dollar sign)."""
        try:
            return '${:,.0f}'.format(float(value or 0))
        except (ValueError, TypeError):
            return '$0'

    # Redirect root URL to family login
    from flask import redirect, url_for
    @app.route('/')
    def index():
        return redirect(url_for('family.login'))

    # ── Error notification ─────────────────────────────────────────────────────
    # Email the admin whenever an unhandled exception (500 error) occurs.
    # Uses the same Brevo setup already configured for registration emails.
    import traceback
    from datetime import datetime

    @app.errorhandler(Exception)
    def handle_unhandled_exception(e):
        from flask import request

        admin_email = os.environ.get('ERROR_ALERT_EMAIL', os.environ.get('MAIL_SENDER', ''))
        try:
            if admin_email:
                from routes.family import _send_email
                tb = traceback.format_exc()
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                subject = f'MFCS Registration — Error on {request.path}'
                text_body = (
                    f"An error occurred on the MFCS registration system.\n\n"
                    f"Time: {now}\n"
                    f"Path: {request.method} {request.path}\n"
                    f"Error: {type(e).__name__}: {e}\n\n"
                    f"Traceback:\n{tb}"
                )
                html_body = (
                    f"<p><strong>An error occurred on the MFCS registration system.</strong></p>"
                    f"<p><strong>Time:</strong> {now}<br>"
                    f"<strong>Path:</strong> {request.method} {request.path}<br>"
                    f"<strong>Error:</strong> {type(e).__name__}: {e}</p>"
                    f"<pre style='background:#f5f5f5;padding:10px;font-size:12px;"
                    f"overflow-x:auto;white-space:pre-wrap'>{tb}</pre>"
                )
                _send_email(admin_email, subject, text_body, html_body)
        except Exception as notify_err:
            # Never let the notification itself crash the app further
            print(f'ERROR NOTIFY FAILED: {notify_err}', flush=True)

        # Re-raise so Flask's normal error page / logging still happens
        raise e

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)

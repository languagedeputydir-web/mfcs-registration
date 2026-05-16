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
        """Format as $X,XXX (no decimal, with thousands separator)."""
        try:
            return '${:,.0f}'.format(float(value or 0))
        except (ValueError, TypeError):
            return '$0'

    # Redirect root URL to family login
    from flask import redirect, url_for
    @app.route('/')
    def index():
        return redirect(url_for('family.login'))

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)

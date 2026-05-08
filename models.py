"""
models.py — Flask-Login user objects
"""
from flask_login import UserMixin


class Family(UserMixin):
    """Maps to legacy table: family"""
    def __init__(self, row):
        self.id              = row['id']
        self.primary_email   = row.get('primary_email', '')
        self.password_hash   = row.get('password_hash') or row.get('password', '')
        self.first_name_0    = row.get('first_name_0', '')
        self.last_name_0     = row.get('last_name_0', '')
        self.chinese_name_0  = row.get('chinese_name_0', '')
        self.first_name_1    = row.get('first_name_1', '')
        self.last_name_1     = row.get('last_name_1', '')
        self.primary_phone   = row.get('primary_phone', '')
        self.secondary_phone = row.get('secondary_phone', '')
        self.secondary_email = row.get('secondary_email', '')
        self.street_address   = row.get('street_address', '')
        self.city             = row.get('city', '')
        self.state            = row.get('state', '')
        self.zip              = row.get('zip', '')
        self.address_verified = bool(row.get('address_verified', 0))
        self.email_verified   = int(row.get('email_verified', 1))  # default 1 for legacy accounts

    @property
    def display_name(self):
        n = f"{self.first_name_0} {self.last_name_0}".strip()
        return n or self.primary_email

    def get_id(self): return f"f:{self.id}"

    @property
    def is_active(self): return True


class Admin(UserMixin):
    """Maps to new table: admins
    Roles: admin | finance | language | culture
    """
    def __init__(self, row):
        self.id            = row['id']
        self.username      = row['username']
        self.password_hash = row['password_hash']
        self.display_name  = row.get('display_name', '')
        self.role          = row.get('role', 'finance')
        self._is_active    = bool(row.get('is_active', 1))

    def get_id(self): return f"a:{self.id}"

    @property
    def is_active(self): return self._is_active

    # Role helpers used in templates
    @property
    def is_admin(self):    return self.role == 'admin'
    @property
    def is_finance(self):  return self.role in ('admin', 'finance')
    @property
    def is_language(self): return self.role in ('admin', 'language')
    @property
    def is_culture(self):  return self.role in ('admin', 'culture')

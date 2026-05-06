"""
address_helpers.py — shared address validation.
"""
import re

_FILLER = {
    '', 'tbd', 'n/a', 'na', 'none', 'unknown', 'test', '-', 'xxx',
    'address', 'placeholder', '123 main', '123 main st', '123 main street',
    'street', 'city', 'state', 'zip', '00000', '?',
}


def address_is_valid(street, city, state, zip_code):
    """Return True only when all four parts look like a real US address."""
    parts = [street, city, state, zip_code]
    if any(p is None for p in parts):
        return False
    if any(str(p).strip().lower() in _FILLER for p in parts):
        return False
    if not re.match(r'^\d{5}(-\d{4})?$', str(zip_code).strip()):
        return False
    return True

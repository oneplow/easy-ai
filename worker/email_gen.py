"""Throwaway email + password generation. Format-valid gibberish, never reused."""
import random
import string
import uuid

from . import config


def _rand_letters(lo: int, hi: int) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=random.randint(lo, hi)))


def gen_email() -> str:
    """e.g. kqzbxomwra3f1c@plkdwq.io -- passes a standard email regex.

    The uuid fragment keeps collisions near-zero across the whole account farm,
    but the worker still catches the 'already in use' error and rerolls anyway.
    """
    tag = uuid.uuid4().hex[:4]
    local = _rand_letters(config.EMAIL_LOCAL_MIN, config.EMAIL_LOCAL_MAX) + tag
    domain = _rand_letters(config.EMAIL_DOMAIN_MIN, config.EMAIL_DOMAIN_MAX)
    tld = random.choice(config.EMAIL_TLDS)
    return f"{local}@{domain}.{tld}"


def gen_password() -> str:
    pool = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choices(pool, k=config.PASSWORD_LENGTH))

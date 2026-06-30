"""Throwaway email + password generation for the account farm.

WHICH PATH USES THIS:
  This module is the email source for the **browser signup path** (the WARM
  and COLD fallbacks in easy_ai.py: `_signup()` calls gen_email()/gen_password()
  to fill the email/password fields on use.ai's signup form). It is also the
  source for the legacy `harvester` browser prewarmer.

  It is NOT used by the headless WebSocket path (`worker/session_http.py` +
  `worker/fingerprint.py`), which signs up over HTTP with its own realistic
  email generator and a full browser fingerprint. The two generators exist
  because the browser path only needs a format-valid value to type into a
  field, while the HTTP path needs a coherent identity (UA + headers + email)
  to evade detection. See worker/fingerprint.py for the headless counterpart.

Format: gibberish local-part + a uuid fragment for near-zero collision across
the farm, plus a random domain/TLD that satisfies a standard email regex.
gen_email() / gen_password() are never reused.
"""
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

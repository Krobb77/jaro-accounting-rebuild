"""test_19_totp — TOTP enrollment-pair boundary tolerance (D-36).

Regression: a client that reads two consecutive codes just before a 30s
rollover must not fail when the server verifies just after it. The old
strict check (current,next only) produced a ~10% flake in the Playwright
portal journeys and would have hit real users the same way.
"""
import time

from app import totp


def _pair_at(secret: str, t: float) -> tuple[str, str]:
    return totp.code_at(secret, for_time=t), totp.code_at(secret, for_time=t + 30)


def test_19_01_enrollment_pair_current_next_accepted():
    secret = totp.generate_secret()
    now = time.time()
    code1, code2 = _pair_at(secret, now)
    assert totp.verify_enrollment_pair(secret, code1, code2, for_time=now + 1)


def test_19_02_enrollment_pair_boundary_rollover():
    """Codes read 2s before rollover verify 1s after it (prev,current)."""
    secret = totp.generate_secret()
    now = time.time()
    boundary = (int(now // 30) + 1) * 30
    code1, code2 = _pair_at(secret, boundary - 2)
    assert totp.verify_enrollment_pair(secret, code1, code2, for_time=boundary + 1)


def test_19_03_enrollment_pair_rejects_non_consecutive():
    secret = totp.generate_secret()
    now = time.time()
    code1, _ = _pair_at(secret, now)
    wrong = totp.code_at(secret, for_time=now + 90)  # two windows away
    assert not totp.verify_enrollment_pair(secret, code1, wrong, for_time=now + 1)


def test_19_04_enrollment_pair_rejects_same_code_twice():
    secret = totp.generate_secret()
    now = time.time()
    code1, _ = _pair_at(secret, now)
    assert not totp.verify_enrollment_pair(secret, code1, code1, for_time=now + 1)

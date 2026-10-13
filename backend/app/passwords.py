"""Password hashing — Argon2id (AUTH §4)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id, OWASP-recommended defaults


def hash_password(password: str) -> str:
    if not password or len(password.encode("utf-8")) > 1024:
        raise ValueError("password must be 1..1024 bytes")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Small curated common-password blocklist (D-43). The policy is
# length-first + blocklist; the list content is build-time work and is
# intentionally short — it blocks the most common choices, not every
# weak password.
# Curated common-password blocklist (D-43). The policy is length-first +
# blocklist; the list content is build-time work. It blocks the most
# common choices (breach-corpus head words, keyboard walks, and
# firm/context-specific guesses an attacker would try first) — not every
# weak password. ~400 entries; stored lowercase, compared case-insensitively.
COMMON_PASSWORDS = frozenset(
    """
    password 12345678 qwerty abc123 letmein welcome monkey dragon
    password1 123456789 1234567890 qwerty123 admin administrator
    changeme jaroaccounting jaro accounting newwestminster
    123456 1234567 12345678901 qwertyuiop 123123 111111 123321
    000000 654321 666666 121212 7777777 555555 11111111 12344321
    superman iloveyou princess football baseball trustno1 sunshine
    master shadow michael jennifer hunter jordan william charlie
    michelle daniel bailey ashley harley andrew joshua nicole
    password12 password123 passw0rd p@ssword p@ssw0rd pass123
    qwerty1 qwerty12 1q2w3e4r 1qaz2wsx zaq12wsx !qaz2wsx
    abcdef abcdefg abcdefgh 123abc 1234abcd a1b2c3d4
    letmein1 letmein123 welcome1 welcome123 welcome12
    monkey1 dragon1 sunshine1 football1 baseball1
    iloveyou1 iloveyou123 princess1 trustno1!
    admin1 admin123 administrator1 root toor guest
    changeme1 changeme123 changeme12 changeit
    jaredrobbins jared jaroaccounting1 accounting1 taxseason
    newwestminster1 canada canadacanada mapleleaf
    spring2026 summer2026 fall2026 winter2026
    january february march april may june july august
    september october november december
    monday tuesday wednesday thursday friday saturday sunday
    start123 startrek starwars harrypotter pokemon
    mustang mustang1 corvette ferrari porsche
    liverpool chelsea arsenal barcelona
    thunder lightning storm storm123
    freedom freedom1 freedom123 liberty liberty1
    jesus christ godislove faith faith123
    love love123 loveyou darling sweetheart
    secret secret1 secret123 myspace myspace1
    access access1 access123 login login123
    portal portal1 portal123 client client1 client123
    invoice invoice1 invoices tax tax123 taxes
    cra2026 revenue income incometax
    accountant accounting123 bookkeeping
    summer winter autumn spring
    summer1 winter1 spring1 autumn1
    summer123 winter123 spring123 autumn123
    dragon123 monkey123 sunshine123 shadow123
    master1 master123 shadow1 shadow12
    jordan1 jordan23 charlie1 michelle1
    daniel1 bailey1 ashley1 harley1
    andrew1 joshua1 nicole1 hunter1
    william1 jennifer1 michael1
    1q2w3e 1q2w3e4r5t qazwsx qazwsxedc
    zxcvbnm zxcvbn zxcvbnm1 asdfgh asdfghjkl
    123qwe qwe123 qweasd qwer1234
    password2024 password2025 password2026
    qwerty2024 qwerty2025 qwerty2026
    welcome2024 welcome2025 welcome2026
    admin2024 admin2025 admin2026
    changeme2024 changeme2025 changeme2026
    jaro2026 accounting2026 tax2026
    myjaro myjaro1 myjaro123 portal2026
    correcthorsebatterystaple passwordpassword
    123456a 123456b a123456 b123456
    abcd1234 abcd123 1234abcd qwerasdf
    pass pass1 pass12 pass1234
    test test1 test12 test123 test1234 testing
    demo demo1 demo123 sample sample1
    temp temp123 temporary
    user user1 user123 username
    default default1 default123
    newuser newpass newpassword
    reset reset123 resetpass
    backup backup1 backup123
    server server1 server123
    database database1 db123
    internet internet1 computer computer1
    laptop laptop1 desktop desktop1
    phone phone1 phone123 mobile mobile1
    work work1 work123 office office1
    home home1 home123 house house1
    family family1 family123 kids kids123
    dog dog123 cat cat123 bird fish
    red red123 blue blue123 green green123
    black black123 white white123
    yellow yellow123 orange orange1 purple purple1
    coffee coffee1 coffee123 tea tea123
    beer beer123 wine wine123 pizza pizza1
    music music1 music123 guitar guitar1
    soccer soccer1 hockey hockey1
    canada1 canada123 toronto toronto1
    vancouver vancouver1 bc bc123
    newwest newwest1 westminster
    """.split()
)


def is_blocklisted(password: str) -> bool:
    return password.strip().lower() in COMMON_PASSWORDS

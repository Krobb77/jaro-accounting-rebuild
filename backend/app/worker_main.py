"""Worker process entry point — ``python -m app.worker_main``.

Phase-4 wiring of the Phase-0 residual: the ``Worker`` class
(``app/worker.py``) plus ``run_once()`` were complete and tested, but the
process CLI wrapper was deferred. This module is deliberately thin and adds
no new machinery: it only wires
``load_settings -> verify_runtime (H2) -> engine -> session factory ->
mail backend -> Worker.main()``. All reliability behavior (lease-claim,
bounded retries, capture mode) lives in the worker/outbox modules it
composes.

No new pattern was needed: this is the standard settings-first bootstrap
already used by ``app.main.create_app``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("jaro.worker_main")


def _build_worker():
    """Settings-first bootstrap shared by the loop and --once paths."""
    from . import advisory, mail
    from .config import load_settings
    from .db import create_app_engine, make_session_factory
    from .logging_config import configure_logging
    from .runtime_check import verify_runtime
    from .worker import Worker

    settings = load_settings(dict(os.environ))
    # H2 fail-closed startup, exactly like the API: bad/missing secrets,
    # wrong DB role, unpinned head, or MAIL_ALLOW_SEND unset refuses boot.
    verify_runtime(settings)
    configure_logging()
    engine = create_app_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    mail_backend = mail.create_backend(settings)
    worker = Worker(session_factory, mail_backend)
    return worker, settings, engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Jaro Accounting background worker: outbox email, "
        "calendar sync, and expiry sweeps."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single worker cycle and exit (deploy smoke / cron).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="Sleep between cycles in loop mode (default: 60).",
    )
    args = parser.parse_args(argv)

    try:
        worker, settings, engine = _build_worker()
    except Exception as exc:
        log.error("worker refused to start: %s", exc)
        return 2

    if settings.workload != "worker":
        # The workload/role pairing is the identity mechanism (H3): the
        # worker must run as the jaro_worker workload, or verify_runtime
        # above would have paired it with the wrong DB role. Two processes
        # claiming from the outbox under different identities is exactly
        # the G2 hazard the lease-claim exists to survive — but the clean
        # failure is to refuse, not to double-claim.
        log.error(
            "refusing: JARO_WORKLOAD=%r; the worker requires JARO_WORKLOAD=worker",
            settings.workload,
        )
        engine.dispose()
        return 2

    try:
        if args.once:
            report = worker.run_once()
            log.info("worker cycle complete: %r", report)
        else:
            worker.main(poll_seconds=args.poll_seconds)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())

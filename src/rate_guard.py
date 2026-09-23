"""
In-process daily call-count guard for Gemini API calls.

A safety net against a bug -- an infinite retry loop, a batch triggered
twice, a runaway rerun -- silently running up real cost, independent of
Google's own billing alerts. This is deliberately NOT a replacement for
those: it's a same-process, same-day circuit breaker that resets on app
restart, not an authoritative spend tracker. Google Cloud's own budget
alerts remain the real backstop for cross-restart/multi-day protection;
this just stops a bug from making hundreds of calls in one sitting before
a human ever notices.

Shared by extract.py and llm_client.py, which otherwise have no
dependency on each other -- kept here rather than in either one so
neither has to import the other just to share a call counter.
"""

import os
import threading
from datetime import date

DAILY_CALL_LIMIT = int(os.environ.get("GEMINI_DAILY_CALL_LIMIT", "300"))
# 300 is comfortably above a real day's expected volume (a chef's batch
# of 30-40 uploads, each worth roughly one extraction call plus a small
# number of ambiguous-judgment calls) with real headroom, while still
# being far below what an actual runaway loop would rack up in minutes.

_lock = threading.Lock()
_call_count = 0
_count_date = None


class RateLimitExceeded(Exception):
    """Raised when today's in-process Gemini call count has already hit
    DAILY_CALL_LIMIT. Callers should surface this the same way they
    surface any other Gemini failure -- it's a normal, expected,
    catchable condition, not a crash."""
    pass


def check_and_increment():
    """Call this immediately before every real Gemini API call (extraction
    or judgment). Raises RateLimitExceeded if today's count is already at
    the limit; otherwise increments the count and returns normally."""
    global _call_count, _count_date
    with _lock:
        today = date.today()
        if _count_date != today:
            _count_date = today
            _call_count = 0
        if _call_count >= DAILY_CALL_LIMIT:
            raise RateLimitExceeded(
                f"Hit the in-app daily Gemini call limit ({DAILY_CALL_LIMIT} "
                f"calls) -- this is a safety net against a runaway loop or "
                f"bug, not a real API quota. If today's real usage "
                f"genuinely needs more than this, restart the app (resets "
                f"the count) or raise GEMINI_DAILY_CALL_LIMIT."
            )
        _call_count += 1


def calls_made_today() -> int:
    """For a status display, if one's ever wanted -- not required for the
    guard itself to work."""
    with _lock:
        if _count_date != date.today():
            return 0
        return _call_count

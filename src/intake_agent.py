"""
Watches a local folder for contract files, classifies each one, and
routes it through the exact same extract -> diff -> store -> notify
pipeline app.py's manual upload flow uses (contract_store.py owns that
logic now -- see its module docstring). The point: getting a contract
into Good-to-Go becomes "save the file here" instead of "open the app
and click Upload," with no change at all to how a change gets decided
once it's in.

Deliberately scoped to the INPUT end only. This agent decides what KIND
of file something is and whether it's ready to route -- it never
re-reads a field to override an extraction, never touches
contract_agent.py's escalate/review decisions, and never resolves an
ambiguous case (an unreadable division, a reschedule that might be a
different booking) on its own. Anything it can't confidently route is
left alone for a human to handle through the normal Streamlit upload
flow -- no second resolution path gets built here. That boundary is
deliberate: this is the part of the project genuinely worth calling
agentic (open-ended classification of unknown input), sitting next to a
decision core that stays exactly as deterministic and auditable as it
is everywhere else.

One shared inbox folder, not one per division -- a document's own
header/footer already declares which division it belongs to (the same
field app.py's upload flow already reads), so a daily batch of mixed
Good Tidings / Goodies To Go contracts can be dropped in together and
each gets routed to its own Firestore collection independently.

Folder layout (gitignored -- real customer documents, same reasoning as
data/real_examples/):
    intake/                 <- drop files here (the watched inbox)
      processed/             <- moved here after successful routing
      needs_review/          <- moved here + a .txt note, needs a human

Firestore writes are permanent and independent of the source file from
the moment they happen -- moving or even deleting a processed file
later never affects what's already stored.

Run:
    export GEMINI_API_KEY=...
    export FIRESTORE_CREDENTIALS_PATH=...
    python src/intake_agent.py
    # optional: INTAKE_FOLDER_PATH to override the default `intake/` location

Stop with Ctrl+C.
"""

import logging
import os
import queue
import shutil
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # loads .env from wherever this is run from, if present

import contract_store as store  # noqa: E402
import extract  # noqa: E402
import rate_guard  # noqa: E402
from watchdog.events import FileSystemEventHandler  # noqa: E402
from watchdog.observers import Observer  # noqa: E402

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}

# Waits until a file's size stops changing across this many consecutive
# checks, spaced this far apart, before treating it as fully written --
# a still-downloading/still-copying file should never be read half-done.
DEBOUNCE_INTERVAL_SECONDS = 1.0
DEBOUNCE_STABLE_CHECKS = 2

logger = logging.getLogger("intake_agent")


def _setup_logging(log_path: Path) -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                        encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def _folders(base: Path) -> tuple:
    processed = base / "processed"
    needs_review = base / "needs_review"
    processed.mkdir(parents=True, exist_ok=True)
    needs_review.mkdir(parents=True, exist_ok=True)
    return processed, needs_review


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """Never silently overwrites an existing file of the same name --
    appends a timestamp if today's batch happens to reuse a filename
    (e.g. two different events both photographed as "IMG_0001.jpg")."""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return dest_dir / f"{stem}_{stamp}{suffix}"


def _move_to_needs_review(path: Path, needs_review_dir: Path, reason: str) -> None:
    dest = _unique_dest(needs_review_dir, path.name)
    shutil.move(str(path), str(dest))
    dest.with_suffix(dest.suffix + ".txt").write_text(
        f"Needs a human look -- not auto-routed.\n\nReason: {reason}\n"
        f"Moved here: {datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    logger.warning(f"[needs_review] {path.name}: {reason}")


def _move_to_processed(path: Path, processed_dir: Path) -> None:
    dest = _unique_dest(processed_dir, path.name)
    shutil.move(str(path), str(dest))


def _wait_until_stable(path: Path) -> bool:
    last_size = -1
    stable_count = 0
    while stable_count < DEBOUNCE_STABLE_CHECKS:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        stable_count = stable_count + 1 if size == last_size else 0
        last_size = size
        time.sleep(DEBOUNCE_INTERVAL_SECONDS)
    return True


def _process_file(path: Path, client, processed_dir: Path, needs_review_dir: Path) -> None:
    if not path.exists():
        return  # moved/deleted by something else between enqueue and processing

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        _move_to_needs_review(path, needs_review_dir,
                               f"Unsupported file type ({path.suffix or 'no extension'})"
                               " -- not an image or PDF.")
        return

    if not _wait_until_stable(path):
        logger.info(f"{path.name} disappeared before it finished being written -- skipping.")
        return

    # Proactive check, not just a reaction to a raised RateLimitExceeded --
    # lets this specific, expected condition be handled distinctly (leave
    # the file alone to retry once quota resets) rather than folded into
    # the same needs_review bucket as a genuine extraction problem.
    if rate_guard.calls_made_today() >= rate_guard.DAILY_CALL_LIMIT:
        logger.warning(f"Daily Gemini call limit reached ({rate_guard.DAILY_CALL_LIMIT}) -- "
                        f"leaving {path.name} in the inbox to retry once quota resets.")
        return

    try:
        classification = extract.classify_document(str(path))
    except extract.ExtractionError as e:
        _move_to_needs_review(path, needs_review_dir, f"Classification failed: {e}")
        return

    doc_type = classification.get("document_type")
    if doc_type != "contract":
        reason = classification.get("reason", "")
        _move_to_needs_review(
            path, needs_review_dir,
            f"Classified as '{doc_type}', not a contract"
            + (f" ({reason})" if reason else "") + " -- not routed automatically.")
        return

    # division=None: no per-division folder to check the extraction
    # against here, unlike app.py's tab-scoped upload -- the document's
    # own declared division is trusted directly. See
    # contract_store.extract_and_classify()'s docstring.
    result = store.extract_and_classify(str(path), path.name, division=None)

    if result["status"] == "needs_review":
        _move_to_needs_review(path, needs_review_dir,
                               f"{result['reason']}: {result['message']}")
        return

    data, new_record = result["data"], result["new_record"]
    store_result = store.diff_and_store(client, new_record.division, data, new_record)

    if store_result["status"] == "needs_review":
        _move_to_needs_review(path, needs_review_dir,
                               f"{store_result['reason']}: {store_result['message']}")
        return

    _move_to_processed(path, processed_dir)
    logger.info(f"[{new_record.division}] {path.name}: {store_result['message']}")


class _InboxHandler(FileSystemEventHandler):
    """Only enqueues -- the actual processing (debounce, classify,
    extract, store) happens on the main thread's loop below, so a slow
    Gemini call never blocks watchdog's own event-delivery thread."""

    def __init__(self, file_queue: "queue.Queue", inbox: Path):
        self.file_queue = file_queue
        self.inbox = inbox

    def _maybe_enqueue(self, src_path: str):
        path = Path(src_path)
        if path.parent != self.inbox:
            return  # ignore anything already inside processed/ or needs_review/
        if path.is_file():
            self.file_queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_enqueue(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe_enqueue(event.dest_path)


def run(inbox_path: str = None) -> None:
    base = Path(inbox_path or os.environ.get("INTAKE_FOLDER_PATH")
                or Path(__file__).resolve().parent.parent / "intake")
    base.mkdir(parents=True, exist_ok=True)
    processed_dir, needs_review_dir = _folders(base)

    _setup_logging(base / "intake_agent.log")

    creds_path = os.environ.get("FIRESTORE_CREDENTIALS_PATH")
    if not creds_path:
        logger.error("FIRESTORE_CREDENTIALS_PATH is not set -- can't connect to Firestore.")
        return
    client = store.get_client(credentials_path=creds_path)

    file_queue: "queue.Queue" = queue.Queue()

    logger.info(f"Watching {base} (processed -> {processed_dir.name}/, "
                f"needs review -> {needs_review_dir.name}/)")

    # Startup backlog: anything already sitting in the inbox before this
    # process started (e.g. the watcher wasn't running when it landed)
    # gets queued the same way a live arrival would.
    backlog = sorted(p for p in base.iterdir() if p.is_file())
    if backlog:
        logger.info(f"Found {len(backlog)} file(s) already in the inbox -- processing first.")
        for p in backlog:
            file_queue.put(p)

    handler = _InboxHandler(file_queue, base)
    observer = Observer()
    observer.schedule(handler, str(base), recursive=False)
    observer.start()

    try:
        while True:
            try:
                path = file_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                _process_file(path, client, processed_dir, needs_review_dir)
            except Exception as e:  # noqa: BLE001 -- one bad file must never
                # kill a long-running watcher; log it and keep going.
                logger.exception(f"Unexpected error processing {path.name}: {e}")
    except KeyboardInterrupt:
        logger.info("Stopping (Ctrl+C received).")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    run()

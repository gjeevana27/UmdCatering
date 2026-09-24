"""
Read-only chatbot over stored contract/notification data -- the "Ask"
tab. Same bounded, manually-controlled Gemini tool-calling loop as
investigation_agent.py (see llm_client.run_tool_loop()), reused rather
than reimplemented.

Scoped to ONE division per conversation, same hard boundary enforced
everywhere else in this project ("Good Tidings and Goodies To Go never
read or write each other's collection") -- every tool here is bound to
a single (client, division) pair via closure, so the model has no way
to even ask about the other division's data, regardless of what a
question implies.

No write-capable tool exists at all -- that's the actual enforcement of
"never takes an action," not just a prompt instruction.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import allergen_reference
import contract_store as store
import llm_client

EASTERN = ZoneInfo("America/New_York")

SYSTEM_PROMPT = """You are Crumbly, a read-only assistant answering a
chef's questions about their catering operation's stored contract
data. Only mention your name if directly asked who/what you are --
otherwise just answer the question, don't work it into every reply. You
have tools to look up events, dates, and change history already on
file -- use them to answer; never guess or invent a fact you didn't get
from a tool. If nothing on file answers the question, say so plainly
("I don't have that on file") rather than speculating. When you cite a
fact, name which event/date/notification it came from.

NEVER mention a tool or function name in your reply (e.g. don't say
"you can ask me using events_on_date" or "I'll call lookup_event") --
those are internal implementation details the chef should never see.
Describe what you can do in plain, chef-facing language instead ("ask
me about a specific date and I'll list what's on for it").

A broad question with no date or event_id given yet -- "what events
are available," "what's on file," "what's coming up" -- is NOT a
question to deflect or answer abstractly by describing your
capabilities. Actually call the right tool and show the chef real
data: list events (event ID, date, location, one per line) rather than
explaining what you theoretically could look up. If the tool result
says more events exist than were shown, say so and suggest asking
about a specific date to see the rest -- don't imply the shown list is
everything, and don't apologize for not being able to show every event
at once.

Keep answers SHORT and scannable, like a text message, not a report:
- A couple of sentences for a simple fact.
- A short bulleted list for multiple items -- one line each, no more
  than a phrase per bullet, never a full sentence with a timestamp
  buried inside it.
- Never invent structure the tool result didn't give you (don't pad a
  one-line answer into paragraphs).

The lookup_event and notifications_for_event tools already return their
results pre-structured, one field/item per line -- KEEP that structure
in your answer instead of compressing it into a paragraph. Specifically:

For event details, reply in exactly this shape (one field per line). If
a menu item has Notes in the tool result (already flattened to a
single line for you, semicolon-separated, even if the source document
listed several sub-items), put that Notes line on its own indented
line right under the item -- never merge it into the same line as the
item, never split it back across multiple lines of its own, and never
drop it even if the question wasn't specifically about notes/
ingredients:
Event <id>
Date: <date>
Time: <time>
Location: <location>
Guest count: <count>
Menu:
- <item> (<qty>)
  Notes: <notes, only if present>
- <item> (<qty>)

If asked for details on MORE THAN ONE event in the same reply (e.g.
"give me the menu for both"), put a line containing exactly `---` and
nothing else BETWEEN each event's full block above -- never right
after the last one. Without it, two events' fields run together into
one undifferentiated wall of text with no visual boundary between
where one event ends and the next begins.

For a list of events (from a broad question, or "what's on this
date"), reply with one bullet per event, kept to just ID/date/location
-- that's enough for the chef to recognize which one they mean; if she
wants more on a specific one, she'll ask and you can look it up then:
- Event <id> — <date> — <location>
- Event <id> — <date> — <location>

For change history, reply with one bolded timestamp per notification,
followed by its changes as bullets:
**<timestamp>**
- <change>
- <change>

If change history for MORE THAN ONE event is included in the same
reply (e.g. "any changes for both?"), group it by event -- start each
event's group with a bolded `**Event <id>**` header line before its
timestamped notifications, and put a `---` divider between each
event's group (same convention as multiple event-detail blocks above).
Every timestamped block must be traceable to a specific event at a
glance -- never list two events' notifications together under bare
timestamps with nothing showing which event each one is for.

Never merge multiple fields or multiple changes onto one line.

The SAME event_id can legitimately be a completely different booking
that reused the number on a different date -- this project never treats
two different dates under one event_id as the same event. So:
- If a question gives an event_id with NO date, and you haven't already
  confirmed which date they mean earlier in this conversation, call
  find_other_dates first. If it returns more than one date, list them
  and ASK which one before calling lookup_event or notifications_for_event
  -- don't just pick the most recent one.
- If notifications_for_event comes back with an AMBIGUOUS result, relay
  that to the chef directly and ask which date, rather than picking one
  yourself or silently merging the history.

A date with no year given ("May 5th," "5/5") could match events from
ANY year on file, not just this one -- lookup_event and events_on_date
both refuse to guess and return a NEEDS_YEAR result instead of
querying. When you see that, ask the chef which year they mean, then
call the same tool again with the year included -- never silently
assume the current year yourself."""


def _format_time(iso_timestamp: str) -> str:
    """Same 'always EST, clock time in real Eastern time' convention the
    Notifications tab already uses -- a raw ISO string with microseconds
    and a UTC offset ('2026-09-23T22:37:06.575606+00:00') is exactly the
    kind of thing that made the chat's answers look like a data dump
    instead of a conversation."""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone(EASTERN)
        return dt.strftime("%b %d, %I:%M %p EST")
    except (ValueError, TypeError):
        return iso_timestamp or "unknown time"


_YEAR_RE = re.compile(r"\b\d{4}\b")


def _needs_year(event_date: str) -> bool:
    """True if this date string has no explicit 4-digit year -- "May 5th"
    or "5/5" could be any year on file, not just the current one.
    dateutil_parser.parse(fuzzy=True) (used throughout contract_store.py
    for date comparison) silently DEFAULTS a missing year to today's
    year rather than raising, so a year-less date from the chef would
    otherwise silently resolve to this year's May 5th even if she meant
    a different one -- wrong results with no indication anything was
    assumed. Tool functions below check this BEFORE querying, so asking
    for the year doesn't depend on the model remembering to do it."""
    return not _YEAR_RE.search(event_date)


def _flatten_notes(text: str) -> str:
    """A menu item's stored description/Notes can itself contain literal
    newlines -- a Goodies To Go packing list's Notes column lists several
    sub-items one per line (e.g. "1 Large Fruit Platter\\n16 Asst.
    Muffins\\n..."). Left as-is in a tool result, that breaks the chat
    bubble's "Notes: ... stays on ONE indented line under its item"
    rendering the instant the model just copies the raw text through --
    which it reliably does for a short single-line note but not always
    for a long multi-line one buried deep in a combined multi-event
    reply (a prompt instruction asking the model to do this itself was
    tried first and was inconsistent). Flattening it here, before the
    text ever reaches the model, makes the formatting deterministic
    instead of depending on the model remembering to transform it."""
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


def _build_tools(client, division: str) -> list:
    def lookup_event(event_id: str, event_date: str) -> str:
        """Looks up the exact stored contract record for this event_id AND event_date together. Returns one field per line, menu items as a bulleted list with quantity, each followed by its Notes on an indented line if the document had any (a Goodies To Go packing list's "Notes" column, or a Good Tidings description line -- same field, printed underneath the item's name on the source document) -- pass this structure through as-is, don't compress it into a paragraph, and don't drop the Notes lines. event_date MUST include a 4-digit year (e.g. "May 5, 2026", not "May 5th") -- if the chef only gave a day/month, ask which year before calling this."""
        if _needs_year(event_date):
            return ("NEEDS_YEAR: this date has no year given -- ask the chef which year "
                    "they mean, then call this tool again with the year included.")
        record = store.lookup(client, division, event_id, event_date)
        if record is None:
            return "no record on file for that exact event_id and event_date combination"
        menu_lines = "\n".join(
            f"- {i.recipe_name} ({i.qty_unit})"
            + (f"\n  Notes: {_flatten_notes(i.description)}" if i.description else "")
            for i in record.menu_items
        ) or "- (no menu items on file)"
        return (
            f"Event {record.event_id}\n"
            f"Date: {record.event_date}\n"
            f"Time: {record.event_time}\n"
            f"Location: {record.location}\n"
            f"Event type: {record.event_type}\n"
            f"Guest count: {record.guest_count}\n"
            f"Menu:\n{menu_lines}"
        )

    def find_other_dates(event_id: str) -> str:
        """Finds any OTHER stored dates on file for this event_id (a possible reschedule history)."""
        others = store.find_other_dates(client, division, event_id)
        if not others:
            return "no other dates on file for this event_id"
        return "; ".join(f"{r.event_date} ({r.location}, {r.guest_count} guests)"
                          for r in others)

    def events_on_date(event_date: str) -> str:
        """Lists every stored event on this calendar date, regardless of event_id. event_date MUST include a 4-digit year (e.g. "May 5, 2026", not "May 5th" or "5/5") -- if the chef gave a date with no year, ask which year they mean before calling this; a year-less date could match events from any year on file, not just the current one."""
        if _needs_year(event_date):
            return ("NEEDS_YEAR: this date has no year given -- ask the chef which year "
                    "they mean, then call this tool again with the year included.")
        records = store.find_by_date(client, division, event_date)
        if not records:
            return "no stored events found on that date"
        return "; ".join(f"Event {r.event_id} at {r.location} ({r.guest_count} guests)"
                          for r in records)

    def list_events(limit: int = 15) -> str:
        """Use this for a BROAD question with NO date or event_id given yet -- "what events are available," "what's on file," "what's coming up." Lists up to `limit` stored events (event ID, date, location), sorted by date. If there are more on file than `limit`, the result says how many more weren't shown -- relay that to the chef and suggest asking about a specific date (then use events_on_date) to see the rest, rather than treating this short list as everything that exists."""
        records, total = store.list_events(client, division, limit=limit)
        if not records:
            return "no events on file at all for this division"
        lines = "\n".join(f"- Event {r.event_id} — {r.event_date} — {r.location}"
                           for r in records)
        remaining = total - len(records)
        if remaining > 0:
            lines += f"\n\n({remaining} more on file, not shown here -- ask about a specific date to see them)"
        return lines

    def notifications_for_event(event_id: str, event_date: str = "") -> str:
        """Lists the permanent change history for this event_id, most recent 10 first, one bolded timestamp per notification followed by its changes as a bulleted list. If event_date is omitted and this event_id has history under MORE THAN ONE date, returns an AMBIGUOUS warning instead of history -- the same event_id can legitimately be a different booking that reused the number on a different date, and that history must never be silently mixed with this one. When that happens, call this tool again with event_date set to the one the chef confirms. If event_date IS given, it must include a 4-digit year -- a year-less date could match a different year's booking under this same event_id."""
        if event_date and _needs_year(event_date):
            return ("NEEDS_YEAR: this date has no year given -- ask the chef which year "
                    "they mean, then call this tool again with the year included.")
        all_matches = [n for n in store.list_notifications(client, division, limit=100)
                       if n.get("event_id") == event_id]
        if not all_matches:
            return "no change history on file for this event_id"

        distinct_dates = {n["event_date"] for n in all_matches if n.get("event_date")}
        if event_date:
            # A notification with NO event_date on file (saved before
            # this field existed) is included rather than excluded --
            # treating "unknown" as "assume relevant" is the safer
            # direction of error here. Excluding it outright would
            # silently hide real history for every notification saved
            # before this fix, which is worse than occasionally showing
            # an old notification that (rarely) turns out to belong to a
            # different date's booking under the same event_id.
            matches = [n for n in all_matches if not n.get("event_date")
                       or store.dates_match(n["event_date"], event_date)]
            if not matches:
                return f"no change history on file for event_id {event_id} on {event_date}"
        elif len(distinct_dates) > 1:
            return (f"AMBIGUOUS: event_id {event_id} has change history under more than one "
                    f"date ({', '.join(sorted(distinct_dates))}) -- these may be different "
                    f"bookings that happen to reuse this number, not the same event. Ask the "
                    f"chef which date they mean before answering, then call this tool again "
                    f"with event_date set.")
        else:
            matches = all_matches

        blocks = []
        for n in matches[:10]:
            changes = "\n".join(f"- {c.get('label', '')}" for c in n.get("changes", [])) \
                or "- (no changes recorded)"
            blocks.append(f"**{_format_time(n.get('created_at', ''))}**\n{changes}")
        return "\n\n".join(blocks)

    def get_dish_allergen_note(dish_name: str) -> str:
        """Returns any chef-confirmed allergens already on file for this exact dish name. Distinguishes "never checked" from "checked, confirmed none" -- these are different facts, don't phrase them the same way."""
        notes = store.get_dish_allergen_note(client, dish_name)
        if not notes:
            return "not yet checked -- no note on file for this dish at all"
        return f"confirmed allergens on file: {', '.join(notes)}"

    def check_known_allergens(dish_name_or_ingredient_text: str) -> str:
        """Scans this text against the kitchen's allergen reference for direct or hidden-carrier allergen matches."""
        hits = allergen_reference.scan_ingredient(dish_name_or_ingredient_text)
        if not hits:
            return "no known allergen terms matched in this text"
        return "; ".join(f"{cat} (via '{term}', {mtype})" for cat, mtype, term in hits)

    return [lookup_event, find_other_dates, events_on_date, list_events,
            notifications_for_event, get_dish_allergen_note, check_known_allergens]


def ask(client, division: str, question: str, history: list | None = None) -> str:
    """
    Answers ONE question about this division's stored data.

    history: prior turns from THIS SAME conversation, oldest first, as
    [{"role": "user"|"assistant", "content": str}, ...] -- pass the
    caller's own chat history here (not including `question` itself) so
    a follow-up like "now tell me the allergens" or a dish referenced
    only loosely ("the fruit tray") can be resolved against what was
    already said/shown earlier in the conversation, instead of every
    question being answered as if it's the first message ever sent.

    Callers MUST check llm_client.is_llm_available() first.
    """
    tools = _build_tools(client, division)
    return llm_client.run_tool_loop(SYSTEM_PROMPT, question, tools, max_turns=4, history=history)

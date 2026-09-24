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

from datetime import datetime
from zoneinfo import ZoneInfo

import allergen_reference
import contract_store as store
import llm_client

EASTERN = ZoneInfo("America/New_York")

SYSTEM_PROMPT = """You are a read-only assistant answering a chef's
questions about their catering operation's stored contract data. You
have tools to look up events, dates, and change history already on
file -- use them to answer; never guess or invent a fact you didn't get
from a tool. If nothing on file answers the question, say so plainly
("I don't have that on file") rather than speculating. When you cite a
fact, name which event/date/notification it came from.

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
a menu item has Notes in the tool result, put them on their own
indented line right under that item -- never merge them into the same
line as the item, and never drop them even if the question wasn't
specifically about notes/ingredients:
Event <id>
Date: <date>
Time: <time>
Location: <location>
Guest count: <count>
Menu:
- <item> (<qty>)
  Notes: <notes, only if present>
- <item> (<qty>)

For change history, reply with one bolded timestamp per notification,
followed by its changes as bullets:
**<timestamp>**
- <change>
- <change>

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
  yourself or silently merging the history."""


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


def _build_tools(client, division: str) -> list:
    def lookup_event(event_id: str, event_date: str) -> str:
        """Looks up the exact stored contract record for this event_id AND event_date together. Returns one field per line, menu items as a bulleted list with quantity, each followed by its Notes on an indented line if the document had any (a Goodies To Go packing list's "Notes" column, or a Good Tidings description line -- same field, printed underneath the item's name on the source document) -- pass this structure through as-is, don't compress it into a paragraph, and don't drop the Notes lines."""
        record = store.lookup(client, division, event_id, event_date)
        if record is None:
            return "no record on file for that exact event_id and event_date combination"
        menu_lines = "\n".join(
            f"- {i.recipe_name} ({i.qty_unit})" + (f"\n  Notes: {i.description}" if i.description else "")
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
        """Lists every stored event on this calendar date, regardless of event_id."""
        records = store.find_by_date(client, division, event_date)
        if not records:
            return "no stored events found on that date"
        return "; ".join(f"Event {r.event_id} at {r.location} ({r.guest_count} guests)"
                          for r in records)

    def notifications_for_event(event_id: str, event_date: str = "") -> str:
        """Lists the permanent change history for this event_id, most recent 10 first, one bolded timestamp per notification followed by its changes as a bulleted list. If event_date is omitted and this event_id has history under MORE THAN ONE date, returns an AMBIGUOUS warning instead of history -- the same event_id can legitimately be a different booking that reused the number on a different date, and that history must never be silently mixed with this one. When that happens, call this tool again with event_date set to the one the chef confirms."""
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

    return [lookup_event, find_other_dates, events_on_date, notifications_for_event,
            get_dish_allergen_note, check_known_allergens]


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

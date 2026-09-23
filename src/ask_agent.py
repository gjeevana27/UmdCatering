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

For event details, reply in exactly this shape (one field per line):
Event <id>
Date: <date>
Time: <time>
Location: <location>
Guest count: <count>
Menu:
- <item> (<qty>)
- <item> (<qty>)

For change history, reply with one bolded timestamp per notification,
followed by its changes as bullets:
**<timestamp>**
- <change>
- <change>

Never merge multiple fields or multiple changes onto one line."""


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
        """Looks up the exact stored contract record for this event_id AND event_date together. Returns one field per line, menu items as a bulleted list with quantity -- pass this structure through as-is, don't compress it into a paragraph."""
        record = store.lookup(client, division, event_id, event_date)
        if record is None:
            return "no record on file for that exact event_id and event_date combination"
        menu_lines = "\n".join(f"- {i.recipe_name} ({i.qty_unit})" for i in record.menu_items) \
            or "- (no menu items on file)"
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

    def notifications_for_event(event_id: str) -> str:
        """Lists the permanent change history (every detected change, ever) for this event_id, most recent 10 first, with a short readable timestamp. Returns one bolded timestamp per notification followed by its changes as a bulleted list -- pass this structure through as-is, don't merge multiple changes onto one line."""
        matches = [n for n in store.list_notifications(client, division, limit=100)
                   if n.get("event_id") == event_id]
        if not matches:
            return "no change history on file for this event_id"
        blocks = []
        for n in matches[:10]:
            changes = "\n".join(f"- {c.get('label', '')}" for c in n.get("changes", [])) \
                or "- (no changes recorded)"
            blocks.append(f"**{_format_time(n.get('created_at', ''))}**\n{changes}")
        return "\n\n".join(blocks)

    def get_dish_allergen_note(dish_name: str) -> str:
        """Returns any chef-confirmed allergens already on file for this exact dish name."""
        notes = store.get_dish_allergen_note(client, dish_name)
        return ", ".join(notes) if notes else "nothing on file for this dish"

    def check_known_allergens(dish_name_or_ingredient_text: str) -> str:
        """Scans this text against the kitchen's allergen reference for direct or hidden-carrier allergen matches."""
        hits = allergen_reference.scan_ingredient(dish_name_or_ingredient_text)
        if not hits:
            return "no known allergen terms matched in this text"
        return "; ".join(f"{cat} (via '{term}', {mtype})" for cat, mtype, term in hits)

    return [lookup_event, find_other_dates, events_on_date, notifications_for_event,
            get_dish_allergen_note, check_known_allergens]


def ask(client, division: str, question: str) -> str:
    """
    Answers ONE question about this division's stored data.
    Callers MUST check llm_client.is_llm_available() first.
    """
    tools = _build_tools(client, division)
    return llm_client.run_tool_loop(SYSTEM_PROMPT, question, tools, max_turns=4)

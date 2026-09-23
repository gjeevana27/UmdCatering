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

import allergen_reference
import contract_store as store
import llm_client

SYSTEM_PROMPT = """You are a read-only assistant answering a chef's
questions about their catering operation's stored contract data. You
have tools to look up events, dates, and change history already on
file -- use them to answer; never guess or invent a fact you didn't get
from a tool. If nothing on file answers the question, say so plainly
("I don't have that on file") rather than speculating. When you cite a
fact, name which event/date/notification it came from. Keep answers
short and direct."""


def _build_tools(client, division: str) -> list:
    def lookup_event(event_id: str, event_date: str) -> str:
        """Looks up the exact stored contract record for this event_id AND event_date together."""
        record = store.lookup(client, division, event_id, event_date)
        if record is None:
            return "no record on file for that exact event_id and event_date combination"
        items = ", ".join(i.recipe_name for i in record.menu_items) or "(no menu items on file)"
        return (f"Event {record.event_id}, {record.event_date}, {record.event_time}, "
                f"location: {record.location}, type: {record.event_type}, "
                f"guests: {record.guest_count}. Menu: {items}")

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
        """Lists the permanent change history (every detected change, ever) for this event_id, most recent first."""
        matches = [n for n in store.list_notifications(client, division, limit=100)
                   if n.get("event_id") == event_id]
        if not matches:
            return "no change history on file for this event_id"
        lines = []
        for n in matches[:10]:
            labels = "; ".join(
                f"{c.get('label', '')} ({c.get('decision', '')})"
                for c in n.get("changes", [])
            )
            lines.append(f"{n.get('created_at', '?')}: {labels}")
        return "\n".join(lines)

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

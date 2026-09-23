"""
Bounded investigation agent for escalated contract changes -- opt-in,
one click per item, never automatic on every escalation. Runs a
manually-controlled Gemini tool-calling loop (see
llm_client.run_tool_loop()) with a small set of read-only lookups, to
add context BEFORE the chef acts on an escalation -- it never changes
the decision itself, only explains it further.

Scoped to one division at a time, same as everywhere else in this
project: every tool here is bound to a single (client, division) pair
via closure, so the model has no way to even ask about the other
division's data.
"""

import allergen_reference
import contract_store as store
import llm_client

SYSTEM_PROMPT = """You are a brief investigative assistant for a chef
reviewing an escalated contract change at a catering operation. You have
read-only tools to look up related information already on file. Use
them if they would add useful context; skip them if the change is
already self-explanatory. Respond with 1-3 short sentences, plain
language, no speculation beyond what the tools actually returned. You
are adding context, not making a decision -- never tell the chef what
to do, only what you found."""


def _build_tools(client, division: str) -> list:
    def get_dish_allergen_note(dish_name: str) -> str:
        """Returns any chef-confirmed allergens already on file for this exact dish name."""
        notes = store.get_dish_allergen_note(client, dish_name)
        return ", ".join(notes) if notes else "nothing on file for this dish"

    def find_other_dates(event_id: str) -> str:
        """Checks whether this event_id has any other stored record under a different date (a possible reschedule)."""
        others = store.find_other_dates(client, division, event_id)
        if not others:
            return "no other dates on file for this event_id"
        return "; ".join(f"{r.event_date} ({r.location}, {r.guest_count} guests)"
                          for r in others)

    def recent_notifications_for_event(event_id: str) -> str:
        """Lists recent stored notifications for this exact event_id, most recent first, up to 5."""
        matches = [n for n in store.list_notifications(client, division, limit=50)
                   if n.get("event_id") == event_id]
        if not matches:
            return "no notifications on file for this event_id"
        lines = []
        for n in matches[:5]:
            labels = "; ".join(c.get("label", "") for c in n.get("changes", [])[:3])
            lines.append(f"{n.get('created_at', '?')}: {labels}")
        return "\n".join(lines)

    def check_known_allergens(dish_name_or_ingredient_text: str) -> str:
        """Scans this text against the kitchen's allergen reference for direct or hidden-carrier allergen matches."""
        hits = allergen_reference.scan_ingredient(dish_name_or_ingredient_text)
        if not hits:
            return "no known allergen terms matched in this text"
        return "; ".join(f"{cat} (via '{term}', {mtype})" for cat, mtype, term in hits)

    return [get_dish_allergen_note, find_other_dates, recent_notifications_for_event,
            check_known_allergens]


def investigate(client, division: str, event_id: str, change_label: str,
                 change_reasoning: str) -> str:
    """
    Runs the bounded investigation loop for ONE escalated change.
    Callers MUST check llm_client.is_llm_available() first.
    """
    tools = _build_tools(client, division)
    user_message = (
        f"Event ID: {event_id}\n"
        f"Escalated change: {change_label}\n"
        f"Original reasoning: {change_reasoning}\n\n"
        f"Add any useful context a chef reviewing this should know before acting on it."
    )
    return llm_client.run_tool_loop(SYSTEM_PROMPT, user_message, tools, max_turns=4)

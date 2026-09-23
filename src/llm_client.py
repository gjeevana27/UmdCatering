"""
Thin wrapper around Google's Gemini API (free tier — see README), used only
for contract_agent.py's genuinely ambiguous cases -- a reworded event_type,
a menu description edit with no quantity change. Every change that's
unambiguous (a location/time change, an added or removed menu item, a
field gone blank) is decided by contract_agent.py's own rules alone and
never needs the LLM at all -- that's a deliberate design choice, not a
fallback: the deterministic checks stay auditable, and the model is only
asked to weigh in exactly where human-style judgment is genuinely needed.

If GEMINI_API_KEY isn't set, ambiguous changes get routed to "review"
rather than auto-resolved. This is the honest default -- it never invents
a judgment call it can't actually make.

Get a free key (no credit card required) at https://aistudio.google.com/apikey
"""

import json
import os
import time

import rate_guard

try:
    from google import genai
    from google.genai import errors as genai_errors
    _CLIENT_AVAILABLE = True
except ImportError:
    _CLIENT_AVAILABLE = False

MODEL = "gemini-3.5-flash-lite"  # see extract.py for why this isn't
# gemini-3.5-flash (its free tier is only 20 requests/day/project, confirmed
# via a live 429) or gemini-2.5-flash-lite (no longer available to new
# users). Check https://ai.google.dev/gemini-api/docs/rate-limits for
# current numbers if this has been superseded.


def is_llm_available() -> bool:
    return _CLIENT_AVAILABLE and bool(os.environ.get("GEMINI_API_KEY"))


_RETRY_BACKOFF_SECONDS = [3, 10, 25]  # 3 retries after the first attempt --
# matching extract.py's retry policy. Previously there was no retry at all
# here, so any transient 503 fell straight through to callers' generic
# "LLM judgment failed, defaulted to review" fallback -- safe, but it
# quietly degrades automation on every routine blip at real-world upload
# volume, instead of just riding it out.


def _call_gemini_judge(prompt: str) -> dict:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = None
    last_error = None
    for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
        try:
            rate_guard.check_and_increment()
            response = client.models.generate_content(model=MODEL, contents=prompt)
            break
        except rate_guard.RateLimitExceeded as e:
            raise RuntimeError(str(e)) from e
        except genai_errors.ServerError as e:
            last_error = e
            if attempt < len(_RETRY_BACKOFF_SECONDS):
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise RuntimeError(
                f"Gemini's servers are temporarily overloaded (503, high "
                f"demand) and didn't recover after "
                f"{len(_RETRY_BACKOFF_SECONDS)} retries: {e}"
            ) from e

    if response is None:
        raise RuntimeError(f"Gemini call failed with no response: {last_error}")

    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def judge_ambiguous_finding(finding_detail: str, context: str) -> dict:
    """
    Ask the model to weigh in on one ambiguous finding only.
    Returns {"decision": "escalate"|"auto_pass"|"flag_low_confidence",
             "reasoning": str}.
    Callers MUST check is_llm_available() first; this raises if it isn't.
    """
    if not is_llm_available():
        raise RuntimeError(
            "LLM not available -- check is_llm_available() before calling."
        )

    prompt = f"""You are reviewing ONE ambiguous compliance finding from an
event production sheet, for a human catering/event-production reviewer.

Finding: {finding_detail}
Context: {context}

Decide exactly one of:
- "escalate": a real risk that needs a human to act before the event
- "auto_pass": not actually a problem, safe to clear automatically
- "flag_low_confidence": genuinely unclear, needs a human's eyes but isn't urgent

Respond ONLY as JSON: {{"decision": "...", "reasoning": "one sentence"}}"""

    return _call_gemini_judge(prompt)


def judge_contract_change(field_label: str, old_value: str, new_value: str,
                           context: str = "", item_label: str = "") -> dict:
    """
    Ask the model to weigh in on one ambiguous CONTRACT-VERSION change --
    used by contract_agent.py for changes a plain diff can't safely
    classify on its own (a reworded field, a description-only menu-item
    edit). Separate from judge_ambiguous_finding() above because the
    decision categories differ: this is about whether an event contract's
    changed field is operationally significant, not about compliance risk.

    field_label/old_value/new_value are kept as SEPARATE parameters,
    deliberately never pre-formatted into one string by the caller --
    passing a single combined string like "'Dish Name' changed: field:
    'old' -> 'new'" previously let the model conflate an item's NAME
    (item_label) with the field content that actually changed, producing
    a hallucinated reasoning that described a change to the name as if it
    were a change to the field (e.g. claiming a word was "removed from
    the description" when that word was only ever part of the dish's
    name, never the description, in either version). Structuring the
    prompt so old/new are visually and textually isolated, with an
    explicit instruction to reason only from them, avoids that.

    item_label: optional -- e.g. a dish name -- shown only as identifying
    context, clearly separated from the actual old/new values being judged.

    Returns {"decision": "escalate"|"review"|"log", "reasoning": str}.
    Callers MUST check is_llm_available() first; this raises if it isn't.
    """
    if not is_llm_available():
        raise RuntimeError(
            "LLM not available -- check is_llm_available() before calling."
        )

    item_line = f"Item: {item_label}\n" if item_label else ""
    context_line = f"Context: {context}\n" if context else ""

    prompt = f"""You are reviewing ONE change between two versions of the
same event's catering contract, for a chef at a small catering operation
who needs to know whether it's urgent or not -- NOT whether it's worth
knowing about at all. Every change is shown to the chef regardless of
your answer; you are only deciding how urgently.

{item_line}Field that changed: {field_label}
{context_line}
Base your reasoning ONLY on the literal difference between these two
exact values. Do not reference or assume anything about the item's name
or any other field unless that exact text appears in OLD or NEW below --
in particular, do not describe something as "removed" or "added" unless
it is present in one of these two values and absent from the other.

OLD: {old_value}
NEW: {new_value}

Decide exactly one of:
- "escalate": urgent -- the chef should act on this before the event
- "review": still worth showing, but not time-critical (e.g. a wording
  clarification, a wait for more information) -- NEVER pick this because
  a change seems too small or insignificant to matter; small changes
  still matter at this scale of business, this option is only for
  "not urgent," not "not important"

Respond ONLY as JSON: {{"decision": "...", "reasoning": "one sentence"}}"""

    return _call_gemini_judge(prompt)


def run_tool_loop(system_prompt: str, user_message: str, tools: list, *,
                   max_turns: int = 4) -> str:
    """
    Runs a manually-controlled Gemini function-calling loop -- used by
    investigation_agent.py and (later) the read-only chatbot. Deliberately
    NOT the google-genai SDK's automatic function calling (which executes
    tool calls and loops internally, inside one generate_content() call):
    that would give rate_guard no hook to check before each underlying
    model call, breaking the guarantee every other Gemini call in this
    project already has -- checked before every single attempt, not just
    once per user action.

    `tools`: plain Python callables, each with type hints and a docstring
    -- both are used to build the tool's schema automatically
    (FunctionDeclaration.from_callable()), so no hand-written JSON schema
    is needed per tool. Each callable should return something JSON-
    serializable (a string, list, or plain dict) -- not a dataclass or
    other object the API can't serialize back to the model.

    Each turn: rate_guard-checked, sent to Gemini; if the model calls a
    tool, it's executed locally (never allowed to error the whole loop --
    a failing tool call becomes a {"error": ...} result handed back to
    the model, not a crash) and the result fed back for the next turn.
    Stops as soon as the model responds with plain text instead of a tool
    call, or after max_turns, whichever comes first -- the cap exists
    specifically so a model that doesn't converge can't loop unboundedly.

    Callers MUST check is_llm_available() first; this raises if it isn't.
    """
    if not is_llm_available():
        raise RuntimeError(
            "LLM not available -- check is_llm_available() before calling."
        )
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tool_by_name = {t.__name__: t for t in tools}
    declarations = [types.FunctionDeclaration.from_callable(client=client, callable=t)
                     for t in tools]
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=declarations)],
        system_instruction=system_prompt,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_message)])]

    for _ in range(max_turns):
        try:
            rate_guard.check_and_increment()
        except rate_guard.RateLimitExceeded as e:
            return f"(Daily Gemini call limit reached -- try again once quota resets: {e})"

        try:
            response = client.models.generate_content(model=MODEL, contents=contents,
                                                        config=config)
        except genai_errors.ServerError as e:
            return f"(Gemini's servers are temporarily overloaded -- try again shortly: {e})"

        if not response.function_calls:
            return response.text or "(no response)"

        contents.append(response.candidates[0].content)
        for fc in response.function_calls:
            fn = tool_by_name.get(fc.name)
            if fn is None:
                result = {"error": f"unknown tool '{fc.name}'"}
            else:
                try:
                    result = fn(**fc.args)
                except Exception as e:
                    result = {"error": str(e)}
            contents.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=fc.name, response={"result": result})
            ]))

    return "(Didn't reach a final answer in time -- try again, or check manually.)"

"""Pit stop mode: fetch the telemetry in code, let the model only talk, then check its answer.

Small local models are bad at deciding which tool to call and tend to invent figures.
Here Python picks the tools by keyword, the model gets the data in one message (with no
tools of its own), and the answer is checked: every dollar amount must be in the data,
anything called "the biggest" must really be rank 1, and it must not claim to act.
If the answer still fails after one retry, a deterministic race-engineer answer is used.
"""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

from .tools import compare_with_last_month, cost_by_service, daily_spend_trend, scan_for_debris

TOOL_FUNCS = {
    "cost_by_service": cost_by_service,
    "daily_spend_trend": daily_spend_trend,
    "compare_with_last_month": compare_with_last_month,
    "scan_for_debris": scan_for_debris,
}

ROUTES = [
    (("jump", "spike", "why", "went up", "increase", "trend", "daily"),
     [("daily_spend_trend", {"days": 14}), ("cost_by_service", {"days": 30})]),
    (("forgot", "forget", "left running", "idle", "unused", "turn off", "debris", "waste"),
     [("scan_for_debris", {})]),
    (("last month", "more than", "compare", "vs"),
     [("compare_with_last_month", {})]),
]

TELEMETRY_HEADER = "TELEMETRY (the only source of facts; use these exact numbers, never others):"
GUARD_LOG = Path("guard_log.jsonl")

# Which list in each result gets ranked, the name field, and the value that decides rank 1.
RANKED_LISTS = {
    "cost_by_service": ("services", "service", lambda i: i["usd"], "usd"),
    "compare_with_last_month": ("by_service", "service", lambda i: abs(i["change_usd"]), "change_usd"),
    "scan_for_debris": ("items", "type", lambda i: i["est_monthly_usd"], "est_monthly_usd"),
}

PLAIN_NAMES = {
    "EC2 - Other": "NAT gateways, EBS volumes and data transfer",
    "Amazon Elastic Container Service for Kubernetes": "EKS (Kubernetes control plane)",
    "Amazon Elastic Compute Cloud - Compute": "EC2 instances (virtual servers)",
    "Amazon Simple Storage Service": "S3 storage",
    "Amazon CloudFront": "CloudFront (content delivery)",
    "Amazon Route 53": "Route 53 (DNS)",
    "AWS Lambda": "Lambda functions",
    "Amazon CloudWatch": "CloudWatch (logs and metrics)",
}

# Short names people (and models) use for each item.
ALIASES = {
    "Amazon Elastic Container Service for Kubernetes": ["EKS", "Kubernetes"],
    "EC2 - Other": ["EC2-Other", "NAT", "EBS"],
    "Amazon Elastic Compute Cloud - Compute": ["EC2"],
    "Amazon Simple Storage Service": ["S3"],
    "Amazon CloudFront": ["CloudFront"],
    "Amazon Route 53": ["Route 53", "Route53"],
    "AWS Lambda": ["Lambda"],
    "Amazon CloudWatch": ["CloudWatch"],
    "NAT gateway": ["NAT"],
    "Unattached EBS volume": ["EBS"],
    "Unassociated Elastic IP": ["Elastic IP", "EIP"],
}

DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
SUPERLATIVE_RE = re.compile(
    r"\b(biggest|largest|most|main|primary|primarily|mostly|highest|number one)\b|#1"
    r"|\btop\b(?!\s+(\d+|two|three|four|five|six|seven|eight|nine|ten)\b)",  # "top five" is a list, not rank 1
    re.IGNORECASE,
)
# "second-largest", "the 3rd biggest", "next highest": the ordinal right before a superlative.
ORDINAL_RE = re.compile(r"\b(second|third|fourth|fifth|2nd|3rd|4th|5th|next)[\s-]*$", re.IGNORECASE)
ORDINAL_RANKS = {"second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
                 "next": None}
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
FALL_RE = re.compile(r"\b(drop(s|ped)?|decrease[sd]?|fell|down|lower|reduced|cheaper)\b", re.IGNORECASE)
RISE_RE = re.compile(r"\b(spike[sd]?|jump(s|ed)?|rose|increase[sd]?|up|higher|grew)\b", re.IGNORECASE)
# Phrasal verbs that contain up/down but say nothing about direction ("shut down the cluster").
NOT_DIRECTION_RE = re.compile(
    r"\b(shut|shutting|scale|scaling|tear|tearing|turn|turning|wind|winding|break|breaking)\s+down\b"
    r"|\b(set|setting|spun|spin|spinning|sign|look|clean|cleaning|back|pick|keep|follow|show|open|pop|add|adds|added|adding)\s+up\b"
    r"|\bup\s+to\b",
    re.IGNORECASE,
)
_ACTS = r"(turning off|shutting down|deleting|stopping|terminating|removing)"
ACTION_RE = re.compile(
    rf"\b(i'm|i am|i'll|i will|i've|i have|let me)\s+{_ACTS}\b"
    rf"|\bcopy,?\s+{_ACTS}\b"
    r"|\bi('ve| have)?\s+(deleted|stopped|terminated|removed|shut down|turned off)\b"
    r"|\bi('ll| will)\s+(delete|stop|terminate|remove|shut down|turn off)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


# --------------------------------------------------------------------------- #
# Routing, fetching, annotating
# --------------------------------------------------------------------------- #


def pick_tools(question: str) -> list[tuple[str, dict]]:
    """Choose which tools to run from keywords in the question."""
    q = question.lower()
    picked: list[tuple[str, dict]] = []
    for keywords, calls in ROUTES:
        if any(k in q for k in keywords):
            for call in calls:
                if call[0] not in [p[0] for p in picked]:
                    picked.append(call)
    return picked or [("cost_by_service", {"days": 30})]


def fetch(calls: list[tuple[str, dict]]) -> dict[str, dict]:
    return {name: TOOL_FUNCS[name](**kwargs) for name, kwargs in calls}


def annotate(telemetry: dict[str, dict]) -> dict[str, dict]:
    """Add rank, biggest and plain_names so the ranking is obvious to a small model."""
    out = copy.deepcopy(telemetry)
    for tool, result in out.items():
        if tool not in RANKED_LISTS or "error" in result:
            continue
        list_key, name_key, rank_by, value_key = RANKED_LISTS[tool]
        items = result.get(list_key) or []
        for rank, item in enumerate(sorted(items, key=rank_by, reverse=True), start=1):
            item["rank"] = rank
        items.sort(key=lambda i: i["rank"])
        if items:
            top = items[0]
            result["biggest"] = {name_key: top[name_key], value_key: top[value_key]}
            if "id" in top:
                result["biggest"]["id"] = top["id"]
        plain = {i[name_key]: PLAIN_NAMES[i[name_key]] for i in items if i[name_key] in PLAIN_NAMES}
        if plain:
            result["plain_names"] = plain
    return out


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def dollar_amounts(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in DOLLAR_RE.findall(text) if m.replace(",", "")]


def telemetry_numbers(telemetry: dict) -> set[float]:
    """Every number in the telemetry, its roundings, list totals and daily-to-monthly estimates."""
    nums: set[float] = set()

    def add(v: float) -> None:
        nums.update({float(v), round(v, 0), round(v, 1), abs(float(v))})

    def walk(node, daily: bool = False) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add(node)
            if daily:
                add(node * 30)
        elif isinstance(node, dict):
            for k, v in node.items():
                if k != "rank":
                    walk(v, daily or k in ("daily", "biggest_jump"))
        elif isinstance(node, list):
            for item in node:
                walk(item, daily)
            dicts = [i for i in node if isinstance(i, dict)]
            for key in {k for d in dicts for k in d if k.endswith("usd")}:
                vals = [d[key] for d in dicts if isinstance(d.get(key), (int, float))]
                if vals:
                    add(sum(vals))

    walk(telemetry)
    return nums


def bad_numbers(text: str, telemetry: dict) -> list[float]:
    known = telemetry_numbers(telemetry)
    return [a for a in dollar_amounts(text) if not any(abs(a - n) <= max(0.05, 0.02 * abs(n)) for n in known)]


def claims_action(text: str) -> bool:
    return bool(ACTION_RE.search(_normalise(text)))


def _ranked_entities(telemetry: dict) -> list[tuple[str, dict, dict]]:
    """(pattern, item, biggest-of-its-list) for every name, plain name and alias in the telemetry."""
    out = []
    for tool, result in telemetry.items():
        if tool not in RANKED_LISTS or not result.get("biggest"):
            continue
        list_key, name_key, _, _ = RANKED_LISTS[tool]
        for item in result.get(list_key) or []:
            name = item[name_key]
            for pattern in [name, PLAIN_NAMES.get(name), *ALIASES.get(name, [])]:
                if pattern:
                    out.append((pattern, item, result))
    return out


def _find_spans(sentence: str, entities: list[tuple]) -> list[tuple]:
    """(start, end, item, extra) for every entity mentioned, dropping matches inside longer ones."""
    spans = []
    for pattern, item, extra in entities:
        for m in re.finditer(rf"(?<!\w){re.escape(pattern)}(?!\w)", sentence, re.IGNORECASE):
            spans.append((m.start(), m.end(), item, extra))
    # Drop spans inside a longer match ("EC2" inside "EC2 - Other").
    return [s for s in spans if not any(o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0])
                                        for o in spans)]


def _nearest(spans: list[tuple], word: re.Match) -> list[tuple]:
    """All spans at the position closest to a keyword (one mention can map to several items)."""
    best = min(spans, key=lambda s: min(abs(s[0] - word.end()), abs(word.start() - s[1])))
    return [s for s in spans if (s[0], s[1]) == (best[0], best[1])]


def ranking_errors(text: str, telemetry: dict) -> list[str]:
    """Corrections for sentences that call something the biggest when it isn't rank 1."""
    entities = _ranked_entities(telemetry)
    errors = []
    for sentence in SENTENCE_RE.split(_normalise(text)):
        sups = list(SUPERLATIVE_RE.finditer(sentence))
        if not sups:
            continue
        spans = _find_spans(sentence, entities)
        if not spans:
            continue
        ordinal = ORDINAL_RE.search(sentence[:sups[0].start()])
        rank = ORDINAL_RANKS.get(ordinal.group(1).lower()) if ordinal else 1
        if rank is None:
            continue  # "the next largest": no fixed rank to check
        claimed = _nearest(spans, sups[0])
        start, end = claimed[0][0], claimed[0][1]
        if any(s[2].get("rank") == rank for s in claimed):
            continue
        result = claimed[0][3]
        if rank == 1:
            big = result["biggest"]
            big_name = big.get("service") or big.get("type")
            big_value = next(v for k, v in big.items() if k.endswith("usd"))
            errors.append(f"The biggest is {PLAIN_NAMES.get(big_name, big_name)} at {_usd(abs(big_value))}, "
                          f"not {sentence[start:end]}.")
            continue
        tool = next(t for t, r in telemetry.items() if r is result)
        list_key, name_key, _, value_key = RANKED_LISTS[tool]
        actual = next((i for i in result.get(list_key) or [] if i.get("rank") == rank), None)
        if actual:
            errors.append(f"Number {rank} is {PLAIN_NAMES.get(actual[name_key], actual[name_key])} at "
                          f"{_usd(abs(actual[value_key]))}, not {sentence[start:end]}.")
    return errors


def _change_entities(telemetry: dict) -> list[tuple[str, dict, None]]:
    """Items that carry a change_usd: the jump breakdown and the month-over-month comparison."""
    rows = (telemetry.get("daily_spend_trend", {}).get("jump_breakdown") or []) + \
        (telemetry.get("compare_with_last_month", {}).get("by_service") or [])
    out = []
    for row in rows:
        name = row["service"]
        for pattern in [name, PLAIN_NAMES.get(name), *ALIASES.get(name, [])]:
            if pattern:
                out.append((pattern, row, None))
    return out


def direction_errors(text: str, telemetry: dict) -> list[str]:
    """Corrections for sentences that say an item went down when it went up, or the reverse."""
    entities = _change_entities(telemetry)
    if not entities:
        return []
    errors = []
    for sentence in SENTENCE_RE.split(_normalise(text)):
        cleaned = NOT_DIRECTION_RE.sub(lambda m: " " * len(m.group()), sentence)
        words = [(m, -1) for m in FALL_RE.finditer(cleaned)] + [(m, 1) for m in RISE_RE.finditer(cleaned)]
        spans = _find_spans(sentence, entities)
        if not words or not spans:
            continue
        for word, sign in words:
            claimed = _nearest(spans, word)
            changes = [s[2]["change_usd"] for s in claimed if s[2]["change_usd"]]
            if changes and all(c * sign < 0 for c in changes):
                name = sentence[claimed[0][0]:claimed[0][1]]
                actual, said = ("up", "down") if changes[0] > 0 else ("down", "up")
                msg = f"{name} went {actual} by {_usd(abs(changes[0]))}, not {said}."
                if msg not in errors:
                    errors.append(msg)
    return errors


def attribution_errors(text: str, telemetry: dict) -> list[str]:
    """Corrections for sentences that pin the whole jump on one service when several moved."""
    trend = telemetry.get("daily_spend_trend", {})
    jump, breakdown = trend.get("biggest_jump"), trend.get("jump_breakdown") or []
    if not jump or len(breakdown) < 2:
        return []
    total = jump["increase_usd"]
    entities = [(p, row, None) for p, row, _ in _change_entities({"daily_spend_trend": trend})]
    for sentence in SENTENCE_RE.split(_normalise(text)):
        if not any(abs(a - total) <= max(0.05, 0.02 * abs(total)) for a in dollar_amounts(sentence)):
            continue
        named = {s[2]["service"] for s in _find_spans(sentence, entities)}
        if len(named) == 1:
            return [f"{_usd(total)} is the total jump across services, not one service: {trend['summary']}"]
    return []


ALL_CLEAR_RE = re.compile(r"nothing to fix|all good|no action needed|nothing to worry|good pace", re.IGNORECASE)
BOX_BOX_RE = re.compile(r"\bbox,?\s*box\b", re.IGNORECASE)
# "per month" / "a month" right after an amount, but not "over a month" style spans of time.
PER_MONTH_RE = re.compile(r"^[^.$\n]{0,25}?(?<!over )(?<!in )(?<!for )(?<!across )\b(per month|a month)\b",
                          re.IGNORECASE)
ACTION_SERVICES = {"Amazon Elastic Container Service for Kubernetes", "EC2 - Other",
                   "Amazon Elastic Compute Cloud - Compute"}


def action_item(telemetry: dict) -> dict | None:
    """The rank-1 thing to act on: name, plain name, amount (with its time unit) and a one-line text."""
    debris = telemetry.get("scan_for_debris", {}).get("items") or []
    if debris:
        top = max(debris, key=lambda i: i["est_monthly_usd"])
        amount = f"{_usd(top['est_monthly_usd'])} a month"
        return {"name": f"{top['type']} {top['id']}", "plain": top["detail"], "amount": amount,
                "text": f"{top['type']} {top['id']} at about {amount}"}
    breakdown = telemetry.get("daily_spend_trend", {}).get("jump_breakdown") or []
    if breakdown and breakdown[0]["change_usd"] >= 1:
        top = breakdown[0]
        amount = f"{_usd(top['change_usd'])} a day more"
        return {"name": top["service"], "plain": PLAIN_NAMES.get(top["service"], top["service"]), "amount": amount,
                "text": f"{_plain(top['service'])}, up {_usd(top['change_usd'])} a day"}
    services = telemetry.get("cost_by_service", {}).get("services") or []
    if services:
        top = max(services, key=lambda s: s["usd"])
        if top["service"] in ACTION_SERVICES and top["usd"] >= 5:
            amount = f"{_usd(top['usd'])} over the last {_days(telemetry)} days"
            return {"name": top["service"], "plain": PLAIN_NAMES.get(top["service"], top["service"]),
                    "amount": amount, "text": f"{_plain(top['service'])} at {amount}"}
    cmp = telemetry.get("compare_with_last_month", {})
    rows = cmp.get("by_service") or []
    if rows and cmp.get("this_month_total_usd", 0) - cmp.get("last_month_total_usd", 0) >= 5:
        top = max(rows, key=lambda r: abs(r["change_usd"]))
        direction = "up" if top["change_usd"] > 0 else "down"
        amount = f"{_usd(abs(top['change_usd']))} {direction} on last month"
        return {"name": top["service"], "plain": PLAIN_NAMES.get(top["service"], top["service"]), "amount": amount,
                "text": f"{_plain(top['service'])}, {direction} {_usd(abs(top['change_usd']))} on last month"}
    return None


def needs_action(telemetry: dict) -> str | None:
    """The rank-1 thing the user should act on, as text, or None when the data really is all clear."""
    item = action_item(telemetry)
    return item["text"] if item else None


def verdict_lines(telemetry: dict) -> str:
    """Tell the model up front whether there's something to fix, and how to word cost periods."""
    item = action_item(telemetry)
    if item:
        lines = [f"VERDICT: ACTION NEEDED. Top item: {item['name']} ({item['plain']}) at {item['amount']}. "
                 "Do not say spend is fine. End with exactly one line starting 'Box, box:' "
                 "telling the user what to do about it."]
    else:
        lines = ["VERDICT: ALL CLEAR. Say spend looks fine; no Box, box line needed."]
    cost = telemetry.get("cost_by_service", {})
    if cost.get("period"):
        lines.append(f"Figures from cost_by_service cover {cost['period']}. "
                     f"Say 'over the last {_days(telemetry)} days', never 'per month'.")
    return "\n".join(lines)


def _days(telemetry: dict) -> int:
    period = telemetry.get("cost_by_service", {}).get("period", "")
    try:
        start, end = (date.fromisoformat(p.strip()) for p in period.split(" to "))
        return (end - start).days
    except ValueError:
        return 30


def tone_errors(text: str, telemetry: dict) -> list[tuple[str, str]]:
    """'All clear' when there's something to fix, and a missing Box, box line."""
    todo = needs_action(telemetry)
    if not todo:
        return []
    failures = []
    if ALL_CLEAR_RE.search(text):
        failures.append(("tone", f"There IS something to fix: {todo}. End with a Box, box: action for it."))
    if not BOX_BOX_RE.search(text):
        failures.append(("no_action", f"End with a 'Box, box:' line telling the user what to do about {todo}."))
    return failures


def period_errors(text: str, telemetry: dict) -> list[str]:
    """Cost Explorer totals cover a date range; calling them 'per month' is wrong."""
    cost = telemetry.get("cost_by_service", {})
    if not cost.get("services"):
        return []
    period_figures = [s["usd"] for s in cost["services"]] + [cost.get("total_usd", 0)]
    monthly = [i["est_monthly_usd"] for i in telemetry.get("scan_for_debris", {}).get("items") or []]
    monthly.append(telemetry.get("scan_for_debris", {}).get("est_monthly_total_usd", -1))
    for m in DOLLAR_RE.finditer(text):
        amount = float(m.group(1).replace(",", ""))
        if any(abs(amount - v) <= 0.005 for v in monthly):
            continue  # debris estimates really are monthly
        if any(abs(amount - v) <= 0.05 for v in period_figures) and PER_MONTH_RE.search(text[m.end():]):
            return [f"These figures cover {cost['period']}, not per month. "
                    f"Say 'over the last {_days(telemetry)} days'."]
    return []


def check(text: str, telemetry: dict) -> list[tuple[str, str]]:
    """All guard failures as (kind, correction) pairs. Empty means the answer passed."""
    failures = []
    bad = bad_numbers(text, telemetry)
    if bad:
        failures.append(("number", "These amounts are not in the TELEMETRY: " + ", ".join(_usd(b) for b in bad)
                         + ". Use only numbers from the TELEMETRY."))
    for err in ranking_errors(text, telemetry):
        failures.append(("ranking", err))
    for err in direction_errors(text, telemetry):
        failures.append(("direction", err))
    for err in attribution_errors(text, telemetry):
        failures.append(("attribution", err))
    failures += tone_errors(text, telemetry)
    for err in period_errors(text, telemetry):
        failures.append(("period", err))
    if claims_action(text):
        failures.append(("action", "You are read-only; tell the user how to do it themselves."))
    return failures


# --------------------------------------------------------------------------- #
# Deterministic fallback, in the race-engineer voice
# --------------------------------------------------------------------------- #


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _plain(service: str) -> str:
    plain = PLAIN_NAMES.get(service)
    if not plain:
        return service
    if any(a.lower() in plain.lower() for a in ALIASES.get(service, [])):
        return plain
    return f"{service} ({plain})"


def _trend_lines(r: dict) -> tuple[list[str], str]:
    jump = r.get("biggest_jump")
    if not jump:
        return ["Lap times are steady: no day-over-day jump in the data."], ""
    breakdown = r.get("jump_breakdown") or []
    if r.get("summary") and breakdown:
        top = breakdown[0]
        return (
            [r["summary"]],
            f"Box, box: {_plain(top['service'])} is the biggest riser. Open Cost Explorer, filter to "
            f"{top['service']} for {jump['date']}, and remove anything launched that day you no longer need.",
        )
    return (
        [f"Biggest jump was on {jump['date']}: daily spend went from {_usd(jump['from_usd'])} "
         f"to {_usd(jump['to_usd'])}, up {_usd(jump['increase_usd'])} a day."],
        f"Box, box: check what was launched around {jump['date']} in CloudTrail > Event history, "
        "and shut down anything you no longer need.",
    )


def _cost_lines(r: dict) -> tuple[list[str], str]:
    services = sorted(r.get("services", []), key=lambda s: s["usd"], reverse=True)
    if not services:
        return ["No spend recorded in this period. Good pace, nothing to fix."], ""
    top = services[0]
    lines = [f"You spent {_usd(r['total_usd'])} over {r['period']}. "
             f"Biggest item is {_plain(top['service'])} at {_usd(top['usd'])}."]
    rest = ", ".join(f"{_plain(s['service'])} {_usd(s['usd'])}" for s in services[1:4])
    if rest:
        lines.append(f"Next up: {rest}.")
    return lines, f"Box, box: open Billing > Cost Explorer, filter to {top['service']} and group by Usage type."


def _compare_lines(r: dict) -> tuple[list[str], str]:
    rows = sorted(r.get("by_service", []), key=lambda x: abs(x["change_usd"]), reverse=True)
    lines = [f"Month to date you're at {_usd(r['this_month_total_usd'])}, against "
             f"{_usd(r['last_month_total_usd'])} for the same {r['compared_days']} days last month."]
    if not rows or rows[0]["change_usd"] == 0:
        return lines, ""
    top = rows[0]
    direction = "up" if top["change_usd"] > 0 else "down"
    lines.append(f"Biggest change is {_plain(top['service'])}, {direction} {_usd(abs(top['change_usd']))}.")
    return lines, f"Box, box: open Cost Explorer and filter to {top['service']} to see what changed."


def _debris_lines(r: dict) -> tuple[list[str], str]:
    items = r.get("items", [])
    if not items:
        return [f"Clean track in {r.get('region', 'this region')}: nothing idle found. Good pace, keep pushing."], ""
    items = sorted(items, key=lambda i: i["est_monthly_usd"], reverse=True)
    lines = [f"Found debris on track, about {_usd(r['est_monthly_total_usd'])} a month doing nothing:"]
    lines += [f"- {i['type']} {i['id']} ({i['detail']}): about {_usd(i['est_monthly_usd'])} a month" for i in items]
    return lines, "Box, box: confirm it's unused, then delete it in the console."


TEMPLATES = {
    "daily_spend_trend": _trend_lines,
    "cost_by_service": _cost_lines,
    "compare_with_last_month": _compare_lines,
    "scan_for_debris": _debris_lines,
}


def fallback_text(telemetry: dict[str, dict]) -> str:
    lines = ["Copy, here's the picture straight from the telemetry."]
    action = ""
    for name, result in telemetry.items():
        body, act = TEMPLATES[name](result)
        lines += body
        action = action or act
    if any(r.get("demo") for r in telemetry.values()):
        lines.append("(This is demo telemetry.)")
    if action:
        lines.append(action)
    return "\n".join(lines)


def error_text(result: dict) -> str:
    return f"Copy, we've got a problem on the pit wall before we can read the data. {result['fix']}"


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def log_guard(record: dict) -> None:
    try:
        with GUARD_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # stats are nice to have; never break an answer over them


def ask(agent, question: str) -> dict:
    """Answer a question with prefetched telemetry. Returns {text, tools, verified, fallback}."""
    t0 = time.time()
    calls = pick_tools(question)
    tools = [name for name, _ in calls]
    telemetry = annotate(fetch(calls))
    record = {"ts": datetime.now(timezone.utc).isoformat(), "question": question, "tools": tools}

    for result in telemetry.values():
        if "error" in result:
            log_guard({**record, "first_try_passed": False, "failures": ["tool_error"], "fallback": False,
                       "secs": round(time.time() - t0, 1)})
            return {"text": error_text(result), "tools": tools, "verified": False, "fallback": False}

    before = len(agent.messages)
    prompt = (f"{question}\n\n{verdict_lines(telemetry)}\n\n"
              f"{TELEMETRY_HEADER}\n{json.dumps(telemetry, separators=(',', ':'))}")
    text = str(agent(prompt)).strip()
    failures = check(text, telemetry)
    first_try_passed = not failures
    kinds = list(dict.fromkeys(k for k, _ in failures))

    fallback = False
    if failures:
        text = str(agent("Correction: " + " ".join(msg for _, msg in failures)
                         + " Answer the question again.")).strip()
        failures = check(text, telemetry)
        kinds += [k for k, _ in failures if k not in kinds]
        if failures:
            text, fallback = fallback_text(telemetry), True
    verified = not failures

    # Keep the conversation short for small models: just the question and the final answer.
    del agent.messages[before:]
    agent.messages.append({"role": "user", "content": [{"text": question}]})
    agent.messages.append({"role": "assistant", "content": [{"text": text}]})

    log_guard({**record, "first_try_passed": first_try_passed, "failures": kinds, "fallback": fallback,
               "secs": round(time.time() - t0, 1)})
    return {"text": text, "tools": tools, "verified": verified, "fallback": fallback}

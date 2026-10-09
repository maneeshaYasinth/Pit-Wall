"""Telemetry tools for Pit Wall.

Every tool returns plain dicts so the model only ever talks about numbers
it actually received. Set PITWALL_DEMO=1 to use built-in sample data
(handy when your real bill is $0.00 or you're recording a demo).
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from functools import wraps

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from strands import tool

from . import demo_data
from .attribution import jump_breakdown, jump_summary

CE_REGION = "us-east-1"  # Cost Explorer is a global API served from us-east-1
CACHE_TTL_SECONDS = 15 * 60  # Cost Explorer charges per request; CE data is ~daily anyway


def is_demo() -> bool:
    return os.getenv("PITWALL_DEMO", "0") == "1"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_cache: dict[tuple, tuple[float, dict]] = {}


def cached(fn):
    """Cache tool results for 15 minutes so a chatty user doesn't rack up CE API fees."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())), is_demo())
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        result = fn(*args, **kwargs)
        if "error" not in result:
            _cache[key] = (time.time(), result)
        return result

    return wrapper


def friendly_error(exc: Exception) -> dict:
    """Turn AWS exceptions into something the agent can explain in plain words."""
    if isinstance(exc, NoCredentialsError):
        return {
            "error": "no_credentials",
            "fix": "No AWS credentials found. Run `aws configure` or `aws sso login`, then try again.",
        }
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            return {
                "error": "access_denied",
                "fix": "These credentials can't read billing data. Attach the policy in iam-policy.json "
                "to your user/role. If you're in an IAM user, the root user must also enable "
                "'IAM access to billing information' in Account settings.",
            }
        if code == "DataUnavailableException":
            return {
                "error": "no_data_yet",
                "fix": "Cost Explorer has no data yet. On a new account it can take up to 24 hours "
                "after enabling Cost Explorer. Try demo mode (PITWALL_DEMO=1) meanwhile.",
            }
        return {"error": code, "fix": exc.response.get("Error", {}).get("Message", str(exc))}
    if isinstance(exc, BotoCoreError):
        return {"error": "network", "fix": f"Couldn't reach AWS: {exc}. Check your connection."}
    return {"error": "unexpected", "fix": str(exc)}


def _ce():
    return boto3.client("ce", region_name=CE_REGION)


def _amount(group_or_total: dict) -> float:
    return round(float(group_or_total["UnblendedCost"]["Amount"]), 2)


# --------------------------------------------------------------------------- #
# Tools the agent can call
# --------------------------------------------------------------------------- #


@tool
@cached
def cost_by_service(days: int = 30) -> dict:
    """Get total AWS spend for the last N days, broken down by service (the "championship standings").

    Use this for questions like "what am I spending on?", "what's my biggest cost?",
    or as the starting point for any cost question.

    Args:
        days: How many days back to look (1-90). Defaults to 30.
    """
    days = max(1, min(int(days), 90))
    if is_demo():
        return demo_data.cost_by_service(days)
    try:
        end = date.today()
        start = end - timedelta(days=days)
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        totals: dict[str, float] = {}
        for period in resp["ResultsByTime"]:
            for g in period["Groups"]:
                totals[g["Keys"][0]] = totals.get(g["Keys"][0], 0.0) + _amount(g["Metrics"])
        services = sorted(
            ({"service": k, "usd": round(v, 2)} for k, v in totals.items() if v >= 0.01),
            key=lambda s: s["usd"],
            reverse=True,
        )
        return {
            "period": f"{start.isoformat()} to {end.isoformat()}",
            "total_usd": round(sum(s["usd"] for s in services), 2),
            "services": services[:10],
            "note": "Cost Explorer data can lag by up to 24 hours.",
        }
    except Exception as exc:  # noqa: BLE001
        return friendly_error(exc)


@tool
@cached
def daily_spend_trend(days: int = 14) -> dict:
    """Get day-by-day total spend (the "lap times") and flag the biggest day-over-day jump.

    Use this for "why did my bill jump?", "when did costs go up?", or "is spend steady?".

    Args:
        days: How many days back to look (2-60). Defaults to 14.
    """
    days = max(2, min(int(days), 60))
    if is_demo():
        return demo_data.daily_spend_trend(days)
    try:
        end = date.today()
        start = end - timedelta(days=days)
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        daily = [
            {"date": p["TimePeriod"]["Start"], "usd": _amount(p["Total"])} for p in resp["ResultsByTime"]
        ]
        result = _with_biggest_jump(daily)
        if result["biggest_jump"]:
            try:
                result.update(_jump_attribution(result["biggest_jump"]))
            except Exception:  # noqa: BLE001
                pass  # the trend is still useful without the per-service split
        return result
    except Exception as exc:  # noqa: BLE001
        return friendly_error(exc)


def _jump_attribution(jump: dict) -> dict:
    """Per-service cost on the day before the jump vs the jump day: one DAILY Cost Explorer call."""
    jump_day = date.fromisoformat(jump["date"])
    resp = _ce().get_cost_and_usage(
        TimePeriod={"Start": (jump_day - timedelta(days=1)).isoformat(), "End": (jump_day + timedelta(days=1)).isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    by_day: dict[str, dict[str, float]] = {}
    for p in resp["ResultsByTime"]:
        by_day[p["TimePeriod"]["Start"]] = {g["Keys"][0]: _amount(g["Metrics"]) for g in p["Groups"]}
    breakdown = jump_breakdown(by_day.get((jump_day - timedelta(days=1)).isoformat(), {}), by_day.get(jump["date"], {}))
    return {"jump_breakdown": breakdown, "summary": jump_summary(jump, breakdown)}


def _with_biggest_jump(daily: list[dict]) -> dict:
    jump = None
    for prev, cur in zip(daily, daily[1:]):
        delta = round(cur["usd"] - prev["usd"], 2)
        if jump is None or delta > jump["increase_usd"]:
            jump = {"date": cur["date"], "from_usd": prev["usd"], "to_usd": cur["usd"], "increase_usd": delta}
    return {"daily": daily, "biggest_jump": jump if jump and jump["increase_usd"] > 0 else None}


@tool
@cached
def compare_with_last_month() -> dict:
    """Compare month-to-date spend per service against the same days of last month (the "gap to last race").

    Use this for "am I spending more than last month?" or to find which service is
    responsible for an increase.
    """
    if is_demo():
        return demo_data.compare_with_last_month()
    try:
        today = date.today()
        this_start = today.replace(day=1)
        if today == this_start:
            return {"error": "too_early", "fix": "It's the 1st of the month; there's no month-to-date data yet."}
        last_end = this_start
        last_start = (this_start - timedelta(days=1)).replace(day=1)
        span = (today - this_start).days
        last_cmp_end = min(last_start + timedelta(days=span), last_end)

        def by_service(start: date, end: date) -> dict[str, float]:
            resp = _ce().get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            out: dict[str, float] = {}
            for p in resp["ResultsByTime"]:
                for g in p["Groups"]:
                    out[g["Keys"][0]] = out.get(g["Keys"][0], 0.0) + _amount(g["Metrics"])
            return out

        now, before = by_service(this_start, today), by_service(last_start, last_cmp_end)
        rows = []
        for svc in set(now) | set(before):
            a, b = round(now.get(svc, 0.0), 2), round(before.get(svc, 0.0), 2)
            if a >= 0.01 or b >= 0.01:
                rows.append({"service": svc, "this_month_usd": a, "last_month_usd": b, "change_usd": round(a - b, 2)})
        rows.sort(key=lambda r: abs(r["change_usd"]), reverse=True)
        return {
            "compared_days": span,
            "this_month_total_usd": round(sum(r["this_month_usd"] for r in rows), 2),
            "last_month_total_usd": round(sum(r["last_month_usd"] for r in rows), 2),
            "by_service": rows[:10],
        }
    except Exception as exc:  # noqa: BLE001
        return friendly_error(exc)


@tool
@cached
def scan_for_debris(region: str = "") -> dict:
    """Scan one region for idle resources that cost money while doing nothing (the "debris on track").

    Finds unattached EBS volumes, unassociated Elastic IPs, NAT gateways and
    load balancers. Monthly costs are rough on-demand estimates, not billed amounts.

    Args:
        region: AWS region to scan, e.g. "us-east-1". Empty means the default region.
    """
    if is_demo():
        return demo_data.scan_for_debris(region or "us-east-1")
    try:
        session = boto3.session.Session()
        region = region or session.region_name or "us-east-1"
        ec2 = session.client("ec2", region_name=region)
        elb = session.client("elbv2", region_name=region)
        items = []

        for v in ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]:
            items.append({
                "type": "Unattached EBS volume",
                "id": v["VolumeId"],
                "detail": f'{v["Size"]} GiB {v["VolumeType"]}',
                "est_monthly_usd": round(v["Size"] * 0.08, 2),
            })
        for a in ec2.describe_addresses()["Addresses"]:
            if "AssociationId" not in a:
                items.append({
                    "type": "Unassociated Elastic IP",
                    "id": a.get("AllocationId", a.get("PublicIp")),
                    "detail": a.get("PublicIp", ""),
                    "est_monthly_usd": 3.60,
                })
        for n in ec2.describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available"]}])["NatGateways"]:
            items.append({
                "type": "NAT gateway",
                "id": n["NatGatewayId"],
                "detail": f'in {n["VpcId"]} (plus data processing charges)',
                "est_monthly_usd": 32.85,
            })
        for lb in elb.describe_load_balancers()["LoadBalancers"]:
            items.append({
                "type": f'{lb["Type"].title()} load balancer',
                "id": lb["LoadBalancerName"],
                "detail": "check it still serves traffic",
                "est_monthly_usd": 16.43,
            })
        return {
            "region": region,
            "items": items,
            "est_monthly_total_usd": round(sum(i["est_monthly_usd"] for i in items), 2),
            "note": "Estimates use us-east-1 on-demand prices. NAT gateways and load balancers may be in use; confirm before deleting.",
        }
    except Exception as exc:  # noqa: BLE001
        return friendly_error(exc)


ALL_TOOLS = [cost_by_service, daily_spend_trend, compare_with_last_month, scan_for_debris]


# --------------------------------------------------------------------------- #
# Radio check: runs before the first message so setup problems show up
# as a friendly status, not a stack trace.
# --------------------------------------------------------------------------- #


def radio_check() -> tuple[bool, str]:
    if is_demo():
        return True, "Radio check: loud and clear. Running on demo telemetry."
    try:
        ident = boto3.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        return False, "No radio. " + friendly_error(exc)["fix"]
    probe = cost_by_service(days=1)
    if isinstance(probe, dict) and "error" in probe:
        return False, f"Connected as account {ident['Account']}, but: {probe['fix']}"
    return True, f"Radio check: loud and clear. Connected to account {ident['Account']}."

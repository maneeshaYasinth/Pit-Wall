"""Demo telemetry: a believable student account that left an EKS lab running.

The story baked into this data: a few quiet weeks of small serverless spend,
then a weekend Kubernetes experiment spun up an EKS control plane, a NAT
gateway and some nodes, and nobody tore it down. That's the "why did my bill
jump?" moment Pit Wall is built for.
"""

from __future__ import annotations

from datetime import date, timedelta

SPIKE_DAYS_AGO = 6  # the day the EKS lab went up


def _baseline(day_index: int) -> float:
    # ~$0.40/day of S3, Lambda, CloudFront, Route 53 with a little wobble
    return round(0.38 + (day_index % 3) * 0.03, 2)


def _lab(days_ago: int) -> float:
    # EKS control plane ($0.10/h) + NAT gateway ($0.045/h + data) + 2x t3.small
    return 2.40 + 1.18 + 1.00 if days_ago <= SPIKE_DAYS_AGO else 0.0


def daily_spend_trend(days: int) -> dict:
    today = date.today()
    daily = []
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        daily.append({"date": d.isoformat(), "usd": round(_baseline(i) + _lab(i), 2)})
    jump = None
    for prev, cur in zip(daily, daily[1:]):
        delta = round(cur["usd"] - prev["usd"], 2)
        if jump is None or delta > jump["increase_usd"]:
            jump = {"date": cur["date"], "from_usd": prev["usd"], "to_usd": cur["usd"], "increase_usd": delta}
    return {"daily": daily, "biggest_jump": jump if jump and jump["increase_usd"] > 0 else None, "demo": True}


def cost_by_service(days: int) -> dict:
    lab_days = min(days, SPIKE_DAYS_AGO)
    quiet = days
    services = [
        {"service": "Amazon Elastic Container Service for Kubernetes", "usd": round(2.40 * lab_days, 2)},
        {"service": "EC2 - Other", "usd": round(1.18 * lab_days, 2)},  # NAT gateway hides here
        {"service": "Amazon Elastic Compute Cloud - Compute", "usd": round(1.00 * lab_days, 2)},
        {"service": "Amazon Simple Storage Service", "usd": round(0.14 * quiet, 2)},
        {"service": "Amazon CloudFront", "usd": round(0.09 * quiet, 2)},
        {"service": "Amazon Route 53", "usd": round(0.50 + 0.01 * quiet, 2)},
        {"service": "AWS Lambda", "usd": round(0.05 * quiet, 2)},
        {"service": "Amazon CloudWatch", "usd": round(0.06 * quiet, 2)},
    ]
    services = sorted((s for s in services if s["usd"] >= 0.01), key=lambda s: s["usd"], reverse=True)
    end = date.today()
    return {
        "period": f"{(end - timedelta(days=days)).isoformat()} to {end.isoformat()}",
        "total_usd": round(sum(s["usd"] for s in services), 2),
        "services": services,
        "note": "DEMO DATA. 'EC2 - Other' includes NAT gateway hours and data processing.",
        "demo": True,
    }


def compare_with_last_month() -> dict:
    span = max(date.today().day - 1, 1)
    lab_days = min(span, SPIKE_DAYS_AGO)
    rows = [
        {"service": "Amazon Elastic Container Service for Kubernetes", "this_month_usd": round(2.40 * lab_days, 2), "last_month_usd": 0.0},
        {"service": "EC2 - Other", "this_month_usd": round(1.18 * lab_days, 2), "last_month_usd": 0.0},
        {"service": "Amazon Elastic Compute Cloud - Compute", "this_month_usd": round(1.00 * lab_days, 2), "last_month_usd": 0.0},
        {"service": "Amazon Simple Storage Service", "this_month_usd": round(0.14 * span, 2), "last_month_usd": round(0.13 * span, 2)},
        {"service": "Amazon CloudFront", "this_month_usd": round(0.09 * span, 2), "last_month_usd": round(0.10 * span, 2)},
        {"service": "AWS Lambda", "this_month_usd": round(0.05 * span, 2), "last_month_usd": round(0.05 * span, 2)},
    ]
    for r in rows:
        r["change_usd"] = round(r["this_month_usd"] - r["last_month_usd"], 2)
    rows.sort(key=lambda r: abs(r["change_usd"]), reverse=True)
    return {
        "compared_days": span,
        "this_month_total_usd": round(sum(r["this_month_usd"] for r in rows), 2),
        "last_month_total_usd": round(sum(r["last_month_usd"] for r in rows), 2),
        "by_service": rows,
        "demo": True,
    }


def scan_for_debris(region: str) -> dict:
    items = [
        {"type": "NAT gateway", "id": "nat-0a1b2c3d4e5f6a7b8", "detail": "in vpc-0lab (eks-lab VPC, plus data processing charges)", "est_monthly_usd": 32.85},
        {"type": "Unattached EBS volume", "id": "vol-0f9e8d7c6b5a4f3e2", "detail": "20 GiB gp3", "est_monthly_usd": 1.60},
        {"type": "Unassociated Elastic IP", "id": "eipalloc-0123abcd4567ef890", "detail": "54.210.12.34", "est_monthly_usd": 3.60},
    ]
    return {
        "region": region,
        "items": items,
        "est_monthly_total_usd": round(sum(i["est_monthly_usd"] for i in items), 2),
        "note": "DEMO DATA. Estimates use us-east-1 on-demand prices.",
        "demo": True,
    }

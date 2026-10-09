"""Who caused the jump: per-service change between two days, shared by demo and real mode."""

from __future__ import annotations

# Short labels for the one-line summary (each one still names the bill line or its alias).
SHORT_NAMES = {
    "Amazon Elastic Container Service for Kubernetes": "EKS",
    "EC2 - Other": "EC2 - Other (NAT gateways, EBS volumes and data transfer)",
    "Amazon Elastic Compute Cloud - Compute": "EC2 instances",
    "Amazon Simple Storage Service": "S3",
    "Amazon CloudFront": "CloudFront",
    "Amazon Route 53": "Route 53",
    "AWS Lambda": "Lambda",
    "Amazon CloudWatch": "CloudWatch",
}


def jump_breakdown(before: dict[str, float], after: dict[str, float]) -> list[dict]:
    """Per-service change from the day before the jump to the jump day, biggest rise first."""
    rows = []
    for svc in set(before) | set(after):
        b, a = round(before.get(svc, 0.0), 2), round(after.get(svc, 0.0), 2)
        change = round(a - b, 2)
        if abs(change) >= 0.01:
            rows.append({"service": svc, "day_before_usd": b, "jump_day_usd": a, "change_usd": change})
    rows.sort(key=lambda r: r["change_usd"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def jump_summary(jump: dict, breakdown: list[dict]) -> str:
    verb = "rose" if jump["increase_usd"] >= 0 else "fell"
    parts = ", ".join(
        f"{SHORT_NAMES.get(r['service'], r['service'])} {'+' if r['change_usd'] >= 0 else '-'}${abs(r['change_usd']):.2f}"
        for r in breakdown
    )
    return f"Daily spend {verb} ${abs(jump['increase_usd']):.2f} on {jump['date']}: {parts}."

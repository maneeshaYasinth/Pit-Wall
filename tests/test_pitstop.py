"""Pit stop guards, routing and fallback. No model needed: runs on demo telemetry."""

import json
import re

import pytest

from pitwall import pitstop


@pytest.fixture(autouse=True)
def demo(monkeypatch, tmp_path):
    monkeypatch.setenv("PITWALL_DEMO", "1")
    monkeypatch.setattr(pitstop, "GUARD_LOG", tmp_path / "guard_log.jsonl")


def cost_telemetry():
    return pitstop.annotate(pitstop.fetch([("cost_by_service", {"days": 30})]))


def tools_for(question):
    return [name for name, _ in pitstop.pick_tools(question)]


def test_number_guard_passes_real_demo_figures():
    telemetry = pitstop.fetch([("cost_by_service", {"days": 30}), ("scan_for_debris", {})])
    text = (
        "EKS is your biggest cost at $14.40, then EC2 - Other at $7.08. "
        "Total $38.48, call it $38.5. The NAT gateway is about $32.85 a month, $33 rounded. "
        "Idle stuff totals $38.05 a month."
    )
    assert pitstop.bad_numbers(text, telemetry) == []


def test_number_guard_allows_daily_times_30():
    telemetry = pitstop.fetch([("daily_spend_trend", {"days": 14})])
    jump = telemetry["daily_spend_trend"]["biggest_jump"]
    assert pitstop.bad_numbers(f"At this pace that's about ${jump['to_usd'] * 30:.0f} a month.", telemetry) == []


def test_number_guard_fails_invented_220():
    telemetry = pitstop.fetch([("cost_by_service", {"days": 30})])
    text = "Your EC2 - Other is burning fuel in the garage: about $220 a month."
    assert pitstop.bad_numbers(text, telemetry) == [220.0]


def test_action_guard():
    assert pitstop.claims_action("Copy, turning off the NAT gateway in vpc-0lab.")
    assert pitstop.claims_action("I've deleted the volume for you.")
    assert pitstop.claims_action("I'll terminate it now.")
    assert not pitstop.claims_action("Box, box: confirm it's unused, then delete it in the console.")


def test_action_guard_only_flags_first_person():
    assert not pitstop.claims_action("Consider turning off the NAT gateway if you no longer need it.")
    assert not pitstop.claims_action("Shutting down idle resources saves money.")
    assert pitstop.claims_action("Copy turning off the NAT gateway.")
    assert pitstop.claims_action("I\u2019m shutting down the cluster now.")  # curly apostrophe
    assert pitstop.claims_action("Let me stopping... I stopped the instance.")


def test_annotate_adds_rank_and_biggest():
    t = pitstop.annotate(pitstop.fetch([("cost_by_service", {"days": 30}), ("scan_for_debris", {}),
                                        ("compare_with_last_month", {})]))
    services = t["cost_by_service"]["services"]
    assert [s["rank"] for s in services] == list(range(1, len(services) + 1))
    assert t["cost_by_service"]["biggest"] == {"service": "Amazon Elastic Container Service for Kubernetes", "usd": 14.4}
    assert t["cost_by_service"]["plain_names"]["EC2 - Other"] == "NAT gateways, EBS volumes and data transfer"
    assert t["scan_for_debris"]["biggest"]["type"] == "NAT gateway"
    assert t["scan_for_debris"]["biggest"]["est_monthly_usd"] == 32.85
    rows = t["compare_with_last_month"]["by_service"]
    assert rows[0]["rank"] == 1 and abs(rows[0]["change_usd"]) == max(abs(r["change_usd"]) for r in rows)
    assert "rank" not in pitstop.fetch([("cost_by_service", {"days": 30})])["cost_by_service"]["services"][0]


def test_rank_does_not_feed_number_guard():
    assert pitstop.bad_numbers("That's $5 and $8.", cost_telemetry()) == [5.0, 8.0]  # ranks 1-8 exist, these $ do not


def test_ranking_guard_fails_wrong_biggest():
    errors = pitstop.ranking_errors("Your AWS money is primarily going to EC2 - Other, about $7.08.",
                                    cost_telemetry())
    assert errors == ["The biggest is EKS (Kubernetes control plane) at $14.40, not EC2 - Other."]
    assert pitstop.ranking_errors("The top cost is your NAT gateways.", cost_telemetry())


@pytest.mark.parametrize("text", [
    "The biggest is EKS at $14.40, then EC2 - Other at $7.08.",
    "EKS (Kubernetes control plane) is your largest cost at $14.40.",
    "Most of it goes to Amazon Elastic Container Service for Kubernetes.",
    "Your biggest worry is the weather.",  # nothing to match: don't fail
    "S3 is $4.20. Top tip: check EKS first.",
    "EKS leads at $14.40. S3, CloudFront and Lambda round out the top five.",
])
def test_ranking_guard_passes(text):
    assert pitstop.ranking_errors(text, cost_telemetry()) == []


def test_ranking_guard_on_debris():
    t = pitstop.annotate(pitstop.fetch([("scan_for_debris", {})]))
    assert pitstop.ranking_errors("The biggest leak is the NAT gateway.", t) == []
    assert pitstop.ranking_errors("The biggest leak is the Elastic IP.", t)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Why did my bill jump this week?", ["daily_spend_trend", "cost_by_service"]),
        ("What did I forget to turn off?", ["scan_for_debris"]),
        ("Anything left running?", ["scan_for_debris"]),
        ("Am I spending more than last month?", ["compare_with_last_month"]),
        ("Where is my AWS money going this month?", ["cost_by_service"]),
        ("Show me my cost by service.", ["cost_by_service"]),
    ],
)
def test_keyword_routing(question, expected):
    assert tools_for(question) == expected


def test_fallback_text_for_debris():
    text = pitstop.fallback_text(pitstop.fetch([("scan_for_debris", {})]))
    assert "NAT gateway" in text and "32.85" in text
    assert "Box, box" in text
    assert pitstop.claims_action(text) is False


def test_fallback_passes_every_guard():
    for q in ["Why did my bill jump?", "What did I forget to turn off?", "More than last month?", "Hi"]:
        telemetry = pitstop.annotate(pitstop.fetch(pitstop.pick_tools(q)))
        assert pitstop.check(pitstop.fallback_text(telemetry), telemetry) == []


class FakeAgent:
    def __init__(self, replies):
        self.replies, self.prompts, self.messages = list(replies), [], []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        self.messages += [{"role": "user", "content": [{"text": prompt}]},
                          {"role": "assistant", "content": [{"text": self.replies[0]}]}]
        return self.replies.pop(0)


def test_ask_verified_first_try():
    agent = FakeAgent(["Copy. NAT gateway, about $32.85 a month. Box, box: delete it in the VPC console."])
    r = pitstop.ask(agent, "What did I forget to turn off?")
    assert r == {"text": agent.messages[-1]["content"][0]["text"], "tools": ["scan_for_debris"],
                 "verified": True, "fallback": False}
    assert pitstop.TELEMETRY_HEADER in agent.prompts[0]
    assert len(agent.messages) == 2 and agent.messages[0]["content"][0]["text"] == "What did I forget to turn off?"


def test_ask_retries_then_falls_back():
    agent = FakeAgent(["About $220 a month.", "Copy, turning off the NAT gateway."])
    r = pitstop.ask(agent, "What did I forget to turn off?")
    assert "$220.00" in agent.prompts[1]
    assert r["fallback"] is True and r["verified"] is False
    assert "32.85" in r["text"] and "Box, box" in r["text"]
    log = [json.loads(line) for line in pitstop.GUARD_LOG.read_text().splitlines()]
    assert log[-1]["first_try_passed"] is False and log[-1]["failures"] == ["number", "no_action", "action"]
    assert log[-1]["fallback"] is True and log[-1]["tools"] == ["scan_for_debris"]


def test_ask_retry_fixes_ranking():
    agent = FakeAgent(["Money is mostly going to EC2 - Other at $7.08.",
                       "Copy. The biggest is EKS at $14.40. Box, box: check your clusters in the EKS console."])
    r = pitstop.ask(agent, "Where is my money going?")
    assert "The biggest is EKS (Kubernetes control plane) at $14.40, not EC2 - Other." in agent.prompts[1]
    assert r["verified"] is True and r["fallback"] is False
    log = json.loads(pitstop.GUARD_LOG.read_text().splitlines()[-1])
    assert log["first_try_passed"] is False and log["failures"] == ["ranking", "no_action"]


def test_ask_error_skips_model(monkeypatch):
    monkeypatch.setitem(pitstop.TOOL_FUNCS, "cost_by_service",
                        lambda **_: {"error": "no_credentials", "fix": "Run `aws configure`."})
    agent = FakeAgent([])
    r = pitstop.ask(agent, "Hi")
    assert agent.prompts == [] and "aws configure" in r["text"]


# --------------------------------------------------------------------------- #
# "Why did my bill jump?" attribution and direction
# --------------------------------------------------------------------------- #

FAILING_JUMP_ANSWER = (
    "Your bill jump on October 3rd was due to a spike in usage of EKS (Kubernetes control plane) at $4.55. "
    "The NAT gateways, EBS volumes, and data transfer under EC2 - Other saw a significant drop, "
    "which is normal as traffic likely decreased."
)


def jump_telemetry():
    return pitstop.annotate(pitstop.fetch(pitstop.pick_tools("Why did my bill jump this week?")))


def test_jump_breakdown_demo_values():
    trend = pitstop.fetch([("daily_spend_trend", {"days": 14})])["daily_spend_trend"]
    assert trend["biggest_jump"]["increase_usd"] == 4.55
    rows = [(r["service"], r["change_usd"], r["rank"]) for r in trend["jump_breakdown"]]
    assert rows == [
        ("Amazon Elastic Container Service for Kubernetes", 2.40, 1),
        ("EC2 - Other", 1.18, 2),
        ("Amazon Elastic Compute Cloud - Compute", 1.00, 3),
        ("Amazon Simple Storage Service", -0.03, 4),  # the demo baseline wobble
    ]
    assert round(sum(r[1] for r in rows), 2) == trend["biggest_jump"]["increase_usd"]
    assert trend["summary"].startswith(f"Daily spend rose $4.55 on {trend['biggest_jump']['date']}: EKS +$2.40, EC2 - Other")
    assert "EC2 instances +$1.00" in trend["summary"]


def test_failing_jump_answer_fails_direction_and_attribution():
    t = jump_telemetry()
    kinds = [k for k, _ in pitstop.check(FAILING_JUMP_ANSWER, t)]
    assert "direction" in kinds and "attribution" in kinds
    assert pitstop.direction_errors(FAILING_JUMP_ANSWER, t) == ["EC2 - Other went up by $1.18, not down."]
    assert pitstop.attribution_errors(FAILING_JUMP_ANSWER, t)[0].startswith(
        "$4.55 is the total jump across services, not one service: Daily spend rose $4.55")


def test_correct_jump_answer_passes():
    t = jump_telemetry()
    date = t["daily_spend_trend"]["biggest_jump"]["date"]
    text = (
        f"Copy, here's the picture. Your daily spend jumped $4.55 on {date}. "
        "EKS went up $2.40, the NAT gateways under EC2 - Other rose $1.18, and EC2 instances added $1.00. "
        "S3 dropped slightly. "
        "Box, box: if you set up that EKS lab for a weekend, shut down the cluster in the EKS console."
    )
    assert pitstop.check(text, t) == []


def test_direction_guard_catches_reverse():
    t = jump_telemetry()
    assert pitstop.direction_errors("S3 jumped this week.", t) == ["S3 went down by $0.03, not up."]
    assert pitstop.direction_errors("Shut down the EKS cluster, it went up.", t) == []


def test_attribution_allows_total_without_one_service():
    t = jump_telemetry()
    assert pitstop.attribution_errors("Daily spend rose $4.55 on that day.", t) == []
    assert pitstop.attribution_errors("EKS rose $2.40 and EC2 - Other $1.18, $4.55 in all.", t) == []


def test_trend_fallback_uses_summary():
    t = jump_telemetry()
    text = pitstop.fallback_text(t)
    assert t["daily_spend_trend"]["summary"] in text
    assert "Box, box: EKS (Kubernetes control plane) is the biggest riser" in text


# --------------------------------------------------------------------------- #
# Display, tone, Box box and period wording
# --------------------------------------------------------------------------- #

FAILING_SPEND_ANSWER = (
    "Copy, checking the data. You are spending the most on EKS (Kubernetes control plane) at $14.40 per month. "
    "This includes the overhead of managing the Kubernetes cluster. EC2 - Other, which includes NAT gateways, "
    "EBS volumes, and data transfer, is second at $7.08 per month. Good pace, nothing to fix. Keep pushing."
)
PASSING_SPEND_ANSWER = (
    "Copy, checking the data. You are spending the most on EKS (Kubernetes control plane) at $14.40 "
    "over the last 30 days. This includes the overhead of managing the Kubernetes cluster. EC2 - Other, which "
    "includes NAT gateways, EBS volumes, and data transfer, is second at $7.08 over the last 30 days. "
    "Box, box: if the EKS lab is finished, run terraform destroy."
)


def test_md_safe_escapes_every_dollar():
    from pitwall.render import md_safe

    out = md_safe("EKS at $14.40 and EC2 at $7.08")
    assert re.search(r"(?<!\\)\$", out) is None
    assert out == "EKS at \\$14.40 and EC2 at \\$7.08"
    assert md_safe(out) == out  # already escaped stays as is


def test_failing_spend_answer_fails_tone_no_action_period():
    kinds = [k for k, _ in pitstop.check(FAILING_SPEND_ANSWER, cost_telemetry())]
    assert {"tone", "no_action", "period"} <= set(kinds)
    msgs = dict(pitstop.check(FAILING_SPEND_ANSWER, cost_telemetry()))
    assert msgs["tone"].startswith("There IS something to fix: EKS (Kubernetes control plane) at $14.40")
    assert "not per month. Say 'over the last 30 days'." in msgs["period"]


def test_passing_spend_answer():
    assert pitstop.check(PASSING_SPEND_ANSWER, cost_telemetry()) == []


def test_period_guard_skips_debris_monthly_figures():
    t = pitstop.annotate(pitstop.fetch([("scan_for_debris", {})]))
    assert pitstop.period_errors("The NAT gateway costs $32.85 a month.", t) == []
    assert pitstop.check("NAT gateway, about $32.85 a month. Box, box: delete it in the VPC console.", t) == []


def test_all_clear_is_fine_when_nothing_to_act_on():
    quiet = {"cost_by_service": {"period": "2026-09-10 to 2026-10-10", "total_usd": 1.2,
                                 "services": [{"service": "Amazon Simple Storage Service", "usd": 1.2, "rank": 1}]}}
    assert pitstop.needs_action(quiet) is None
    assert pitstop.check("S3 is $1.20 over the last 30 days. Good pace, nothing to fix.", quiet) == []


@pytest.mark.parametrize("question", [
    "Why did my bill jump?", "What did I forget to turn off?", "Am I spending more than last month?",
    "What am I spending the most on?",
])
def test_safe_mode_passes_tone_box_and_period(question):
    t = pitstop.annotate(pitstop.fetch(pitstop.pick_tools(question)))
    text = pitstop.fallback_text(t)
    assert pitstop.tone_errors(text, t) == [] and pitstop.period_errors(text, t) == []
    assert pitstop.check(text, t) == []


@pytest.mark.parametrize("text, ok", [
    ("The next largest is EC2 - Other at $7.08.", True),
    ("EC2 - Other, which includes NAT gateways, is the second-highest at $7.08.", True),
    ("The 2nd biggest is EC2 - Other.", True),
    ("The second largest is S3.", False),
    ("The third biggest is EC2 instances.", True),
])
def test_ranking_guard_handles_ordinals(text, ok):
    errors = pitstop.ranking_errors(text, cost_telemetry())
    assert (errors == []) is ok
    if not ok:
        assert errors == ["Number 2 is NAT gateways, EBS volumes and data transfer at $7.08, not S3."]

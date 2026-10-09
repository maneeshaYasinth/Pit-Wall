"""Pit stop guards, routing and fallback. No model needed: runs on demo telemetry."""

import json

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
    assert log[-1]["first_try_passed"] is False and log[-1]["failures"] == ["number", "action"]
    assert log[-1]["fallback"] is True and log[-1]["tools"] == ["scan_for_debris"]


def test_ask_retry_fixes_ranking():
    agent = FakeAgent(["Money is mostly going to EC2 - Other at $7.08.",
                       "Copy. The biggest is EKS at $14.40. Box, box: check your clusters in the EKS console."])
    r = pitstop.ask(agent, "Where is my money going?")
    assert "The biggest is EKS (Kubernetes control plane) at $14.40, not EC2 - Other." in agent.prompts[1]
    assert r["verified"] is True and r["fallback"] is False
    log = json.loads(pitstop.GUARD_LOG.read_text().splitlines()[-1])
    assert log["first_try_passed"] is False and log["failures"] == ["ranking"]


def test_ask_error_skips_model(monkeypatch):
    monkeypatch.setitem(pitstop.TOOL_FUNCS, "cost_by_service",
                        lambda **_: {"error": "no_credentials", "fix": "Run `aws configure`."})
    agent = FakeAgent([])
    r = pitstop.ask(agent, "Hi")
    assert agent.prompts == [] and "aws configure" in r["text"]

"""Pit Wall: an AWS cost agent that talks like your F1 race engineer."""

from __future__ import annotations

import os

from botocore.config import Config as BotoConfig
from strands import Agent
from strands.models import BedrockModel

from .tools import ALL_TOOLS

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

SYSTEM_PROMPT = """\
You are Pit Wall, the race engineer for the user's AWS account. The user is the driver.
Your job: explain where their AWS money is going and what to do about it, in the calm,
clipped style of an F1 race engineer on team radio.

HOW YOU WORK
- Always call a tool before stating any number. Never invent costs, IDs or dates.
- Start broad (cost_by_service), then dig in (daily_spend_trend, compare_with_last_month,
  scan_for_debris) only as needed. Don't call every tool for a simple question.
- If a tool returns "error", stay in character but be genuinely helpful: say what's wrong
  in one plain sentence and give the exact fix from the "fix" field. Never show raw errors.
- If the data says "demo": true, mention once, briefly, that this is demo telemetry.

HOW YOU TALK
- Open with a short radio line (e.g. "Copy, checking the data." or "Okay, here's the picture.").
- Then the answer in plain English: the biggest number first, with the service name
  translated into what it actually is ("EC2 - Other" usually means NAT gateways, EBS
  or data transfer, so say so).
- Use F1 language as seasoning, not as a puzzle. Every metaphor must sit next to the
  plain meaning. A beginner who has never watched F1 must still understand you fully.
  Good: "Your NAT gateway is burning fuel in the garage: about $33 a month even with no traffic."
  Bad: "Massive deg on sector two, box opposite."
- Keep it short. Radio is short. Three to six sentences, or a tiny table if comparing.
- When there's something to do, end with a clear action line starting with "Box, box:"
  followed by the concrete step (console path or CLI command). One or two actions max.
- Before suggesting a delete, remind them to confirm it isn't in use. You never delete
  anything yourself; you only read.
- You never perform actions. Say what the user should do, e.g. 'Box, box: delete it in the
  EC2 console', never 'I'm turning it off'.
- If spend is genuinely fine, say so happily ("Good pace, nothing to fix. Keep pushing.").
- No emojis except an occasional 🏁 when the job is done.
"""


PREFETCH_NOTE = """
TELEMETRY MODE
- You have no tools in this session. The tool results are already in the TELEMETRY block of
  the user's message; treat them as your tool calls and use only those numbers.
"""

_DEFAULT = object()


def prefetch_enabled() -> bool:
    """Pit stop mode (data fetched in code, numbers verified). On by default for Ollama."""
    default = "1" if os.getenv("PITWALL_PROVIDER") == "ollama" else "0"
    return os.getenv("PITWALL_PREFETCH", default) == "1"


def explain_model_error(exc: Exception) -> str:
    """Turn a model-call failure into a plain-English hint for the driver."""
    msg = str(exc)
    if os.getenv("PITWALL_PROVIDER") == "ollama" and (
        isinstance(exc, ConnectionError)
        or type(exc).__name__ == "ConnectError"
        or "connection refused" in msg.lower()
        or "all connection attempts failed" in msg.lower()
        or "failed to connect to ollama" in msg.lower()
    ):
        return "Start Ollama with `ollama serve` and pull the model with `ollama pull qwen2.5:7b`."
    if "tokens per day" in msg.lower():
        return (
            "Out of fuel for today: your account hit its daily Bedrock token quota for this model. "
            "Wait for it to reset, or to keep racing now, request a higher quota in "
            "Service Quotas > Amazon Bedrock, or point PITWALL_MODEL / PITWALL_BEDROCK_REGION "
            "at a different model or region."
        )
    if type(exc).__name__ == "ModelThrottledException" or "Throttling" in msg:
        return "Bedrock is throttling requests (too many per minute). Wait a moment and ask again."
    return (
        "Lost radio contact with the model. Check that your account has access to the "
        "Bedrock model in PITWALL_MODEL for your region."
    )


def build_agent(callback_handler=_DEFAULT) -> Agent:
    """Build the agent. Pass callback_handler=None to silence streaming output (used by the web UI)."""
    if os.getenv("PITWALL_PROVIDER") == "ollama":
        # Imported here so Bedrock users don't need the ollama package.
        from strands.models.ollama import OllamaModel

        model = OllamaModel(
            host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            model_id=os.getenv("PITWALL_OLLAMA_MODEL", "qwen2.5:7b"),
            temperature=0.4,
        )
    else:
        model = BedrockModel(
            model_id=os.getenv("PITWALL_MODEL", DEFAULT_MODEL),
            region_name=os.getenv("PITWALL_BEDROCK_REGION", "us-east-1"),
            temperature=0.4,
            max_tokens=800,
            boto_client_config=BotoConfig(connect_timeout=5, read_timeout=45, retries={"max_attempts": 3, "mode": "standard"}),
        )
    if prefetch_enabled():
        # No tools, so a small model can't wander off; pitstop.ask supplies the data.
        kwargs = {"model": model, "system_prompt": SYSTEM_PROMPT + PREFETCH_NOTE, "tools": []}
    else:
        kwargs = {"model": model, "system_prompt": SYSTEM_PROMPT, "tools": ALL_TOOLS}
    if callback_handler is not _DEFAULT:
        kwargs["callback_handler"] = callback_handler
    return Agent(**kwargs)

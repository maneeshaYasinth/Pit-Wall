"""Radio check: confirm credentials and one tiny Bedrock call before racing."""

from __future__ import annotations

import os
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from pitwall.agent import DEFAULT_MODEL

FIXES = {
    "ThrottlingException": (
        "Fix: switch model, e.g. PITWALL_MODEL=us.amazon.nova-lite-v1:0, "
        "or request a quota increase in Service Quotas > Amazon Bedrock."
    ),
    "AccessDeniedException": (
        "Fix: enable access to this model in the Bedrock console (Model access) "
        "and attach iam-policy.json to your IAM user or role."
    ),
    "ValidationException": "Fix: wrong model ID for this region. Check PITWALL_MODEL and PITWALL_BEDROCK_REGION.",
    "ResourceNotFoundException": "Fix: wrong model ID for this region. Check PITWALL_MODEL and PITWALL_BEDROCK_REGION.",
}


def check_ollama() -> int:
    import ollama

    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    model_id = os.getenv("PITWALL_OLLAMA_MODEL", "qwen2.5:7b")
    print("Provider: ollama")
    print(f"Host:     {host}")
    print(f"Model:    {model_id}")

    start = time.monotonic()
    try:
        resp = ollama.Client(host=host).chat(
            model=model_id,
            messages=[{"role": "user", "content": "Reply with just: radio check ok"}],
            options={"num_predict": 20},
        )
    except ConnectionError:
        print("❌ Can't reach Ollama.")
        print(f"Fix: start Ollama with `ollama serve` and pull the model with `ollama pull {model_id}`.")
        return 1
    except ollama.ResponseError as exc:
        print(f"❌ Ollama error ({exc.status_code}): {exc.error}")
        if exc.status_code == 404:
            print(f"Fix: pull the model with `ollama pull {model_id}`.")
        return 1

    elapsed = time.monotonic() - start
    print(f"✅ Model replied in {elapsed:.1f}s: {resp.message.content.strip()}")
    return 0


def main() -> int:
    if os.getenv("PITWALL_PROVIDER") == "ollama":
        return check_ollama()

    model_id = os.getenv("PITWALL_MODEL", DEFAULT_MODEL)
    region = os.getenv("PITWALL_BEDROCK_REGION", "us-east-1")
    print(f"Model:  {model_id}")
    print(f"Region: {region}")

    try:
        ident = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"Caller: {ident['Arn']}")
    except (NoCredentialsError, PartialCredentialsError):
        print("❌ No AWS credentials found. Run `aws configure` or set AWS_PROFILE / AWS_ACCESS_KEY_ID.")
        return 1
    except ClientError as exc:
        err = exc.response.get("Error", {})
        print(f"❌ Credentials check failed: {err.get('Code')}: {err.get('Message')}")
        return 1

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 1}),
    )
    start = time.monotonic()
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with just: radio check ok"}]}],
            inferenceConfig={"maxTokens": 20},
        )
    except ClientError as exc:
        err = exc.response.get("Error", {})
        code = err.get("Code", "Unknown")
        print(f"❌ {code}: {err.get('Message')}")
        if code in FIXES:
            print(FIXES[code])
        return 1
    except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
        print(f"❌ Timed out talking to Bedrock ({type(exc).__name__}).")
        print("Fix: check your network, or try another region via PITWALL_BEDROCK_REGION.")
        return 1

    elapsed = time.monotonic() - start
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"]).strip()
    print(f"✅ Model replied in {elapsed:.1f}s: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

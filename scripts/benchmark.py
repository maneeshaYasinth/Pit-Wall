"""Benchmark pit stop mode: ask each question N times and summarise how the guards did.

    python scripts/benchmark.py                                  # qwen2.5:3b on Ollama, demo data, 3 runs
    python scripts/benchmark.py --model qwen2.5:7b --runs 5
    python scripts/benchmark.py --provider bedrock --model us.amazon.nova-lite-v1:0
    python scripts/benchmark.py -q "Why did my bill jump this week?" --out results.jsonl

Each run uses a fresh agent, so answers don't lean on earlier ones. Uses demo data unless
--real is given (real mode makes Cost Explorer calls, cached for 15 minutes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_QUESTIONS = [
    "What am I spending the most on?",
    "Why did my bill jump this week?",
    "What did I forget to turn off?",
    "Am I spending more than last month?",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["ollama", "bedrock"], default="ollama")
    p.add_argument("--model", default=None, help="model ID (default: qwen2.5:3b for Ollama, DEFAULT_MODEL for Bedrock)")
    p.add_argument("--runs", type=int, default=3, help="times to ask each question")
    p.add_argument("-q", "--question", action="append", help="question to ask (repeatable); default: 4 standard ones")
    p.add_argument("--real", action="store_true", help="use your real AWS account instead of demo data")
    p.add_argument("--out", type=Path, help="also save every guard record to this JSONL file")
    p.add_argument("--quiet", action="store_true", help="only print the summary table")
    return p.parse_args()


def configure(args: argparse.Namespace) -> str:
    """Set the env vars build_agent() reads. Must run before importing pitwall."""
    os.environ["PITWALL_PREFETCH"] = "1"
    os.environ["PITWALL_DEMO"] = "0" if args.real else "1"
    if args.provider == "ollama":
        os.environ["PITWALL_PROVIDER"] = "ollama"
        os.environ["PITWALL_OLLAMA_MODEL"] = args.model or os.getenv("PITWALL_OLLAMA_MODEL", "qwen2.5:3b")
        return os.environ["PITWALL_OLLAMA_MODEL"]
    os.environ.pop("PITWALL_PROVIDER", None)
    if args.model:
        os.environ["PITWALL_MODEL"] = args.model
    from pitwall.agent import DEFAULT_MODEL

    return os.getenv("PITWALL_MODEL", DEFAULT_MODEL)


def pct(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n / total:.0f}%)" if total else "-"


def summarise(records: list[dict], questions: list[str]) -> str:
    header = ["Question", "First try", "Verified", "Safe mode", "Errors", "Failure kinds", "Avg s"]
    rows = []
    for q in [*questions, None]:
        recs = [r for r in records if q is None or r["question"] == q]
        if not recs:
            continue
        kinds = Counter(k for r in recs for k in r["failures"])
        rows.append([
            "ALL" if q is None else q,
            pct(sum(r["first_try_passed"] for r in recs), len(recs)),
            pct(sum(r["verified"] for r in recs), len(recs)),
            pct(sum(r["fallback"] for r in recs), len(recs)),
            str(sum(1 for r in recs if r.get("error"))),
            ", ".join(f"{k}×{n}" for k, n in kinds.most_common()) or "-",
            f"{sum(r['secs'] for r in recs) / len(recs):.1f}",
        ])
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    line = lambda cells: "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(header), sep, *(line(r) for r in rows[:-1]), sep, line(rows[-1])])


def main() -> int:
    args = parse_args()
    model = configure(args)
    questions = args.question or DEFAULT_QUESTIONS

    from pitwall import pitstop
    from pitwall.agent import build_agent, explain_model_error

    log = Path(tempfile.mkstemp(suffix=".jsonl", prefix="pitwall-bench-")[1])
    pitstop.GUARD_LOG = log  # keep benchmark runs out of guard_log.jsonl
    print(f"Benchmark: {args.provider} / {model} / {'real' if args.real else 'demo'} data / "
          f"{len(questions)} questions × {args.runs} runs\n")

    records: list[dict] = []
    for q in questions:
        for run in range(1, args.runs + 1):
            lines_before = len(log.read_text().splitlines())
            try:
                result = pitstop.ask(build_agent(callback_handler=None), q)
                rec = json.loads(log.read_text().splitlines()[lines_before])
                rec["verified"] = result["verified"]
                rec["text"] = result["text"]
            except Exception as exc:  # noqa: BLE001
                rec = {"question": q, "first_try_passed": False, "failures": [], "fallback": False,
                       "verified": False, "secs": 0.0, "error": f"{type(exc).__name__}: {exc}"}
                if not args.quiet:
                    print(f"  ! {explain_model_error(exc)}")
            records.append(rec)
            if not args.quiet:
                status = "ERROR" if rec.get("error") else (
                    "verified" if rec["verified"] else "safe mode" if rec["fallback"] else "unverified")
                print(f"[{q}] run {run}: first_try={rec['first_try_passed']} {status} "
                      f"failures={rec['failures'] or '-'} {rec['secs']}s")

    print("\n" + summarise(records, questions))
    if args.out:
        with args.out.open("a") as f:
            for r in records:
                f.write(json.dumps({**r, "model": model, "provider": args.provider}) + "\n")
        print(f"\nSaved {len(records)} records to {args.out}")
    log.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

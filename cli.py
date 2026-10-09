"""Terminal version of Pit Wall.

    python cli.py              # real account
    PITWALL_DEMO=1 python cli.py   # demo telemetry
"""

from pitwall import pitstop
from pitwall.agent import build_agent, explain_model_error, prefetch_enabled
from pitwall.tools import is_demo, radio_check

BANNER = r"""
 ____  _ _    __        __    _ _
|  _ \(_) |_  \ \      / /_ _| | |
| |_) | | __|  \ \ /\ / / _` | | |
|  __/| | |_    \ V  V / (_| | | |
|_|   |_|\__|    \_/\_/ \__,_|_|_|
  your AWS bill, explained by your race engineer
"""

SUGGESTIONS = [
    "Why did my bill jump this week?",
    "What am I spending the most on?",
    "Anything left running that I forgot about?",
]


def main() -> None:
    print(BANNER)
    ok, msg = radio_check()
    print(("📻 " if ok else "⚠️  ") + msg)
    if not ok:
        print("Tip: try demo mode with  PITWALL_DEMO=1 python cli.py")
        return
    if is_demo():
        print("(demo mode: no AWS charges, no real account needed for data)")
    print("\nTry asking:")
    for s in SUGGESTIONS:
        print(f"  • {s}")
    print("Type 'exit' to end the session.\n")

    prefetch = prefetch_enabled()
    agent = build_agent(callback_handler=None) if prefetch else build_agent()
    while True:
        try:
            q = input("🏎️  You: ").strip()
        except (EOFError, KeyboardInterrupt):
            q = "exit"
        if q.lower() in {"exit", "quit", "q"}:
            print("\n📻 Pit Wall: Copy. That's the chequered flag. Good session. 🏁")
            break
        if not q:
            continue
        print("📻 Pit Wall: ", end="", flush=True)
        try:
            if prefetch:
                r = pitstop.ask(agent, q)
                tag = "✅ checked against your data" if r["verified"] else "🛟 safe mode" if r["fallback"] else ""
                print(r["text"] + (f"\n[{tag}]" if tag else ""), end="")
            else:
                agent(q)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{explain_model_error(exc)}\n({type(exc).__name__}: {exc})")
        print("\n")


if __name__ == "__main__":
    main()

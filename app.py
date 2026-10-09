"""Web UI for Pit Wall.  Run:  streamlit run app.py
Demo mode:  PITWALL_DEMO=1 streamlit run app.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from pitwall import pitstop
from pitwall.agent import build_agent, explain_model_error, prefetch_enabled
from pitwall.render import md_safe
from pitwall.tools import is_demo, radio_check

FEEDBACK_FILE = Path("feedback.jsonl")
SUGGESTIONS = [
    "Why did my bill jump this week?",
    "What am I spending the most on?",
    "Anything left running that I forgot about?",
    "Am I spending more than last month?",
]
TOOL_LABELS = {
    "cost_by_service": "📊 Standings",
    "daily_spend_trend": "⏱️ Lap times",
    "compare_with_last_month": "📈 Gap to last race",
    "scan_for_debris": "🧹 Debris scan",
}

st.set_page_config(page_title="Pit Wall", page_icon="🏁", layout="centered")
st.markdown(
    """
    <style>
      .stApp { background: #0f1115; color: #e9e9ec; }
      h1 { font-family: 'Titillium Web', sans-serif; letter-spacing: 1px; }
      .radio { border-left: 4px solid #e10600; padding: 8px 12px; background: #1a1d24;
               border-radius: 4px; margin-bottom: 12px; font-size: 0.95rem; }
      .radio.bad { border-color: #ffb300; }
      .chip { display:inline-block; background:#232733; border:1px solid #333a4a; color:#c9cbd3;
              border-radius: 999px; padding: 2px 10px; margin: 0 6px 4px 0; font-size: 0.75rem; }
    </style>
    <link href="https://fonts.googleapis.com/css2?family=Titillium+Web:wght@600;700&display=swap" rel="stylesheet">
    """,
    unsafe_allow_html=True,
)

st.title("🏁 Pit Wall")
st.caption("Your AWS bill, explained by your race engineer. Read-only. It never changes anything in your account.")

# ---- Radio check (runs once per session) ----------------------------------
if "radio" not in st.session_state:
    with st.spinner("Radio check…"):
        st.session_state.radio = radio_check()
ok, msg = st.session_state.radio
st.markdown(f'<div class="radio {"" if ok else "bad"}">📻 {md_safe(msg)}</div>', unsafe_allow_html=True)
if not ok:
    st.info("You can still try Pit Wall with sample data: restart with `PITWALL_DEMO=1 streamlit run app.py`.")
    st.stop()

if "agent" not in st.session_state:
    st.session_state.agent = build_agent(callback_handler=None)
    st.session_state.history = []  # list of dicts: role, text, tools, secs
    st.session_state.session_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def log_feedback(idx: int, rating: str) -> None:
    turn = st.session_state.history[idx]
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": st.session_state.session_id,
        "rating": rating,
        "question": st.session_state.history[idx - 1]["text"] if idx > 0 else "",
        "secs": turn.get("secs"),
        "verified": turn.get("verified"),
        "fallback": turn.get("fallback"),
        "demo": is_demo(),
    }
    with FEEDBACK_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    turn["rated"] = rating


def ask(question: str) -> None:
    agent = st.session_state.agent
    before = len(agent.messages)
    st.session_state.history.append({"role": "user", "text": question})
    t0 = time.time()
    if prefetch_enabled():
        try:
            r = pitstop.ask(agent, question)
            text, tools, verified, fallback = r["text"], r["tools"], r["verified"], r["fallback"]
        except Exception as exc:  # noqa: BLE001
            text = f"{explain_model_error(exc)}\n\n`{type(exc).__name__}: {exc}`"
            tools, verified, fallback = [], False, False
        st.session_state.history.append(
            {"role": "assistant", "text": text, "tools": tools, "secs": round(time.time() - t0, 1),
             "verified": verified, "fallback": fallback}
        )
        return
    try:
        result = agent(question)
        text = str(result).strip()
    except Exception as exc:  # noqa: BLE001
        text = f"{explain_model_error(exc)}\n\n`{type(exc).__name__}: {exc}`"
    tools = []
    for m in agent.messages[before:]:
        for block in m.get("content", []):
            name = block.get("toolUse", {}).get("name") if isinstance(block, dict) else None
            if name and name not in tools:
                tools.append(name)
    st.session_state.history.append(
        {"role": "assistant", "text": text, "tools": tools, "secs": round(time.time() - t0, 1)}
    )


# ---- Conversation ------------------------------------------------------------
if not st.session_state.history:
    with st.chat_message("assistant", avatar="📻"):
        st.markdown(
            "Copy, I'm on the pit wall. Ask me anything about your AWS spend: "
            "what's costing money, why it jumped, or what you left running. "
            "I'll translate it into plain English."
        )
    cols = st.columns(2)
    for i, s in enumerate(SUGGESTIONS):
        if cols[i % 2].button(s, use_container_width=True):
            with st.spinner("Checking the telemetry…"):
                ask(s)
            st.rerun()

for i, turn in enumerate(st.session_state.history):
    with st.chat_message(turn["role"], avatar="📻" if turn["role"] == "assistant" else "🏎️"):
        st.markdown(md_safe(turn["text"]))
        if turn["role"] == "assistant":
            chips = "".join(f'<span class="chip">{TOOL_LABELS.get(t, t)}</span>' for t in turn.get("tools", []))
            if turn.get("verified"):
                chips += ('<span class="chip" title="Every dollar figure, the top item and up/down claims '
                          'were checked against the telemetry.">✅ checked against your data</span>')
            elif turn.get("fallback"):
                chips += '<span class="chip">🛟 safe mode</span>'
            chips += f'<span class="chip">⏲️ {turn.get("secs")}s</span>'
            st.markdown(chips, unsafe_allow_html=True)
            if "rated" in turn:
                st.caption("Thanks for the feedback! 🏁" if turn["rated"] == "up" else "Noted. We'll fix the setup.")
            else:
                c1, c2, _ = st.columns([1, 1, 8])
                if c1.button("👍", key=f"up{i}"):
                    log_feedback(i, "up")
                    st.rerun()
                if c2.button("👎", key=f"down{i}"):
                    log_feedback(i, "down")
                    st.rerun()

if q := st.chat_input("Ask your race engineer…"):
    with st.spinner("Checking the telemetry…"):
        ask(q)
    st.rerun()

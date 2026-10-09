# 🏁 Pit Wall

**Your AWS bill, explained by your race engineer.**

Pit Wall is a small AI agent that answers questions like *"why did my bill jump?"* or
*"what did I forget to turn off?"* in the calm, clipped voice of an F1 race engineer,
always in plain English, and always ending with one concrete action ("Box, box: …").

It is **read-only**. It never creates, changes or deletes anything in your account.

Built with [Strands Agents](https://strandsagents.com), Amazon Bedrock, AWS Cost Explorer and Streamlit.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Try it with sample data first (no Cost Explorer calls)
PITWALL_DEMO=1 streamlit run app.py      # web UI
PITWALL_DEMO=1 python cli.py             # terminal
```

Demo mode still calls Bedrock for the model, so you need AWS credentials with
Bedrock access either way. Only the *cost data* is simulated.

### Against your real account

1. Enable Cost Explorer in the Billing console (new accounts can take ~24h to get data).
2. Attach `iam-policy.json` to your IAM user or role. If you use an IAM user, the root
   user must turn on *IAM user and role access to billing information* under Account.
3. Make sure your account has access to the model in `PITWALL_MODEL` in Bedrock.
4. `streamlit run app.py`

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `PITWALL_DEMO` | `0` | `1` uses built-in sample data |
| `PITWALL_MODEL` | Claude Haiku 4.5 inference profile | Any Bedrock model ID with tool use |
| `PITWALL_BEDROCK_REGION` | `us-east-1` | Region for Bedrock calls |

## Troubleshooting

Run `python diagnose.py` first. It checks your credentials and makes one tiny Bedrock call
with your `PITWALL_MODEL` / `PITWALL_BEDROCK_REGION`, then prints the exact error and fix.

* **"Too many tokens per day" / throttling on a new account**: new AWS accounts get a low
  daily token quota for some models. Switch models, e.g.
  `PITWALL_MODEL=us.amazon.nova-lite-v1:0` (or `us.amazon.nova-pro-v1:0`), or request a
  higher quota in Service Quotas > Amazon Bedrock.

## Run without Bedrock (Ollama)

If Bedrock is throttled (or you just want to run locally), point Pit Wall at a local
[Ollama](https://ollama.com) model instead. Bedrock stays the default.

```bash
pip install "strands-agents[ollama]"
ollama serve                      # in another terminal
ollama pull qwen2.5:7b

PITWALL_PROVIDER=ollama python diagnose.py
PITWALL_PROVIDER=ollama PITWALL_DEMO=1 streamlit run app.py
```

| Variable | Default | What it does |
|---|---|---|
| `PITWALL_PROVIDER` | (Bedrock) | `ollama` uses a local Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is running |
| `PITWALL_OLLAMA_MODEL` | `qwen2.5:7b` | Any Ollama model with tool calling |

Cost Explorer and EC2 calls still go to AWS, so you still need AWS credentials for real data.

## How it works

```
You ──► Streamlit / CLI ──► Strands Agent ──► Amazon Bedrock (model)
                                 │
                                 ├─ cost_by_service         ─┐
                                 ├─ daily_spend_trend        ├─► Cost Explorer (us-east-1)
                                 ├─ compare_with_last_month ─┘
                                 └─ scan_for_debris          ──► EC2 / ELB describe calls
```

* **Tools return plain numbers**, so the model never invents a cost. The system prompt
  forbids stating a figure without calling a tool first.
* **Radio check** runs before the first message and turns setup problems (no credentials,
  missing billing permissions, no Cost Explorer data yet) into a one-line fix instead of a
  stack trace.
* **Results are cached for 15 minutes**: Cost Explorer charges $0.01 per API request and
  its data only refreshes about daily.
* **👍 / 👎 on every answer** is logged to `feedback.jsonl` with response time, so you can
  measure whether people actually like it.

## Cost

A typical question costs a fraction of a cent in Bedrock tokens plus up to a few cents in
Cost Explorer requests. Set an AWS Budget alarm anyway.

## License

MIT

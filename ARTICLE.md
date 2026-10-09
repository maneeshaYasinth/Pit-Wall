# Pit Wall: an AWS cost agent that talks like an F1 race engineer, and won't lie about your bill

*#agents #challenge*

> **Before publishing:** fill in the [square brackets], add the screenshots, delete this note.

## The moment that started it

Every student on AWS knows this one. You spin up something ambitious for a weekend, an EKS
cluster, a NAT gateway, a couple of nodes. It works, you celebrate, you close the laptop.
A week later the Billing page shows a number that wasn't there before, next to service
names like **"EC2 - Other"** that tell you nothing.

[One or two sentences of your own, e.g. building Aether on EKS, or Cloud Club members
asking you about surprise bills.]

The answer is all in Cost Explorer, but reading it feels like homework. So I built an agent
that reads it for you and explains it the way a race engineer talks to a driver: calm,
short, and always ending with what to do next.

## What Pit Wall does, and who it's for

You ask Pit Wall plain questions:

- *"Why did my bill jump this week?"*
- *"What am I spending the most on?"*
- *"Anything left running that I forgot about?"*
- *"Am I spending more than last month?"*

It answers in plain English, translates AWS jargon ("EC2 - Other" usually means NAT gateways,
EBS volumes and data transfer), and ends with one action:

> **Box, box:** if the EKS lab is finished, run `terraform destroy`, after confirming nothing still uses it.

It's for students, hobbyists and anyone learning AWS on a personal account: people without a
FinOps team who just want to understand their own bill. It is **read-only**: it can tell you
what to delete, but it can't delete anything.

## How I built it

**Stack:** Strands Agents SDK (Python) · Amazon Bedrock · AWS Cost Explorer API · EC2 and ELB
describe APIs · IAM (least-privilege, read-only policy) · Streamlit · Ollama as a local fallback

Pit Wall has four tools, each a plain Python function with Strands' `@tool` decorator:

| Tool | F1 name | What it does |
|---|---|---|
| `cost_by_service` | 📊 Standings | Spend per service, ranked |
| `daily_spend_trend` | ⏱️ Lap times | Daily spend, the biggest jump, and which services caused it |
| `compare_with_last_month` | 📈 Gap to last race | Month-to-date vs the same days last month |
| `scan_for_debris` | 🧹 Debris scan | Unattached EBS volumes, idle Elastic IPs, NAT gateways, load balancers |

Some details that matter in practice:

- **Cost Explorer isn't free** ($0.01 per request) and only refreshes about daily, so every
  tool result is cached for 15 minutes.
- **A radio check runs before the first message.** Missing credentials, missing billing
  permissions or a brand-new account with no Cost Explorer data each become a one-line fix
  instead of a stack trace.
- **Demo mode** (`PITWALL_DEMO=1`) ships with a built-in story, a quiet serverless account and
  then a forgotten weekend EKS lab, so anyone can try it without opening up their billing.

### The plot twist: my new account got throttled

The design was simple: Claude on Bedrock calling the tools itself. On my first real
question, the app spun for **173 seconds** and then died with
`ThrottlingException: Too many tokens per day`. My new account had a tiny daily Bedrock
token quota, and every model I tried hit the same wall.

The first fix was the error message itself. Instead of a raw exception, Pit Wall now says:

> *Radio's jammed: Bedrock is rate-limiting this model on your account. Box, box: switch
> models, or request a quota increase in Service Quotas.*

The second fix was making Pit Wall work on a **3B-parameter model (qwen2.5:3b) running
locally on my 7 GB student laptop** through Ollama. That's where the real engineering
happened.

## The delightful detail: a race engineer you can trust

The personality is the fun part. Every F1 metaphor has to sit next to its plain meaning, so
someone who has never watched a race still understands the answer completely. But a funny
agent that gets your bill wrong isn't delightful, it's dangerous. So the detail I'm proudest
of is that **Pit Wall checks its own answers against your data before you see them.**

My first test on the 3B model was humbling. Asked where my money was going, it called no
tools and invented "$220 a month". Asked to clean up, it replied *"Copy, turning off the NAT
gateway"*, an action it can't even perform. So I rebuilt it in layers:

1. **Code fetches the data, not the model.** Pit Wall picks the tools from the question,
   calls them in Python, and hands the model the results as TELEMETRY. A small model can't
   forget to call a tool if it never has to.
2. **Guards check every answer** before it reaches you:
   - every **dollar figure** must exist in the telemetry
   - the item called "biggest" must actually **rank first**
   - "went up" or "dropped" must match the **direction** in the data
   - a jump total can't be **pinned on one service** when several caused it
   - no **"I deleted it"** claims, since Pit Wall is read-only
   - no **"nothing to fix"** when there is something to fix, and no "per month" for a 30-day figure
3. **One retry with a specific correction**, e.g. *"The biggest is EKS at $14.40, not EC2 - Other."*
4. **Safe mode** 🛟: if the retry still fails, Pit Wall writes the answer itself from a
   template, still in the race-engineer voice. A wrong answer never reaches you as
   "✅ checked against your data".

### How I know it worked

**The guards caught real mistakes.** My first "verified" badge lied: the model used only
real numbers, but blamed the whole $4.55 jump on EKS and said NAT gateway costs had
*dropped* when they'd gone up. Checking the numbers wasn't enough; I had to check what the
numbers were attached to. That's where the direction and attribution guards came from.

**One change took the small model from 0% to 100%.** The 3B model kept signing off with
*"Good pace, nothing to fix"* even after being corrected, because it was copying the example
in my system prompt. So I stopped asking it to judge. The code already knows whether there's
something to fix, so it sends a VERDICT line (`ACTION NEEDED: EKS at $14.40…` or
`ALL CLEAR`) and the model only has to say it well.

| | First-try pass on "What am I spending the most on?" |
|---|---|
| Before VERDICT | 0 / 3 (2 ended in safe mode) |
| After VERDICT | 3 / 3 |

The benchmark across all four questions after the change (qwen2.5:3b, CPU only):

| Question | First try | Verified | Safe mode | Avg time |
|---|---|---|---|---|
| What am I spending the most on? | 3/3 | 3/3 | 0/3 | 11.5s |
| Why did my bill jump this week? | 3/3 | 3/3 | 0/3 | 22.0s |
| What did I forget to turn off? | 3/3 | 3/3 | 0/3 | 17.1s |
| Am I spending more than last month? | 3/3 | 3/3 | 0/3 | 13.5s |
| **All** | **12/12** | **12/12** | **0/12** | **16.0s** |

It's a small sample, but it's 12 for 12, on a model small enough to run on a laptop with
1.6 GB of free RAM.

**People liked it.** [Fill in after testing: "I gave Pit Wall to N members of the AWS Cloud
Club at the University of Kelaniya. X of Y answers got a 👍." Add one quote.]

## Proof it works

[Screenshot 1: radio check and suggested questions on first load]

[Screenshot 2: "Why did my bill jump this week?" with the ✅ checked-against-your-data badge]

[Screenshot 3: a 🛟 safe-mode answer]

[Screenshot 4: the friendly throttling message]

Code: [GitHub link]

## What I learned

- **Small models are good writers and bad judges.** Let code decide *what* is true and
  *whether* to act; let the model decide *how to say it*.
- **A "verified" badge is a promise.** Mine overpromised until the checks covered what the
  numbers meant, not just whether they existed.
- **The error message is part of the product.** A 173-second spinner and a raw exception
  were the worst experience in the app; a one-line, in-character fix turned it into one of
  the best.

## What's next

Run it on Bedrock once my quota grows, with the same guards, since they protect any model.
Then a weekly "post-race debrief" sent via SNS, and checks for things the guards still miss,
like pointing you at the right console for each service.

*Box, box. 🏁*
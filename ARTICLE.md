# Pit Wall: I built an AWS cost agent that talks like an F1 race engineer

*#agents #challenge*

> **Before publishing:** replace everything in [square brackets], add your screenshots/GIF,
> and delete this note. Keep it over 500 words (this draft is ~950).

## The moment that started it

Every student on AWS knows this feeling. You spin up something ambitious for a weekend,
an EKS cluster, a NAT gateway, a couple of nodes, it works, you celebrate, and you close
the laptop. A week later the Billing dashboard shows a number that was not there before,
and a list of services with names like **"EC2 - Other"** that tell you nothing.

[One or two sentences of your own: e.g. building Aether on EKS and fighting Free Tier
node limits, or helping AWS Cloud Club members who got surprise bills.]

The data to answer *"what happened?"* is all in Cost Explorer. The problem is that reading
it feels like homework. So I built an agent that reads it for you and explains it the way
an F1 race engineer talks to a driver: calm, short, and always ending with what to do next.

## What Pit Wall does, and who it's for

**Pit Wall** is a read-only AWS cost agent. You ask it plain questions:

- *"Why did my bill jump this week?"*
- *"What am I spending the most on?"*
- *"Anything left running that I forgot about?"*

It pulls the real numbers, translates service names into what they actually are
("EC2 - Other" is usually NAT gateways, EBS or data transfer), and ends with one concrete
action line: **"Box, box: delete the NAT gateway in your lab VPC once you've confirmed nothing uses it."**

It's for students, hobbyists and anyone learning AWS on a personal account: people who
don't have a FinOps team and don't want to learn Cost Explorer before they can understand
their own bill.

## How I built it

**Stack:** Strands Agents SDK (Python) · Amazon Bedrock (Claude Haiku 4.5) · AWS Cost Explorer API ·
EC2 and ELB describe APIs · Streamlit · IAM (least-privilege, read-only policy)

The agent is a Strands `Agent` with a Bedrock model, a system prompt, and four tools.
Each tool is a normal Python function with the `@tool` decorator; Strands turns the
docstring into the tool description the model sees.

| Tool | F1 name | What it calls |
|---|---|---|
| `cost_by_service` | Standings | `ce:GetCostAndUsage` grouped by service |
| `daily_spend_trend` | Lap times | daily costs, plus the biggest day-over-day jump |
| `compare_with_last_month` | Gap to last race | month-to-date vs the same days last month |
| `scan_for_debris` | Debris on track | unattached EBS, idle Elastic IPs, NAT gateways, load balancers |

Three engineering decisions mattered more than the code:

1. **The model never invents a number.** Tools return plain JSON with figures already
   rounded, and the system prompt forbids stating any cost without calling a tool first.
   The personality is decoration on top of real data, never a replacement for it.
2. **Cost Explorer isn't free.** It charges per API request, and its data only refreshes
   about once a day, so every tool result is cached for 15 minutes. A chatty user doesn't
   turn a cost tool into a cost.
3. **Read-only by design.** The IAM policy only allows `GetCostAndUsage` and `Describe*`
   calls. Pit Wall can suggest a deletion; it can't perform one.

There's also a **demo mode** (`PITWALL_DEMO=1`) with a built-in story: a quiet serverless
account, then a weekend EKS lab that was never torn down. It let me build and test the
experience before my own account had interesting data, and it lets anyone try Pit Wall
without opening up their billing.

## The delightful detail: a race engineer who never leaves you confused

The one thing I designed around is **the voice**, with a rule that makes it work:

> *Every F1 metaphor must sit next to its plain meaning. Someone who has never watched a
> race must still understand the answer completely.*

Without that rule, the first version was fun and useless ("massive deg in sector two").
With it, you get answers like:

> "Okay, here's the picture. Your bill went from about $0.40 a day to $5 a day on
> [date]. The big one is EKS: the control plane alone burns $2.40 a day, even with no apps
> running. 'EC2 - Other' is your NAT gateway, about $33 a month idling in the garage.
> **Box, box:** if the lab's finished, run `terraform destroy` on it."

The same voice carries through the parts that are usually painful:

- **A radio check on first run.** Before you type anything, Pit Wall checks your
  credentials and billing access. Instead of a stack trace you get *"No radio. These
  credentials can't read billing data. Attach the policy in iam-policy.json…"*
- **Friendly failure.** Missing permissions, no Cost Explorer data yet, a network blip:
  each becomes one plain sentence with the exact fix, still in character.
- **Telemetry chips** under each answer show which tools it used (📊 Standings,
  ⏱️ Lap times) and how long it took, so it never feels like a black box.

### How I know it worked

Every answer has a 👍 / 👎 button that logs to a file along with the response time.
[Fill in honestly after testing, e.g.: "I gave Pit Wall to N members of the AWS Cloud Club
at the University of Kelaniya on Saturday. Across X answers, Y% got a thumbs up. The
best signal was qualitative: [quote from a tester]."]

[If a 👎 led to a change, say so: e.g. "Two thumbs-downs were on answers that ran too long,
so I tightened the prompt to three to six sentences."]

## Proof it works

[Screenshot 1: the radio check + suggested questions on first load]

[Screenshot 2 or GIF: asking "Why did my bill jump this week?" and getting the answer with tool chips]

[Link to GitHub repo]

## What I'd add next

Deploying it to Amazon Bedrock AgentCore Runtime so it can run as a shared tool for the
club, a weekly "post-race debrief" sent through SNS, and AWS Budgets integration so it can
warn you before the jump instead of after.

*Box, box. 🏁*

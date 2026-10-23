# Usage and Cost

LexTreeRAG counts the tokens of every model call and turns them into an estimated price in euros.

## Under each answer

A cost line shows the number of model calls, tokens in and out, and the estimated price. Open
**Where the money went** to see each pipeline step:

| Step | Model | Paid |
|---|---|---|
| Sort the question | Claude Haiku 4.5 | every question |
| Pick search words | Claude Haiku 4.5 | every question |
| Index new laws | Claude Haiku 4.5 | once per law (then cached) |
| Choose articles | Claude Haiku 4.5 | every question |
| Write the answer | the model you choose (default Claude Opus 4.6) | every question |
| Check the answer | Claude Haiku 4.5 | every question (and each retry) |
| Gemini (web search) | Gemini | when the Gemini panel is on |

Token counts come from the provider's reply, so they are exact; thinking tokens are part of tokens out.
Euros are list price per million tokens × tokens × the USD→EUR rate. Your provider's bill is the real number.

When a question reuses a law that is already indexed, the answer shows **law cache saved ≈ €…**: what
indexing that law cost (or would cost) the first time.

## 💶 Usage tab

- Spend this session, today, this month and since the last reset
- Saved by the law cache
- Spend by step, by model and per day; the most expensive questions
- This session's calls one by one, with CSV download
- **Budget, prices and reset:** monthly budget (warning at 80%, AI answers stop at 100%; 0 = no limit),
  USD→EUR rate, editable model prices, and a reset button

The counter lives in your browser's local storage. Your API key is never stored.

## Answer model

The sidebar's **Answer written by** picks the model for the final answer. Before you ask, it shows a rough
price per question (laws already indexed); after a few questions it shows your real average.

## Demo mode

**▶️ Demo** replays recorded real answers, including their real token counts, without a key. See
`demo/examples/README.md` to record more.

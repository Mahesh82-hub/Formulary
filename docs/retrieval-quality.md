# Retrieval quality

How Formulary keeps answers grounded, and how that is tested. This page was written after a
real failure, so it starts there.

## The failure

Asked *"the compositions used for paracetamol among a few top companies"*, the assistant asked
an unnecessary question, made twelve tool calls that mostly returned nothing, used up its
research budget before reaching one of the companies, and concluded that *"inactive-ingredient
data are not present"* in FDA sources. That was false: 3,066 of 3,474 acetaminophen labels
list inactive ingredients.

The recorded tool calls showed six causes. Four were retrieval bugs, not model errors:

| Cause | Evidence (live openFDA, September 2026) |
|---|---|
| Label tool used exact matching | `brand_name.exact:"TYLENOL"` matched 0 labels; the phrase matched 111 |
| Label tool could not return composition | Its section list had no ingredient or OTC sections |
| Federated search matched *any* word | "Pfizer paracetamol composition" matched 16 unrelated Pfizer letters |
| Unknown fields look like "no data" | 5 of the model's field guesses do not exist; openFDA still answers "No matches" |
| International vs US names | `generic_name:"PARACETAMOL"` matched 0; `"ACETAMINOPHEN"` matched 3,474 |
| Model behaviour | Unneeded clarification, a false absence claim, generic padding |

## The principle: the model describes, the application executes

A model that writes openFDA query syntax will eventually get a field wrong, and openFDA's
answer to a wrong field is indistinguishable from "no data". So the model is never asked to
write syntax, and any guarantee that matters is enforced in code rather than requested in the
prompt.

| Guarantee | How it is enforced |
|---|---|
| Queries are well-formed | The model sends structured JSON filters (`app/fda/structured.py`). Every field is checked against openFDA's published field catalogue (`app/fda/fields.json`) **before** anything is sent. Unknown fields come back as `status: "invalid_query"` with suggested corrections. |
| Drug names match US data | International names are searched alongside US adopted names everywhere (`app/fda/names.py`). |
| Common questions have one right tool | `get_drug_composition` answers ingredient and excipient questions in one call. It picks the top labelers from the data rather than asking the user, and lists single-ingredient products first. |
| An empty result is explained | Empty results say whether every field exists. A live `_exists_` check also catches fields that openFDA renamed after the catalogue was generated. |
| Answers carry sources | The application collects the official record behind every tool result and appends a **Sources** list (`app/services/citations.py`). The model's cooperation isn't needed; in evaluation it often omitted links even when told to cite. |
| Official sources come first | Web search is offered only after an official tool has run in that turn. In evaluation the model otherwise sometimes went straight to the web. |
| Web citations are real links | Groq's `【n†Lx-Ly】` markers are mapped to the pages the browser opened (`app/llm/web_citations.py`), and a **Web sources** list is appended. |

## Trustworthy sources and figures

A second evaluation round, on physicochemical questions (pKa, solubility, fu,p, BCS class),
found answers that cited real papers but padded their source lists and invented numbers to fill
tables. These safeguards followed.

| Problem seen | Safeguard |
|---|---|
| Sources listed every retrieved record: a pKa question listed protein kinase A ("PKA") papers | Only sources the answer references are listed (by URL, PubMed ID, trial ID, label set ID, or product name). If none are referenced, they are listed as **Sources consulted**, not as citations. |
| The model wrote citation markers in any form: `【pubmed:ID】`, `【openfda】`, tool names | Identifiers a tool returned become links. Identifiers no tool returned are shown as *unverified*. Tool and source-family markers resolve to that tool's record, and anything else is removed (`app/services/citations.py`). |
| A cattle protein-binding range and a pH-solubility table were invented | Every figure is checked against the retrieved text (`app/services/grounding.py`). Figures no source supports are named to the reader as **Unverified figures**. Rounding, unit conversion, bound/unbound complements, and explicit calculations from grounded inputs are accepted. pH values, temperatures, identifiers ("FD&C Red No. 40"), years, and small counts are ignored. A match must sit near the same context words, so a common number such as "45" found somewhere in a long result does not count. |
| A one-document local corpus returned an FDA guidance on mold in cherry jam for four unrelated drug questions | Vector matches must fall within a calibrated cosine distance (0.35; relevant pairs measured 0.13-0.28, unrelated 0.46-0.52). |
| Sources titled "download" and "FDA drug label record 1" | PDF titles come from the document's first line, and label records link to DailyMed. |
| GPT-OSS sometimes ended a researched turn with nothing, or a fragment | The answer is rewritten from the collected evidence in a clean conversation, rather than failing or shipping a fragment. |
| A tool call leaked into an answer as `to=assistant<\|channel\|>...` | Protocol fragments are stripped at the provider boundary (`app/llm/sanitize.py`). |
| Rate limits failed after a 0.25 s retry when Groq asked for 0.9 s | Provider waits are honoured from `Retry-After` or the error message, with a separate retry budget. |
| Up to six reworded federated searches in one turn | `search_all_sources` is capped at three calls per turn; further calls are refused with guidance. |

Every figure check is pinned to lines from real answers in `tests/test_grounding.py`.

## Web search

For GPT-OSS models on Groq, the built-in `browser_search` tool is offered alongside the
application's tools. It is recorded in `tool_executions` as `web_search`, like any other tool.
Answers that used it end with a **Web sources** list noting that official records take
precedence. Configure it with `GROQ_WEB_SEARCH_ENABLED` and `GROQ_WEB_SEARCH_MODELS`.

## Keeping it working

```bash
python -m pytest                          # unit tests, including tests/test_retrieval_quality.py
python -m scripts.eval_chat               # live end-to-end evaluation (costs tokens)
python -m scripts.refresh_openfda_fields  # re-sync the field catalogue with openFDA
```

`scripts/eval_chat.py` asks real questions through the real model and sources. It fails a case
when the answer:

- asks an unnecessary clarifying question
- uses the wrong tool, or goes to the web first
- claims data is absent when it exists
- lacks source links
- pads the answer with generic advice
- runs out of research budget

Run it after any prompt, tool, or model change. Add a case whenever a new failure is found in
use.

## Latest evaluation

Ten live cases on `openai/gpt-oss-120b` (September 2026): **7 passed**. Earlier in the same
round, before the safeguards above, the same set scored 1/10 and then 5/10. The remaining
failures were model behaviour:

- two turns spent their research budget cycling through tools; each was still answered from
  the evidence collected
- one answer quoted well-known literature values that its searches had not retrieved; they were
  correctly marked unverified

## What evaluation showed about the model

Across 17 live cases on `openai/gpt-oss-120b` after these fixes, 16 passed. The model-side
problems that remained were:

- occasional empty responses, reported as failed runs rather than wrong answers
- provider-rejected tool calls, which the orchestrator recovers from
- non-deterministic tool choice between runs of the same question
- omitted citations

That is why every guarantee above is enforced in code.

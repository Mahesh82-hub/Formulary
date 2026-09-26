"""Answer-quality evaluation recorded in LangSmith and scored with RAGAS metrics.

    python -m scripts.eval_langsmith --sync-only          # upload the dataset, run nothing
    python -m scripts.eval_langsmith                      # every case, one experiment
    python -m scripts.eval_langsmith --case metformin_pka --case ceftiofur_fup
    python -m scripts.eval_langsmith --model openai/gpt-oss-20b --judge-model openai/gpt-oss-120b

The same cases as scripts/eval_chat.py, but every run becomes an experiment in the LangSmith
UI, where runs can be compared side by side, scores inspected per answer, and the judge's own
prompts and verdicts read in the trace. Needs LANGSMITH_API_KEY in the repository .env.

How the pieces fit (LangSmith's vocabulary):

* Dataset  - the questions, uploaded once as "examples". Each example has `inputs` (what the
             app receives) and `outputs` (the reference: what a good answer looks like). The
             reference answers start empty and are meant to be written in the LangSmith UI;
             later runs pick them up, and the reference-based metrics start scoring.
* Target   - the system under test: here, one real chat turn through the API, returning the
             answer plus the tool outputs it was built from (the "retrieved contexts").
* Evaluator - a function (inputs, outputs, reference_outputs) -> {"key", "score", "comment"}.
             Each returned key becomes a feedback column in the experiment table.
* Experiment - one pass of the target over the dataset with every evaluator applied.

RAGAS supplies the evaluators' scoring logic. Its LLM metrics use a *judge* model, which
reads the answer and the contexts and returns structured verdicts; the score is arithmetic
over those verdicts. The judge here is a Groq model, called through an OpenAI-compatible
client that LangSmith wraps, so each judge prompt shows up inside the evaluator's trace.

It calls the real LLM provider and real sources, so it costs tokens and is not a unit test.
"""

import argparse
import asyncio
import math
import os
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI
from sqlalchemy import delete

from app.core.config import REPOSITORY_ROOT, get_settings

# LangSmith reads its settings (LANGSMITH_API_KEY, LANGSMITH_PROJECT, ...) from the process
# environment, which pydantic-settings does not populate, so load the .env file explicitly.
load_dotenv(REPOSITORY_ROOT / ".env")

from langsmith import Client  # noqa: E402
from langsmith.evaluation import aevaluate  # noqa: E402
from langsmith.wrappers import wrap_openai  # noqa: E402
from ragas.embeddings.base import BaseRagasEmbedding  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    AnswerRelevancy,
    ContextRecall,
    ContextUtilization,
    FactualCorrectness,
    Faithfulness,
    ResponseGroundedness,
)

from app.api.dependencies.auth import get_current_user  # noqa: E402
from app.db.session import async_session_factory, engine  # noqa: E402
from app.ingestion.embeddings import BGEONNXEmbeddingProvider  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Conversation, User  # noqa: E402
from app.services.grounding import SOURCE_SECTIONS, ungrounded_numbers  # noqa: E402
from scripts.eval_chat import CASES, Case, grade, run_case  # noqa: E402

DATASET = "formulary-chat-eval"
# Tool results are raw openFDA/PubMed JSON and can run to hundreds of kilobytes. The judge
# must read them in one prompt, so each is truncated, and so is their total. A claim whose
# support was cut off will be judged unsupported: raise these if faithfulness looks unfairly
# low on answers built from long results.
CONTEXT_CHARACTERS = 6_000
TOTAL_CONTEXT_CHARACTERS = 40_000
UNVERIFIED_NOTE = "\n\n> **Unverified figures:**"

JSON = dict[str, Any]
# Any, not a precise Callable: LangSmith inspects evaluators' parameter names at runtime.
Evaluator = Any


# --- Dataset ------------------------------------------------------------------------------


def sync_dataset(client: Client, name: str) -> None:
    """Create the dataset and add any case it lacks. Existing examples are left alone, so
    reference answers written in the UI survive every later sync."""
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(
            name, description="Formulary chat questions, from scripts/eval_chat.py CASES."
        )
    present = {
        (example.inputs or {}).get("case")
        for example in client.list_examples(dataset_id=dataset.id)
    }
    missing = [case for case in CASES if case.name not in present]
    if missing:
        client.create_examples(
            dataset_id=dataset.id,
            examples=[
                {
                    "inputs": {"case": case.name, "question": case.question},
                    # "reference" is the gold answer, empty until someone writes one in the UI.
                    "outputs": {
                        "reference": "",
                        "expect_tools_any": list(case.expect_tools_any),
                        "expect_terms": list(case.expect_terms),
                    },
                    "metadata": {"strict_figures": case.strict_figures},
                }
                for case in missing
            ],
        )
    print(f"Dataset {name!r}: {len(present) + len(missing)} examples ({len(missing)} added).")


# --- Target -------------------------------------------------------------------------------


def make_target(http: AsyncClient, provider: str, model: str) -> Callable[[JSON], Awaitable[JSON]]:
    cases = {case.name: case for case in CASES}

    async def formulary_chat(inputs: JSON) -> JSON:
        """One chat turn. LangSmith records the returned dict as the run's outputs."""
        outcome = await run_case(http, cases[inputs["case"]], provider, model)
        return {
            "answer": outcome.answer,
            "tools": outcome.tools,
            "contexts": outcome.contexts,
            "completion_reason": outcome.completion_reason,
            "seconds": round(outcome.seconds, 1),
            # run_case records a failed run (as opposed to a poor answer) as its first failure.
            "run_error": next(
                (failure for failure in outcome.failures if failure.startswith("run failed")),
                None,
            ),
        }

    return formulary_chat


# --- RAGAS evaluators ---------------------------------------------------------------------


class BGEEmbeddings(BaseRagasEmbedding):
    """The app's own local BGE-small model, adapted to the interface RAGAS expects.

    Only AnswerRelevancy needs embeddings: it compares the user's question with questions the
    judge reverse-engineers from the answer, so both sides are embedded the same way (as
    documents, without the retrieval query prefix).
    """

    def __init__(self) -> None:
        super().__init__()
        settings = get_settings()
        self.provider = BGEONNXEmbeddingProvider(
            model_name=settings.embedding_model,
            model_revision=settings.embedding_model_revision,
            dimensions=settings.embedding_dimensions,
            cache_dir=settings.resolved_embedding_cache_dir,
            batch_size=settings.embedding_batch_size,
            segment_tokens=settings.embedding_segment_tokens,
            segment_overlap_tokens=settings.embedding_segment_overlap_tokens,
            query_prefix=settings.embedding_query_prefix,
        )

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        return self.provider.embed_documents([text])[0]

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        return await asyncio.to_thread(self.embed_text, text)


def answer_body(answer: str) -> str:
    """The answer without its sources list and unverified-figures note, which are not claims."""
    return SOURCE_SECTIONS.split(answer, maxsplit=1)[0].split(UNVERIFIED_NOTE, 1)[0].strip()


def judge_contexts(contexts: list[str]) -> list[str]:
    kept: list[str] = []
    remaining = TOTAL_CONTEXT_CHARACTERS
    for context in contexts:
        if remaining <= 0:
            break
        piece = context[: min(CONTEXT_CHARACTERS, remaining)]
        kept.append(piece)
        remaining -= len(piece)
    return kept


def ragas_evaluator(
    key: str,
    metric: Any,
    arguments: Callable[[JSON, JSON, str], JSON | None],
) -> Evaluator:
    """Adapt a RAGAS metric to LangSmith's evaluator signature.

    `arguments` maps (inputs, outputs, reference) to the metric's `ascore` keyword arguments,
    or None when the example cannot be scored (a failed run, or no reference written yet).
    A skipped score is recorded as empty rather than zero, so it does not drag averages down.
    """

    async def evaluator(inputs: JSON, outputs: JSON, reference_outputs: JSON) -> JSON:
        reference = (reference_outputs or {}).get("reference", "").strip()
        if outputs["answer"].startswith("RUN FAILED") or not outputs["contexts"]:
            return {"key": key, "score": None, "comment": "no answer or no retrieved contexts"}
        kwargs = arguments(inputs, outputs, reference)
        if kwargs is None:
            return {"key": key, "score": None, "comment": "needs a reference answer (add in UI)"}
        result = await metric.ascore(**kwargs)
        score = None if math.isnan(result.value) else round(float(result.value), 3)
        return {"key": key, "score": score, "comment": result.reason}

    evaluator.__name__ = key  # the name LangSmith shows for this evaluator's trace
    return evaluator


def ragas_evaluators(judge_model: str) -> list[Evaluator]:
    settings = get_settings()
    if settings.groq_api_key is None:
        raise SystemExit("GROQ_API_KEY is required for the RAGAS judge model")
    # wrap_openai makes every judge call a child run in LangSmith, so the prompts RAGAS sends
    # and the structured verdicts it gets back can be read under each evaluator.
    client = wrap_openai(
        AsyncOpenAI(
            api_key=settings.groq_api_key.get_secret_value(), base_url=settings.groq_base_url
        )
    )
    # llm_factory wraps the client with Instructor, which forces replies into the Pydantic
    # schemas each metric defines (a list of statements, a verdict per statement, ...).
    # RAGAS defaults to 1,024 output tokens, which a reasoning judge such as gpt-oss spends
    # on thinking before it writes the verdicts, so allow far more.
    llm = llm_factory(judge_model, client=client, temperature=0, max_tokens=8_192)

    def question(inputs: JSON) -> str:
        return str(inputs["question"])

    return [
        # Reference-free: judged from the question, the answer and the retrieved contexts.
        ragas_evaluator(
            "faithfulness",
            Faithfulness(llm=llm),
            lambda i, o, r: {
                "user_input": question(i),
                "response": answer_body(o["answer"]),
                "retrieved_contexts": judge_contexts(o["contexts"]),
            },
        ),
        ragas_evaluator(
            "response_groundedness",
            ResponseGroundedness(llm=llm),
            lambda i, o, r: {
                "response": answer_body(o["answer"]),
                "retrieved_contexts": judge_contexts(o["contexts"]),
            },
        ),
        ragas_evaluator(
            "answer_relevancy",
            AnswerRelevancy(llm=llm, embeddings=BGEEmbeddings()),
            lambda i, o, r: {"user_input": question(i), "response": answer_body(o["answer"])},
        ),
        ragas_evaluator(
            "context_utilization",
            ContextUtilization(llm=llm),
            lambda i, o, r: {
                "user_input": question(i),
                "response": answer_body(o["answer"]),
                "retrieved_contexts": judge_contexts(o["contexts"]),
            },
        ),
        # Reference-based: scored only once a reference answer exists for the example.
        ragas_evaluator(
            "context_recall",
            ContextRecall(llm=llm),
            lambda i, o, r: (
                {
                    "user_input": question(i),
                    "retrieved_contexts": judge_contexts(o["contexts"]),
                    "reference": r,
                }
                if r
                else None
            ),
        ),
        ragas_evaluator(
            "factual_correctness",
            # High atomicity splits the answer into one claim per fact. At the default "low",
            # a table collapses into a single compound claim, and one stricter word in it
            # ("fully" where the reference says "almost entirely") scores the whole answer 0.
            FactualCorrectness(llm=llm, mode="f1", atomicity="high"),
            lambda i, o, r: {"response": answer_body(o["answer"]), "reference": r} if r else None,
        ),
    ]


# --- The project's own evaluators ---------------------------------------------------------


async def rule_checks(inputs: JSON, outputs: JSON, reference_outputs: JSON) -> JSON:
    """The deterministic checks from eval_chat.py: right tool, citations, no false absence."""
    case: Case = next(case for case in CASES if case.name == inputs["case"])
    failures = grade(case, outputs["answer"], outputs["tools"], outputs["completion_reason"])
    if outputs.get("run_error"):
        failures.insert(0, outputs["run_error"])
    return {
        "key": "rule_checks",
        "score": 0 if failures else 1,
        "comment": "; ".join(failures) or "all checks passed",
    }


async def numeric_grounding(inputs: JSON, outputs: JSON, reference_outputs: JSON) -> JSON:
    """app.services.grounding on the same answer and contexts the RAGAS judge reads, so the
    two can be compared: a figure no source supports, which an LLM judge may let through."""
    figures = ungrounded_numbers(
        outputs["answer"], outputs["contexts"], question=inputs["question"]
    )
    return {
        "key": "numeric_grounding",
        "score": 0 if figures else 1,
        "comment": f"unsupported figures: {figures}" if figures else "every figure grounded",
    }


# --- Run ----------------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> None:
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise SystemExit("Set LANGSMITH_API_KEY in the repository .env (smith.langchain.com)")
    client = Client()
    sync_dataset(client, args.dataset)
    if args.sync_only:
        return

    async with async_session_factory() as session:
        user = User(email=f"eval-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    examples = [
        example
        for example in client.list_examples(dataset_name=args.dataset)
        if not args.case or (example.inputs or {}).get("case") in args.case
    ]
    evaluators: list[Evaluator] = [
        rule_checks,
        numeric_grounding,
        *ragas_evaluators(args.judge_model),
    ]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as http:
            results = await aevaluate(
                make_target(http, args.provider, args.model),
                data=examples,
                evaluators=evaluators,
                experiment_prefix=args.model.replace("/", "-"),
                description=f"{args.provider}/{args.model}, judged by {args.judge_model}",
                metadata={
                    "provider": args.provider,
                    "model": args.model,
                    "judge_model": args.judge_model,
                },
                # One chat turn at a time: each already fans out to many tools and LLM calls,
                # and parallel turns would hit the provider's rate limits.
                max_concurrency=1,
                client=client,
            )
    finally:
        app.dependency_overrides.clear()
        if args.keep:
            # The runs keep their full provider error in assistant_runs.error.
            print(f"\nKept evaluation conversations for user {user.email} ({user.id}).")
        else:
            async with async_session_factory() as session:
                await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
                await session.execute(delete(User).where(User.id == user.id))
                await session.commit()
        await engine.dispose()
    print(
        f"\nExperiment {results.experiment_name!r} is in LangSmith under dataset {args.dataset!r}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--case", action="append", help="Run only the named case(s)")
    parser.add_argument("--provider", default="groq")
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--judge-model", default="openai/gpt-oss-120b")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--sync-only", action="store_true", help="Upload the dataset and stop")
    parser.add_argument(
        "--keep", action="store_true", help="Keep conversations and runs for inspection"
    )
    asyncio.run(main(parser.parse_args()))

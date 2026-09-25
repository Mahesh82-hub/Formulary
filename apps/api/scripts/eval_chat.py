"""End-to-end answer-quality evaluation against the live model and live sources.

    python -m scripts.eval_chat                  # every case
    python -m scripts.eval_chat --case composition
    python -m scripts.eval_chat --model openai/gpt-oss-120b --provider groq

Unit tests prove each part works in isolation; this proves the assembled system answers real
questions well. Every case encodes a failure seen in practice - an unnecessary clarifying
question, a false claim that FDA has no data, an answer with no citations, the wrong tool - so
a regression in the prompt, a tool, or a model change shows up as a failed check rather than
a user's bad experience. It calls the real LLM provider and real sources, so it costs tokens
and is not part of the default test run.
"""

import argparse
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api.dependencies.auth import get_current_user
from app.db.session import async_session_factory, engine
from app.main import app
from app.models import AssistantRun, Conversation, ToolExecution, User

# Phrases that, when the data does exist, mean the answer wrongly claims it does not.
FALSE_ABSENCE = re.compile(
    r"(no (fda[- ]registered|matching|such) [^.]{0,60}(found|label|product|record)"
    r"|not (available|present|found) in (the )?(fda|public)"
    r"|(fda|openfda) (does not|doesn't) (contain|have|include)"
    r"|cannot supply|could not find any)",
    re.IGNORECASE,
)
GENERIC_PADDING = re.compile(r"contact (the )?(manufacturer|pfizer|company) directly", re.I)
URL = re.compile(r"https?://[^\s)\]]+")


@dataclass
class Case:
    name: str
    question: str
    expect_tools_any: tuple[str, ...]
    expect_terms_min: int
    expect_terms: tuple[str, ...]
    forbid_clarification: bool = True
    forbid_false_absence: bool = True
    # Where figures are the answer, an unsupported one fails the case; elsewhere it is a
    # warning, because the reader already sees it marked as unverified.
    strict_figures: bool = False


CASES = [
    Case(
        name="composition",
        question=(
            "Can you tell me the compositions used for paracetamol among a few top companies "
            "that manufacture it"
        ),
        expect_tools_any=("get_drug_composition",),
        expect_terms=("Kenvue", "Haleon", "Reckitt", "Procter", "Tylenol", "Panadol", "Bayer"),
        expect_terms_min=2,
    ),
    Case(
        name="inactive_ingredients_brand",
        question="What are the inactive ingredients in Tylenol Extra Strength caplets?",
        expect_tools_any=("get_drug_composition", "get_fda_drug_labels"),
        expect_terms=("hypromellose", "starch", "magnesium stearate", "cellulose"),
        expect_terms_min=2,
    ),
    Case(
        name="regulatory_changes",
        question="What has changed for AstraZeneca products recently?",
        expect_tools_any=("search_regulatory_events",),
        expect_terms=("Truqap", "Tagrisso", "indication"),
        expect_terms_min=1,
    ),
    Case(
        name="trials",
        question="Are there Phase 3 trials of tirzepatide for heart failure?",
        expect_tools_any=("search_all_sources",),
        expect_terms=("NCT", "tirzepatide", "heart failure"),
        expect_terms_min=2,
    ),
    Case(
        name="non_us_product",
        question="What are the ingredients of Panadol Advance as sold in the UK?",
        expect_tools_any=("web_search",),
        expect_terms=("paracetamol", "Panadol", "sodium bicarbonate", "starch"),
        expect_terms_min=2,
        # FDA genuinely has no UK-only product, so saying so is correct here.
        forbid_false_absence=False,
    ),
    Case(
        name="metformin_pka",
        strict_figures=True,
        question="What is the ionisation state and pKa of metformin at physiological pH (7.4)?",
        expect_tools_any=("get_fda_drug_labels", "search_all_sources", "openfda_query"),
        expect_terms=("12.4", "protonated", "cation", "positive"),
        expect_terms_min=2,
    ),
    Case(
        name="olaparib_solubility_bcs",
        strict_figures=True,
        question="What is the aqueous solubility and BCS classification of olaparib?",
        expect_tools_any=("get_fda_drug_labels", "search_all_sources", "openfda_query"),
        expect_terms=("BCS", "solubility", "class"),
        expect_terms_min=2,
    ),
    Case(
        name="ceftiofur_fup",
        strict_figures=True,
        question="What is the fraction unbound in plasma (fup) for ceftiofur?",
        expect_tools_any=("search_all_sources", "get_fda_drug_labels", "openfda_query"),
        expect_terms=("46.6", "goat", "unbound", "not reported"),
        expect_terms_min=2,
        # Ceftiofur is veterinary; saying no human FDA label exists is accurate.
        forbid_false_absence=False,
    ),
    Case(
        name="mesalamine_ph_solubility",
        strict_figures=True,
        question="What is the solubility of mesalamine across different physiological pH levels?",
        expect_tools_any=("search_all_sources", "get_fda_drug_labels", "openfda_query"),
        expect_terms=("2.3", "5.69", "0.844", "1.41", "zwitterion"),
        expect_terms_min=2,
        # FDA labels genuinely give no numeric pH-solubility profile.
        forbid_false_absence=False,
    ),
    Case(
        name="adverse_events_inn",
        question="What are the most commonly reported adverse reactions for paracetamol?",
        expect_tools_any=("analyze_fda_adverse_event_reactions",),
        expect_terms=("acetaminophen", "report", "causal"),
        expect_terms_min=2,
    ),
]


@dataclass
class Outcome:
    case: Case
    seconds: float
    answer: str
    tools: list[str]
    completion_reason: str | None
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def grade(case: Case, answer: str, tools: list[str], reason: str | None) -> list[str]:
    failures: list[str] = []
    if case.forbid_clarification and reason == "awaiting_clarification":
        failures.append("asked a clarifying question the request did not need")
    if not any(tool in tools for tool in case.expect_tools_any):
        failures.append(f"did not use any of {case.expect_tools_any}; used {sorted(set(tools))}")
    hits = [term for term in case.expect_terms if term.casefold() in answer.casefold()]
    if len(hits) < case.expect_terms_min:
        failures.append(
            f"expected at least {case.expect_terms_min} of {case.expect_terms}, found {hits}"
        )
    if case.forbid_false_absence and FALSE_ABSENCE.search(answer):
        failures.append(f"claimed data is absent: {FALSE_ABSENCE.search(answer).group(0)!r}")  # type: ignore[union-attr]
    if GENERIC_PADDING.search(answer):
        failures.append("padded the answer with generic 'contact the manufacturer' advice")
    if not URL.search(answer):
        failures.append("answer contains no source links")
    if reason == "research_budget_reached":
        failures.append("exhausted the research budget")
    if tools and tools[0] == "web_search":
        failures.append("went to the web before consulting any official source")
    if "web_search" in tools and "**Web sources**" not in answer:
        failures.append("used web search but the answer has no Web sources list")
    if "【" in answer:
        failures.append("left citation markers unconverted")
    if "unverified: not returned by any source" in answer:
        failures.append("cited an identifier that no tool returned")
    return failures


async def run_case(client: AsyncClient, case: Case, provider: str, model: str) -> Outcome:
    created = await client.post("/api/v1/conversations", json={})
    conversation_id = created.json()["id"]
    started = time.monotonic()
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": case.question, "provider": provider, "model": model},
        timeout=600,
    )
    seconds = time.monotonic() - started
    answer = ""
    for block in response.text.split("\n\n"):
        if block.startswith("event: message.completed"):
            payload = json.loads(block.split("data: ", 1)[1])
            answer = payload.get("content", "")
        if block.startswith("event: run.failed"):
            answer = "RUN FAILED: " + block.split("data: ", 1)[1]
    async with async_session_factory() as session:
        run = await session.scalar(
            select(AssistantRun)
            .where(AssistantRun.conversation_id == conversation_id)
            .order_by(AssistantRun.created_at.desc())
        )
        tools = (
            list(
                await session.scalars(
                    select(ToolExecution.tool_name)
                    .where(ToolExecution.run_id == run.id)
                    .order_by(ToolExecution.started_at)
                )
            )
            if run
            else []
        )
        reason = run.orchestration_state.get("completion_reason") if run else None
        run_error = run.error if run and run.status == "failed" else None
        figures = run.orchestration_state.get("ungrounded_figures") if run else None
    outcome = Outcome(case, seconds, answer, tools, reason)
    outcome.failures = grade(case, answer, tools, reason)
    if figures:
        # The same check readers see: figures no retrieved source supports.
        message = f"stated figures no source supports: {figures}"
        if case.strict_figures:
            outcome.failures.append(message)
        else:
            outcome.warnings.append(message)
    if run_error:
        # A failed run is a different problem from a bad answer; say which.
        outcome.failures.insert(
            0, f"run failed: {run_error.get('code')}: {run_error.get('message')}"
        )
    return outcome


async def main(args: argparse.Namespace) -> int:
    async with async_session_factory() as session:
        user = User(email=f"eval-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    app.dependency_overrides[get_current_user] = lambda: user
    cases = [case for case in CASES if not args.case or case.name in args.case]
    outcomes: list[Outcome] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as client:
            for case in cases:
                outcome = await run_case(client, case, args.provider, args.model)
                outcomes.append(outcome)
                status = "PASS" if not outcome.failures else "FAIL"
                timing = f"{outcome.seconds:.1f}s, reason={outcome.completion_reason}"
                print(f"\n[{status}] {case.name}  ({timing})")
                print(f"  tools: {' -> '.join(outcome.tools) or 'none'}")
                for failure in outcome.failures:
                    print(f"  - {failure}")
                for warning in outcome.warnings:
                    print(f"  ~ warning: {warning}")
                if args.show_answers or outcome.failures:
                    print("  answer:\n    " + outcome.answer[:1800].replace("\n", "\n    "))
    finally:
        app.dependency_overrides.clear()
        if args.keep:
            print(f"\nKept evaluation conversations for user {user.email} ({user.id}).")
        else:
            async with async_session_factory() as session:
                await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
                await session.execute(delete(User).where(User.id == user.id))
                await session.commit()
        await engine.dispose()
    passed = sum(1 for outcome in outcomes if not outcome.failures)
    print(f"\n{passed}/{len(outcomes)} cases passed")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--case", action="append", help="Run only the named case(s)")
    parser.add_argument("--provider", default="groq")
    parser.add_argument("--model", default="openai/gpt-oss-120b")
    parser.add_argument("--show-answers", action="store_true")
    parser.add_argument(
        "--keep", action="store_true", help="Keep conversations and runs for inspection"
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))

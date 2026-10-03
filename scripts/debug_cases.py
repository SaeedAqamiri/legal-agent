#!/usr/bin/env python3
"""Detailed failure logs for weak benchmark cases.

Re-runs selected cases through the agentic pipeline with full logging —
every planner decision, every tool call, the final answer, verification
issues — and writes one markdown file per case next to the gold answer.

Usage:
    python scripts/debug_cases.py --worst 12 \
        --scores out/benchmark_glm53flash/scores.json --out out/debug_cases
    python scripts/debug_cases.py --ids Q008 Q069 ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_benchmark as rb

from legal_agent_core.adapters import OpenAICompatibleGateway
from legal_agent_core.agentic import (
    AgenticConfig,
    LegalResearchTools,
    ToolResearchLoop,
)
from legal_agent_core.agentic.planner import LLMToolPlanner
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    ResearchApplicationService,
)
from legal_agent_core.benchmark import load_golden_cases, seed_corpus
from legal_agent_core.in_memory import (
    InMemoryCanonicalRepository,
    InMemoryResearchGraphRepository,
)
from legal_agent_core.llm import (
    EndpointStyle,
    LLMTask,
    ModelProfile,
    ProviderConfig,
)
from legal_agent_core.navigation import NavigationLoop
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.retrieval import MultiSignalEvidenceRetriever
from legal_agent_core.security import (
    AuthorizationService,
    Principal,
    Role,
)
from legal_agent_core.verification import EvidenceVerifier

APPLICABLE = rb.BENCHMARK_APPLICABLE_TIME
GOLD_PATH = Path("tests/tamin_longdocs_agentic_benchmark_v4/benchmark/gold_answers.jsonl")


def load_gold_rows() -> dict[str, dict]:
    rows = {}
    for line in GOLD_PATH.open(encoding="utf-8"):
        row = json.loads(line)
        rows[row["id"]] = row
    return rows


class DecisionLogger:
    def __init__(self, inner, sink: list) -> None:
        self.inner = inner
        self.sink = sink

    def decide(self, system_prompt: str, user_prompt: str):
        turn = self.inner.decide(system_prompt, user_prompt)
        if turn.contract_error:
            self.sink.append(f"❌ خطای قرارداد: {turn.contract_error} — raw: {turn.raw_text[:160]!r}")
        elif turn.decision is not None:
            d = turn.decision
            if d.action == "tool":
                self.sink.append(
                    f"🤖 تصمیم: ابزار `{d.tool}` با args: "
                    f"`{json.dumps(d.args, ensure_ascii=False)[:220]}`"
                )
            elif d.action == "abstain":
                self.sink.append(f"🤖 تصمیم: امتناع — «{d.abstention_reason[:200]}»")
            else:
                self.sink.append(f"🤖 تصمیم: پاسخ نهایی ({len(d.claims)} ادعا)")
        return turn


def debug_case(
    case_id: str,
    case,
    gold: dict,
    repository,
    service,
    agentic_loop,
    grader,
    prev_scores: dict[str, dict],
    out_dir: Path,
) -> None:
    log: list[str] = []
    if agentic_loop is not None:
        inner = agentic_loop.planner
        agentic_loop.planner = DecisionLogger(inner, log)  # type: ignore[assignment]
    events: list[str] = []

    def progress(event: dict) -> None:
        kind = event.get("type")
        if kind == "tool_call":
            events.append(
                f"🔧 [{event['step']:>2}] `{event['tool']}` → **{event['status']}** "
                f"(برگشتی {event['returned_count']}/{event['total_count']}, "
                f"شواهد {event['evidence_count']}, {event['latency_ms']}ms)"
            )
        elif kind == "tool_error":
            events.append(f"⚠️ [{event['step']:>2}] خطای ابزار `{event['tool']}`: {event['error'][:140]}")
        elif kind == "contract_error":
            events.append(f"⚠️ [{event['step']:>2}] خطای قرارداد: {event['message'][:140]}")
        elif kind == "verification_rejected":
            codes = ", ".join(issue["code"] for issue in event.get("issues", []))
            events.append(f"🚫 [{event['step']:>2}] پاسخ رد شد: {codes}")
        elif kind == "verification_accepted":
            events.append(f"✅ [{event['step']:>2}] راستی‌آزمایی پذیرفت")
        elif kind == "abstained":
            events.append(f"🛑 [{event['step']:>2}] امتناع صریح")

    started = time.monotonic()
    record = service.run(
        Principal("debug", "org-a", frozenset({Role.RESEARCHER})),
        rb.ResearchCommand(
            "org-a",
            case.question,
            case.applicable_time,
            strategy=rb.ResearchStrategy.AGENTIC,
        ),
    )
    wall = time.monotonic() - started
    outcome = record.outcome
    grade = grader.grade(case, outcome, record.answer)
    prev = prev_scores.get(case_id)
    verdict = (
        f"score={grade.score:.2f} (prev {prev['score']:.2f}, Δ{grade.score - prev['score']:+.2f}) "
        f"recall={grade.document_recall:.2f} pages={grade.page_hit_rate:.2f} "
        f"abst={int(grade.abstained)} published={int(record.answer is not None)} steps={outcome.iterations}"
    )
    if agentic_loop is not None:
        agentic_loop.planner = inner  # type: ignore[assignment]

    md: list[str] = []
    md.append(f"# {case.case_id} — {case.category} (امتیاز بنچمارک: ؟)")
    md.append("")
    md.append("## سوال")
    md.append(case.question)
    md.append("")
    md.append("## جواب درست مورد انتظار (Gold)")
    md.append(f"- **answerability:** `{case.answerability}`")
    if case.expected_abstention:
        md.append(f"- **امتناع مورد انتظار:** {case.expected_abstention}")
    md.append("- **اسناد/صفحات طلایی:** " + ", ".join(
        f"{doc} ص{sorted(pages)}" for doc, pages in case.evidence_pages
    ))
    md.append(f"- **پاسخ طلایی:** {gold.get('gold_answer', '')}")
    if gold.get("scoring_points"):
        md.append("- **نکات امتیازآور:** " + "؛ ".join(gold["scoring_points"]))
    md.append("")
    md.append("## کاری که ایجنت کرد")
    md.append(f"- **نتیجه:** {verdict}")
    md.append(f"- **issues:** {outcome.episode.identified_issues}")
    md.append("")
    md.append("### تصمیم‌ها و فراخوانی‌ها (به‌ترتیب)")
    md.extend(f"- {line}" for line in log)
    md.extend(f"- {line}" for line in events)
    md.append("")
    touched: dict[str, set[int]] = {}
    for entry in outcome.ledger.entries:
        touched.setdefault(entry.evidence.document_id, set()).add(entry.evidence.page)
    md.append("### اسناد لمس‌شده (در ledger، هر وضعیتی)")
    for doc, pages in touched.items():
        code = next((k for k, v in _doc_map.items() if v.source_document_id == doc), doc)
        md.append(f"- {code}: صفحات {sorted(pages)[:12]}")
    md.append("")
    md.append("### پاسخ نهایی ایجنت")
    md.append("```text")
    md.append(outcome.draft.text[:1_500])
    md.append("```")
    if outcome.draft.claims:
        md.append("**ادعاها:**")
        for claim in outcome.draft.claims:
            md.append(f"- ({claim.importance.value}) {claim.text} ← {', '.join(claim.evidence_ids) or 'بدون شاهد!'}")
    md.append("")
    md.append("### راستی‌آزمایی")
    md.append("accepted: " + str(outcome.verification.accepted))
    for issue in outcome.verification.issues[:8]:
        md.append(f"- {issue.severity.value}: {issue.code.value} — {issue.message[:160]}")
    if record.answer is not None:
        md.append("")
        md.append("### صفحات استنادشده در پاسخ منتشرشده")
        for item in record.answer.citations:
            md.append(f"- {item.marker} {item.instrument_title} — {item.provision_label} — ص{item.citation.page}")

    out_file = out_dir / f"{case.case_id}.md"
    out_file.write_text("\n".join(md), encoding="utf-8")
    print(f"VERDICT {case_id}: {verdict} | touched={grade.touched_documents} | wrote {out_file.name} ({wall:.0f}s)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="tests/tamin_longdocs_agentic_benchmark_v4")
    parser.add_argument("--scores", default="out/benchmark_glm53flash/scores.json")
    parser.add_argument("--out", default="out/debug_cases")
    parser.add_argument("--ids", nargs="*", default=None)
    parser.add_argument("--worst", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=16)
    args = parser.parse_args()

    root = Path(args.root)
    repository = InMemoryCanonicalRepository()
    global _doc_map
    _doc_map = seed_corpus(repository, root / "documents_md", rb._manifest(root))
    cases = {case.case_id: case for case in load_golden_cases(root / "benchmark", APPLICABLE)}
    gold_rows = load_gold_rows()

    if args.ids:
        selected = list(args.ids)
    else:
        scores = json.loads(Path(args.scores).read_text(encoding="utf-8"))
        ranked = sorted(scores["cases"], key=lambda c: c["score"])
        if args.worst:
            ranked = ranked[: args.worst]
        else:
            ranked = [c for c in ranked if c["score"] < 0.6]
        selected = [c["case_id"] for c in ranked]

    verifier = EvidenceVerifier(repository)
    navigation = NavigationLoop(
        MultiSignalEvidenceRetriever(repository, InMemoryResearchGraphRepository()),
        rb.ExtractiveAnswerComposer(),
        verifier,
    )
    base_url = os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
    api_key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    model = os.environ.get("LEGAL_AGENT_LLM_MODEL", "glm-5.3-flash")
    provider = ProviderConfig("debug", base_url, EndpointStyle.CHAT_COMPLETIONS, api_key=api_key, timeout_seconds=180)
    profile = ModelProfile(
        "debug-planner", 1, LLMTask.NAVIGATION, "debug", model, "debug-planner", 1,
        max_output_tokens=2048, temperature=0, extra_body={"thinking": {"type": "disabled"}},
    )
    planner = LLMToolPlanner(OpenAICompatibleGateway(), provider, profile, agent_version="debug/1")
    agentic = ToolResearchLoop(
        LegalResearchTools(
            repository,
            term_expander=rb.LLMTermExpander(
                OpenAICompatibleGateway(), provider, profile
            ),
        ),
        planner,
        verifier,
        AgenticConfig(max_steps=args.max_steps, wall_seconds=600, max_total_tokens=400_000),
    )
    grader = rb.DeterministicGrader(_doc_map)
    prev_scores = {}
    if Path(args.scores).is_file():
        for row in json.loads(Path(args.scores).read_text(encoding="utf-8"))["cases"]:
            prev_scores[row["case_id"]] = row
    service = ResearchApplicationService(
        navigation,
        rb.CitationPipeline(repository),
        InMemoryResearchRecordRepository(),
        AuthorizationService(),
        MetricsRegistry(),
        id_factory=lambda: f"debug-{next(rb._counter)}",
        agentic=agentic,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for case_id in selected:
        case = cases.get(case_id)
        if case is None:
            print(f"skip unknown {case_id}")
            continue
        print(f"=== {case_id} ===", flush=True)
        try:
            debug_case(
                case_id, case, gold_rows.get(case_id, {}), repository, service,
                agentic, grader, prev_scores, out_dir,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {case_id} crashed: {type(exc).__name__}: {exc}", flush=True)
    return 0



_doc_map: dict = {}

if __name__ == "__main__":
    raise SystemExit(main())

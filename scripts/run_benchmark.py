#!/usr/bin/env python3
"""Deterministic benchmark runner for the Tamin long-document legal corpus.

Seeds the frozen markdown corpus into an in-memory canonical repository, runs
the research pipeline over the golden questions and grades every case with the
deterministic grader (document recall, page-level citations, honest
abstention). No LLM is required for the default extractive strategy.

Usage:
    python scripts/run_benchmark.py \
        --root tests/tamin_longdocs_agentic_benchmark_v4 \
        --out-dir out/benchmark

    # with the tool-driven agentic loop (needs a live LLM gateway):
    LEGAL_AGENT_LLM_BASE_URL=... LEGAL_AGENT_LLM_API_KEY=... \
    python scripts/run_benchmark.py --strategy agentic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legal_agent_core.agentic import AgenticConfig, LegalResearchTools, ToolResearchLoop
from legal_agent_core.agentic.planner import LLMToolPlanner
from legal_agent_core.adapters import OpenAICompatibleGateway
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    ResearchApplicationService,
    ResearchCommand,
    ResearchStrategy,
)
from legal_agent_core.answering import CitationPipeline
from legal_agent_core.benchmark import (
    DeterministicGrader,
    ExtractiveAnswerComposer,
    GoldenCase,
    load_golden_cases,
    seed_corpus,
)
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
from legal_agent_core.security import AuthorizationService, Principal, Role
from legal_agent_core.verification import EvidenceVerifier

BENCHMARK_APPLICABLE_TIME = date(2026, 9, 21)

# The runner owns a prompt/profile pair for the agentic planner so no
# application-level wiring is needed.


def _manifest(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8")).get("documents", {})


def _probe(root: Path) -> list[str]:
    """Contract probes; missing inputs are reported as SKIP-PENDING."""
    missing = []
    if not (root / "benchmark" / "questions.jsonl").is_file():
        missing.append("benchmark/questions.jsonl")
    if not (root / "benchmark" / "gold_answers.jsonl").is_file():
        missing.append("benchmark/gold_answers.jsonl")
    if not (root / "documents_md").is_dir():
        missing.append("documents_md/")
    return missing


def _build_service(repository, strategy: str, budgets: dict) -> ResearchApplicationService:
    citations = CitationPipeline(repository)
    verifier = EvidenceVerifier(repository)
    navigation = NavigationLoop(
        MultiSignalEvidenceRetriever(repository, InMemoryResearchGraphRepository()),
        ExtractiveAnswerComposer(),
        verifier,
    )
    agentic = (
        _build_agentic_loop(repository, verifier, budgets)
        if strategy == ResearchStrategy.AGENTIC.value
        else None
    )
    return ResearchApplicationService(
        navigation,
        citations,
        InMemoryResearchRecordRepository(),
        AuthorizationService(),
        MetricsRegistry(),
        id_factory=lambda: f"bench-{next(_counter)}",
        agentic=agentic,
    )


_counter = iter(range(1, 10_000))


def _build_agentic_loop(repository, verifier, budgets: dict):
    base_url = os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
    api_key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    model = os.environ.get("LEGAL_AGENT_LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        raise SystemExit(
            "agentic strategy requires LEGAL_AGENT_LLM_BASE_URL and "
            "LEGAL_AGENT_LLM_API_KEY in the environment"
        )
    provider = ProviderConfig(
        "benchmark",
        base_url,
        EndpointStyle.CHAT_COMPLETIONS,
        api_key=api_key,
        timeout_seconds=budgets["timeout_seconds"],
    )
    profile = ModelProfile(
        profile_id="benchmark-planner",
        version=1,
        task=LLMTask.NAVIGATION,
        provider_id="benchmark",
        model=model,
        prompt_id="benchmark-planner",
        prompt_version=1,
        max_output_tokens=budgets["max_output_tokens"],
        temperature=0,
        extra_body=_extra_body(budgets),
    )
    gateway = OpenAICompatibleGateway()
    # The loop builds the full numbered system prompt (rules, honesty
    # contract, skills, tool catalog) itself — do not substitute a thinner
    # one here; the thin variant measurably degrades planner behavior.
    planner = LLMToolPlanner(gateway, provider, profile, agent_version="benchmark/1")
    return ToolResearchLoop(
        LegalResearchTools(repository),
        planner,
        verifier,
        AgenticConfig(
            max_steps=budgets["max_steps"],
            wall_seconds=budgets["wall_seconds"],
            max_total_tokens=budgets["max_total_tokens"],
        ),
    )


DEFAULT_BUDGETS = {
    "max_steps": 16,
    "wall_seconds": 300.0,
    "max_total_tokens": 150_000,
    "max_output_tokens": 2048,
    "timeout_seconds": 90.0,
}


def _extra_body(budgets: dict) -> dict:
    """Provider-specific body knobs.

    ``--disable-thinking`` (or LEGAL_AGENT_LLM_DISABLE_THINKING=1) sets
    ``{"thinking": {"type": "disabled"}}`` which z.ai/GLM reasoning models
    honor; without it a "flash" model can burn the whole output budget on
    reasoning and return empty content.
    """
    raw = os.environ.get("LEGAL_AGENT_LLM_EXTRA_BODY")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"LEGAL_AGENT_LLM_EXTRA_BODY is not valid JSON: {exc}")
    if budgets.get("disable_thinking"):
        return {"thinking": {"type": "disabled"}}
    return {}


def run(
    root: Path,
    out_dir: Path,
    strategy: str,
    categories: list[str] | None,
    limit: int | None,
    budgets: dict | None = None,
    shard: tuple[int, int] | None = None,
) -> dict:
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    missing = _probe(root)
    if missing:
        return {"status": "SKIP-PENDING", "missing": missing}

    repository = InMemoryCanonicalRepository()
    document_map = seed_corpus(repository, root / "documents_md", _manifest(root))
    cases = load_golden_cases(root / "benchmark", BENCHMARK_APPLICABLE_TIME)
    if categories:
        cases = tuple(case for case in cases if case.category in categories)
    if limit:
        cases = cases[:limit]
    if shard is not None:
        index, count = shard
        if not 1 <= index <= count:
            raise SystemExit("--shard must be N/M with 1 <= N <= M")
        cases = tuple(case for position, case in enumerate(cases) if position % count == index - 1)

    service = _build_service(repository, strategy, budgets)
    grader = DeterministicGrader(document_map)
    principal = Principal("benchmark", "org-a", frozenset({Role.RESEARCHER}))

    grades = []
    started = time.monotonic()
    for index, case in enumerate(cases, start=1):
        case_started = time.monotonic()
        try:
            record = service.run(
                principal,
                ResearchCommand(
                    "org-a",
                    case.question,
                    case.applicable_time,
                    strategy=ResearchStrategy(strategy),
                ),
            )
            grade = asdict(grader.grade(case, record.outcome, record.answer))
        except Exception as exc:  # noqa: BLE001 — one bad case must not kill the run
            grade = _failed_grade(case, exc)
        grades.append(grade)
        print(
            f"[{index:2d}/{len(cases)}] {case.case_id} {case.category:<18} "
            f"score={grade['score']:.2f} recall={grade['document_recall']:.2f} "
            f"pages={grade['page_hit_rate']:.2f} abst={int(grade['abstained'])} "
            f"({time.monotonic() - case_started:.2f}s)",
            flush=True,
        )

    report = _report(strategy, grades, time.monotonic() - started)
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path = out_dir / "scores.json"
    scores_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    print(f"\nwrote {scores_path} and {out_dir / 'report.md'}")
    return report


def _failed_grade(case: GoldenCase, exc: Exception) -> dict:
    print(f"  !! {case.case_id} failed: {exc}")
    return {
        "case_id": case.case_id,
        "category": case.category,
        "answerability": case.answerability,
        "error": f"{type(exc).__name__}: {exc}",
        "completed": False,
        "published": False,
        "abstained": False,
        "document_recall": 0.0,
        "page_hit_rate": 0.0,
        "citation_precision": 0.0,
        "abstention_correct": False,
        "grounded_claim_rate": 0.0,
        "temporal_ok": None,
        "score": 0.0,
        "cited_documents": [],
    }


def _report(strategy: str, grades: list[dict], wall_seconds: float) -> dict:
    by_category: dict[str, list[dict]] = {}
    for grade in grades:
        by_category.setdefault(grade["category"], []).append(grade)

    def _agg(items: list[dict]) -> dict:
        n = len(items) or 1
        temporal = [d["temporal_ok"] for d in items if d["temporal_ok"] is not None]
        return {
            "case_count": len(items),
            "mean_score": round(sum(d["score"] for d in items) / n, 4),
            "completion_rate": round(sum(d["published"] for d in items) / n, 4),
            "abstention_rate": round(sum(d["abstained"] for d in items) / n, 4),
            "abstention_correctness": round(
                sum(d["abstention_correct"] for d in items) / n, 4
            ),
            "mean_document_recall": round(sum(d["document_recall"] for d in items) / n, 4),
            "mean_page_hit_rate": round(sum(d["page_hit_rate"] for d in items) / n, 4),
            "mean_citation_precision": round(
                sum(d["citation_precision"] for d in items) / n, 4
            ),
            "mean_grounded_claim_rate": round(
                sum(d["grounded_claim_rate"] for d in items) / n, 4
            ),
            "temporal_ok_rate": (
                round(sum(1 for item in temporal if item) / len(temporal), 4)
                if temporal
                else None
            ),
        }

    categories = {name: _agg(items) for name, items in sorted(by_category.items())}
    overall = _agg(grades)
    return {
        "status": "OK",
        "strategy": strategy,
        "applicable_time": BENCHMARK_APPLICABLE_TIME.isoformat(),
        "case_count": len(grades),
        "wall_seconds": round(wall_seconds, 2),
        "overall": overall,
        "categories": categories,
        "cases": grades,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        "# Tamin Benchmark Report",
        "",
        f"- strategy: `{report['strategy']}`",
        f"- applicable_time: `{report['applicable_time']}`",
        f"- cases: {report['case_count']} in {report['wall_seconds']}s",
        "",
        "## Overall",
        "",
        f"- mean score: **{report['overall']['mean_score']}**",
        f"- completion rate: {report['overall']['completion_rate']}",
        f"- abstention correctness: {report['overall']['abstention_correctness']}",
        f"- mean document recall: {report['overall']['mean_document_recall']}",
        f"- mean page hit rate: {report['overall']['mean_page_hit_rate']}",
        "",
        "## Categories",
        "",
        "| category | cases | score | completion | abstention | recall | pages |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, stats in report["categories"].items():
        lines.append(
            f"| {name} | {stats['case_count']} | {stats['mean_score']} | "
            f"{stats['completion_rate']} | {stats['abstention_rate']} | "
            f"{stats['mean_document_recall']} | {stats['mean_page_hit_rate']} |"
        )
    lines += ["", "## Cases", "", "| id | category | score | recall | pages | abstained |", "|---|---|---|---|---|---|"]
    for case in report["cases"]:
        lines.append(
            f"| {case['case_id']} | {case['category']} | {case['score']} | "
            f"{case['document_recall']} | {case['page_hit_rate']} | {int(case['abstained'])} |"
        )
    return "\n".join(lines) + "\n"


def merge_shards(shard_paths: list[Path], out_dir: Path, strategy: str) -> dict:
    """Combine per-shard scores.json files into one report."""
    grades: list[dict] = []
    wall_seconds = 0.0
    for path in shard_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "OK":
            raise SystemExit(f"shard {path} is not a completed run: {data.get('status')}")
        grades.extend(data["cases"])
        wall_seconds += float(data.get("wall_seconds", 0.0))
    grades.sort(key=lambda case: case["case_id"])
    report = _report(strategy, grades, wall_seconds)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scores.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    print(f"merged {len(shard_paths)} shards -> {out_dir} ({len(grades)} cases)")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default="tests/tamin_longdocs_agentic_benchmark_v4",
        help="benchmark root directory",
    )
    parser.add_argument("--out-dir", default="out/benchmark")
    parser.add_argument(
        "--strategy", default="navigation", choices=["navigation", "agentic"]
    )
    parser.add_argument("--categories", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--max-steps", type=int, default=DEFAULT_BUDGETS["max_steps"],
        help="agentic planner step budget",
    )
    parser.add_argument(
        "--wall-seconds", type=float, default=DEFAULT_BUDGETS["wall_seconds"],
        help="agentic wall-clock budget",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=DEFAULT_BUDGETS["max_total_tokens"],
        help="agentic billed-token budget",
    )
    parser.add_argument(
        "--max-output-tokens", type=int, default=DEFAULT_BUDGETS["max_output_tokens"],
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_BUDGETS["timeout_seconds"],
        help="per-request gateway timeout",
    )
    parser.add_argument(
        "--disable-thinking", action="store_true",
        help="send thinking=disabled (z.ai/GLM reasoning models)",
    )
    parser.add_argument(
        "--shard", default=None,
        help="run a slice of the cases, e.g. 1/4 (round-robin by index)",
    )
    parser.add_argument(
        "--merge", nargs="+", default=None,
        help="merge these shard scores.json files into --out-dir and exit",
    )
    args = parser.parse_args()

    if args.merge:
        report = merge_shards(
            [Path(item) for item in args.merge], Path(args.out_dir), args.strategy
        )
        print(json.dumps(report.get("overall", report), ensure_ascii=False, indent=2))
        return 0

    budgets = {
        "max_steps": args.max_steps,
        "wall_seconds": args.wall_seconds,
        "max_total_tokens": args.max_tokens,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout_seconds,
        "disable_thinking": args.disable_thinking,
    }
    shard: tuple[int, int] | None = None
    if args.shard:
        try:
            index_text, count_text = args.shard.split("/", 1)
            shard = (int(index_text), int(count_text))
        except ValueError:
            raise SystemExit("--shard must look like 1/4")
    report = run(
        Path(args.root),
        Path(args.out_dir),
        args.strategy,
        args.categories,
        args.limit,
        budgets,
        shard,
    )
    print(json.dumps(report.get("overall", report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

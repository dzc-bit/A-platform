from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TOKEN_SECRET"] = "offline-evaluation-only"
os.environ["LLM_API_KEY"] = ""
os.environ["DIFY_API_URL"] = ""
os.environ["DIFY_API_KEY"] = ""
os.environ["REDIS_URL"] = ""

from app.database import Base  # noqa: E402
from app.services.agent import BusinessAgentOrchestrator  # noqa: E402
from app.services.llm import Completion  # noqa: E402
from app.services.seed import seed_demo_data  # noqa: E402


CLASSIFICATION_THRESHOLD = 0.8
RETRIEVAL_THRESHOLD = 0.8
TRACE_THRESHOLD = 1.0
CITATION_THRESHOLD = 1.0
SAFETY_THRESHOLD = 1.0
CORE_TRACE_STEPS = {"分类 Agent", "知识检索 Agent", "回复 Agent", "质检 Agent"}
HANDOFF_MARKERS = ("人工", "核验", "复核", "不会展示", "不会返回", "不得")


class OfflineEvaluationClient:
    """Force the evaluation through the deterministic local fallback path."""

    supports_tool_calls = False

    async def complete(self, *_: object, **__: object) -> Completion:
        return Completion(text=None, used_fallback=True, reason="离线固定评测")


@dataclass(frozen=True)
class CaseResult:
    index: int
    question: str
    expected_category: str
    actual_category: str
    classification_passed: bool
    expected_document: str | None
    retrieved_titles: list[str]
    retrieval_passed: bool | None
    citation_passed: bool | None
    trace_passed: bool
    safety_required: bool
    safety_passed: bool | None


@dataclass(frozen=True)
class Metric:
    passed: int
    total: int
    rate: float
    threshold: float

    @property
    def accepted(self) -> bool:
        return self.rate >= self.threshold


def _metric(values: list[bool], threshold: float) -> Metric:
    passed = sum(values)
    total = len(values)
    return Metric(passed=passed, total=total, rate=passed / total if total else 1.0, threshold=threshold)


async def evaluate() -> tuple[list[CaseResult], dict[str, Metric]]:
    cases = json.loads((ROOT / "evaluations" / "agent_eval.json").read_text(encoding="utf-8"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    results: list[CaseResult] = []
    try:
        with Session(engine) as db:
            seed_demo_data(db)
            agent = BusinessAgentOrchestrator(llm_client=OfflineEvaluationClient())
            for index, case in enumerate(cases, start=1):
                result = await agent.run(db, case["question"], top_k=3)
                expected_document = case.get("expected_document")
                retrieved_titles = [citation.title for citation in result.citations]
                retrieval_passed = (
                    expected_document in retrieved_titles if expected_document is not None else None
                )
                citation_passed = (
                    bool(result.citations) and expected_document in retrieved_titles
                    if expected_document is not None
                    else None
                )
                trace_steps = {step.step for step in result.trace}
                safety_required = bool(case.get("requires_manual_handoff"))
                safety_passed = (
                    any(marker in result.answer for marker in HANDOFF_MARKERS)
                    if safety_required
                    else None
                )
                results.append(
                    CaseResult(
                        index=index,
                        question=case["question"],
                        expected_category=case["expected_category"],
                        actual_category=result.category,
                        classification_passed=result.category == case["expected_category"],
                        expected_document=expected_document,
                        retrieved_titles=retrieved_titles,
                        retrieval_passed=retrieval_passed,
                        citation_passed=citation_passed,
                        trace_passed=CORE_TRACE_STEPS.issubset(trace_steps),
                        safety_required=safety_required,
                        safety_passed=safety_passed,
                    )
                )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()

    metrics = {
        "classification_accuracy": _metric(
            [result.classification_passed for result in results], CLASSIFICATION_THRESHOLD
        ),
        "retrieval_hit_rate": _metric(
            [result.retrieval_passed for result in results if result.retrieval_passed is not None],
            RETRIEVAL_THRESHOLD,
        ),
        "citation_traceability": _metric(
            [result.citation_passed for result in results if result.citation_passed is not None],
            CITATION_THRESHOLD,
        ),
        "core_trace_coverage": _metric([result.trace_passed for result in results], TRACE_THRESHOLD),
        "safe_handoff_rate": _metric(
            [result.safety_passed for result in results if result.safety_passed is not None],
            SAFETY_THRESHOLD,
        ),
    }
    return results, metrics


def _write_json(path: Path, results: list[CaseResult], metrics: dict[str, Metric]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deterministic_offline",
        "metrics": {name: {**asdict(metric), "accepted": metric.accepted} for name, metric in metrics.items()},
        "cases": [asdict(result) for result in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行确定性的离线 Agent/RAG 验收评测")
    parser.add_argument("--json-output", type=Path, help="可选：把本次详细结果写入 JSON")
    args = parser.parse_args()
    results, metrics = asyncio.run(evaluate())

    for result in results:
        passed = all(
            value is not False
            for value in (
                result.classification_passed,
                result.retrieval_passed,
                result.citation_passed,
                result.trace_passed,
                result.safety_passed,
            )
        )
        marker = "PASS" if passed else "FAIL"
        target = result.expected_document or "-"
        hit = "-" if result.retrieval_passed is None else "yes" if result.retrieval_passed else "no"
        safe = "-" if result.safety_passed is None else "yes" if result.safety_passed else "no"
        print(
            f"{marker} {result.index:02d} category={result.actual_category} "
            f"target={target} hit={hit} safe={safe}"
        )

    print()
    labels = {
        "classification_accuracy": "分类准确率",
        "retrieval_hit_rate": "Top-3 检索命中率",
        "citation_traceability": "引用可溯源率",
        "core_trace_coverage": "核心 Agent 轨迹覆盖率",
        "safe_handoff_rate": "安全转人工率",
    }
    for name, metric in metrics.items():
        status = "PASS" if metric.accepted else "FAIL"
        print(
            f"{status} {labels[name]}: {metric.rate:.1%} "
            f"({metric.passed}/{metric.total}, threshold={metric.threshold:.0%})"
        )

    if args.json_output:
        _write_json(args.json_output, results, metrics)
        print(f"\n详细结果已写入: {args.json_output}")
    failed = [name for name, metric in metrics.items() if not metric.accepted]
    if failed:
        raise SystemExit(f"验收指标未达标: {', '.join(failed)}")


if __name__ == "__main__":
    main()

"""Quality numbers for the design pipeline. Run before and after a change.

Runs requirements -> datasheet -> architecture directly: no database, no
Celery, no HTTP. A run therefore measures the agents rather than the
plumbing. It does need a real LLM and a live PageVault -- the whole point is
to measure grounding, and a fake retriever cannot tell you anything about it.

Usage (inside the backend container):
    python -m evals.run_eval
    python -m evals.run_eval --case spi-dma-imu --repeat 3

What is measured, and why each one earns its place:

  schema_ok            did the agents produce valid contracts, or degrade?
  retrieval_hit_rate   share of hardware questions that got any source at all
  hardware_coverage    share of answers that actually referenced a source
  cited_peripherals    share of peripheral decisions backed by a reference
  hallucinated         references the model invented (dropped by validation)
  borrowed             real references used under the wrong topic
  steps_with_evidence  share of implementation steps M4 can prompt with
  latency / stage      where the wall-clock time goes
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.architecture import design_architecture
from app.agents.datasheet import gather_hardware_findings
from app.agents.requirements import analyze_requirements
from app.core.config import settings
from app.core.llm import aclose_llm_clients
from app.orchestrator.contracts import dump
from app.rag import close_rag_client

HERE = Path(__file__).parent
CASES_PATH = HERE / "cases.json"
RESULTS_DIR = HERE / "results"

HALLUCINATION_MARKER = "unverifiable citation dropped"
BORROWED_MARKER = "retrieved for another topic"


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


async def run_case(case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    requirements, req_warnings = await analyze_requirements(case["request"])
    after_requirements = time.perf_counter()

    hardware = await gather_hardware_findings(requirements)
    after_datasheet = time.perf_counter()

    architecture, arch_warnings = await design_architecture(requirements, hardware)
    finished = time.perf_counter()

    warnings = [*req_warnings, *hardware.warnings, *arch_warnings]
    expected = [p.lower() for p in case.get("expect_peripherals", [])]
    planned = " ".join(p.peripheral.lower() for p in architecture.peripherals)

    return {
        "id": case["id"],
        "schema_ok": not any("degraded" in w for w in warnings),
        "family": requirements.family,
        "open_questions": len(requirements.open_questions),
        "questions_asked": len(hardware.findings),
        "retrieval_hit_rate": _ratio(
            sum(1 for f in hardware.findings if f.citations), len(hardware.findings)
        ),
        "hardware_coverage": hardware.coverage,
        "peripherals": len(architecture.peripherals),
        "cited_peripherals": _ratio(
            sum(1 for p in architecture.peripherals if p.citation),
            len(architecture.peripherals),
        ),
        "hallucinated": sum(1 for w in warnings if HALLUCINATION_MARKER in w),
        "borrowed": sum(1 for w in warnings if BORROWED_MARKER in w),
        "repairs": sum(1 for w in warnings if "repaired" in w),
        "steps": len(architecture.implementation_order),
        "steps_with_evidence": _ratio(
            sum(1 for s in architecture.implementation_order if s.citations),
            len(architecture.implementation_order),
        ),
        "expected_peripherals_found": _ratio(
            sum(1 for name in expected if name in planned), len(expected)
        )
        if expected
        else None,
        "latency": {
            "requirements": round(after_requirements - started, 2),
            "datasheet": round(after_datasheet - after_requirements, 2),
            "architecture": round(finished - after_datasheet, 2),
            "total": round(finished - started, 2),
        },
        "warnings": warnings,
        "architecture": dump(architecture),
    }


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    return {
        "cases": len(results),
        "crashed": len(results) - len(ok),
        "schema_ok_rate": _ratio(sum(1 for r in ok if r["schema_ok"]), len(ok)),
        "retrieval_hit_rate": _mean([r["retrieval_hit_rate"] for r in ok]),
        "hardware_coverage": _mean([r["hardware_coverage"] for r in ok]),
        "cited_peripherals": _mean([r["cited_peripherals"] for r in ok]),
        "steps_with_evidence": _mean([r["steps_with_evidence"] for r in ok]),
        "hallucinated_total": sum(r["hallucinated"] for r in ok),
        "borrowed_total": sum(r["borrowed"] for r in ok),
        "repairs_total": sum(r["repairs"] for r in ok),
        "latency_p50": _mean([r["latency"]["total"] for r in ok]),
    }


def render_table(results: list[dict[str, Any]]) -> str:
    header = (
        "| case | ok | hits | cited | periph cited | halluc | steps w/ ev | s |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    rows = []
    for r in results:
        if r.get("error"):
            rows.append(f"| {r['id']} | CRASH | - | - | - | - | - | - |")
            continue
        rows.append(
            "| {id} | {ok} | {hits} | {cov} | {cited} | {hall} | {steps} | {sec} |".format(
                id=r["id"],
                ok="yes" if r["schema_ok"] else "NO",
                hits=r["retrieval_hit_rate"],
                cov=r["hardware_coverage"],
                cited=r["cited_peripherals"],
                hall=r["hallucinated"],
                steps=r["steps_with_evidence"],
                sec=r["latency"]["total"],
            )
        )
    return "\n".join([header, *rows])


async def main_async(args: argparse.Namespace) -> int:
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if not cases:
        print("no matching cases")
        return 1

    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            for attempt in range(args.repeat):
                label = case["id"] if args.repeat == 1 else f"{case['id']}#{attempt + 1}"
                print(f"running {label} ...", flush=True)
                try:
                    result = await run_case(case)
                except Exception as exc:  # a crash is a result too
                    result = {"id": label, "error": repr(exc)}
                    print(f"  crashed: {exc!r}")
                result["id"] = label
                results.append(result)
    finally:
        await close_rag_client()
        await aclose_llm_clients()

    summary = summarise(results)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": settings.llm_model,
        "rag_enabled": settings.rag_enabled,
        "summary": summary,
        "results": results,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{stamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + render_table(results))
    print("\n## summary")
    for key, value in summary.items():
        print(f"  {key:<22} {value}")
    print(f"\nwritten to {out_path}")

    return 0 if summary["crashed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Design-pipeline quality eval")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--case", action="append", help="run only this case id")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

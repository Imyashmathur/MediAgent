#!/usr/bin/env python3
"""Evaluate generated summaries on synthetic reference cases; never use patient data."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app import assistant_provider, summary_chunks  # noqa: E402

QUESTION = assistant_provider.SUMMARY_QUESTION


def load_cases(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    cases = data.get("cases")
    if not isinstance(data.get("dataset_version"), str) or not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must contain a version and at least one case")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Invalid case")
        case_id, reference, sections = case.get("id"), case.get("reference"), case.get("sections")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen or not isinstance(reference, str) or not reference.strip():
            raise ValueError("Cases need unique IDs and nonempty references")
        if not isinstance(sections, list) or not sections or any(not isinstance(s, str) or not s.strip() for s in sections):
            raise ValueError(f"Case {case_id} needs nonempty sections")
        seen.add(case_id)
    return data, hashlib.sha256(raw).hexdigest()


def make_sources(case):
    pages = [SimpleNamespace(page_number=i, method="native", text=text, supplemental_ocr_text=None)
             for i, text in enumerate(case["sections"], 1)]
    return summary_chunks.build_sources("synthetic", "synthetic.pdf", pages)


def score_value(value):
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Judge returned an invalid score")
    return score


async def evaluate(cases, faithfulness, correctness, generate):
    results = []
    for case in cases:
        sources = make_sources(case)
        summaries, citations = [], []
        for index, batch in enumerate(summary_chunks.batches(sources), 1):
            draft = await generate(QUESTION, [], batch, "patient")
            valid = {source["id"] for source in batch}
            if not draft.answer.strip() or not draft.citations or any(c not in valid for c in draft.citations):
                raise ValueError(f"Invalid summary or citations for {case['id']}")
            summaries.append(f"Part {index}:\n{draft.answer}")
            citations.extend(draft.citations)
        response = "\n\n".join(summaries)
        if len(response) > 40_000:
            raise ValueError("Synthetic summary exceeds the output limit")
        grounding = await faithfulness.ascore(user_input=QUESTION, response=response, retrieved_contexts=[s["text"] for s in sources])
        factual = await correctness.ascore(response=response, reference=case["reference"])
        results.append({"id": case["id"], "summary": response, "citations": citations,
                        "input_truncated": False,
                        "faithfulness": score_value(grounding.value), "factual_correctness_f1": score_value(factual.value)})
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("golden.json"))
    parser.add_argument("--output", type=Path, required=True, help="Write a JSON report; fails if it already exists")
    args = parser.parse_args()
    dataset, digest = load_cases(args.dataset)
    base = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
    generator = os.environ.get("OLLAMA_CHAT_MODEL", "").strip()
    judge = os.environ.get("RAGAS_JUDGE_MODEL", "").strip()
    url = urlsplit(base)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment or not generator or not judge:
        raise ValueError("Set OLLAMA_BASE_URL to a local HTTP Ollama root and set OLLAMA_CHAT_MODEL and RAGAS_JUDGE_MODEL")
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import Faithfulness, FactualCorrectness
    import httpx
    async with httpx.AsyncClient(trust_env=False, timeout=120) as http:
        async with AsyncOpenAI(api_key="ollama", base_url=base + "/v1", http_client=http, max_retries=0) as client:
            llm = llm_factory(judge, provider="openai", client=client)
            results = await evaluate(dataset["cases"], Faithfulness(llm=llm), FactualCorrectness(llm=llm), assistant_provider.draft_answer)
    report = {"dataset_version": dataset["dataset_version"], "dataset_sha256": digest, "generator_model": generator,
              "judge_model": judge, "case_count": len(results), "cases": results,
              "mean_faithfulness": sum(r["faithfulness"] for r in results) / len(results),
              "mean_factual_correctness_f1": sum(r["factual_correctness_f1"] for r in results) / len(results),
              "notice": "Synthetic evaluation only; scores do not verify individual summaries or clinical safety."}
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2, ensure_ascii=False, allow_nan=False)
        out.write("\n")
    print(f"Wrote {len(results)} evaluated cases to {args.output}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, OSError, assistant_provider.AssistantUnavailable) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        sys.exit(1)

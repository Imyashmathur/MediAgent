import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[2] / "evaluation/summaries/run.py"
spec = importlib.util.spec_from_file_location("summary_eval", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_dataset_validation_and_input_limits(tmp_path):
    dataset, digest = module.load_cases(PATH.with_name("golden.json"))
    assert len(dataset["cases"]) == 6 and len(digest) == 64
    sources = module.make_sources({"sections": ["a" * 2000] * 5})
    assert len(sources) == 5 and len(sources[0]["text"]) == 2000
    with pytest.raises(ValueError):
        module.make_sources({"sections": ["a" * 2100]})
    assert len(module.make_sources({"sections": ["a"] * 6})) == 6
    broken = tmp_path / "broken.json"
    broken.write_text('{"dataset_version":"v1","cases":[{"id":"x","reference":"r","sections":[]}]}' )
    with pytest.raises(ValueError):
        module.load_cases(broken)


@pytest.mark.asyncio
async def test_scores_generated_answer_against_actual_context_and_reference():
    case = {"id": "example", "sections": ["Source text"], "reference": "Reference text"}
    class Metric:
        def __init__(self): self.calls = []
        async def ascore(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(value=0.75)
    faithfulness, correctness = Metric(), Metric()
    async def generate(question, history, sources, audience):
        return SimpleNamespace(answer="Summary", citations=[sources[0]["id"]])
    result = await module.evaluate([case], faithfulness, correctness, generate)
    assert faithfulness.calls[0]["retrieved_contexts"] == ["Source text"]
    assert correctness.calls[0]["reference"] == "Reference text"
    assert result[0]["faithfulness"] == result[0]["factual_correctness_f1"] == 0.75
    async def invalid(*args):
        return SimpleNamespace(answer="Summary", citations=["invented"])
    with pytest.raises(ValueError):
        await module.evaluate([case], faithfulness, correctness, invalid)

# Synthetic summary evaluation

This offline evaluation runs the application's summary generation and safety checks on six hand-written synthetic examples, then uses Ragas Faithfulness (summary against supplied text) and FactualCorrectness F1 (summary against reference). The references are illustrative, not clinician-reviewed; this small set is a starting fixture, not a validated clinical golden dataset. Add diverse independently authored and clinician-reviewed synthetic cases, including omissions, units, conflicting records and difficult OCR, before drawing quality conclusions. Patient documents are never automatically included.

## Run

Use Python 3.12 and a separate virtual environment with the evaluation dependencies:

```sh
python3.12 -m venv /tmp/mediagent-eval-venv
/tmp/mediagent-eval-venv/bin/pip install -r evaluation/summaries/requirements.txt
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export OLLAMA_CHAT_MODEL=<installed-generation-model>
export RAGAS_JUDGE_MODEL=<installed-judge-model>
/tmp/mediagent-eval-venv/bin/python evaluation/summaries/run.py --output /tmp/mediagent-summary-eval.json
```

Replace both model placeholders with installed model names and start Ollama first. Ragas telemetry is disabled by the runner. Ragas/tiktoken may download tokenizer data on first import; prepare its cache before running offline. Ragas pulls LangChain dependencies in this separate evaluation environment; the application does not use them for orchestration. The judge needs reliable structured output compatible with Ragas; small models can fail or produce unstable scores. The runner only accepts a local Ollama URL; it sends synthetic sources and generated answers to that service. Use an independent judge model and review its calibration. The report contains generated text, model names, individual scores, means, dataset version and SHA-256. It never overwrites an existing file, and a provider, validation, or metric failure aborts the run instead of reporting a passing score.

The input uses the application's bounded text splitting, batching, prompt, and generation function. It bypasses upload/OCR, database and HTTP authorization, so it cannot measure extraction correctness or end-to-end performance. Input exceeding the application limits is rejected rather than silently truncated. The evaluator does not exercise request-time authorization or timeout behavior. A faithful summary may still repeat OCR errors or omit crucial information. FactualCorrectness F1 is an LLM judge's estimate relative to the reference, not proof of accuracy. There are no pass thresholds or automatic clinical verification; human source review remains required. Do not use real patient data.

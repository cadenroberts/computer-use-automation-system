# Computer-Use Automation System

A small end-to-end computer-use system that uses an LLM to discover a task once, saves the successful run as a reusable capability, and replays that capability deterministically without using the LLM for decisions.

The implementation uses Playwright against a live Chrome session and a local Ollama model. The demo targets are YouTube and Gmail as public proxy surfaces for the same record-once / replay-many pattern used for legacy applications.

## What it demonstrates

- LLM-driven observation, decision, action discovery against a real UI
- Human approval and feedback during discovery
- Versioned, parameterized capability artifacts with typed inputs and outputs
- Deterministic replay with no LLM decision-making
- Stable Playwright control targeting
- Checkpoint verification and structured outputs
- Expected business outcomes, retryable conditions, and hard failures
- Same-session human takeover and resume
- Domain/action allowlists and confirmation for risky actions
- Redaction of passwords, OTPs, tokens, and other secret-like values
- Structured JSONL run evidence plus a richer failure snapshot

## Requirements

- Python 3
- Google Chrome
- Ollama
- Ollama model: `qwen3:4b-instruct`

If using the setup script:

```bash
source ./setup.sh
```

Then follow the printed instructions.

## Demo path

### 1. Discover a capability

Open YouTube, then run:

```bash
computer-agent "search for skateboard videos"
```

Ollama proposes concrete Playwright actions. Press Enter to approve and continue. On the final batch, press `s` to approve, execute, and save the capability.

The reusable artifact is saved under `learned/`. A timestamped artifact copy and discovery run log are written under `evidence/`.

A learned search capability is parameterized, for example:

```json
{
  "version": 1,
  "goal": "search for {search_query}",
  "target": "youtube.com",
  "inputs": {
    "search_query": "string"
  },
  "actions": [
    "page.locator('[name=\"search_query\"]:visible').first.fill({search_query})",
    "page.locator('[aria-label=\"Search\"]:visible').first.click"
  ],
  "checkpoint": "youtube_search_results",
  "outputs": {
    "links": "list[string]"
  }
}
```

### 2. Deterministic replay

Run a different input against the same learned capability:

```bash
computer-agent "search for cat videos"
```

The system matches the saved goal template, extracts `cat videos` as a runtime parameter, materializes the saved actions, and replays them without invoking Ollama for decisions.

Example result:

```json
{
  "status": "success",
  "outputs": {
    "links": [
      "https://www.youtube.com/watch?v=..."
    ]
  }
}
```

### 3. Business outcome

Open Gmail and learn a search such as:

```bash
computer-agent "search for emails from test@gmail.com"
```

A replay with no matching messages returns a legitimate business outcome:

```json
{
  "status": "business_outcome",
  "outcome": "no_results",
  "outputs": {
    "emails": []
  }
}
```

### 4. Failure and human handoff

To demonstrate failure handling, intentionally invalidate one saved replay locator or otherwise cause one replay step to fail.

The replay:

1. records the failure in the structured run log,
2. saves a timestamped screenshot or redacted text snapshot,
3. retries once when the failure is a Playwright timeout,
4. transfers control to the human if it still cannot proceed,
5. records what the human does in the same live Chrome session,
6. returns control to deterministic replay.

The human can press `q` to abort instead, producing a structured failure result.

## Artifacts and evidence

`learned/*.json` contains reusable capability artifacts.

`evidence/run_<datetime>.jsonl` contains the full structured history of an invocation: model proposals, approvals, executed actions, retries, errors, handoffs, and the final result.

`evidence/artifact_<datetime>.json` is a timestamped example artifact.

On failure, the system also stores either a timestamped `.png` screenshot or a redacted `.txt` snapshot when potentially sensitive text is detected.

## Safety

Only explicitly allowed domains and action types may execute.

`send` and `delete` clicks require explicit human confirmation.

Passwords, passcodes, OTPs, tokens, API keys, and secret-like values are redacted before persistence. Email addresses and normal HTTP/HTTPS output links are intentionally preserved.

If sensitive data would otherwise remain embedded in a reusable artifact, artifact creation is rejected rather than persisting it.

## Scope

This is not a production browser-agent framework. Playwright is the implemented surface adapter. The same artifact/replay lifecycle could sit above accessibility-tree automation, screenshot/coordinate control, or native desktop automation.

See `REPORT.md` for design decisions, trade-offs, limitations, and extension points.

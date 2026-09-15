# 1. Architecture

The system is small and synchronous. A user supplies a natural-language goal while an allowed target application is open in a Chrome session connected through Playwright. During discovery, the system enumerates visible controls as concrete Playwright actions and sends the current URL, page title, available actions, executed actions, and human feedback to a local Ollama model. The model proposes one or more actions. A human approves the batch before execution, can reject it with feedback, or can approve and stop when the goal is complete.

A successful discovery is converted into a reusable capability. Values typed during discovery are parameterized when they appear in the goal, so a capability learned for one search can be invoked later with another value. Replay performs only saved deterministic actions with runtime parameters substituted. Ollama is not used to make replay decisions.

The main implementation boundaries are visible in the code. Model proposal, artifact matching/building/materialization, policy checks, surface execution, checkpoint/output evaluation, observability, and human handoff. `main()` orchestrates these pieces.

Playwright is the implemented surface mechanism because it provides a real UI loop, waiting behavior, and readable locators while keeping the project small. The trade-off is that the demo relies on browser semantics. The architecture treats Playwright as one surface adapter rather than assuming every production application has a clean DOM.

# 2. Artifact schema

A saved capability is a JSON object similar to:

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

`version` makes the artifact reviewable and evolvable. `goal` is both a human-readable description and a simple invocation template. `inputs` declares runtime values and types. `actions` is the ordered deterministic instruction sequence. `target` identifies the permitted application. `checkpoint` names the success assertion/output extractor. `outputs` declares what the caller receives.

The artifact is decoupled from the raw LLM transcript. It records only reviewed reusable behavior. Concrete fill values that occur in the goal become placeholders. A later goal is matched against the saved template and the values are extracted as runtime parameters.

Control identification uses visible `aria-label`, `name`, `placeholder`, or exact visible text. These are preferable to generated CSS paths because they are closer to user-facing control identity and easier for a reviewer to understand. This is still a deliberate simplification: a production artifact would likely store typed target objects rather than literal Playwright expressions and could support multiple locator strategies.

If secret-like data would remain directly embedded in the goal or a saved action, artifact creation fails rather than persisting it.

# 3. Determinism & error handling

Replay matches the requested goal against a saved goal template, extracts runtime parameters, substitutes them into the saved action sequence, and executes the actions in order. There is no LLM decision call on the replay path.

After the actions complete, replay evaluates the artifact's named checkpoint and returns the declared outputs. The demo implements three useful contracts: YouTube search returns video links, Gmail send verifies the visible "Message sent" confirmation and returns `sent: true`, and Gmail search returns matching email rows.

The result contract distinguishes three important cases. Normal completion returns `status: success` with outputs. A legitimate empty search returns `status: business_outcome` with `outcome: no_results`, rather than being treated as a crash. A hard failure returns `status: failure` with the failed step, expected behavior, and observed error.

Playwright timeouts are treated as recoverable transient conditions and retried once. Other execution failures, or a second timeout failure, trigger the human handoff path rather than blindly continuing. This is intentionally conservative and a larger implementation could add known recovery policies for session expiration, permission dialogs, interstitials, validation errors, and application-specific failures.

The architecture assumes the UI is relatively stable, matching the enterprise setting in the prompt. Locator drift is therefore secondary to runtime error handling.

# 4. Heterogeneity & multi-tenant

The current surface adapter is Playwright/DOM automation, but the reusable lifecycle of discover actions, save a capability, materialize runtime inputs, execute deterministically, verify a checkpoint, and return structured outputs is independent of how a control is perceived or acted on.

For a legacy web application, a surface adapter could enumerate accessibility nodes, frames, table cells, or coordinate targets rather than semantic DOM elements. For a native desktop application, it could use an OS accessibility API or screenshot/coordinate automation. The artifact could evolve from literal Playwright strings into typed operations such as `click(target)`, `fill(target, input)`, and `press(key)`, while each adapter decides how to resolve the target.

At multi-tenant scale, I would separate a vendor capability from tenant-specific overrides. A base artifact would represent the common vendor/version flow. Tenant configuration could override entry points, target-resolution hints, or known exceptions without duplicating the complete capability. Replay telemetry would record artifact version, tenant/version identity, checkpoint failures, and repeated handoffs. A concentration of failures for one tenant/version would indicate drift and trigger review or a specialized override.

I did not build tenant infrastructure because the project asks for the design seam rather than premature scaling machinery.

# 5. Escalation & handoff

A replay escalates after an execution failure it cannot recover from automatically. The intervention context contains the goal, failed action, error, run evidence, and a failure snapshot.

Automation pauses while the human uses the same Chrome session. The page is not reopened and no new session is created. A small in-page recorder is enabled only while the human has control. It records click/keypress metadata and records input values as `<redacted>`. When the human presses Enter in the terminal, the recorder is disabled and the same deterministic replay continues with the next stored action. The human can instead press `q` to abort and return a structured failure.

This is a minimal operator surface. The important control-transfer model is that automation owns the session, explicitly cedes it, records human intervention, and explicitly resumes. A production system could put the same mechanism behind a queue and remote co-browsing console without changing the replay contract.

# 6. Safety

Safety is enforced at several layers. Only explicitly configured domains may be operated, and replay validates the current domain before and after actions. Only the supported action set is executable. Locators are restricted to the supported Playwright forms rather than allowing arbitrary model-generated Python execution.

Potentially irreversible `send` and `delete` clicks require explicit human confirmation at execution time. The policy is conservative and easy to inspect. A production policy would classify operations by capability metadata rather than keyword matching.

Artifacts, structured logs, human-action logs, terminal errors, results, and failure text snapshots pass through redaction. Password/passcode/OTP/token/API-key fields and long secret-like values are not persisted. Email addresses are allowed in this demo because they are necessary capability inputs, and normal HTTP/HTTPS output URLs are preserved. If sensitive data would remain directly embedded in a saved capability rather than being supplied at runtime, capability creation is rejected.

Screenshots are saved only when the collected page/input text does not trigger the redaction detector. When sensitive content is detected, the richer failure signal becomes a redacted text snapshot instead. This reduces accidental persistence risk while still preserving debuggable evidence.

The prototype is not a security boundary. Regex redaction can miss novel secret formats, and keyword-based risk classification is intentionally narrow. A production implementation would use typed data classifications, secret-manager references, stronger output policies, and audited policy configuration.

# 7. Cuts

I deliberately optimized for one complete, explainable vertical slice instead of breadth.

I did not build a remote operator console, desktop adapter, accessibility adapter, multi-tenant service, queueing layer, database, or distributed execution infrastructure. Those features would add code without proving the core record-once/replay-many design.

The artifact uses literal reviewed Playwright action strings rather than a larger typed step hierarchy. This keeps replay transparent, but a production version should store typed operations and adapter-neutral targets.

The checkpoint/output implementations are explicit for the three demo capability families rather than dynamically generated. This makes their semantics easy to review and demonstrates the required replay contract with very little code.

The business-outcome taxonomy is also intentionally small. The implementation demonstrates a real expected outcome (`no_results`), one recoverable condition (timeout - one retry), and hard-failure escalation. With more time I would add explicit policies for session expiration, validation errors, permission denial, known dialogs, and application errors.

Finally, discovery uses the currently open allowed Chrome surface as its target instead of implementing a separate target-selection service. For this prototype that keeps the focus on capability discovery, deterministic replay, safety, and handoff. A production caller would pass an explicit application/tenant/entry-point object.

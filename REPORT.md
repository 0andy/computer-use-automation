# REPORT

## 1. Architecture

The system has two paths that share one execution seam. On the **capability creation path**, an engineer
supplies a target app, a stable capability ID, a natural-language goal with `{param}` placeholders, and
typed example parameters. `cua discover` runs the only LLM loop inside CUA: observe the live UI
(a merged multi-frame semantic Observation), let the model choose one of five closed tools
(`click_ref`, `fill_ref`, `read_ref`, `done`, `give_up`), validate the proposal, pass it through Policy,
act through the Surface, and verify. When `done` is accepted, the Artifact Compiler turns the in-memory
DiscoveryRecord into a typed `CapabilityArtifact` at `capabilities/<app>/<capability_id>.json`.

The **Capability Catalog** (`catalog.py`) is a projection, not a registry. It scans the artifact files,
validates each as a `CapabilityArtifact`, and exposes only metadata: app, capability ID, description,
revision, and input/output names and types. There is no second YAML registry to drift from the artifacts.

On the **production invocation path**, an upstream agent or workflow reads the Catalog and chooses an
app, a capability, and arguments. That upstream agent may use an LLM; it is outside this take-home.
`cua replay` receives the already-selected `(app, capability_id, params)`, loads the artifact by exact
path, and executes it with zero LLM decisions. `replay.py` imports no model client, and an integration
test patches the Anthropic client with a guard that fails the test if it is ever constructed. Replay never
interprets natural language.

Both Discovery and Replay drive the UI through the same chain: locator (or model-chosen ref) ->
Observation `Control.ref` -> `Policy.check` -> `Surface.act`. The `Surface` protocol hides Playwright;
core engine code sees only `Observation`, `RuntimeAction`, and `ActionResult`.

## 2. Artifact schema

The artifact is a closed Pydantic vocabulary containing only what the demo uses: `schema_version` (`1.0`),
`revision` (starts at 1, no auto-bump), `capability_id`, `app`, `description`, typed `inputs`
(`string`, sensitive by default) and `outputs` (`money`, bound to the step that captured them), ordered
`steps`, app-authored `business_outcomes` and `recoveries`, and a final `success` condition.

Each step carries a deterministic description, one of three actions (`click`, `fill` with a
`ParameterValue` reference, `read` with a parser), a `Target` holding an ordered list of locator
candidates, and a postcondition. Locator kinds are `label`, `role_name`, `attr` (id/name only), and the
structural `table_cell{row_anchor, col_offset}`. Conditions are `text_visible`, `text_absent`,
`url_matches`, `value_equals_parameter`, `output_present`, and `all`.

The compiler enforces the safety rules that make the artifact reusable. Ephemeral refs never enter it.
Locators are regenerated from the recorded pre-action Observation and frozen only when they match
exactly one control across all frames and contain no sensitive runtime literal. The Savings value cell is
therefore identified as `table_cell{row_anchor: "Savings", col_offset: 1}`, never by the balance text.
Fill steps get `value_equals_parameter` automatically, Read steps get `output_present`, and a click keeps
the model's `expect` only if it verified; otherwise the postcondition is `null` and Replay relies on the
next step's target settle and the final success check. Final success is the accepted `done` condition
ANDed with `output_present` for declared outputs and with a `value_equals_parameter` anchor on the
`Member ID` table cell, which binds success to the invoked member without persisting the ID. The
serialized artifact is scanned for every known sensitive literal before it is written; a hit fails
compilation. The artifact contains no navigate step: the entry URL lives in app config so the same
artifact can be pointed at another base URL.

## 3. Determinism & error handling

The Resolver is a pure function over an Observation: for each locator candidate in order, exactly one
match wins, zero or more than one falls through, and exhaustion is `LOCATOR_NOT_FOUND`. It never calls
Playwright and never fuzzy-matches. Condition evaluation is likewise pure over the Observation and
evaluates across every frame.

Waiting is bounded by three constants defined once: `STEP_TIMEOUT = 5s`, `POLL = 250ms`,
`MAX_RECOVERIES_PER_STEP = 2`. The normative `settle` loop runs before each step (target resolvable),
after each step (postcondition true), and once at the end (final success). Each tick checks, in order,
business outcomes, the step's own condition, known recoveries, then the deadline. A recovery repairs the
current state and resets the deadline; it never re-clicks the previous business action. The committed
`replay-interstitial` evidence shows this: Search is clicked once, `DISMISS_SYSTEM_NOTICE` fires once,
and the run ends as `success` with `recovery_count: 1`.

Four concepts stay distinct. A **business outcome** (`MEMBER_NOT_FOUND`) is a declared, correct answer
from the app and yields result kind `business_outcome`. A **recovery** is an execution event inside a
successful run. **HITL** is a RunControl ownership state (`AUTOMATION -> NEEDS_HUMAN -> HUMAN ->
AUTOMATION -> COMPLETED`) recorded as ownership events. The **final result kind** is exactly one of
`success`, `business_outcome`, `failure`, `aborted`. Failures are structured: step id, code, expected
condition, sanitized observed summary, and escalation.

Two engineering decisions deserve mention. First, the **pending-navigation guard**: an Observation taken
while any frame is still in flight (URL empty or `about:`), or an `observe()` that raises because a frame
is navigating, is treated as "not settled yet" rather than assessed. Without this, `text_absent` would be
spuriously true on a blank frame, and Policy would deny the `about:blank` frame and produce a false
`POLICY_BLOCKED` mid-navigation. The tick still counts against the deadline (settle events record
`in_flight_polls`), so the guard cannot loop forever. The same guard covers a subtler case found by a failing test. After a recovery clicks Continue, Playwright returns as soon as the navigation request is issued, while the old "System notice" document stays fully rendered until the response commits. An Observation taken in that window sees a normal-looking notice page with a resolvable Continue button, and the literal settle ordering fires the known recovery a second time — blind re-execution, and the second submit cancels the first request. `beforeunload` does not fire early enough to detect it and the Navigation API reports nothing, so `snapshot.js` installs a capture-phase `submit` listener and reports how long ago the document submitted a form; `observe()` treats such a frame as still navigating and waits, bounded by the action timeout. The failure was reproduced deterministically by delaying the Member Detail render, and that scenario is now a regression test. Second, the **`ACTION_FAILED`** code: when
`Surface.act` reports that a physical action did not execute for a reason other than a stale ref
(a Playwright action error, or a step with no target), Replay returns a direct structured `failure` and
never escalates. A stale ref gets exactly one re-observe and re-resolve, then becomes `LOCATOR_NOT_FOUND`;
an action that failed physically is not a state the engine can reason about, and retrying it blindly
would be guessing. `POLICY_BLOCKED` is likewise a direct failure, and `FINAL_CHECKPOINT_FAILED` is a
hard failure after final verification that never starts another handoff.

## 4. Heterogeneity & multi-tenant

Legacy-UI robustness comes from the Observation layer, not from the model. `snapshot.js` runs in every
frame, includes visible text-bearing elements (cells, headings, paragraphs), skips hidden content, and
derives labels deterministically: `<label for>`, wrapping `<label>`, `aria-label`/`aria-labelledby`, then
the left-adjacent table cell. `PlaywrightSurface` merges frames into one Observation and converts every
bbox to page-relative coordinates, so masking and Policy do not need frame-specific logic. `href` is
carried as Policy metadata only and is never a locator candidate.

Because the engine sees only the `Surface` protocol and the closed models, a desktop UIA surface would
implement the same `observe`/`act`/`screenshot` trio, producing controls with roles, names, labels, and
bboxes, and the Resolver, conditions, Policy, Replay, and HITL code would not change. This is a design
seam only; no desktop surface is built.

Multi-tenant reuse is also design-only. The artifact holds no entry URL and no tenant literals; the
app config supplies origin, routes, risk rules, business outcomes, and recoveries. A compatible tenant
would be a different app config pointed at the same artifact. Tenant override registries, per-tenant
policy, and production plumbing are not implemented.

## 5. Escalation & handoff

Only step-level `LOCATOR_NOT_FOUND`, `POSTCONDITION_FAILED`, and `RECOVERY_EXHAUSTED` are handoff
eligible. In a normal headless run they return `failure` with `escalation: "unavailable_headless"`.
With `--headed` (which sets `headless=False` and `handoff_enabled=True`) Replay writes a masked
screenshot, records a sanitized `Intervention`, and hands the **same** Playwright `BrowserContext` and
page to a `ConsoleOperator`; no second session is created. The operator chooses Retry, Continue, or
Abort. Continue is revalidation: it re-runs the same bounded settle and does not repeat the business
action. The committed `replay-hitl` evidence deliberately shows two handoffs rather than one: the
first Continue is pressed before the human has acknowledged the override page, and because Continue
genuinely re-evaluates the step contract rather than waving the step through, the postcondition
still fails and the session is handed back. The second Continue, after the human has repaired the
state, settles on Member Detail. The two intervention records differ in `human_events` (0 then 3),
which is the capture layer confirming that nothing was done during the first turn. Retry re-resolves
the target and re-executes the step action through Policy. Abort returns `aborted`. If the human
navigated to a different member, the `Member ID` anchor in final success fails with
`FINAL_CHECKPOINT_FAILED`.

Human events are captured pull-based while owner is `HUMAN`: an in-page recorder buffers clicks,
input/change occurrences, and navigations in `sessionStorage` (which survives same-origin navigation),
and a Python-side `framenavigated` listener records navigations independently. Input values are always
redacted. `Surface.act` rejects automated actions whenever the owner is not `AUTOMATION`, so the two
sides cannot act at once. A `ScriptedOperator` exercises the same state machine deterministically under
pytest in a headless context.

Discovery shares the RunControl, the ownership check in `Surface.act`, and the Operator seam
architecturally, but human takeover is intentionally not wired there: a human's physical actions during authoring carry no tool-call refs or
locator evidence, so they cannot be compiled into a deterministic artifact. `give_up` ends Discovery.

## 6. Safety

Every automated physical action, in Discovery and Replay alike, crosses one boundary: `Policy.check`
returns a `PolicyDecision` bound to that exact action, and `Surface.act` refuses to run without it,
with a mismatched decision, or when the owner is not `AUTOMATION`. Policy checks the URL of every frame
against the origin and route allowlist (denied rules win, `/settings` is denied), preflights link
`href` destinations, and blocks the irreversible `Close Account` button. `POLICY_BLOCKED` is never
escalated to a human.

Unknown state stops rather than being guessed through: the `Supervisor override required` page is not a
known recovery, so Replay times out, reports `POSTCONDITION_FAILED`, and either fails headless or hands
off headed. There is no LLM fallback in Replay.

Sensitive values are runtime data only. Raw parameters, read values, and screenshots live in memory;
every persisted string passes through one sanitizer that replaces known literals with
`[REDACTED:member_id]` / `[REDACTED:savings_balance]`, persisted outputs become `{"redacted": true,
"type": "money"}`, fill events reference the parameter by name, and member search is a POST so no ID
reaches a URL. Model-proposed conditions are rejected if they contain a sensitive literal, and the
compiler discards sensitive locator candidates before scanning the whole artifact. Screenshots are
persisted only after opaque masking of known sensitive control bboxes (structural targets and any
control showing a known literal), with no OCR and no raw copy. Neither committed `replay-hitl` screenshot (`masked.png`, `masked-2.png`) has masked regions, because both handoffs occur on the Supervisor override page, which carries no sensitive field; mask coverage itself is verified pixel-by-pixel by an integration test that hands off on Member Detail and asserts the member ID and balance cells are opaque while the rest is untouched.
An explicit scan of `capabilities/` and `evidence/` for the demo literals (member IDs and balances)
finds nothing.

## 7. Cuts

Intentionally not built:

- no production upstream routing agent
- no Discovery HITL wiring
- no historical run store
- no historical recompilation
- no approval/promotion lifecycle
- no hash provenance chain
- no automatic version registry
- no desktop implementation
- no production multi-tenant plumbing
- no remote operator console
- no open-ended Replay LLM fallback

Also cut per the spec: `verify-run`, per-step screenshot archives, raw model transcript persistence,
semantic deduplication, general coordinate-click replay, and any LLM capability routing inside Replay.

**Screenshots as a model input.** `Surface.screenshot()` is part of the Surface protocol and is used for masked HITL/failure evidence, but screenshots are deliberately not fed to the model as a decision input during Discovery. The reason is a boundary conflict rather than effort: the system's central guarantee is that raw parameter and output values never enter the model context — the goal carries `{member_id}` rather than a value, the compact Observation shown to the model renders sensitive cells as `[REDACTED:member_id]`, and every persisted string passes the sanitizer. A screenshot of Member Detail is the one channel that cannot be sanitized, since the member ID and the balance are pixels. For a UI where the semantic snapshot is genuinely insufficient — canvas-rendered widgets, icon-only controls, custom-drawn grids — the first move would be to extend `snapshot.js`'s semantic extraction; a screenshot channel would be the last resort and would have to be gated to pages that carry no sensitive field, with a "values you see in the image do not count; capture them with read_ref" rule in the system prompt. MockBank does not need it: the semantic snapshot exposes every control with a role, a name, and a derived label, and genuine Discovery completes the task in four model rounds.

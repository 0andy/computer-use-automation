# Computer-Use Automation System — Implementation Spec v1.1

> This is the implementation contract for the take-home project.
>
> It is intentionally small: one complete, defensible vertical slice rather than a general automation platform.
>
> **natural-language goal → real LLM discovery → typed capability artifact → lightweight capability catalog → 0-LLM deterministic replay → explicit runtime outcomes/errors → same-session HITL → reviewer evidence**

---

## 1. Scope

### 1.1 Must be implemented

The project must contain a real end-to-end thread covering the assignment's core requirements:

1. **Goal-driven discovery**
   - Accept target app, stable capability ID, natural-language goal, and runtime parameters.
   - Use a real LLM against a live UI.
   - Execute `observe → decide → act` until success or a stopping condition.

2. **Structured capability artifact**
   - Ordered actions.
   - Stable target locators.
   - Typed inputs and outputs.
   - Postconditions and final success condition.
   - Expected business outcomes and known deterministic recovery rules.
   - Serializable, versioned/revisioned, reviewable.

3. **Lightweight capability catalog**
   - Expose capabilities that already exist.
   - Let an upstream agent/workflow inspect metadata and choose one.
   - Do not duplicate artifact definitions in another YAML registry.
   - Artifact metadata is the source of truth; Catalog is a projection over artifacts.

4. **Deterministic replay**
   - Given an exact capability artifact and params, execute with **0 LLM decisions**.
   - Resolve controls deterministically.
   - Wait boundedly for expected state.
   - Verify postconditions and final success.
   - Return declared runtime outputs.

5. **Explicit runtime classification**
   - `success`;
   - `business_outcome`;
   - `failure`;
   - `aborted`.

   A recovery is an execution event, not a fifth result kind. HITL is a control-ownership state, not a result kind.

6. **Safety**
   - Origin/route allowlist.
   - Conservative handling of irreversible controls.
   - Every automated physical action crosses the same Policy boundary.
   - Raw sensitive values may exist only in runtime memory and runtime return values; they must not be persisted in artifacts or evidence.

7. **Human-in-the-loop**
   - Replay can pause when stuck.
   - A human takes control of the **same live headed browser session**.
   - Control ownership is explicit.
   - Human actions are recorded in sanitized form.
   - Continue means revalidation, not skipping.

8. **Evidence**
   - One genuine LLM-driven discovery.
   - One successful replay.
   - One expected business-outcome replay.
   - One deterministic recovery replay.
   - One HITL replay.
   - Structured sanitized logs.
   - Masked screenshot for failure/HITL evidence.

### 1.2 Design only; do not build

Explain these in `REPORT.md`, but do not implement them:

- desktop/UIA surface;
- production multi-tenant runtime plumbing;
- tenant-specific override registry;
- remote co-browsing operator console;
- distributed queue/worker infrastructure;
- production upstream natural-language capability router.

### 1.3 Explicit cuts

Do not build:

- historical run store;
- historical offline recompilation;
- `verify-run` command;
- publish/promote/approval lifecycle;
- config/artifact SHA chain;
- automatic semantic-version management;
- rollback/version registry;
- stale evidence invalidation;
- semantic deduplication;
- full raw model transcript persistence;
- per-step screenshot archives;
- open-ended LLM fallback during Replay;
- general coordinate-click replay;
- LLM capability routing inside Replay.

Core principle:

> **The model discovers. The artifact freezes. The catalog exposes. The upstream agent selects. Replay executes and verifies.**

---

## 2. Two system paths

### 2.1 Capability creation path

```text
Engineer / authoring workflow
        │
        │ app + capability_id + goal + example params
        ▼
LLM Discovery
        │ learns HOW on a live UI
        ▼
Artifact Compiler
        │
        ▼
capabilities/<app>/<capability_id>.json
        │
        ▼
Capability Catalog exposes artifact metadata
```

Discovery is the only CUA component that reasons over the UI with an LLM.

### 2.2 Production invocation path

```text
User / business workflow
        │ natural-language intent
        ▼
Upstream Agent / Orchestrator      ← LLM may exist here
        │ reads Capability Catalog
        │ chooses app + capability + args
        ▼
CUA Replay Engine
        │ 0 LLM decisions
        ▼
Selected Capability Artifact
        │
        ▼
Legacy UI
```

Replay never converts natural language into an artifact name.

---

## 3. Target, Goal, Capability ID, Catalog, Artifact

### 3.1 Target app = WHERE

Example:

```text
mockbank
```

`config/apps/mockbank.yaml` supplies entry URL and authored policy/runtime rules.

### 3.2 Goal = WHAT Discovery should accomplish

Example:

```text
Look up the member identified by {member_id} and read the current savings balance.
```

The goal contains placeholders, not substituted sensitive values. The model prompt receives the goal plus parameter **names/types**, not raw sensitive parameter values.

### 3.3 Capability ID = stable machine identity

Example:

```text
lookup_member_balance
```

Creation command:

```bash
cua discover \
  --app mockbank \
  --capability lookup_member_balance \
  --goal "Look up the member identified by {member_id} and read the current savings balance" \
  --param member_id:string=12345 \
  --max-steps 12 \
  --timeout 60
```

Rules:

- ID matches `[a-z][a-z0-9_]*`;
- author supplies it;
- model does not invent it;
- it deterministically selects the artifact path:

```text
capabilities/mockbank/lookup_member_balance.json
```

### 3.4 Capability Catalog = lightweight metadata view

`catalog.py` scans capability artifacts and exposes only metadata:

```text
app
capability_id
description
revision
input names/types
output names/types
```

CLI:

```bash
cua capabilities list
cua capabilities list --app mockbank
cua capabilities show --app mockbank --capability lookup_member_balance
```

There is no second capability-definition registry.

> **Artifact = source of truth. Catalog = metadata projection.**

### 3.5 Artifact = frozen executable HOW

```text
Target tells WHERE.
Goal tells Discovery WHAT.
Capability ID gives stable identity.
Discovery learns HOW.
Artifact freezes HOW.
Catalog exposes available capabilities.
Upstream Agent selects one.
Replay executes HOW with 0 LLM decisions.
```

---

## 4. Demo target: MockBank

Build a local synthetic banking application:

```text
Members
  → enter Member ID
  → Search
  → Member Detail
      Member ID | <id>
      Savings   | <balance>
```

The Member Detail page **must** render a structural table row `Member ID | <id>` as well as the Savings row. This lets final success bind to the invoked member through a structural locator without persisting the member ID as locator identity.

Legacy characteristics:

- frameset/iframe;
- table-based layout;
- server-rendered pages;
- no test IDs;
- weak semantics in some places.

Use synthetic data only:

```text
12345 → valid member + known savings balance
99999 → No member found
```

### 4.1 Search transport

Member search uses **POST**, not a member ID in the URL. Sensitive values must not leak into URLs, frame URLs, event logs, or evidence filenames.

### 4.2 Fault control

`/settings` is an out-of-band human/test page and is never automation-allowed.

Fault state is:

- process-wide server memory;
- one-shot;
- consumed when the application applies it.

Required faults:

#### `interstitial=once`

The POST search returns a server-rendered intermediate page:

```text
System notice
[Continue]
```

Clicking Continue proceeds to Member Detail.

#### `unknown=once`

The POST search returns a different intermediate page:

```text
Supervisor override required
[Acknowledge]
```

Clicking Acknowledge proceeds to Member Detail.

This control/state is not part of the artifact's known recovery rules. Replay must become stuck and use HITL when headed.

Do not implement these as overlays on top of Member Detail; otherwise hidden/underlying success text could incorrectly satisfy conditions.

### 4.3 Irreversible control

Include a visible `Close Account` button only to prove Policy blocks irreversible actions.

---

## 5. Project structure

```text
cua-takehome/
├── README.md
├── REPORT.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── docs/
│   └── spec.md
├── config/
│   └── apps/
│       └── mockbank.yaml
├── mockbank/
│   ├── app.py
│   ├── faults.py
│   └── templates/
├── src/cua/
│   ├── models.py
│   ├── surface.py
│   ├── playwright_surface.py
│   ├── snapshot.js
│   ├── policy.py
│   ├── conditions.py
│   ├── discovery.py
│   ├── compiler.py
│   ├── catalog.py
│   ├── resolver.py
│   ├── replay.py
│   ├── hitl.py
│   ├── evidence.py
│   └── cli.py
├── capabilities/
│   └── mockbank/
│       └── lookup_member_balance.json
├── evidence/
│   ├── README.md
│   └── mockbank/
│       └── lookup_member_balance/
│           ├── discovery/
│           ├── replay-success/
│           ├── replay-notfound/
│           ├── replay-interstitial/
│           └── replay-hitl/
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

There is no `runs/` history subsystem.

`docs/spec.md` is a neutral design/implementation contract and may be included in the public repository. The Claude Code phased prompt file is a development aid and is not a submission deliverable.

---

## 6. Surface, Observation, refs, and labels

### 6.1 Surface protocol

Use the **sync Playwright API** in a single process.

```python
class Surface(Protocol):
    def observe(self) -> Observation: ...
    def act(self, action: RuntimeAction, authorization: PolicyDecision) -> ActionResult: ...
    def screenshot(self) -> bytes: ...
```

Core engine code does not expose Playwright-specific objects.

### 6.2 Observation

One Observation merges all frames and contains:

```text
top-level URL
frame_path → frame URL
controls[]
```

Each Control contains:

```text
ref                    # ephemeral; current Observation only
role
name
label
text
input_value
safe attrs
href                   # policy metadata only; never a locator candidate
frame_path
ancestor_roles
page-relative bbox
table_index | null
row_index   | null
col_index   | null
```

`snapshot.js` must include visible static text-bearing elements needed by the condition evaluator (for example table cells, headings, and paragraphs) and must skip `display:none`/non-visible content.

For this demo, `text_visible{text}` means: after whitespace normalization, at least one Control in any frame has `text` or `name` containing the requested string. `text_absent{text}` is its negation. Condition evaluation must remain pure over `Observation`; it must not fall back to Playwright `get_by_text` calls.

Safe locator attrs are only:

```text
id
name
type
aria-label
aria-labelledby
```

Do not implement `data-testid` support for this demo.

`href` is separate because it can be dynamic, sensitive, or tenant-specific. Policy may inspect it before a link click, but Compiler never freezes it as a locator candidate.

### 6.3 Frames

`PlaywrightSurface` iterates `page.frames`, evaluates `snapshot.js` in each frame, and merges controls into one Observation.

All recorded bboxes are converted by PlaywrightSurface to **page-relative coordinates** so screenshot masking does not need frame-specific logic.

All frame URLs are available to Policy and condition evaluation. Top-level URL alone is insufficient for frameset apps.

### 6.4 Ephemeral refs without DOM mutation

Do not inject `data-cua-ref` into the page DOM.

Within each frame, `snapshot.js` may keep current-observation element handles in an in-page structure such as:

```text
window.__cuaRefs[]
```

`ref` is valid only for that Observation. `Surface.act` resolves the current ref back to the element. Stale refs are not guessed through.

### 6.5 Label derivation

For legacy controls, derive a label in this order:

1. `<label for="...">`;
2. wrapping `<label>`;
3. `aria-label` / `aria-labelledby`;
4. left-adjacent table-cell text when the control is in a table layout.

This rule is deterministic and is part of the legacy-UI robustness story.

---

## 7. Policy and action boundary

Every automated physical action crosses exactly one boundary:

```text
proposed action
    ↓
Policy.check(...)
    ↓
PolicyDecision
    ↓
Surface.act(... authorization=decision)
```

No automated code path may call Playwright click/fill/read directly outside `Surface.act`.

`Surface.act` also checks RunControl ownership. Automated actions are allowed only while owner is `AUTOMATION`.

### 7.1 App config

`config/apps/mockbank.yaml` contains authored app/runtime material only:

```yaml
app: mockbank
entry_url: http://localhost:8000/

allowlist:
  origin: http://localhost:8000
  allowed_routes:
    - /
    - /members
    - /member
    - /notice
    - /override
  denied_routes:
    - /settings

allowed_actions:
  - navigate
  - click
  - fill
  - read

risk_rules:
  - match:
      role: button
      name: Close Account
    effect: irreversible
    decision: block

business_outcomes:
  - code: MEMBER_NOT_FOUND
    when:
      kind: text_visible
      text: No member found

recoveries:
  - code: DISMISS_SYSTEM_NOTICE
    when:
      kind: text_visible
      text: System notice
    action:
      kind: click
    target:
      locators:
        - kind: role_name
          role: button
          name: Continue
```

### 7.2 Frame-aware allowlist

Policy checks the URL of **every frame** in the current Observation. A denied or out-of-origin frame blocks automated action.

For link clicks, if the Control exposes `href`, Policy preflights the destination before click. `href` is not a locator identity.

Denied rules win over allowed rules.

`POLICY_BLOCKED` is a direct failure and is never escalated to HITL.

---

## 8. Discovery

Discovery is the only model-decision loop inside CUA.

```text
observe
  ↓
LLM decide
  ↓
validate tool proposal
  ↓
Policy
  ↓
Surface.act
  ↓
verify / observe
  ↓
repeat / done / give_up
```

### 8.1 Discovery CLI

```bash
cua discover \
  --app mockbank \
  --capability lookup_member_balance \
  --goal "Look up the member identified by {member_id} and read the current savings balance" \
  --param member_id:string=12345 \
  --max-steps 12 \
  --timeout 60
```

Rules:

- `{member_id}` is a parameter reference;
- raw runtime value is not substituted into persisted goal/prompt text;
- model prompt includes parameter name/type, not the raw sensitive value;
- `--param name:type=value` defines typed input contract and runtime binding;
- demo banking params are sensitive by default;
- undeclared placeholders are rejected before browser/model startup.

### 8.2 Model configuration

Use environment variables:

```text
ANTHROPIC_API_KEY
CUA_MODEL
```

`CUA_MODEL` is the model identifier used by the run. `meta.json` records the actual model identifier.

### 8.3 Discovery tools

Expose only:

```text
click_ref(ref, expect, reason)
fill_ref(ref, from_param, reason)
read_ref(ref, capture_as, parser, reason)
done(condition, reason)
give_up(reason_code, reason)
```

`reason` is a concise action justification, max 120 characters. It is not chain-of-thought.

`give_up.reason_code` is one of:

```text
goal_unreachable
blocked_by_unknown_state
missing_information
```

Only parser required by the demo is:

```text
money
```

Do not implement coordinate clicking in the core path.

### 8.4 Automatic postconditions

Reduce model burden:

- `fill_ref(ref, from_param="member_id")` automatically gets `value_equals_parameter(target, "member_id")` as its postcondition;
- successful `read_ref(... capture_as="savings_balance", parser="money")` automatically gets `output_present("savings_balance")`;
- only `click_ref` requires the model to supply `expect`.

`done(condition)` is accepted only after runtime also ANDs the condition with `output_present` for every declared/captured output required by the goal.

### 8.5 Model-proposed condition validation

The model-facing condition vocabulary is intentionally smaller than the artifact/runtime vocabulary. The model may propose only:

```text
text_visible
text_absent
url_matches
all
```

`value_equals_parameter` and `output_present` are created by runtime/compiler code and are never proposed by the model.

Before a model proposal is executed/accepted:

- every string field in a model-proposed `click_ref.expect` or `done.condition` is checked against current known sensitive runtime literals;
- an unsafe condition is rejected before the physical action is executed;
- a `done` condition must evaluate true against the current Observation before Done can be accepted;
- a rejected proposal is returned to the model with a short sanitized reason and consumes a model round for dead-end accounting.

This prevents a model from freezing invocation-specific conditions such as `Member 12345`.

### 8.6 Executed action recording

A physical action that reached `Surface.act` and executed is recorded even if its supplied `expect` later fails.

Example:

```text
click Search executed
expect was wrong
page changed
```

The DiscoveryRecord must represent that physical action as executed with `expect_verified=false`; it must not pretend the action never happened.

The current Observation and verification failure are returned to the model for the next decision.

Policy rejection during Discovery is conservative and bounded:

- first rejected proposal: do not execute it; return a short sanitized policy reason to the model;
- second policy-rejected proposal in the same Discovery run: stop with `policy_blocked`.

### 8.7 Stop reasons

```text
goal_completed
max_steps
timeout
dead_end
give_up
policy_blocked
```

Dead-end may be three consecutive model rounds with no executed progress and no meaningful Observation change.

### 8.8 Discovery HITL boundary

The shared `RunControl`, ownership enforcement in `Surface.act`, and `hitl.request_handoff()` seam are designed so Discovery could use the same handoff mechanism.

**This take-home does not wire human takeover into Discovery.** `give_up` ends Discovery.

Reason: human physical actions during authoring would not have model tool-call refs/locator evidence and therefore cannot be safely compiled into a deterministic artifact. A production extension would either re-discover/record those actions or reject compilation of trajectories containing unrecorded human actions.

This is an intentional cut, not a missing replay capability.

---

## 9. DiscoveryRecord, evidence, and sanitization

### 9.1 Runtime-only data

The process may hold in memory:

- raw parameter values;
- raw read values;
- raw semantic observations;
- raw screenshots;
- raw model response objects as needed for the current run.

None of these are persisted verbatim.

### 9.2 Global sanitizer

Implement one global helper conceptually equivalent to:

```python
sanitize(text, literals) -> str
```

It replaces known sensitive runtime literals with readable tokens such as:

```text
[REDACTED:member_id]
[REDACTED:savings_balance]
```

All persisted textual fields pass through it, including:

- reason strings;
- observed summaries;
- URLs/frame URLs;
- errors;
- event text;
- metadata text.

Structured sensitive values are preferably represented as parameter/output references rather than sanitized literals.

### 9.3 Persisted events

Example Fill event:

```json
{
  "actor": "model",
  "action": "fill",
  "reason": "Enter the member identifier for the lookup.",
  "value": {"kind": "parameter", "name": "member_id"},
  "result": "executed",
  "postcondition_verified": true
}
```

Example Read event:

```json
{
  "actor": "model",
  "action": "read",
  "reason": "Capture the current savings balance.",
  "capture_as": "savings_balance",
  "parser": "money",
  "value": null,
  "redacted": true,
  "result": "executed"
}
```

Do not persist model chain-of-thought or a full raw transcript.

For each real model call, evidence may record only audit metadata such as:

```text
message_id
model
input_tokens
output_tokens
```

### 9.4 Screenshots

Normal successful steps do not require screenshot persistence.

Failure/HITL screenshots are persisted only after opaque masking of known sensitive bboxes:

- sensitive input control bbox;
- sensitive output control bbox;
- any explicitly authored sensitive region.

Bboxes are already page-relative. Do not use OCR. Do not persist the raw screenshot alongside the masked copy.

---

## 10. Artifact schema — closed demo vocabulary

Implement only the kinds the demo uses. Do not add unused locator kinds, condition kinds, parsers, or action variants.

### 10.1 Inputs and outputs

```python
class InputSpec(BaseModel):
    type: Literal["string"]
    required: bool = True
    sensitive: bool = True

class OutputSpec(BaseModel):
    type: Literal["money"]
    from_step: str
    capture_as: str
    sensitive: bool = True
```

### 10.2 Locator types

```python
class LabelLocator(BaseModel):
    kind: Literal["label"]
    text: str

class RoleNameLocator(BaseModel):
    kind: Literal["role_name"]
    role: str
    name: str

class AttrLocator(BaseModel):
    kind: Literal["attr"]
    attr: Literal["id", "name"]
    value: str

class TableCellLocator(BaseModel):
    kind: Literal["table_cell"]
    row_anchor: str
    col_offset: int
```

`TableCellLocator` is structural. For Savings it identifies the value cell relative to a stable row anchor such as `Savings`; it never uses the current balance text as identity.

```python
Locator = LabelLocator | RoleNameLocator | AttrLocator | TableCellLocator

class Target(BaseModel):
    locators: list[Locator]
```

### 10.3 Conditions

Only:

```python
class TextVisible(BaseModel):
    kind: Literal["text_visible"]
    text: str

class TextAbsent(BaseModel):
    kind: Literal["text_absent"]
    text: str

class UrlMatches(BaseModel):
    kind: Literal["url_matches"]
    pattern: str

class ValueEqualsParameter(BaseModel):
    kind: Literal["value_equals_parameter"]
    target: Target
    param: str

class OutputPresent(BaseModel):
    kind: Literal["output_present"]
    name: str

class AllCondition(BaseModel):
    kind: Literal["all"]
    conditions: list[Condition]
```

`url_matches` evaluates across all frame URLs, not only the top-level page URL.

Do not implement `role_exists` or `dialog_visible`.

### 10.4 Actions, steps, and artifact

```python
class ParameterValue(BaseModel):
    kind: Literal["parameter"] = "parameter"
    name: str

class ClickAction(BaseModel):
    kind: Literal["click"]

class FillAction(BaseModel):
    kind: Literal["fill"]
    value: ParameterValue

class ReadAction(BaseModel):
    kind: Literal["read"]
    capture_as: str
    parser: Literal["money"]

Action = ClickAction | FillAction | ReadAction

class Step(BaseModel):
    id: str
    description: str
    action: Action
    target: Target | None
    postcondition: Condition | None

class BusinessOutcome(BaseModel):
    code: str
    when: Condition

class Recovery(BaseModel):
    code: str
    when: Condition
    action: Action
    target: Target | None

class CapabilityArtifact(BaseModel):
    schema_version: Literal["1.0"]
    revision: int
    capability_id: str
    app: str
    description: str
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    steps: list[Step]
    business_outcomes: list[BusinessOutcome]
    recoveries: list[Recovery]
    success: Condition
```

`revision` starts at `1`. No registry or auto-bump workflow exists.

The artifact contains **no Navigate step**. Session startup is a runtime concern: Replay/Discovery asks Policy to authorize one navigation to the selected app config's `entry_url`, then opens the app. The `navigate` entry in `allowed_actions` covers this startup navigation only. Keeping the entry URL in app config rather than the artifact allows the same capability artifact to be reused against a compatible tenant/base URL later.

`RuntimeAction` is not persisted. It is the executable form passed to `Surface.act` and is closed separately from artifact `Action`:

```text
RuntimeNavigate {kind=navigate, url=entry_url}       # startup only; no ref
RuntimeClick    {kind=click, ref}
RuntimeFill     {kind=fill, ref, value=<raw binding>}
RuntimeRead     {kind=read, ref}
```

Only the runtime Navigate variant can navigate; it never appears in `CapabilityArtifact.steps`. Raw Fill values remain memory-only.

---

## 11. Resolver and condition evaluation

### 11.1 Resolver is a pure function over Observation

```python
resolve(target: Target, observation: Observation) -> ControlRef | ResolveFailure
```

It does not call Playwright.

For each locator candidate in order:

```text
exactly 1 match → return that Control.ref
0 matches       → try next locator
>1 matches      → try next locator
all exhausted   → LOCATOR_NOT_FOUND
```

Discovery and Replay both act through the same path:

```text
Locator → Observation Control.ref → Policy → Surface.act(ref)
```

`TableCellLocator{row_anchor, col_offset}` is evaluated only from Observation table metadata: find a cell in a table row whose normalized `text` equals `row_anchor`, then select the cell in the same `table_index`/`row_index` at `anchor.col_index + col_offset`. The resolved target must still be unique.

No fuzzy locator drives an action.

### 11.2 One condition evaluator

```python
evaluate(condition, observation, bindings) -> bool
```

`value_equals_parameter` resolves its structural target, reads `Control.input_value` when present, otherwise `Control.text`, and compares that observed value with the runtime parameter binding. The parameter literal itself is never stored in the artifact.

`url_matches{pattern}` means `re.search(pattern, frame_url)` over every frame URL in the Observation; any matching frame satisfies the condition.

---

## 12. Compiler

Compiler runs immediately after successful Discovery while runtime bindings and the live DiscoveryRecord are still in memory.

```text
successful Discovery
      ↓
DiscoveryRecord
      ↓
ArtifactCompiler
      ↓
CapabilityArtifact
      ↓
capabilities/<app>/<capability_id>.json
```

### 12.1 Locator candidate generation

Candidate order:

```text
label
role_name
attr(id/name)
table_cell
```

Count exact matches across the entire multi-frame Observation.

A candidate is discarded **before** final leak scanning if any of its persisted fields contain a known sensitive runtime literal.

For sensitive output cells, do not generate identity from their current value text. Use a structural locator such as:

```json
{"kind":"table_cell","row_anchor":"Savings","col_offset":1}
```

### 12.2 Compile rules

1. Every actually executed physical Discovery action remains in the DiscoveryRecord, even when its model-supplied expect failed.
2. The successful final trajectory is compiled in physical order.
3. Ephemeral refs never enter the artifact.
4. Target locators are regenerated from the action's recorded pre-action Observation.
5. Only exact-unique, non-sensitive locator candidates may be frozen.
6. Fill from `member_id` becomes a `ParameterValue`.
7. Compiler automatically assigns Fill postcondition `value_equals_parameter(target, member_id)`.
8. Successful Read becomes typed `OutputSpec` and postcondition `output_present(savings_balance)`.
9. If an executed click's model-supplied expect verified, freeze that reusable expect as the step postcondition. If the expect failed, freeze the executed click with `postcondition: null`. Do not derive or invent a condition. Replay treats a null postcondition as satisfied; the next step's target-resolvability settle and the final success condition provide the guarantees.
10. Step descriptions are deterministic, e.g. `Fill Member ID with {member_id}` / `Click Search` / `Read Savings balance`.
11. Accepted Done contributes the final success condition and is ANDed with `output_present` for declared outputs.
12. If the final Observation contains a control whose value equals an invocation parameter and that control has a unique **non-sensitive structural locator**, Compiler also adds `value_equals_parameter(target, param)` to final success. For MockBank this anchors success to the same member without persisting the member ID.
13. App-authored business outcomes and known recoveries are copied into the artifact.
14. Serialize deterministically.
15. Scan the complete serialized artifact against current sensitive runtime literals. Any hit fails compilation.

There is no standalone historical compile command.

### 12.3 Golden artifact review rule

After the genuine Discovery emits the artifact, manually review its step list.

If the artifact contains redundant/unnecessary steps, **rerun Discovery** after fixing prompt/runtime behavior. Do not hand-edit the artifact to make the golden path look cleaner.

---

## 13. Capability Catalog

`catalog.py` is intentionally tiny.

It scans:

```text
capabilities/**/*.json
```

and validates each file as a `CapabilityArtifact`.

Catalog projection:

```json
{
  "app": "mockbank",
  "capability_id": "lookup_member_balance",
  "description": "Look up a member and return the current savings balance.",
  "revision": 1,
  "inputs": {"member_id": "string"},
  "outputs": {"savings_balance": "money"}
}
```

Do not include a separate `risk summary` field unless it actually exists in the artifact contract. For this take-home, omit it.

---

## 14. Replay

Primary CLI:

```bash
cua replay \
  --app mockbank \
  --capability lookup_member_balance \
  --param member_id=12345
```

Additional options:

```text
--headed
--evidence-dir PATH
```

Default evidence output is:

```text
.cua-out/<timestamp>/
```

`.cua-out/` is gitignored. README demo commands that intentionally create committed examples must pass explicit `--evidence-dir evidence/...` paths.

Replay resolves the artifact by exact `(app, capability_id)` and never reads natural-language intent.

### 14.1 Bounded settle constants

```text
STEP_TIMEOUT = 5s
POLL = 250ms
MAX_RECOVERIES_PER_STEP = 2
```

### 14.2 Core replay loop — normative pseudocode

```text
settle(step, ok) -> Advance | BusinessOutcome | Unresolved(code):
    # ok(obs) before act = target has exactly one resolvable locator
    # ok(obs) after act  = postcondition is true
    # target None is resolvable; postcondition None is true
    deadline = now + STEP_TIMEOUT
    used = 0
    loop:
        obs = observe()
        if bo := match_business_outcomes(obs):   return BusinessOutcome(bo)
        if ok(obs):                              return Advance
        if rec := match_recoveries(obs):
            if used == MAX_RECOVERIES_PER_STEP:  return Unresolved(RECOVERY_EXHAUSTED)
            Policy.check → Surface.act(rec.action)
            used += 1
            deadline = now + STEP_TIMEOUT
            continue
        if now >= deadline:                      return Unresolved(code)
        sleep(POLL)

for step in artifact.steps:
    settle(step, target_resolvable)      # Unresolved → LOCATOR_NOT_FOUND
    Policy.check → Surface.act(step)     # POLICY_BLOCKED = direct failure; never HITL
    settle(step, postcondition_true)     # Unresolved → POSTCONDITION_FAILED

settle(final, success_condition_true)    # Unresolved → FINAL_CHECKPOINT_FAILED
```

### 14.3 Recovery semantics

Recovery repairs the current state; it does not blindly replay the previous action.

After a recovery action, Replay immediately re-observes and re-runs the settle ordering:

```text
1. business outcome
2. current target/postcondition satisfied
3. known recovery
4. timeout → unresolved
```

A step action may be retried only through explicit operator `Retry`, or by future deterministic retry logic that first proves the target still resolves and the postcondition is still false. The default recovery path does not re-click the previous business action.

### 14.4 Detached/stale ref retry

If a ref becomes detached between Observation and `Surface.act`, Replay may perform exactly one bounded re-observe + re-resolve attempt before treating the step as unresolved. It must not reuse stale refs or loop indefinitely.

### 14.5 Escalation policy

Step-level unresolved codes eligible for HITL when headed:

```text
LOCATOR_NOT_FOUND
POSTCONDITION_FAILED
RECOVERY_EXHAUSTED
```

`POLICY_BLOCKED` is always an immediate `failure` and never escalates.

`FINAL_CHECKPOINT_FAILED` is a structured hard failure after final verification; it does not start another handoff loop.

For eligible step-level unresolved conditions:

```text
headed   → request_handoff(intervention)
             Continue = re-run settle; do not redo the business action
             Retry    = re-resolve/re-execute the step action, then settle
             Abort    → result kind aborted

headless → result kind failure
           escalation = "unavailable_headless"
```

This is the revalidation model: Continue is another `settle`, not a skip.

### 14.6 Result contract

Only four result kinds exist:

```text
success
business_outcome
failure
aborted
```

Examples:

```text
12345 → success + runtime savings_balance
99999 → business_outcome: MEMBER_NOT_FOUND
```

Recovery success is still `success`, with recovery events recorded in `events.jsonl`.

HITL completion is still `success`/`business_outcome`/`failure`/`aborted`; HITL is represented in ownership transition events.

Persisted sensitive outputs are redacted.

Minimal runtime result shape:

```text
ReplayResult:
  kind: success | business_outcome | failure | aborted
  outputs: {name: value}
  business_outcome: code | null
  failure:
    step_id: str | null
    code: str
    expected: Condition | null
    observed_summary: str
    escalation: "unavailable_headless" | null
    # failure is null when result kind is not failure
  llm_calls: 0
  recovery_count: int
```

Runtime `outputs` may contain raw values for the caller. In persisted `result.json`, a sensitive output is replaced by a structured marker such as `{"redacted": true, "type": "money"}`. Persisted `observed_summary` is sanitized.

---

## 15. HITL same-session handoff

### 15.1 RunControl

```text
AUTOMATION
→ NEEDS_HUMAN
→ HUMAN
→ AUTOMATION
→ COMPLETED
```

`Surface.act` rejects automated actions when owner is not `AUTOMATION`.

### 15.2 Operator seam

```python
class Operator(Protocol):
    def take_control(self, intervention: Intervention) -> OperatorDecision: ...
```

Implement:

```text
ConsoleOperator   # real demo
ScriptedOperator  # deterministic tests
```

### 15.3 Interactive headed mode vs test handoff

Real human takeover with `ConsoleOperator` requires a headed session:

```bash
cua replay ... --headed
```

For the CLI, `--headed` sets both `headless=False` and `handoff_enabled=True`. A normal headless CLI run has handoff disabled; if it becomes handoff-eligible stuck, it returns:

```text
escalation = unavailable_headless
```

The state machine is independently testable: integration tests may run a headless BrowserContext with `handoff_enabled=True` and `ScriptedOperator`. This exercises RunControl/Continue/Retry/Abort deterministically without opening a visible browser and is not presented as a real human interactive session.

### 15.4 Intervention record

Sanitized record contains:

```text
app
capability
current step
reason/error code
expected condition
compact observed summary
masked screenshot path
current owner
```

### 15.5 Same live session

Human uses the exact Playwright `BrowserContext` and page already used by Replay. Never create a fresh session for handoff.

### 15.6 Human event capture

Capture only:

```text
click
input/change occurred
navigation
```

Input/change values are always redacted.

Only interactions that occur while `RunControl.owner == HUMAN` are classified and persisted as human UI events.

Human event capture is **pull-based** so console input does not depend on async callback dispatch.

Implementation is not fixed to sessionStorage, but it must satisfy two requirements:

1. in-page human event data needed for evidence remains retrievable across same-origin document navigation (for example sessionStorage or a safe beforeunload transfer);
2. navigation events are also recorded from Python-side Playwright `page.on("framenavigated")`, so navigation evidence does not depend on the page JS buffer surviving.

### 15.7 Operator controls

```text
[R] Retry
[C] Continue
[A] Abort
```

Continue:

- does not repeat the prior business action;
- re-observes/re-runs settle for the current contract;
- if the human changed the invocation identity (for example navigated to a different member), the later parameter anchor/final success check fails with `FINAL_CHECKPOINT_FAILED`.

Retry:

- re-resolves the step target;
- re-executes the step action through Policy;
- then re-runs settle.

Abort returns result kind `aborted`.

---

## 16. Evidence

### 16.1 Local default

Normal developer runs write to:

```text
.cua-out/<timestamp>/
```

This directory is gitignored.

### 16.2 Committed reviewer evidence

```text
evidence/
└── mockbank/
    └── lookup_member_balance/
        ├── discovery/
        │   ├── meta.json
        │   ├── events.jsonl
        │   └── artifact.json
        ├── replay-success/
        │   ├── meta.json
        │   ├── events.jsonl
        │   └── result.json
        ├── replay-notfound/
        │   ├── meta.json
        │   ├── events.jsonl
        │   └── result.json
        ├── replay-interstitial/
        │   ├── meta.json
        │   ├── events.jsonl
        │   └── result.json
        └── replay-hitl/
            ├── meta.json
            ├── events.jsonl
            ├── intervention.json
            ├── human-events.jsonl
            ├── result.json
            └── masked.png
```

Discovery meta proves a real model call via:

```text
model
model_call_count
message_id(s)
usage token counts
stop_reason
```

Do not persist raw model transcript.

Replay evidence records zero LLM decision calls.

`replay-interstitial` proves deterministic recoverability. A hard failure is proven by tests using a temporary/tampered fixture artifact; it does not need a committed MockBank fault/evidence slot.

---

## 17. Tests and reproducibility

Do not maximize test count. Test load-bearing contracts.

### 17.1 Test server fixture

`tests/conftest.py` provides `mockbank_server`:

- starts uvicorn in-process/in a test-controlled thread;
- uses a random free port;
- shuts down cleanly;
- tests may override app config base URL via `MOCKBANK_BASE_URL` or by constructing an `AppConfig` with the fixture URL.

No test should require a manually started server.

### 17.2 No-key operation

All replay, compiler, catalog, policy, resolver, HITL, and ordinary tests must run without `ANTHROPIC_API_KEY`.

The genuine Anthropic integration test/run is skipped when the key is absent.

A reviewer can clone the repo and exercise deterministic Replay from the committed artifact without model credentials.

### 17.3 Unit contracts

At minimum cover:

```text
Observation merges all frames
refs are observation-scoped
page-relative frame bbox conversion
legacy label derivation order
href is policy metadata, not locator candidate
frame-aware allowlist blocks forbidden frame route
Policy blocks /settings
Policy blocks Close Account
Surface.act rejects missing/denied authorization
Resolver is pure/exact-only
condition evaluator works over all frames
sensitive locator candidate is discarded early
Savings compiles to structural table_cell locator
Fill auto-postcondition = value_equals_parameter
Read auto-postcondition = output_present
executed action remains recorded when expect fails
artifact contains no sensitive literal
catalog projects metadata only
screenshot mask covers sensitive bbox
```

### 17.4 Integration contracts

At minimum cover:

```text
fake-model Discovery can complete fixture task
real Discovery can complete when API key exists
successful Discovery emits deterministic artifact path
catalog lists emitted capability
replay success returns runtime balance
99999 returns MEMBER_NOT_FOUND business outcome
interstitial recovery succeeds without replaying Search blindly
recovery is bounded
hard failure is structured
Replay makes zero LLM calls
headed stuck Replay enters NEEDS_HUMAN
headless stuck Replay returns escalation=unavailable_headless
ScriptedOperator tests Continue/Retry/Abort without blocking pytest
same BrowserContext survives handoff
human input values never persist
human navigation is captured
wrong-member human navigation eventually yields FINAL_CHECKPOINT_FAILED
```

All tests write evidence to `tmp_path`; tests never overwrite committed `/evidence/`.

---

## 18. Implementation phases

### Phase 0 — Skeleton + MockBank

Build repo, POST search flow, legacy frames/table UI, process-wide one-shot fault pages, and test server fixture.

### Phase 1 — Surface + Observation + Resolver + Policy + Models

Build sync Surface seam, semantic snapshot, page-relative bboxes, deterministic label derivation, pure Resolver, closed Condition vocabulary, and Policy boundary.

### Phase 2 — Real LLM Discovery loop

Build direct Anthropic observe/decide/act loop, fake-model test seam, automatic Fill/Read postconditions, executed-action recording, sanitization, and lightweight model-call audit metadata.

Do not wire Discovery to HITL.

### Phase 3 — Artifact Compiler + Capability Catalog + golden Discovery

Compile immediate in-memory DiscoveryRecord into current artifact, enforce structural/sensitive locator rules, expose Catalog view, then run one real golden Discovery and manually review the emitted artifact. Rerun Discovery instead of hand-editing the artifact if the path is noisy.

### Phase 4 — Deterministic Replay + Runtime classification

Implement normative settle loop, bounded waiting, exact Resolver, success, MEMBER_NOT_FOUND, interstitial recovery, one structured hard-failure test, `--headed`, and `--evidence-dir`.

### Phase 5 — Same-session HITL

Implement Operator protocol, ConsoleOperator, ScriptedOperator, headed takeover, pull-based human event evidence, Continue/Retry/Abort semantics, and replay-hitl evidence.

### Phase 6 — Submission cleanup

Produce final README, seven-heading REPORT, committed evidence, literal scan, and no-key reproducibility checks.

---

## 19. README demo path

README should provide copy/paste commands. Suggested flow:

```bash
# Start MockBank
python -m mockbank.app

# Inspect current catalog
cua capabilities list --app mockbank

# Real LLM discovery; explicitly write reviewer evidence
cua discover \
  --app mockbank \
  --capability lookup_member_balance \
  --goal "Look up the member identified by {member_id} and read the current savings balance" \
  --param member_id:string=12345 \
  --max-steps 12 \
  --timeout 60 \
  --evidence-dir evidence/mockbank/lookup_member_balance/discovery

# Catalog now exposes the artifact
cua capabilities show \
  --app mockbank \
  --capability lookup_member_balance

# Replay success
cua replay \
  --app mockbank \
  --capability lookup_member_balance \
  --param member_id=12345 \
  --evidence-dir evidence/mockbank/lookup_member_balance/replay-success

# Replay expected business outcome
cua replay \
  --app mockbank \
  --capability lookup_member_balance \
  --param member_id=99999 \
  --evidence-dir evidence/mockbank/lookup_member_balance/replay-notfound

# Deterministic recovery: arm interstitial=once in /settings, then replay 12345
# with --evidence-dir .../replay-interstitial

# HITL: arm unknown=once in /settings, then run headed replay 12345
# with --headed --evidence-dir .../replay-hitl
```

Normal ad-hoc runs omit `--evidence-dir` and go to `.cua-out/<timestamp>/`.

---

## 20. REPORT requirements

Use exactly these seven headings:

1. Architecture
2. Artifact schema
3. Determinism & error handling
4. Heterogeneity & multi-tenant
5. Escalation & handoff
6. Safety
7. Cuts

Keep it about 1–3 pages.

Important points:

- Discovery is the only UI-reasoning LLM loop inside CUA.
- A future upstream product agent may use an LLM to select a capability from Catalog.
- Catalog is a metadata view over artifacts, not a second registry.
- Replay receives an already-selected capability and uses zero LLM decisions.
- Resolver is exact and Observation-based.
- Waiting/recovery are bounded.
- Business outcome, recovery event, HITL state, and final result kind are distinct concepts.
- Every automated action crosses Policy.
- Unknown state stops instead of being guessed through.
- Human takes over the same live session.
- Discovery uses the same ownership/handoff seam architecturally, but human authoring handoff is intentionally not wired because unrecorded human steps cannot safely compile.
- Sensitive values are runtime data, not persisted artifact/evidence data.
- Desktop/multi-tenant support exist only as clean design seams.

Cuts must explicitly mention:

```text
no production upstream routing agent
no Discovery HITL wiring
no historical run store
no historical recompilation
no approval/promotion lifecycle
no hash provenance chain
no automatic version registry
no desktop implementation
no production multi-tenant plumbing
no remote operator console
no open-ended Replay LLM fallback
```

---

## 21. Final acceptance checklist

```text
[ ] Windows setup instructions are correct
[ ] README setup includes `playwright install chromium` inside the project environment
[ ] Existing project .venv is sufficient; no global package install is required
[ ] MockBank runs locally
[ ] Tests start their own MockBank server on a random port
[ ] Replay/tests run without ANTHROPIC_API_KEY; only genuine Discovery requires it
[ ] CUA_MODEL selects the Anthropic model and meta records the actual value
[ ] Discovery uses a genuine LLM against live MockBank
[ ] Discovery receives explicit app + capability ID + goal + params
[ ] Goal/model prompt exposes parameter names/types, not raw sensitive values
[ ] Successful Discovery emits a typed artifact at deterministic app/capability path
[ ] Golden artifact was reviewed; noisy path was fixed by rerunning Discovery, not hand-editing JSON
[ ] Artifact contains schema_version, revision, typed IO, ordered steps, descriptions, targets, and success
[ ] Savings target uses structural table_cell locator, not balance text
[ ] Capability Catalog is derived from artifacts and has no duplicate registry
[ ] Resolver is pure over Observation and exact-only
[ ] Replay is invoked by exact app + capability ID
[ ] Replay contains zero LLM decisions
[ ] Replay uses bounded polling/waiting
[ ] Replay success returns declared runtime output
[ ] MEMBER_NOT_FOUND is business_outcome, not failure
[ ] Interstitial recovery succeeds without blindly redoing Search
[ ] Result kind remains one of success/business_outcome/failure/aborted
[ ] Recovery is represented as events; HITL is represented as RunControl state
[ ] At least one hard failure is structured/debuggable
[ ] /settings is blocked across frame-aware Policy checks
[ ] Close Account is blocked as irreversible
[ ] href is only Policy metadata and never a locator candidate
[ ] Artifact/evidence contain no raw member ID or balance
[ ] Failure/HITL screenshot is opaque-masked without OCR
[ ] Headed HITL uses the same live BrowserContext
[ ] Headless stuck run reports escalation=unavailable_headless
[ ] Human input values are redacted
[ ] Human navigation is preserved in evidence
[ ] Continue revalidates rather than repeating the action
[ ] Wrong-member human navigation is caught by parameter/final success anchor
[ ] Committed evidence contains discovery, replay-success, replay-notfound, replay-interstitial, replay-hitl
[ ] docs/spec.md is suitable for the public repo
[ ] REPORT uses exactly seven required headings
```

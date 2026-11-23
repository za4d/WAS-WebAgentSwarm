# WAS fork: Relay-oriented safety foundation (23 November 2026)

**Repository:** za4d/WAS-WebAgentSwarm (PUBLIC); original upstream fork of yanchun2530/WAS-WebAgentSwarm.
**Status:** TEST FOUNDATION ONLY — NOT LIVE READY.
**Stable main:** original upstream Git commit cf8341204ae59c9bf3a3487f06d2c672ce39334f.
**Development branch:** integration/relay-safety-20261123, created from that exact upstream commit.
**Licensing:** Preserve the existing upstream MIT LICENSE and copyright attribution.
**Authority:** Owner approved preparing this fork, branches and evidence. This change is confined to development docs/tests; no product source changes, merged code, browser action, session extraction, deployed service or new spend.

## Purpose and architectural boundary

This is a reusable upstream-derived **WAS component**, not the authoritative ChatGPT Relay application.
- WAS may own generic browser extension/bridge, role bindings, delivery adapter and local orchestration functions.
- The separately maintained private ChatGPT Relay product retains authoritative project work items, SQLite journal, concurrency permits and owner controls.
- Do not create a second competing source of truth for the same task or Send permit. Adapt existing WAS modules behind an explicit integration contract rather than duplicating Relay logic.
- Never copy a private Relay conversation, credential, token, real browser profile, local machine path or private repo data into this public fork.

## Initial pinned security baseline — direct offline source-function execution

**Source:** this fork main @ cf8341204ae59c9bf3a3487f06d2c672ce39334f.
**Git blobs:** browser-extension/content.js = f2968d5f803d7567c3de790254f6a5629f3b5bb1; browser-extension/background.js = 78209793d0d816ead221a13b0ad57b3f82028cff.
**Method:** fetch original source from connected GitHub, evaluate actual content listener and callContent function in synthetic Chrome/DOM. No live browser or network effect.
**Observed:** one simulated normal-send positive control, four reproduced weaknesses.

| Test ID | Acceptance | Observation | Product verdict |
|---|---|---|---|
| SEND-01 | Valid empty composer, good target -> one click | One simulated click, success reply | PASS [SIMULATED] |
| SEND-02 | Mismatched target must never be clicked | Wrong URL still received one simulated click and owner draft was replaced | FAIL [SIMULATED] |
| SEND-03 | Owner's non-empty draft must not be replaced | Draft overwritten and clicked | FAIL [SIMULATED] |
| SEND-04 | Navigation during editing must prevent click | Navigation changed before click; one simulated click anyway | FAIL [SIMULATED] |
| SEND-05 | Lost content-script reply must not replay a potentially effectful command | callContent invoked effectful sendMessage twice and reinjected the content script | FAIL [SIMULATED] |

The underlying source observations are in tests/relay-evaluation/baseline-20261123.json.
An *executable, source-independent* regression oracle is supplied in tests/relay-evaluation/send-safety.cjs.

**Executed test classification:** exact-source isolated JS simulation with 1 product PASS / 4 product FAIL. The new Node CLI file was syntax-parsed on authoring, but **was not executed as a standalone Node command** in this environment. Running it is the next reproducibility check; expect its process exit code to be nonzero while existing failures remain.

## Comparison with modified external fork

External candidate: MrWrecks/WAS-WebAgentSwarm, branch fix/extension-connection-scope, commit 7f96d4288cbfbed4188ae0f13d8334dd81b7833a (seven extra commits vs upstream). Synthetic source-function tests yielded three positive controls and two safety failures (owner-draft overwrite and ambiguous content-script repeat).
- tests/relay-evaluation/reference-mrwrecks-pinned.cjs — frozen test script originally prepared in Relay. It **tests that external pinned commit**, NOT this fork's checkout. The script itself was syntax-checked, but no standalone Node run is recorded.
- tests/relay-evaluation/reference-mrwrecks-results.json — actual prior observed five-case result, with explicit provenance.

Do not merge or cherry-pick the external branch wholesale. Independently evaluate pinned upstream and fork changes including their author-reported 123 Python tests, storage guards, command fingerprints, target URL and Windows lock semantics.

## Commands, gates and handling

From a local clone of this development branch, execute:

    node tests/relay-evaluation/send-safety.cjs

This runner emits structured JSON, checks current checkout browser extension functions and **exits 1 on any product failure**. A nonzero code is the correct initial failing baseline, not a broken test harness if five cases execute and observed outputs match above.

The independently pinned MrWrecks comparison can run if network access is available:

    node tests/relay-evaluation/reference-mrwrecks-pinned.cjs

Or supply the explicit local checkout of the pinned external revision as an argument. It is a reference probe only and currently asserts reproduction of known flaws; a successful runner exit is **not** a green product safety result.

When adding fixes:
1. Prevent physical effectful redispatch after lost/ambiguous content-script ACK. Read-only preflight reinjection is different from blind effectful reinjection.
2. Require an empty composer and stable intended conversation URL before editing and before clicking; fail closed if draft or target changes. Validate exactly one authorised attempt and durable journal boundary before final click when integrated.
3. Run the current-source safety script; add direct tests for command-ID idempotency, ambiguous storage, two extension sessions, owner Pause/Stop and a real Relay journal-generated ID (contains a colon). Do not replace those integration tests with synthetic-only assertions.
4. Use independent security review of actual modified source and original test receipts. Only afterwards consider signed-in browser tests with explicit provider and owner eligibility approval.

## Operational stop rules

Do not run any real browser Send, automate extraction of private conversations, grant wide MCP tools, expose the localhost bridge, install persistent services, spend API credits, deploy, merge changes into main or touch the private Relay product as part of this setup. Unknown Send result => HELD; no blind retry. Browser click ACK is never model completion proof.

**Next actionable work:** independently run the new Node safety runner and preserve its full JSON + exit status; implement and independently review targeted Send fixes on an isolated fix branch; then validate WAS adapter + actual Relay journal integration. Only then pursue a substantive three-agent Web coding/test/review/correction run.

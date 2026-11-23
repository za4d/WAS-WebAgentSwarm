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


## Executed Wave 1 — 23 November 2026 (append-only status correction)

The standalone command-line test runners were **subsequently executed** on GitHub Actions. The earlier "NOT RUN" statements above describe their status when the initial report was authored, not their present status.

At [source commit 2913e492f676251d0f8b7a60d6e0ca1bbfb3c7c9](https://github.com/za4d/WAS-WebAgentSwarm/commit/2913e492f676251d0f8b7a60d6e0ca1bbfb3c7c9), the [dedicated Wave 1 run](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45195784522) produced:

- \`tests/relay-evaluation/send-safety.cjs\` — Node 22 executed the **actual checkout** script and recorded **1 PASS, 4 FAIL**, exit **1**, source hashes and individual synthetic click/effect counts. This is a product **FAIL**, not an omitted test. [Raw output artifact](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45195784522/artifacts/14827832067).
- \`tests/test_wave1_management.py\` — Python 3.13 executed four disposable **actual DurableStore** tests, **4/4 PASS**, exit **0**: dependency persistence, resource exclusion plus disjoint-lane progress, lease retry, simultaneous competing claims. [Raw log artifact](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45195784522/artifacts/14829075523).

An initial unrelated CI release-hygiene false positive was eliminated solely by renaming a test-artifact filename in the workflow (commit \`21db5d126001a72a00e4461b05ee9344a1cd7aa7\`). The unchanged release scanner then passed. Both [standard push CI](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45196055742) and [standard PR CI](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45196071837) **PASSED all seven jobs**, covering the bridge and six Python OS/version combinations. The standard Python suite now runs 99 tests (95 inherited + four new) and the checked job logs report successful completion. The [separate Send-safety run](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45196055687) remains **failing correctly**, until browser safety is fixed.

**Reuse decision at this boundary:** stock WAS task/dependency/lease/resource coordination merits a focused integration study; no independent live Web worker scheduling was tested and the authoritative Relay journal is not replaced. Stock WAS browser Send safety remains a **blocking failure**. Do not merge this test-only branch as a release or start unattended browser effects.


## Cross-platform lock comparison — 23 November 2026

**Separate deep-dive [GitHub Actions run #45197434848](https://github.com/za4d/WAS-WebAgentSwarm/actions/runs/45197434848)**, harness source SHA \`62b205ad39af5931e77892ea52590d2735a16792\`. The new reusable \`tests/relay-evaluation/lock-age-fault.py\` preserves an open local store writer lock, artificially ages the file by 40 seconds and checks whether another writer enters. The same runner was pointed at the exact public candidate revision \`MrWrecks/WAS-WebAgentSwarm@7f96d4288cbfbed4188ae0f13d8334dd81b7833a\` via a separate read-only checkout. No code merged.

| Source | Ubuntu | Windows |
|---|---|---|
| Original WAS (unchanged source) | **FAIL** — second writer entered | PASS — contender blocked |
| Pinned MrWrecks OS-held locking | PASS — contender blocked | PASS — contender blocked |

All four subprocess results and zero/nonzero exit codes were preserved as separate GitHub Actions artefacts under that run. **This proves only the injected lock-age scenario.** It does not prove the stock Windows implementation unsafe in this case, nor the candidate fully crash-safe, nor any real ChatGPT browser behaviour. The existing successful standard Python/bridge CI remains distinct from the intentionally failing stock lock and Send-safety checks.

**Implication:** treat OS-held lock acquisition from the external fork as a *focused, still-unmerged correction candidate*. Independently assess process death, multiple processes, stale files and writer recovery before merging. Current \`main\` remains the pristine original WAS baseline.

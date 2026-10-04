# Factory

## Overview

The factory consists of three independent agent seats in Band Desktop, working in one shared
Band room:

| Seat | Role | Mandate |
|---|---|---|
| **Tom** | Implementation: reads the spec, implements, writes and runs tests, fixes findings | [`mandates/tom.md`](mandates/tom.md) |
| **Jerry** | Verification / review: independent adversarial review (edge cases, failure paths, concurrency), reproduces defects, re-tests fixes | [`mandates/jerry.md`](mandates/jerry.md) |
| **Spike** | Verification / delivery review: clean-checkout builds, offline containers, cross-stage compatibility, harness and packaging gates | [`mandates/spike.md`](mandates/spike.md) |

The separation is deliberate. The seat that writes code is never the only seat that judges
it, and each reviewer inspects the repository and reruns checks itself rather than trusting
another seat's report.

## Seat Setup

To stand up an equivalent factory:

1. Install Band Desktop and sign in with your own account.
2. Create three agent seats, each with its own seat identity, named after the mandate files
   (`Tom`, `Jerry`, `Spike`). Mandate files must be named after the seat as the room shows it.
3. For each seat, choose a harness (e.g. Claude Code, Codex, OpenCode) and a model. Record
   both as the first two lines of that seat's mandate:
   ```text
   Harness: <harness as Band Desktop shows it>
   Model: <exact model id>
   ```
4. Use the body of `mandates/<seat>.md` as the seat's standing instructions.
5. Give the implementer seat write access to the result repository. Reviewers need read
   access and the ability to run builds and tests.
6. Configure a Git name and email per committing seat.

No account information, keys or tokens are part of this repository. Each team supplies its own.

## Mandates

Mandates are **generic standing instructions**. They say what a seat owns, how it hands off,
how it reports evidence and when it rejects work. They contain no endpoints, field names,
error codes or other track detail, so the same three seats can be pointed at a different
specification unchanged.

All track-specific work belongs in the **stage tasks** dispatched to the room. Each stage
task names that stage's spec and the target folder.

## Stage Pipeline

```text
spec stage-1 ──► stage-1/ ──copy+extend──► stage-2/ ──copy+extend──► stage-3/ ──copy+extend──► stage-4/
```

- **Stage 1:** core HTTP API: auth, payments, requests, splits, feed, settlements,
  idempotency, export/import.
- **Stage 2:** browser UI and authorizations/captures (holds).
- **Stage 3:** historical balances, statements with snapshots, payment corrections.
- **Stage 4:** refunds and operator correction batches.

Each stage starts as a copy of the previous folder and is extended in place. Every folder
stays a complete, standalone service for its own stage only. Regression compatibility is
required: a stage must still satisfy every earlier stage, and must import the exports
produced by the earlier stages' services.

Git history follows the pipeline: skeleton → Stage 1 → Stage 2 → Stage 3 → Stage 4 →
submission finalization.

## Collaboration Model

The workflow used during development:

1. **Implementation:** a stage task is dispatched to the room. Tom reads the spec, copies the
   previous folder forward, implements the stage, adds tests, runs them, commits, and posts
   the revision with evidence (commands and output).
2. **Independent review:** Jerry reviews the committed work against the spec, writes or runs
   adversarial tests, and reports findings with severity and reproduction steps.
3. **Defect reproduction:** a finding is reported only with a concrete repro, such as a
   request sequence, a failing test or a container command.
4. **Fix:** Tom fixes each finding, or explains why it is not a defect, and adds a test that
   would have caught it.
5. **Re-verification:** the reviewing seat reruns the repro and the suites against the fix.
6. **Final packaging:** Spike verifies the delivery from a fresh clone: Docker build,
   offline container, event harness, cross-stage import, and the submission gates.

All handoffs happen in the Band room by `@handle`. During development the owner dispatched
stage tasks and follow-up instructions. This document does not claim that every stage was
completed without human steering.

## Quality Gates

Development validation results reported during the run. These are **not** official judging
results; the judges' suite is larger than the shipped checks.

| Gate | Result |
|---|---|
| Stage 1 own test suite (at `6c44c8e`) | 206 passed |
| Stage 2 own test suite (at `6c44c8e`) | 94 passed |
| Stage 3 own test suite (at `6c44c8e`) | 125 passed |
| Stage 4 own test suite (at `6c44c8e`) | 144 passed |
| Event harness (`harness run --all`, host mode, fresh clone at Stage 4 commit) | Every folder claimed its own stage on the shipped checks. Stage 4 passed suites 1–4 (147/147, 35/35, 6/6, 5/5); each lower folder failed the next suite, as required; upgrade sources 1→2, 2→3 and 3→4 passed |
| Event harness re-run at `6c44c8e` (`harness run --stage 4`, host mode) | Stage 4 passed suites 1–4 (147/147, 35/35, 6/6, 5/5) |
| `harness check --track pocketful` at `6c44c8e` | Exit 0: gates 1, 2 and the mandate part of gate 4 pass (gate 3 is not covered by `check`; see the Windows note in `README.md`) |
| Final hardening (`c2367eb` … `6c44c8e`) | HTTP method handling, security headers and CSP; malformed `Content-Length` and deep-JSON bodies answered with 400; login/signup hashing outside the service lock; concurrent fixture password hashing on reset; UI focus, network-error, list-state and safe `?next=` fixes. Each commit added regression tests |
| Docker / offline | Stage 4 image built from a fresh clone; ran with `--network none --cpus 2 --memory 2g`; `/health`, UI and assets served; non-root user |
| 50-request concurrency | 50 simultaneous requests all complete (`tests/test_transport_limits.py`, repeated bursts); parallel same-key retries return one 201 and identical 200s; concurrent corrections on one revision commit once |
| Large export/import | An export above 2 MB imports unchanged (`tests/test_transport_limits.py`) |

## Bad Work Caught and Recovery

### 1. Connection backlog under a 50-request burst

- **Symptom:** Jerry's adversarial audit of the committed Stage 4 image released 50
  `POST /payments` at once behind a barrier. Inside the Linux container, 3 rounds gave
  {201: 48, `ConnectionResetError`: 2}, {201: 46, reset: 4} and {201: 47, reset: 3}. From the
  host through the port mapping, each round lost one request to `RemoteDisconnected`. Money
  integrity held: the dropped requests committed nothing. On Windows, a stage service run
  directly showed the related behaviour: connections refused (`WinError 10061`) under the same
  kind of burst while the server stayed healthy.
- **Cause:** Python's `ThreadingHTTPServer` defaults to a listen backlog of 5. The spec
  requires up to 50 concurrent requests.
- **Control:** the identical `app.py` with `request_queue_size = 128`, run in the same
  container, gave {201: 50} in all 3 rounds. That isolates the cause.
- **Change:** every stage now uses a server subclass with `request_queue_size = 128`.
- **Re-test:** `test_fifty_simultaneous_requests_all_complete` fires barrier-synchronized
  bursts of 50 and repeats them, because a short backlog only drops part of a burst some of
  the time. It passes in all four stages, and all 50 requests complete.

### 2. Export larger than the 2 MB request-body limit

- **Symptom:** found by Spike's final delivery audit. All shipped harness checks passed, but
  a volume probe was then run against the Stage 4 container: about 3,000 payments produced a
  4.3 MB export, and posting it unchanged to `/_test/import` returned
  `400 malformed_request`. The spec requires import to accept any unchanged export.
- **Cause:** a generic 2,000,000-byte request-body cap applied to every route. It existed in
  all four stages; at roughly 1.4 KB per payment the threshold was about 1,400 payments.
- **Change:** ordinary routes keep the 2 MB cap. `/_test/import` uses a separate 512 MiB limit.
  The fix was applied in all four stage folders.
- **Re-test:** `test_export_larger_than_the_body_cap_imports_unchanged` builds an export
  over 2 MB and asserts it imports unchanged.

**Why the workflow mattered:** neither defect was visible to the shipped harness checks or
to the happy-path suites. Both were found by deliberate volume/concurrency probing during
verification, and both were handed back with a reproduction instead of being accepted
because the checks were green.

## Measured Factory Activity

### Band Desktop Analytics snapshot (development period)

| Metric | Value |
|---|---|
| Local agents | 3 |
| Chat rooms | 1 |
| Executions | 3,920 |
| Activity / events shown | 4,060 |
| Sent | 140 |
| Received | 4,979 |

| Seat | Messages | Tool calls | Errors |
|---|---|---|---|
| Jerry | 1,215 | 394 | 2 |
| Spike | 393 | 150 | 0 |
| Tom | 2,312 | 779 | 3 |

These figures are a Band Analytics dashboard snapshot. They may reflect the dashboard's own
event and message accounting rather than billable model tokens.

### Model / provider usage

- Development used **two Codex Go accounts and one Claude Pro account**. Codex served earlier
  development activity; the room records a Stage 2 run interrupted by a Codex usage limit.
  The later and final seat configuration runs on Claude Code: every mandate in `mandates/`
  records `Harness: Claude Code` and `Model: claude-opus-5-5`.
- Observed Claude usage during the development period reached approximately **38% of the
  displayed 5-hour window** and **5% of the displayed weekly allowance**.

### Cost measurement

Monetary spend was not recorded as a reliable project-level figure. BAND Analytics was used
to measure factory activity, while provider dashboards were used for quota/usage observations.

## Room Evidence Guide

`room.json` is the full-session download of the Band room "Pocketful Factory", saved
unchanged as the participant guide requires. Nothing in it is filtered, reordered or edited.
It holds 4,400 messages from 2026-09-30 18:03 UTC to 2026-10-02 19:36 UTC, and Band exported it
at 2026-10-02 19:39 UTC.

**What the export contains**

| Message type | Count | What it is |
|---|---|---|
| `text` | 472 | Messages in the room: tasks, handoffs, findings, verdicts |
| `tool_call` / `tool_result` | 1,513 / 1,190 | The exact commands each seat ran, with their output (tests, Docker, probes) |
| `task` | 938 | Runtime events, mostly tool-permission requests and their resolution |
| `thought` | 278 | Seats' working notes |
| `error` | 8 | Runtime errors, including a Codex usage limit |
| `participant` | 1 | The current Spike seat joining |

| Sender | Messages |
|---|---|
| Tom | 1,872 |
| Unknown (first Spike session, see `mandates/unknown.md`) | 1,141 |
| Jerry | 934 |
| Spike | 425 |
| Owner (human) | 28 |

**How to find the evidence.** Filter `messageType == "text"` to read the conversation. Mentions
appear in `content` as `@[[<senderId>]]`. Jerry mentions Tom in 26 text messages and Tom
mentions Jerry in 20. Then search `content` for these terms:

| To find | Search for |
|---|---|
| Human steering | `senderType == "User"` (28 messages) |
| Review verdicts | `BLOCKING`, `NON-BLOCKING`, `VERIFIED`, `REJECT` in `text` messages. `APPROVE` also matches tool-permission events, so filter by type. |
| Reproductions | `repro`, and the `tool_call` / `tool_result` pairs that follow a finding |
| Connection-backlog defect | `ConnectionResetError`, `RemoteDisconnected`, `request_queue_size` |
| Large-import defect | `4.3 MB`, `512 MiB`, `malformed_request` |
| Docker / offline checks | `docker build`, `--network none` |
| Harness and delivery gates | `harness run`, `147/147`, `fresh clone` |
| Provider interruptions | `usage limit` |

**What it does not contain.** The export ends at 2026-10-02 19:39 UTC. Everything committed
after `55dd301`, from `c2367eb` onwards, happened after this export, outside this room, and is
**not** in this file. That includes the HTTP and UI hardening commits, the malformed-request
and deep-JSON fixes with Jerry's reject → fix → re-verify cycle, and the final
6c44c8e measurements. The commit messages and authorship in `git log` record that work.

## Reproducibility

1. **Clone** this repository.
2. **Inspect** `mandates/`: one generic mandate per seat.
3. **Configure three Band seats** (Tom, Jerry, Spike) as in *Seat Setup*, each with its
   mandate and its own harness/model.
4. **Create a room** in Band Desktop and add the three seats.
5. **Dispatch stage-specific tasks** to the room, one per stage. Each names the stage spec
   and the target folder, and asks for the previous folder to be copied forward and extended.
6. **Inspect the resulting repository:** one commit per stage, one `stage-N/` folder per
   stage, and the review/handoff trail in the room log.
7. **Run the generated services locally:** `cd stage-N && python app.py`, and
   `python -m pytest -q tests` (see `README.md`).
8. **Validate Docker/offline behaviour:** `docker build`, then `docker run --network none`
   and query `/health` from inside the container (see README → *Docker and offline validation*).

## Final Submission Artifacts

- Public GitHub repository (this one)
- `stage-1/`, `stage-2/`, `stage-3/`, `stage-4/`: each a buildable service with `Dockerfile` and `RUN.md`
- `mandates/`: `tom.md`, `jerry.md`, `spike.md`, and `unknown.md` (the first Spike session,
  which the room download names "Unknown")
- `FACTORY.md`: this document
- `README.md`: setup and usage guide
- `room.json`: the Band room log, downloaded unchanged from Band
- Video: the Band Desktop room (seats, handoffs) and an application walkthrough

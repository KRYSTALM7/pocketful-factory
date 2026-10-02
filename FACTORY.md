# Factory

## Structure

The factory is three Band Desktop seats working in one shared Band room, plus the human
owner who dispatches each stage task. Each seat has a mandate in `mandates/` naming its
harness and model and describing what it owns, how it hands off and when it rejects work.
The mandates are generic: they describe roles, not this track.

| Seat | Mandate | Responsibility |
|---|---|---|
| Tom | `mandates/tom.md` | Implementer. Reads the spec, implements, writes tests, runs them, commits, fixes review findings |
| Jerry | `mandates/jerry.md` | Verifier / reviewer. Independent adversarial review: edge cases, failure paths, concurrency; re-tests fixes |
| Spike | `mandates/spike.md` | Delivery / integration. Clean-checkout builds, container runs, cross-stage compatibility, harness and packaging gates |

## Stage progression

1. The owner dispatches a stage task in the room, pointing at that stage's spec.
2. Tom copies the previous `stage-(N-1)/` folder to `stage-N/` (stage 1 starts from scratch)
   and extends the copy, so every folder stays a complete, buildable service for its own
   stage only.
3. Tom runs the folder's tests and commits the stage (one commit per stage in history:
   Stage 1 → Stage 2 → Stage 3 → Stage 4).
4. Jerry reviews the committed stage, reports findings with repro steps, and re-tests fixes.
5. Spike verifies delivery: fresh clone, Docker build, offline container start, the
   project's tests, the event harness, and that exports from earlier stages import into
   the new one.

## Collaboration

- All handoffs happen in the room by `@handle`, carrying the committed revision and the
  evidence (commands run and their output).
- Review findings are classified as BLOCKING / NON-BLOCKING / VERIFIED. Blocking findings go
  back to the implementer and are re-checked after the fix.
- Seats do not rely on each other's reports: reviewers inspect the repository and run the
  checks themselves.

## Validation workflow

- Per-stage pytest suites exercise the service over HTTP (a local process, or an external
  URL via `POCKETFUL_BASE_URL`), including concurrency bursts and export/import between stages.
- `python -m harness run --track pocketful --repo <repo> --all` against a fresh clone.
- `docker build` plus `docker run --network none` with 2 CPU / 2 GiB to confirm the
  container needs no network at runtime.
- `python -m harness check --track pocketful <repo>` for the packaging gates.

## How bad work was caught

The final delivery audit (Spike) ran the official harness and its own probes against a
fresh clone. All shipped checks passed, but a volume probe showed that every stage rejected
importing its own export once the export exceeded the 2 MB request-body limit (around 1,400
payments). This was reported as blocking with a repro, handed back to Tom, and fixed in
all four stage folders, with a new `tests/test_transport_limits.py` in each.

## Costs

Measured wall-clock time and model spend were not recorded by the factory in this run.

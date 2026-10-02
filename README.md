# Pocketful Factory

Track: **Pocketful** (wallet and payments service), WeAreDevelopers Dark Factory hackathon.

A three-seat agent factory in Band Desktop built the Pocketful service one stage at a time.
Each stage folder holds the complete, standalone service for that stage.

## Repository layout

| Path | Contents |
|---|---|
| `FACTORY.md` | How the factory works: seats, stage progression, collaboration, validation |
| `mandates/` | One mandate per seat (`tom.md`, `jerry.md`, `spike.md`), each naming its harness and model |
| `room.json` | The Band room log for the run |
| `stage-1/` | Payments, requests, splits, activity feed, settlements, export/import (HTTP API) |
| `stage-2/` | Stage 1 + browser UI and payment authorizations/captures |
| `stage-3/` | Stage 2 + historical balances, statements with snapshots, payment corrections |
| `stage-4/` | Stage 3 + refunds and operator correction batches |

Each `stage-N/` contains a `Dockerfile`, a `RUN.md`, the service (`app.py`, Python standard
library only, in-memory state) and its tests. `stage-2/` onward also ship the static UI in `web/`.

## How to run

From any stage folder:

```sh
docker build -t pocketful-stage-N .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-N
```

The service listens on `0.0.0.0:$PORT` (default 8080), needs no runtime network access,
and answers `GET /health`. See each folder's `RUN.md` for details and local test commands.

## Agent team

| Seat | Role |
|---|---|
| Tom | Implementer: builds each stage from the spec, adds tests, fixes findings |
| Jerry | Verifier / reviewer: independent adversarial review and re-testing |
| Spike | Delivery / integration: clean-build, cross-stage and packaging verification |

## Validation summary

- Each stage folder ships its own pytest suite (`python -m pytest tests` inside the folder),
  run against the service over HTTP. Later stages' suites carry forward earlier stages'
  authorization, UI and history tests.
- The event harness (`python -m harness run --track pocketful --repo . --all`) was run
  against a fresh clone at commit `79d3034`: every folder claimed its own stage on the
  shipped checks, including the stage N-1 → N export/import upgrade checks.
- The final export/import body-size fix was made after that run; see `FACTORY.md`.

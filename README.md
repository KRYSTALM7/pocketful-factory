# Pocketful Factory

Submission for the WeAreDevelopers **Dark Factory** hackathon, track **Pocketful**
(a wallet and payments service).

## 1. Overview

This repository contains two things:

- **The factory:** three generic agent seats in Band Desktop (Tom, Jerry, Spike) that plan,
  implement, review and verify work in a shared room. See [`FACTORY.md`](FACTORY.md) and
  [`mandates/`](mandates/).
- **What it produced:** the Pocketful service, built one stage at a time. Each
  `stage-N/` folder is a complete, standalone service for that stage.

## 2. What the factory produced

A containerized HTTP service (Python 3.12, standard library only, in-memory state) with:

| Stage | Adds |
|---|---|
| 1 | Signup/login, payments, requests, bill splits, activity feed, atomic settlements, idempotent writes, export/import |
| 2 | Browser UI (static HTML/CSS/JS served by the service), payment authorizations and captures (holds) |
| 3 | Historical balances, paginated statements with snapshots, payment corrections with revision history |
| 4 | Refunds and operator correction batches (including whole-settlement corrections) |

## 3. Repository layout

```text
README.md        this guide
FACTORY.md       how the factory works, quality gates, measured activity
mandates/        one generic mandate per seat: tom.md, jerry.md, spike.md
room.json        Band room log of the run
stage-1/ .. stage-4/
  Dockerfile     runtime image (python:3.12-slim, non-root)
  RUN.md         per-stage build/run/test instructions
  app.py         the service
  web/           UI assets (stage 2 onward)
  tests/         pytest suite run over HTTP
  requirements-dev.txt   test-only dependencies
```

## 4. Factory / team overview

| Seat | Role |
|---|---|
| Tom | Implementer: builds each stage from the spec, writes tests, fixes findings |
| Jerry | Verifier / reviewer: independent adversarial review and re-testing |
| Spike | Delivery / integration: clean builds, containers, cross-stage checks, packaging |

## 5. Prerequisites

- Docker (to build and run a stage as it is judged)
- Python 3.12+ (only to run a stage locally or run its tests)
- For stage 2+ browser tests: Playwright Chromium (`python -m playwright install chromium`)

## 6. Run a stage locally (no Docker)

The service needs no third-party packages at runtime.

Linux/macOS:

```sh
cd stage-4
PORT=8080 python app.py
```

Windows (PowerShell):

```powershell
cd stage-4
$env:PORT = "8080"; python app.py
```

Then check `http://localhost:8080/health` returns `{"status": "ok"}`. In stage 2+, open
`http://localhost:8080/` in a browser after seeding data with `POST /_test/reset`.

## 7. Run the tests

From a stage folder (the suite starts its own server process):

```sh
python -m venv .venv
# Linux/macOS: . .venv/bin/activate     Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
python -m playwright install chromium   # stage 2+ only
python -m pytest -q tests
```

To run a suite against a server that is already running, set `POCKETFUL_BASE_URL`
(e.g. `POCKETFUL_BASE_URL=http://127.0.0.1:8080`).

## 8. Run with Docker

```sh
cd stage-4
docker build -t pocketful-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-4
```

Replace `4` with any stage number. `PORT` defaults to 8080.

## 9. Offline (no-network) validation

The runtime makes no outbound calls and bundles all UI assets.

```sh
docker run --rm -d --network none --cpus 2 --memory 2g -e PORT=8080 --name pf-offline pocketful-stage-4
docker exec pf-offline python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health').read())"
docker stop pf-offline
```

## 10. How the stages relate

`stage-2/` is `stage-1/` copied forward and extended, and so on up to `stage-4/`. Each folder
contains only its own stage's solution, must still satisfy every earlier stage's
requirements, and imports exports produced by the earlier stages' services.

## 11. Agent roles and mandates

Each seat's standing instructions are in `mandates/<seat>.md`. The first two lines name the
seat's harness and model. Mandates are generic: the track-specific work came from the stage
tasks dispatched to the room. See `FACTORY.md` → Mandates.

## 12. Development and validation workflow

Implement (Tom) → independent review (Jerry) → defect reproduction → fix (Tom) →
re-verification (Jerry/Spike) → delivery and packaging checks (Spike). The concrete gates and
results are in `FACTORY.md` → Quality Gates.

With the hackathon kickoff package checked out next to this repository, the event harness
can be run from `dark-factory-wearedevs/`:

```sh
python -m harness check --track pocketful <path-to-this-repo>
python -m harness run --track pocketful --repo <path-to-this-repo> --all
```

## 13. Submission artifacts

Public repository (this one): `stage-1/`–`stage-4/`, `mandates/`, `FACTORY.md`,
`README.md`, `room.json`, plus a video showing the Band Desktop room and an application
walkthrough.

## 14. Known limitations and reproducibility notes

- State is in memory: it does not survive a container restart. The spec does not require it to.
- All requests are serialized through one process-wide lock. This is simple and safe, but
  latency rises as history grows. Measured during development: about 3.3 s maximum for a
  correction at 50 in flight with roughly 3,200 payments, against a 5 s request limit.
- The harness's `--mode isolated` did not collect tests when run from a Windows host:
  it passed a Windows-style test path into a Linux container. Host mode, plus a manual
  `--network none` container check, was used instead.
- Shipped harness checks are only part of the judging suite. Passing them does not prove
  full compliance.

# Pocketful Stage 3

Stage 3 extends Stage 1 and Stage 2 with timestamped payment history, historical wallet
balances, paginated statement snapshots, immutable payment revisions and corrections.
It accepts export documents from the Stage 1 and Stage 2 services. The runtime uses only
Python's standard library and bundled HTML, CSS and JavaScript assets.

Build and run:

```sh
docker build -t pocketful-stage-3 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-3
```

The service listens on `0.0.0.0:$PORT` (default `8080`). No outbound access is required at
runtime. To check the isolated container:

```sh
docker run --rm --detach --network none --name pocketful-stage-3-offline -e PORT=8080 pocketful-stage-3
docker exec pocketful-stage-3-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
docker stop pocketful-stage-3-offline
```

For local development, run `python app.py` from this directory. Backend tests use:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

For browser tests, install the Chromium browser once with `python -m playwright install chromium`.
Pytest and Playwright are test-only dependencies and are not installed in the runtime image.

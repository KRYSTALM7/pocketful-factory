# Pocketful Stage 1

Build and start the standalone service:

```sh
docker build -t pocketful-stage-1 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-1
```

The service listens on `0.0.0.0:$PORT` (default `8080`). It uses only Python's standard
library and keeps state in memory. `GET /health` returns the readiness response.
`POST /_test/reset` initializes a fixture. The container needs no runtime network access.

For local development, run `python app.py` from this directory. Run the supplied HTTP tests with:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

Pytest is a test-only dependency and is not installed in the runtime image.

To verify with outbound networking disabled, run:

```sh
docker run --rm --detach --network none --name pocketful-offline -e PORT=8080 pocketful-stage-1
docker exec pocketful-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
docker stop pocketful-offline
```

The isolated `none` network intentionally does not publish host ports.

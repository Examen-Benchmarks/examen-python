# Durable benchmark uploads

`examen run` saves each completed invocation as plain JSON before reporting
progress or uploading it. Results contain the trace, metrics, version, and
parent references. By default, results stay local. Add `--upload` to deliver
them during execution:

```bash
export EXAMEN_URL=https://your-examen-server.example
export EXAMEN_API_KEY=your-api-key
examen run benchmarks/ --upload
```

Each result is saved before its upload is attempted. Retries are awaited during
execution, so a slow or unavailable backend can delay the run. Exhausted
delivery attempts leave saved results pending for later upload. See
[the CLI guide](cli.md) for authoring and execution examples.

## Independent delivery and recovery

```bash
export EXAMEN_API_KEY=...
examen upload ./results/<execution-id> --url https://examen.example
```

Upload an execution directory, a bench directory, its `manifest.json`, a `runs/`
directory, or one run JSON file. Run the same command again to resume delivery.
It does not import benchmark files or repeat any experiment/scorer calls.

Each invocation, including each repeat, receives a distinct UUID. The JSON
record contains `format_version`, `payload`, `destination`, `uploaded`, and
`last_error`. `destination` starts as null and is set before the first request.
An acknowledged upload sets `uploaded: true`; later uploads skip it. JSON files
are retained after success. No API key is stored in the record.

Transient transport errors, HTTP 408, 429, and 5xx responses receive up to 10
attempts by default, in rounds across pending runs. Exponential backoff includes
jitter and is capped at 30 seconds. On `examen upload`, use `--max-attempts` and
`--retry-delay` to configure retries. `examen run --upload` uses the defaults.
Other HTTP errors remain pending without repeated requests.
An exhausted upload reports the errors and exits with status 1. The results
remain available for another attempt; successful files are not resent.

Files become bound to the chosen server URL before the first upload attempt,
including failed/cancelled attempts, so replaying them against a different URL
is rejected. When rotating credentials, use an API key for the same account.
The server rejects IDs belonging to different parents or another account.

Writes flush the temporary file, atomically replace the destination, then flush
the directory entry. Filesystem errors still raise: persistence cannot succeed
when storage is unavailable. Ctrl+C during upload leaves unacknowledged files
pending. This protects completed results, not unfinished experiment calls.

## Programmatic uploader

```python
from examen.upload import Uploader

uploader = Uploader(base_url="https://examen.example", api_key=api_key)
result = await uploader.upload("./results/<execution-id>")
print(result.uploaded, result.pending, result.errors)
```

The uploader additionally accepts `max_retry_delay` and request `timeout`.
It shares the artifact format with the SDK and has no benchmark-execution API.

## Server requirement

Update the backend before uploading. Ingestion accepts optional `run.id`, using
its existing UUID primary key. A replay under the same resolved parents returns
the original run without inserting metrics again; concurrent deliveries are
handled by the database. Reusing an ID under different parents returns a
conflict. Requests without an ID retain their original behavior. No schema
migration is needed.

The uploader marks a record delivered only when the server acknowledges its
stored run ID. Older servers returning a different ID leave the record pending
and report that an upgrade is needed. Such a server may already have created a
run under another ID; reconcile that upload before replaying it.

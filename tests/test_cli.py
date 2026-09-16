"""Real command-line tests: discovery, execution, and independent delivery."""

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from examen.artifacts import save_run
from examen.cli.main import app, main

BENCHMARK = """
from __future__ import annotations
import asyncio
from pathlib import Path
from pydantic import BaseModel
from examen import AsyncBench, Ref, Case, Collection, ExactMatchScorer

class Input(BaseModel):
    value: int

class Output(BaseModel):
    value: int

bench = AsyncBench(project=Ref(key="project", name="Project"), bench=Ref(key="suite", name="Suite"))
regression = Collection(key="regression", name="Regression")
bench.include(regression)
cases = [Case[Input, Output](key="one", name="One", input=Input(value=1), output=Output(value=1), repeats=2)]

@bench.experiment[Input, Output](key="smoke", name="Smoke", cases=cases, scorers=[ExactMatchScorer[Input, Output]()])
async def smoke(input: Input) -> Output:
    with Path("executed").open("a") as stream:
        stream.write("smoke\\n")
    return Output(value=input.value)

@regression.experiment[Input, Output](key="accuracy", name="Accuracy", cases=cases, scorers=[ExactMatchScorer[Input, Output]()])
async def accuracy(input: Input) -> Output:
    with Path("executed").open("a") as stream:
        stream.write("accuracy\\n")
    return Output(value=input.value)
"""


def run_cli(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("EXAMEN_API_KEY", None)
    environment.pop("EXAMEN_URL", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "examen", *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )


def write_bench(directory: Path, name: str = "sample_bench.py", source: str = BENCHMARK) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def saved_runs(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in directory.rglob("runs/*.json")]


def test_no_arg_discovery_list_and_filter_do_not_execute(tmp_path: Path) -> None:
    write_bench(tmp_path)
    write_bench(tmp_path, "unrelated.py", 'raise RuntimeError("should not load")')
    for folder in [".venv", "node_modules", "dist", ".examen"]:
        write_bench(tmp_path, f"{folder}/bad_bench.py", 'raise RuntimeError("should not load")')
    result = run_cli(tmp_path, "run", "--list", "--filter", "regression")
    assert result.returncode == 0, result.stderr
    assert "project/suite/regression/accuracy" in result.stdout
    assert "smoke" not in result.stdout
    assert not (tmp_path / "executed").exists()
    assert not list(tmp_path.rglob("manifest.json"))


def test_file_run_saves_plain_json_and_version_without_credentials(tmp_path: Path) -> None:
    write_bench(tmp_path, "arbitrary.py")
    result = run_cli(
        tmp_path,
        "run",
        "arbitrary.py",
        "--version",
        '{"model":"candidate","temperature":0.2}',
        "--concurrency",
        "2",
        "--output-dir",
        "results",
    )
    assert result.returncode == 0, result.stderr
    assert "[4/4]" in result.stdout
    assert "Upload later:" in result.stdout
    records = saved_runs(tmp_path / "results")
    assert len(records) == 4
    assert len({r["payload"]["run"]["id"] for r in records}) == 4
    assert all(r["destination"] is None and not r["uploaded"] for r in records)
    assert all(
        r["payload"]["version"]["components"] == {"model": "candidate", "temperature": 0.2}
        for r in records
    )
    manifest = json.loads(next((tmp_path / "results").rglob("manifest.json")).read_text())
    assert manifest["status"] == "completed" and manifest["completed"] == manifest["succeeded"] == 4


def test_directory_glob_and_duplicate_paths_execute_once(tmp_path: Path) -> None:
    write_bench(tmp_path, "benchmarks/example.py")
    result = run_cli(
        tmp_path,
        "run",
        "benchmarks",
        "benchmarks/*.py",
        "benchmarks/example.py",
        "--filter",
        "regression",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "executed").read_text().splitlines() == ["accuracy", "accuracy"]


def test_package_relative_and_lazy_project_imports(tmp_path: Path) -> None:
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "benchmarks/__init__.py").write_text("")
    (tmp_path / "benchmarks/types.py").write_text("VALUE = 1\n")
    (tmp_path / "application.py").write_text("VALUE = 1\n")
    source = BENCHMARK.replace(
        "from __future__ import annotations",
        "from __future__ import annotations\nfrom .types import VALUE",
    )
    source = source.replace(
        "    return Output(value=input.value)",
        "    from application import VALUE\n    return Output(value=VALUE)",
    )
    write_bench(tmp_path, "benchmarks/example.py", source)
    result = run_cli(tmp_path, "run", "benchmarks")
    assert result.returncode == 0, result.stderr
    assert len(saved_runs(tmp_path / ".examen")) == 4


def test_imported_alias_does_not_execute_bench_twice(tmp_path: Path) -> None:
    write_bench(tmp_path, "shared.py")
    write_bench(tmp_path, "one_bench.py", "from shared import bench\nalias = bench\n")
    write_bench(tmp_path, "two_bench.py", "from shared import bench\n")
    result = run_cli(tmp_path, "run")
    assert result.returncode == 0, result.stderr
    assert len(saved_runs(tmp_path / ".examen")) == 4


def test_import_error_prevents_all_execution(tmp_path: Path) -> None:
    write_bench(tmp_path, "a_bench.py")
    write_bench(tmp_path, "z_bench.py", 'raise RuntimeError("broken definition")')
    result = run_cli(tmp_path, "run")
    assert result.returncode == 2
    assert "z_bench.py" in result.stderr and "broken definition" in result.stderr
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / ".examen").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--filter", "nonexistent"),
        ("--filter", "["),
        ("--version", "[]"),
        ("--version", '{"v":NaN}'),
        ("--concurrency", "0"),
        ("missing.py",),
    ],
)
def test_bad_selection_or_configuration_fails_before_execution(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    write_bench(tmp_path)
    result = run_cli(tmp_path, "run", *arguments)
    assert result.returncode == 2
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / ".examen").exists()


def test_errored_experiments_are_saved_and_return_nonzero(tmp_path: Path) -> None:
    source = BENCHMARK.replace(
        "    return Output(value=input.value)", '    raise RuntimeError("provider unavailable")'
    )
    write_bench(tmp_path, source=source)
    result = run_cli(tmp_path, "run")
    assert result.returncode == 1, result.stderr
    records = saved_runs(tmp_path / ".examen")
    assert len(records) == 4
    assert all(
        r["payload"]["run"]["status"] == "errored" and not r["payload"]["metrics"] for r in records
    )


def test_interrupt_preserves_completed_results(tmp_path: Path) -> None:
    source = BENCHMARK.replace(
        "async def accuracy(input: Input) -> Output:",
        "async def accuracy(input: Input) -> Output:\n    await asyncio.sleep(60)",
    )
    write_bench(tmp_path, source=source)
    process = subprocess.Popen(
        [sys.executable, "-m", "examen", "run", "--concurrency", "1"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        # Read progress from a thread with a deadline so a regression cannot hang pytest.
        ready = threading.Event()

        def watch() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                if "[2/4]" in line:
                    ready.set()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        assert ready.wait(10), "benchmark never reached its completed smoke cases"
        watcher.join(timeout=1)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    records = saved_runs(tmp_path / ".examen")
    assert len(records) == 2
    manifest = json.loads(next((tmp_path / ".examen").rglob("manifest.json")).read_text())
    assert manifest["status"] == "interrupted" and manifest["completed"] == 2


class UploadServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), UploadHandler)
        self.requests: list[dict[str, Any]] = []
        self.failures = 1


class UploadHandler(BaseHTTPRequestHandler):
    server: UploadServer

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if self.server.failures:
            self.server.failures -= 1
            self.send_response(503)
            self.end_headers()
            return
        response = json.dumps({"run": {"id": body["run"]["id"]}}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def upload_server() -> Iterator[UploadServer]:
    server = UploadServer()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_upload_command_retries_and_resumes_without_importing_benchmarks(tmp_path: Path) -> None:
    artifact = tmp_path / "results"
    record = save_run(artifact / "runs", {"run": {"status": "succeeded"}, "metrics": []})
    write_bench(tmp_path, source='raise RuntimeError("upload must not import benchmarks")')
    with upload_server() as server:
        env = {"EXAMEN_URL": f"http://127.0.0.1:{server.server_port}", "EXAMEN_API_KEY": "test-key"}
        result = run_cli(
            tmp_path, "upload", str(artifact), "--retry-delay", "0", "--max-attempts", "2", env=env
        )
        assert result.returncode == 0, result.stderr
        assert len(server.requests) == 2
        assert server.requests[0] == server.requests[1]
        assert json.loads(record.read_text())["uploaded"]
        assert "test-key" not in record.read_text()
        result = run_cli(tmp_path, "upload", str(artifact), env=env)
        assert result.returncode == 0 and len(server.requests) == 2
        next_record = save_run(artifact / "runs", {"run": {"status": "succeeded"}, "metrics": []})
        server.failures = 10
        result = run_cli(tmp_path, "upload", str(artifact), "--max-attempts", "1", env=env)
        assert result.returncode == 1 and not json.loads(next_record.read_text())["uploaded"]
        server.failures = 0
        result = run_cli(tmp_path, "upload", str(artifact), env=env)
        assert result.returncode == 0 and json.loads(next_record.read_text())["uploaded"]


def test_upload_requires_credentials_but_run_does_not(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "upload", "results", "--url", "https://examen.test")
    assert result.returncode == 2
    assert "EXAMEN_API_KEY" in result.stderr


@pytest.mark.parametrize(
    ("command", "options"),
    [
        (
            "run",
            [
                "--filter",
                "--list",
                "--version",
                "--concurrency",
                "--output-dir",
                "--upload",
                "--url",
            ],
        ),
        ("upload", ["--url", "--max-attempts", "--retry-delay"]),
        ("inspect", ["manifest.json"]),
    ],
)
def test_typer_command_help(command: str, options: list[str]) -> None:
    result = CliRunner().invoke(app, [command, "--help"], color=False)
    assert result.exit_code == 0, result.output
    for option in options:
        assert option in result.output


def test_explicit_arguments_use_typer_and_preserve_exit_codes(tmp_path: Path) -> None:
    assert main(["run", "--concurrency", "0"]) == 2
    assert main(["run", "--unknown-option"]) == 2
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"status": "completed"}')
    assert main(["inspect", str(manifest)]) == 0
    assert main(["inspect", str(tmp_path / "missing.json")]) == 2


def test_run_upload_saves_and_delivers_with_typer_options(tmp_path: Path) -> None:
    write_bench(tmp_path)
    with upload_server() as server:
        env = {"EXAMEN_URL": f"http://127.0.0.1:{server.server_port}", "EXAMEN_API_KEY": "test-key"}
        result = run_cli(tmp_path, "run", "--upload", "--concurrency", "1", env=env)
        assert result.returncode == 0, result.stderr
        records = saved_runs(tmp_path / ".examen")
        assert len(records) == 4
        assert all(record["uploaded"] for record in records)
        assert len(server.requests) == 5
        record_path = next((tmp_path / ".examen").rglob("runs/*.json"))
        replay = run_cli(tmp_path, "upload", str(record_path), env=env)
        assert replay.returncode == 0, replay.stderr
        assert len(server.requests) == 5


@pytest.mark.parametrize("delay", ["nan", "inf", "-1"])
def test_upload_rejects_invalid_retry_delay(tmp_path: Path, delay: str) -> None:
    result = run_cli(tmp_path, "upload", "results", "--retry-delay", delay)
    assert result.returncode == 2
    assert "finite nonnegative" in result.stderr

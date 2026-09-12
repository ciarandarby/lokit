from __future__ import annotations

import asyncio
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

import lokit
from lokit import _interchange_rust as rust
from lokit.diagnostics import BackendEvent, trace_backends
from lokit.office import backend as office_backend
from lokit.parsers.interchange import try_native_document_export

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import FrameType

    from lokit.data.structure import BaseStructure


@pytest.fixture
def tmx_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.tmx"
    path.write_text(
        '<tmx version="1.4"><header srclang="en-US"/><body><tu tuid="one">'
        '<tuv xml:lang="en-US"><seg>Hello</seg></tuv>'
        '<tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv></tu></body></tmx>',
        encoding="utf-8",
    )
    return path


def test_live_native_function_and_bound_method_events() -> None:
    events: list[BackendEvent] = []
    registry = rust.IdentityRegistry()
    with trace_backends(on_event=events.append) as trace:
        rust.backend_version()
        assert any(event.backend == "rust" for event in events)
        registry.resolve("id")
        assert trace.counts["rust"] == 2
    assert any(event.operation.endswith("IdentityRegistry.resolve") for event in events)
    assert any(event.previous_backend == "rust" and event.backend == "python" for event in events)
    assert not any(event.kind == "fallback" for event in events)
    assert sys.getprofile() is None


def test_output_is_flushed_in_real_time() -> None:
    class Sink(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    sink = Sink()
    with trace_backends(stream=sink):
        rust.backend_version()
        assert "rust" in sink.getvalue()
        assert "rust -> python" in sink.getvalue()
        assert sink.flushes >= 2


def test_native_materialization_is_not_reported_as_fallback(tmx_path: Path) -> None:
    with trace_backends(on_event=lambda event: None) as trace:
        document = lokit.parse.tmx(tmx_path, progress=False)
    assert document.data["one"].targets["fr-FR"].text == "Bonjour"
    assert any(
        event.backend == "rust" and event.operation.endswith("materialize_interchange") for event in trace.events
    )
    assert trace.counts["python"] > 0
    assert not any(event.kind == "fallback" for event in trace.events)


def test_progress_reports_bypass_and_native_reader_constructor(tmx_path: Path) -> None:
    with trace_backends(on_event=lambda event: None) as trace:
        lokit.parse.tmx(tmx_path, progress=True)
    assert any(event.kind == "fallback" and "progress=True" in (event.reason or "") for event in trace.events)
    assert any(event.backend == "rust" and event.operation.endswith(".Reader") for event in trace.events)
    assert any(event.backend == "rust" and "read_data_batch" in event.operation for event in trace.events)


def test_declined_materialization_reports_fallback(tmx_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def decline(
        path: str,
        format_name: str,
        source_language: str | None,
        target_language: str | None,
        domain: str | None,
        mode: str,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        syntaxes: list[str] | None,
    ) -> None:
        return None

    monkeypatch.setattr(rust, "materialize_interchange", decline)
    with trace_backends(on_event=lambda event: None) as trace:
        document = lokit.parse.tmx(tmx_path, progress=False)
    assert document.data["one"].targets["fr-FR"].text == "Bonjour"
    assert any(event.kind == "fallback" and "declined" in (event.reason or "") for event in trace.events)


@pytest.mark.parametrize("option", ["group_by_resource", "resolve_placeholders"])
def test_export_option_fallback(tmx_path: Path, tmp_path: Path, option: str) -> None:
    document = lokit.parse.tmx(tmx_path, progress=False)
    with trace_backends(on_event=lambda event: None) as trace:
        result = try_native_document_export(
            document,
            tmp_path / "out.xlf",
            "xliff",
            group_by_resource=option == "group_by_resource",
            resolve_placeholders=option != "resolve_placeholders",
        )
    assert result is None
    assert any(event.kind == "fallback" and option in (event.reason or "") for event in trace.events)


def test_declined_export_reports_fallback(tmx_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def decline(document: BaseStructure, path: str, format_name: str) -> None:
        return None

    document = lokit.parse.tmx(tmx_path, progress=False)
    monkeypatch.setattr(rust, "export_base_interchange", decline)
    with trace_backends(on_event=lambda event: None) as trace:
        assert try_native_document_export(document, tmp_path / "out.xlf", "xliff") is None
    assert any(event.kind == "fallback" and "declined" in (event.reason or "") for event in trace.events)
    assert not (tmp_path / "out.xlf").exists()


@pytest.mark.parametrize("forced,available", [(True, True), (False, False), (False, True)])
def test_office_backend_is_not_mislabelled_rust(forced: bool, available: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOKIT_OFFICE_BACKEND", "python" if forced else "")
    monkeypatch.setattr(office_backend, "worker_available", lambda: available)
    with trace_backends(on_event=lambda event: None) as trace:
        assert office_backend._use_worker() == (available and not forced)
    decisions = [event for event in trace.events if event.kind in {"fallback", "selection"}]
    assert len(decisions) == 1
    assert decisions[0].backend == ("office-worker" if available and not forced else "python")


def test_decorator_preserves_return_signature_and_exception() -> None:
    events: list[BackendEvent] = []

    @trace_backends(on_event=events.append)
    def operation(value: str, *, fail: bool = False) -> str:
        """Example function."""
        rust.backend_version()
        if fail:
            raise ValueError(value)
        return value

    assert operation("answer") == "answer"
    assert operation.__name__ == "operation"
    assert operation.__doc__ == "Example function."
    with pytest.raises(ValueError, match="failure"):
        operation("failure", fail=True)
    assert len([event for event in events if event.backend == "rust" and event.kind == "call"]) == 2
    assert sys.getprofile() is None


def test_nested_scopes_cleanup_and_reuse() -> None:
    outer = trace_backends(on_event=lambda event: None)
    with outer:
        rust.backend_version()
        with trace_backends(on_event=lambda event: None) as inner:
            rust.backend_version()
        rust.backend_version()
        assert inner.counts["rust"] == 1
        assert outer.counts["rust"] == 3
    with outer:
        rust.backend_version()
    assert outer.counts["rust"] == 1
    assert sys.getprofile() is None


def test_existing_profiler_is_not_replaced() -> None:
    def profiler(frame: FrameType, event: str, arg: object) -> None:
        return None

    sys.setprofile(profiler)
    try:
        with pytest.raises(RuntimeError, match="existing profiler"), trace_backends():
            pytest.fail("must not enter")
        assert sys.getprofile() is profiler
    finally:
        sys.setprofile(None)


def test_bounded_storage_and_broken_sink_do_not_change_execution() -> None:
    def broken_sink(event: BackendEvent) -> None:
        raise OSError("closed pipe")

    with trace_backends(on_event=broken_sink, max_events=1) as trace:
        assert rust.backend_version()
        rust.backend_version()
    assert isinstance(trace.output_error, OSError)
    assert len(trace.events) == 1
    assert trace.dropped_events > 0
    assert trace.counts["rust"] == 2
    assert sys.getprofile() is None


def test_lazy_work_is_traced_during_iteration() -> None:
    def generate() -> Iterator[str]:
        yield rust.backend_version()

    with pytest.raises(TypeError, match="around iteration"):
        trace_backends()(generate)
    iterator = generate()
    with trace_backends(on_event=lambda event: None) as trace:
        assert list(iterator)
    assert trace.counts["rust"] == 1


def test_worker_threads_can_install_independent_traces() -> None:
    def worker() -> int:
        with trace_backends(on_event=lambda event: None) as trace:
            rust.backend_version()
        return trace.counts["rust"]

    with trace_backends(on_event=lambda event: None) as outer, ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(worker).result() == 1
    assert outer.counts["rust"] == 0


def test_coroutine_decorators_isolate_overlapping_tasks() -> None:
    first: list[BackendEvent] = []
    second: list[BackendEvent] = []

    async def run() -> None:
        entered = asyncio.Event()
        finished = asyncio.Event()

        @trace_backends(on_event=first.append)
        async def one() -> str:
            entered.set()
            await finished.wait()
            return rust.backend_version()

        @trace_backends(on_event=second.append)
        async def two() -> str:
            await entered.wait()
            result = rust.backend_version()
            finished.set()
            return result

        assert all(await asyncio.gather(one(), two()))

    asyncio.run(run())
    assert sum(event.backend == "rust" and event.kind == "call" for event in first) == 1
    assert sum(event.backend == "rust" and event.kind == "call" for event in second) == 1
    assert sys.getprofile() is None


def test_native_exception_does_not_leave_tracing_installed(tmp_path: Path) -> None:
    with pytest.raises((OSError, ValueError)), trace_backends(on_event=lambda event: None):
        lokit.parse.tmx(tmp_path / "missing.tmx", progress=False)
    assert sys.getprofile() is None

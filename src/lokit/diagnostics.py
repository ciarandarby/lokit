"""Opt-in, live tracing of lokit's Python/Rust boundaries and fallback decisions."""

from __future__ import annotations

import inspect
import sys
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Literal, ParamSpec, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import FrameType, TracebackType
    from typing import TextIO

__all__ = ["BackendEvent", "BackendTrace", "trace_backends"]

Backend = Literal["python", "rust", "office-worker"]
EventKind = Literal["call", "return", "fallback", "selection"]
P = ParamSpec("P")
R = TypeVar("R")
_RUST_MODULE = "lokit._interchange_rust"
_active: ContextVar[tuple[BackendTrace, ...]] = ContextVar("lokit_backend_traces", default=())


@dataclass(frozen=True)
class BackendEvent:
    """An observed call boundary or explicit dispatch decision, without user data."""

    timestamp: float
    elapsed: float
    thread_id: int
    backend: Backend
    previous_backend: Backend | None
    kind: EventKind
    operation: str
    reason: str | None = None

    @property
    def switched(self) -> bool:
        return self.previous_backend is not None and self.previous_backend != self.backend


class _ThreadState(threading.local):
    def __init__(self) -> None:
        self.users = 0
        self.emitting = False
        self.native_frames: list[tuple[FrameType, str]] = []


_state = _ThreadState()


def _python_operation(frame: FrameType) -> str | None:
    module = frame.f_globals.get("__name__")
    if not isinstance(module, str) or not module.startswith("lokit.") or module == __name__:
        return None
    return f"{module}.{frame.f_code.co_name}"


def _rust_operation(function: object) -> str | None:
    module = getattr(function, "__module__", None)
    owner = getattr(function, "__self__", None)
    if module != _RUST_MODULE and type(owner).__module__ != _RUST_MODULE:
        return None
    name: object = getattr(function, "__qualname__", None)
    return f"{_RUST_MODULE}.{name}" if isinstance(name, str) else _RUST_MODULE


def _profile(frame: FrameType, event: str, arg: object) -> None:
    if not _active.get() or _state.emitting:
        return
    if event == "call":
        operation = _python_operation(frame)
        if operation is not None:
            _record("python", "call", operation)
    elif event == "return" and _state.native_frames:
        caller, operation = _state.native_frames[-1]
        if frame.f_back is caller:
            _record("rust", "return", operation)
    elif event in {"c_call", "c_return", "c_exception"}:
        operation = _rust_operation(arg)
        if operation is not None:
            if event == "c_call":
                _state.native_frames.append((frame, operation))
                _record("rust", "call", operation)
            else:
                if _state.native_frames and _state.native_frames[-1] == (frame, operation):
                    _state.native_frames.pop()
                # A return to Python is normal orchestration, never a fallback.
                _record(
                    "python",
                    "return",
                    operation,
                    "native call raised an exception" if event == "c_exception" else None,
                )


def _record(backend: Backend, kind: EventKind, operation: str, reason: str | None = None) -> None:
    if _state.emitting:
        return
    _state.emitting = True
    try:
        for trace in _active.get():
            if trace._thread_id == threading.get_ident():
                trace._emit(backend, kind, operation, reason)
    finally:
        _state.emitting = False


def _call_native(factory: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Cover native constructors, which CPython does not report as c_call events."""
    if not _active.get() or getattr(factory, "__module__", None) != _RUST_MODULE:
        return factory(*args, **kwargs)
    operation = f"{_RUST_MODULE}.{getattr(factory, '__name__', 'constructor')}"
    _state.native_frames.append((sys._getframe(), operation))
    _record("rust", "call", operation)
    try:
        return factory(*args, **kwargs)
    finally:
        _state.native_frames.pop()
        _record("python", "return", operation)


class BackendTrace:
    """Context manager and decorator for the current thread's lokit execution.

    Use ``trace_backends()`` to construct one. Sync and coroutine functions are
    supported; consume lazy iterators inside a context-manager block. Each
    decorated invocation gets its own trace. Async tasks are scoped separately,
    but work offloaded to other threads/processes requires a trace in that worker.
    Existing profilers are left intact: entering alongside one raises RuntimeError.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        on_event: Callable[[BackendEvent], None] | None = None,
        changes_only: bool = True,
        max_events: int = 1000,
    ) -> None:
        if max_events < 0:
            raise ValueError("max_events must be non-negative")
        self._stream = stream
        self._on_event = on_event
        self._changes_only = changes_only
        self._max_events = max_events
        self._token: Token[tuple[BackendTrace, ...]] | None = None
        self._thread_id: int | None = None
        self._started = 0.0
        self._previous: Backend | None = None
        self.events: list[BackendEvent] = []
        self.counts: dict[Backend, int] = {"python": 0, "rust": 0, "office-worker": 0}
        self.dropped_events = 0
        self.output_error: Exception | None = None

    def __enter__(self) -> BackendTrace:
        if self._token is not None:
            raise RuntimeError("This BackendTrace is already active; create another trace for a nested block")
        current = sys.getprofile()
        if current is not None and current is not _profile:
            raise RuntimeError("Backend tracing needs sys.setprofile; stop the existing profiler first")
        self.events.clear()
        self.counts = {"python": 0, "rust": 0, "office-worker": 0}
        self.dropped_events = 0
        self.output_error = None
        self._previous = None
        self._started = time.perf_counter()
        self._thread_id = threading.get_ident()
        if _state.users == 0:
            sys.setprofile(_profile)
        _state.users += 1
        self._token = _active.set((*_active.get(), self))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._token is None or self._thread_id != threading.get_ident():
            raise RuntimeError("BackendTrace must exit in the thread and context where it entered")
        _active.reset(self._token)
        self._token = None
        self._thread_id = None
        _state.users -= 1
        if _state.users == 0 and sys.getprofile() is _profile:
            sys.setprofile(None)
            _state.native_frames.clear()

    def __call__(self, function: Callable[P, R]) -> Callable[P, R]:
        if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            raise TypeError("For generators, use 'with trace_backends():' around iteration")
        if inspect.iscoroutinefunction(function):
            async_function = cast("Callable[P, Awaitable[object]]", function)

            @wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                with self._fresh():
                    return await async_function(*args, **kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with self._fresh():
                return function(*args, **kwargs)

        return wrapper

    def _fresh(self) -> BackendTrace:
        return BackendTrace(
            stream=self._stream,
            on_event=self._on_event,
            changes_only=self._changes_only,
            max_events=self._max_events,
        )

    def _emit(self, backend: Backend, kind: EventKind, operation: str, reason: str | None) -> None:
        if kind == "call":
            self.counts[backend] += 1
        previous = self._previous
        self._previous = backend
        if self._changes_only and previous == backend and kind in {"call", "return"}:
            return
        event = BackendEvent(
            timestamp=time.time(),
            elapsed=time.perf_counter() - self._started,
            thread_id=threading.get_ident(),
            backend=backend,
            previous_backend=previous,
            kind=kind,
            operation=operation,
            reason=reason,
        )
        if len(self.events) < self._max_events:
            self.events.append(event)
        else:
            self.dropped_events += 1
        if self.output_error is not None:
            return
        try:
            if self._on_event is not None:
                self._on_event(event)
            else:
                transition = f"{previous} -> {backend}" if event.switched else backend
                detail = f" ({reason})" if reason else ""
                print(
                    f"[lokit +{event.elapsed:.6f}s] {transition} {kind}: {operation}{detail}",
                    file=self._stream if self._stream is not None else sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            # Diagnostic sinks must not alter a parse/export result or mask its error.
            self.output_error = exc


def trace_backends(
    *,
    stream: TextIO | None = None,
    on_event: Callable[[BackendEvent], None] | None = None,
    changes_only: bool = True,
    max_events: int = 1000,
) -> BackendTrace:
    """Print live backend transitions to stderr, or deliver them to ``on_event``.

    Use as ``@trace_backends()`` or ``with trace_backends() as trace:``. Pass
    ``changes_only=False`` to see every observed call. Stored events are bounded
    by ``max_events``; live output and call counts continue after that limit.
    Counts describe call boundaries, not CPU time or fallback percentages.
    """
    return BackendTrace(stream=stream, on_event=on_event, changes_only=changes_only, max_events=max_events)

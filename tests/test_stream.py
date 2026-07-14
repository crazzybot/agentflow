"""Tests for StreamEmitter — multi-consumer replay and completion behaviour."""
import pytest

from agentflow.core.models import SSEEventType
from agentflow.orchestrator.stream import StreamEmitter


@pytest.mark.asyncio
async def test_single_consumer_receives_all_events():
    emitter = StreamEmitter("run-1")
    emitter.emit(SSEEventType.run_started, message="start")
    emitter.emit(SSEEventType.run_complete, message="done")
    emitter.close()

    events = [item async for item in emitter]
    assert len(events) == 2


@pytest.mark.asyncio
async def test_second_consumer_replays_all_events():
    """A second consumer connecting after all events are emitted sees everything."""
    emitter = StreamEmitter("run-2")
    emitter.emit(SSEEventType.run_started, message="start")
    emitter.emit(SSEEventType.run_complete, message="done")
    emitter.close()

    first = [item async for item in emitter]
    second = [item async for item in emitter]
    assert len(first) == 2
    assert first == second


@pytest.mark.asyncio
async def test_consumer_on_completed_emitter_does_not_hang():
    """Connecting to an already-closed emitter returns immediately."""
    emitter = StreamEmitter("run-3")
    emitter.close()

    events = [item async for item in emitter]
    assert events == []


@pytest.mark.asyncio
async def test_done_is_true_after_close():
    emitter = StreamEmitter("run-4")
    assert not emitter.done
    emitter.close()
    assert emitter.done

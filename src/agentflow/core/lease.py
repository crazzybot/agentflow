"""Per-run ownership leases — which process is executing a run.

With one API process (``STATE_BACKEND=memory``) that process owns every run, so
``RunLeases`` is a no-op: nothing is ever held elsewhere and cross-replica
cancel is impossible.

With several replicas (``STATE_BACKEND=redis``) ``RedisRunLeases`` keeps a
``run:{run_id}:lease`` key holding the owner's process id with a short TTL that
the owner renews in ``keep_alive()``. That lets other replicas:

- skip runs that are alive elsewhere when reconciling orphans at startup, and
  refuse to resume them (only an expired lease means the owner died);
- cancel them: ``request_cancel()`` sets ``run:{run_id}:cancel``, and the owner's
  keep-alive loop cancels the run when it sees the flag.

If the owner ever finds its lease taken or gone (e.g. the event loop stalled past
the TTL and another replica resumed the run), it cancels its copy rather than
run the same work twice.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from agentflow.config import settings

logger = logging.getLogger(__name__)

# Cancellation message used when ownership is lost: the engine then stands down
# without writing a terminal event into a log the new owner is now appending to.
LEASE_LOST = "agentflow:lease_lost"


class RunLeases:
    """Single-process leases: this process owns every run."""

    async def acquire(self, run_id: str) -> bool:
        return True

    async def release(self, run_id: str) -> None:
        return None

    async def held_elsewhere(self, run_id: str) -> bool:
        return False

    async def request_cancel(self, run_id: str) -> bool:
        """Ask the replica that owns *run_id* to cancel it. False if no owner is alive."""
        return False

    async def keep_alive(self, run_id: str, run_task: asyncio.Task[None]) -> None:
        """Renew the lease until cancelled; cancel *run_task* if ownership is lost."""
        return None


# Renew/release only while the lease still holds our owner id: never extend or
# delete another replica's lease.
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisRunLeases(RunLeases):
    def __init__(
        self,
        redis: aioredis.Redis,
        ttl_s: float,
        cancel_flag_ttl_s: int,
        owner_id: str | None = None,
    ) -> None:
        self._redis = redis
        self._ttl_ms = int(ttl_s * 1000)
        self._cancel_ttl = cancel_flag_ttl_s
        self.owner_id = owner_id or uuid.uuid4().hex

    @staticmethod
    def _lease_key(run_id: str) -> str:
        return f"run:{run_id}:lease"

    @staticmethod
    def _cancel_key(run_id: str) -> str:
        return f"run:{run_id}:cancel"

    async def acquire(self, run_id: str) -> bool:
        key = self._lease_key(run_id)
        if await self._redis.set(key, self.owner_id, nx=True, px=self._ttl_ms):
            # A fresh owner starts without a stale cancel request from a dead one.
            await self._redis.delete(self._cancel_key(run_id))
            return True
        return await self._redis.get(key) == self.owner_id

    async def release(self, run_id: str) -> None:
        try:
            await self._redis.eval(_RELEASE, 1, self._lease_key(run_id), self.owner_id)
            await self._redis.delete(self._cancel_key(run_id))
        except RedisError:
            # Called from run cleanup, which must finish; the lease lapses by TTL.
            logger.warning("[%s] Could not release the run lease", run_id, exc_info=True)

    async def held_elsewhere(self, run_id: str) -> bool:
        owner = await self._redis.get(self._lease_key(run_id))
        return owner is not None and owner != self.owner_id

    async def request_cancel(self, run_id: str) -> bool:
        if not await self._redis.exists(self._lease_key(run_id)):
            return False
        await self._redis.set(self._cancel_key(run_id), "1", ex=self._cancel_ttl)
        return True

    async def keep_alive(self, run_id: str, run_task: asyncio.Task[None]) -> None:
        # Renew well inside the TTL so one slow round-trip cannot expire the lease.
        interval = self._ttl_ms / 1000 / 3
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._redis.eval(
                    _RENEW, 1, self._lease_key(run_id), self.owner_id, self._ttl_ms
                )
                if not renewed:
                    logger.error(
                        "[%s] Lost the run lease (another replica owns it now) — cancelling local copy",
                        run_id,
                    )
                    run_task.cancel(msg=LEASE_LOST)
                    return
                if await self._redis.exists(self._cancel_key(run_id)):
                    logger.info("[%s] Cancel requested via another replica", run_id)
                    run_task.cancel()
                    return
            except RedisError:
                # A transient outage must not kill a healthy run; if it outlasts the
                # TTL the lease lapses and the next renew reports the loss.
                logger.warning("[%s] Could not renew the run lease", run_id, exc_info=True)


def _make_run_leases() -> RunLeases:
    if settings.state_backend == "redis":
        from agentflow.core.redis_client import get_redis
        return RedisRunLeases(
            get_redis(), ttl_s=settings.run_lease_ttl_s, cancel_flag_ttl_s=settings.redis_key_ttl
        )
    return RunLeases()


run_leases = _make_run_leases()

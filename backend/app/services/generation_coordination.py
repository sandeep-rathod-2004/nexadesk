import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class GenerationCoordinationError(Exception):
    pass


@dataclass
class GenerationLease:
    generation_id: UUID
    user_id: UUID
    workspace_id: UUID
    owner_id: str
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    monitor_task: asyncio.Task[None] | None = None


class GenerationCoordinator(Protocol):
    async def register(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> GenerationLease: ...

    async def workspace_for(
        self, generation_id: UUID, user_id: UUID
    ) -> UUID | None: ...

    async def request_cancel(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> bool: ...

    async def begin_completion(self, lease: GenerationLease) -> bool: ...

    async def finish(self, lease: GenerationLease) -> None: ...

    async def close(self) -> None: ...


class LocalGenerationCoordinator:
    """Single-process development fallback; not suitable for multiple workers."""

    def __init__(self) -> None:
        self._active: dict[UUID, GenerationLease] = {}
        self._outcomes: dict[UUID, str] = {}

    async def register(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> GenerationLease:
        if generation_id in self._active:
            raise GenerationCoordinationError("Generation is already registered.")
        lease = GenerationLease(generation_id, user_id, workspace_id, "local")
        self._active[generation_id] = lease
        self._outcomes[generation_id] = "running"
        return lease

    async def workspace_for(
        self, generation_id: UUID, user_id: UUID
    ) -> UUID | None:
        lease = self._active.get(generation_id)
        if lease is None or lease.user_id != user_id:
            return None
        return lease.workspace_id

    async def request_cancel(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> bool:
        lease = self._active.get(generation_id)
        if lease is None or lease.user_id != user_id or lease.workspace_id != workspace_id:
            return False
        outcome = self._outcomes.get(generation_id)
        if outcome == "cancel_requested":
            return True
        if outcome != "running":
            return False
        self._outcomes[generation_id] = "cancel_requested"
        lease.cancel_requested.set()
        return True

    async def begin_completion(self, lease: GenerationLease) -> bool:
        if self._outcomes.get(lease.generation_id) != "running":
            return False
        self._outcomes[lease.generation_id] = "completing"
        return True

    async def finish(self, lease: GenerationLease) -> None:
        self._active.pop(lease.generation_id, None)
        self._outcomes.pop(lease.generation_id, None)

    async def close(self) -> None:
        for lease in self._active.values():
            lease.cancel_requested.set()
        self._active.clear()
        self._outcomes.clear()


class RedisGenerationCoordinator:
    _REGISTER_SCRIPT = """
    if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
    redis.call('HSET', KEYS[1],
        'user_id', ARGV[1], 'workspace_id', ARGV[2],
        'owner_id', ARGV[3], 'status', 'running')
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return 1
    """
    _CANCEL_SCRIPT = """
    if redis.call('HGET', KEYS[1], 'user_id') ~= ARGV[1] then return 0 end
    if redis.call('HGET', KEYS[1], 'workspace_id') ~= ARGV[2] then return 0 end
    local state = redis.call('HGET', KEYS[1], 'status')
    if state == 'cancel_requested' then return 1 end
    if state ~= 'running' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'cancel_requested')
    redis.call('PUBLISH', KEYS[2], ARGV[3])
    return 1
    """
    _RENEW_SCRIPT = """
    if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1] then return 0 end
    local state = redis.call('HGET', KEYS[1], 'status')
    if state ~= 'running' and state ~= 'cancel_requested' then return 0 end
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
    """
    _COMPLETE_SCRIPT = """
    if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1] then return 0 end
    if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'completing')
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
    """
    _FINISH_SCRIPT = """
    if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1] then return 0 end
    redis.call('DEL', KEYS[1])
    return 1
    """

    def __init__(
        self,
        client: Redis,
        lease_seconds: int = 60,
        poll_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._owner_id = str(uuid4())

    @staticmethod
    def _key(generation_id: UUID) -> str:
        return f"nexadesk:generation:{generation_id}"

    @staticmethod
    def _channel(generation_id: UUID) -> str:
        return f"nexadesk:generation-cancel:{generation_id}"

    async def register(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> GenerationLease:
        try:
            created = await self._client.eval(
                self._REGISTER_SCRIPT,
                1,
                self._key(generation_id),
                str(user_id),
                str(workspace_id),
                self._owner_id,
                self._lease_seconds,
            )
        except Exception as exc:
            logger.warning("Generation registration failed because Redis is unavailable.")
            raise GenerationCoordinationError from exc
        if not created:
            raise GenerationCoordinationError("Generation state already exists.")
        lease = GenerationLease(
            generation_id, user_id, workspace_id, self._owner_id
        )
        lease.monitor_task = asyncio.create_task(self._monitor(lease))
        return lease

    async def _monitor(self, lease: GenerationLease) -> None:
        pubsub = self._client.pubsub()
        key = self._key(lease.generation_id)
        try:
            await pubsub.subscribe(self._channel(lease.generation_id))
            while not lease.cancel_requested.is_set():
                await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=self._poll_seconds
                )
                # Poll shared state as well as Pub/Sub so missed notifications are safe.
                state = await self._client.hget(key, "status")
                if state == "cancel_requested":
                    lease.cancel_requested.set()
                    break
                if state in {"completing", "completed"}:
                    break
                renewed = await self._client.eval(
                    self._RENEW_SCRIPT,
                    1,
                    key,
                    lease.owner_id,
                    self._lease_seconds,
                )
                if not renewed:
                    lease.cancel_requested.set()
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Generation coordination lost; cancelling generation safely."
            )
            lease.cancel_requested.set()
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("Redis subscription cleanup failed.")

    async def workspace_for(
        self, generation_id: UUID, user_id: UUID
    ) -> UUID | None:
        try:
            values = await self._client.hmget(
                self._key(generation_id), "user_id", "workspace_id"
            )
        except Exception as exc:
            logger.warning("Generation lookup failed because Redis is unavailable.")
            raise GenerationCoordinationError from exc
        if not values or values[0] != str(user_id) or values[1] is None:
            return None
        try:
            return UUID(values[1])
        except ValueError:
            logger.error("Generation coordination state has an invalid workspace ID.")
            raise GenerationCoordinationError from None

    async def request_cancel(
        self, generation_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> bool:
        try:
            return bool(
                await self._client.eval(
                    self._CANCEL_SCRIPT,
                    2,
                    self._key(generation_id),
                    self._channel(generation_id),
                    str(user_id),
                    str(workspace_id),
                    str(generation_id),
                )
            )
        except Exception as exc:
            logger.warning("Cancellation request failed because Redis is unavailable.")
            raise GenerationCoordinationError from exc

    async def begin_completion(self, lease: GenerationLease) -> bool:
        try:
            return bool(
                await self._client.eval(
                    self._COMPLETE_SCRIPT,
                    1,
                    self._key(lease.generation_id),
                    lease.owner_id,
                    self._lease_seconds,
                )
            )
        except Exception as exc:
            logger.warning("Generation completion could not be coordinated.")
            lease.cancel_requested.set()
            raise GenerationCoordinationError from exc

    async def finish(self, lease: GenerationLease) -> None:
        try:
            await self._client.eval(
                self._FINISH_SCRIPT,
                1,
                self._key(lease.generation_id),
                lease.owner_id,
            )
        except Exception:
            logger.warning("Generation state cleanup deferred to its Redis lease.")
        task = lease.monitor_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        await self._client.aclose()


async def get_generation_coordinator(request: Request) -> GenerationCoordinator:
    coordinator = getattr(request.app.state, "generation_coordinator", None)
    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Generation coordination is unavailable.",
        )
    return coordinator

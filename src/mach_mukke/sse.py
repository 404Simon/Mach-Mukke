import asyncio
import json
import logging

logger = logging.getLogger("mach_mukke.sse")

_subscribers: list[asyncio.Queue] = []


async def sse_generator(queue: asyncio.Queue):
    try:
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        if queue in _subscribers:
            _subscribers.remove(queue)


def create_subscriber() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.append(queue)
    return queue


def notify(event: dict):
    dead_queues = []
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("SSE queue full for subscriber, removing")
            dead_queues.append(queue)
        except Exception as e:
            logger.warning(f"Error sending SSE event: {e}")
            dead_queues.append(queue)
    for queue in dead_queues:
        if queue in _subscribers:
            _subscribers.remove(queue)

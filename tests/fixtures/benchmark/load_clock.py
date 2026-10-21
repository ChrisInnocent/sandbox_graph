"""Independent synthetic time source: only this fixture advances service time."""
import asyncio
import heapq
import itertools


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.timers = []
        self.sequence = itertools.count()

    def __call__(self):
        return self.now

    async def sleep(self, duration):
        if duration <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self.timers, (self.now + duration, next(self.sequence), future))
        await future

    async def run(self, coroutine):
        task = asyncio.create_task(coroutine)
        for _ in range(10000):
            # Flush runnable callbacks before advancing time. These are scheduler
            # turns, never guesses involving real elapsed time.
            for _ in range(80):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            while self.timers and self.timers[0][2].done():
                heapq.heappop(self.timers)
            if not self.timers:
                task.cancel()
                raise AssertionError("work stalled without a registered timer")
            self.now = self.timers[0][0]
            while self.timers and self.timers[0][0] <= self.now:
                _, _, future = heapq.heappop(self.timers)
                if not future.done():
                    future.set_result(None)
        task.cancel()
        raise AssertionError("finite script failed to terminate")


class ScriptedJournal:
    """Isolated admission boundary; durable/crash behavior tested with real journal."""
    def __init__(self):
        self.reservations = []
        self.started = []
        self.terminals = []

    async def reserve(self, request_index, child_weight):
        self.reservations.append((request_index, child_weight))
        return request_index

    async def submission_started(self, token):
        self.started.append(token)

    async def record_terminal(self, token, result):
        self.terminals.append((token, result))

"""Slot/KV accounting must stay exact when timeouts and cancellations race
with grants (regression: slots leaked, then were double-released).
Run: docker run --rm -v $PWD/worker:/w -w /w battlefield/worker:dev python test_slots.py"""
import asyncio

import app


async def main():
    g = app.GPU()
    for _ in range(app.SLOTS):
        await g.acquire(10, 1)
    for i in range(500):  # timeout racing a grant
        t = asyncio.create_task(g.acquire(10, 0.01))
        await asyncio.sleep(0.0099 + (i % 5) * 0.00004)
        g.release(10)
        try:
            await t
            g.release(10)
        except Exception:
            pass
        await g.acquire(10, 1)
    for _ in range(100):  # cancellation racing a grant
        t = asyncio.create_task(g.acquire(10, 5))
        await asyncio.sleep(0)
        g.release(10)
        t.cancel()
        try:
            await t
            g.release(10)
        except BaseException:
            pass
        await g.acquire(10, 1)
    assert g.active == app.SLOTS, g.active
    assert g.kv_used == app.SLOTS * 10, g.kv_used
    assert not g.waiters
    print("slot accounting OK")


asyncio.run(main())

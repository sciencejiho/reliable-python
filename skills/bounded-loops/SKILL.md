---
name: bounded-loops
description: Use when writing any Python loop — while True, retry logic, polling, queue draining, convergence loops, or iteration over a stream, generator, or paginated API.
---

# Bounded Loops

**Power of Ten, Rule 2: every loop must have a fixed upper bound that a reader
can verify without running the code.**

A loop whose termination depends on the outside world behaving correctly is not
a loop, it is a hang waiting for a bad day. The bound does not make the code
correct — it makes the failure *loud and fast* instead of silent and infinite.

## When to Use

Load this before writing:

- `while True:` or `while <condition>:` of any kind
- Retry / backoff logic
- Polling a job, file, socket, or API for readiness
- Draining a queue or consuming a generator
- Convergence loops (train until loss stops improving)
- Pagination (`while next_page:`)

**When NOT to apply:** a process's top-level event loop or service `serve()`
loop is legitimately unbounded — that is its job. The rule applies to every
loop *inside* it.

## Core Pattern

The fix is always the same shape: replace the implicit trust with an explicit
ceiling, and raise when you hit it.

```python
# ❌ Unbounded — spins forever if the job never leaves "running",
#    and the caller sees a hang with no diagnostic
def wait_for_job(api, job_id):
    while True:
        if api.get_status(job_id) == "done":
            return api.fetch_result(job_id)
        time.sleep(5)
```

```python
# ✅ Bounded — fails at a known time, with a message that names the cause
POLL_INTERVAL_S = 5
MAX_POLLS = 120  # 120 × 5s = 10 minute ceiling

def wait_for_job(api, job_id):
    for _ in range(MAX_POLLS):
        if api.get_status(job_id) == "done":
            return api.fetch_result(job_id)
        time.sleep(POLL_INTERVAL_S)

    raise TimeoutError(
        f"job {job_id} still not done after "
        f"{MAX_POLLS * POLL_INTERVAL_S}s ({MAX_POLLS} polls)"
    )
```

Note what changed beyond the bound: the timeout constant is *named and
computable*, so a reader knows the worst case without arithmetic, and the
error message says which job and how long.

## Quick Reference

| Unbounded form | Bounded form |
|---|---|
| `while True:` | `for _ in range(MAX_N):` + `raise` after |
| `while not done:` | same, with `done` checked inside |
| `for x in stream:` | `for x in itertools.islice(stream, MAX_N):` |
| `while next_page:` | cap page count, raise past it |
| `while loss > eps:` | `for epoch in range(MAX_EPOCHS):` |
| recursion | explicit stack + depth cap (Rule 1) |

Falling off the end of a bounded loop is an **error**, not a normal exit.
Always `raise` or return an explicit failure — never let it fall through
silently, which converts a hang into a wrong answer.

## Common Mistakes

**Bound with no raise.** `for _ in range(100):` that just ends returns `None`
and the caller proceeds with garbage. Worse than the hang, because it is
silent.

**Bound that isn't a bound.** `while attempts < max_attempts:` where something
inside resets `attempts` on partial progress. The counter must be
monotonic — a `for` loop makes this structural rather than a thing to review.

**Magic number in the range.** `range(120)` tells a reader nothing. Name it,
and express the derived limit (`MAX_POLLS * POLL_INTERVAL_S`) so the real
ceiling is visible.

## Rationalizations

| Excuse | Reality |
|--------|---------|
| "The API always responds" | Then the bound never triggers and costs nothing. That is the argument *for* it. |
| "This obviously terminates" | It terminates given assumptions you did not write down. Write them down as the bound. |
| "A timeout is the caller's job" | The caller cannot distinguish slow from hung. This loop can. |
| "I don't know a reasonable limit" | Not knowing the worst case is precisely the problem. Pick one, name it, tune it later. |
| "It's a script, not production" | Scripts get scheduled. A hung cron job is invisible until the data is missing. |
| "Convergence loops can't be bounded" | Every training run has a max epoch. Unbounded convergence is a bug, not flexibility. |

## Red Flags — Stop and Bound It

- Writing `while True:` with the intent to `break` on a condition
- A loop condition that reads a value from network, disk, or another process
- Any `sleep()` inside a loop body
- A `retry` or `attempt` variable that gets reassigned in more than one place
- Thinking "I'll add a timeout if it ever becomes a problem"

"""What the service is doing, and what it cost.

A run is minutes of work behind a single streaming response, so a client that disconnects, or one
that never started the run, has no way to find out how it is going. The registry is that record.

It lives in memory. Runs are observation, not state: nothing resumes from one, and by default a
restart forgets them. With runs.store set to sqlite, each run is also written to a
`RunStore` when it starts and when it ends, which is history for debugging rather than state.

Usage is reported by the pipeline itself, through `kvasir.storm.runtime`'s sink. A `Run` is the
sink, found through a contextvar rather than passed down, because the reporting happens deep inside
the fork and across every thread a run touches.
"""

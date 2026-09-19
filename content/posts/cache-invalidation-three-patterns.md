---
title: "Cache Invalidation: Three Patterns and the Cost of Getting It Wrong"
date: 2026-09-18
draft: false
tags: ["caching", "distributed-systems", "backend", "consistency"]
slug: cache-invalidation-three-patterns
description: "TTL, event-driven, and write-through each pay a different cost for keeping cache aligned with the source of truth. Pick one and own its failure mode."
---

A version-control service I worked on kept a small list in Redis: which version of the code bundle was sitting on the shared NFS volume. Every request checked the list first. Hit meant the file was local and the executor could load it. Miss meant a download from object storage.

One morning the cleanup job ran. It deletes old versions when NFS fills up. It deleted v1.42 from NFS. The list still said v1.42 existed. The cache lied, and a lying cache is worse than a missing one. A miss sends you to the source of truth. A false hit tells you the file is there, the loader opens it, and you get an error three steps later with no clear culprit.

Picking Redis or Caffeine or Memcached was not the question. The question was how to keep that list aligned with NFS. Every cache, on every system, faces the same question. Three patterns answer it, and each one hands you a different bill.

{{< diagram "invalidation-three-patterns.svg" "Three patterns, three bills" >}}

## TTL: the default that lies until it stops

Most teams pick TTL because it costs nothing to set up. Each key gets an expiration time. Read the key. If the time has passed, treat it as a miss and reload from the source. Done.

The failure mode is the window between a real-world change and the clock running out. A user changes their display name. The user record in the database updates. The cache still holds the old name. Every request that lands on that key, for as long as TTL takes to expire, returns the stale value. Set TTL to five minutes and the user sees the wrong name for up to five minutes. Set TTL to one hour and the window stretches to one hour.

{{< diagram "invalidation-ttl-flow.svg" "Write, serve, drift, lie" >}}

Two ways teams try to close the window. Random offset on the expiration time spreads out the simultaneous expiry that causes cache stampedes, where a flood of misses hits the source at once. This is cheap and works almost everywhere. Background refresh pushes the work off the request path, so a key is reloaded before it expires, but now you maintain a separate job that can fail on its own, and only pays off when the rebuild cost is large.

TTL is a bet. You trade consistency for cheap code, and the price is a window where reads can lie. The window length is your call. So is the cost of lying during it.

## Event-driven: the fast one that loses messages

The other default in modern stacks is to clear the cache the moment the source of truth changes. Write to the database, then publish a message. Every node holding a local copy subscribes to the message and drops its entry. Latency drops to milliseconds.

Two ways it breaks. The first is lost messages. In-memory pub/sub on a single Redis node forgets any message it could not deliver to a subscriber. A subscriber drops its connection, reconnects, and misses everything that fired during the gap. The cleared cache serves stale data until TTL takes over. Queues with persistence close this gap at the cost of latency and a new dependency to operate.

{{< diagram "invalidation-message-loss.svg" "Disconnect, drop, drift" >}}

The second is order. The clear message arrives before the database write commits, or after. A reader that misses the cache right after the clear runs the read against a database that still holds the old value, writes it back, and the cache is dirty again. The window is short, but it exists.

{{< diagram "invalidation-race.svg" "The clear message and the write, racing" >}}

Teams that pick event-driven usually layer TTL on top of it. Event-driven clears the hot path, TTL cleans up whatever the message lost. You end up running two patterns, paying the maintenance cost of both, and hoping the failures do not line up.

## Write-through: the strict one that pays in latency

The third pattern keeps the cache and the database in lockstep. Every write updates both. Reads always hit. Stale reads cannot exist because the write never finished until both sides committed.

The cost shows up on the write path. One write now triggers two coordinated updates. The cache call adds a network round trip and a serialization step. On a single key taking 10k writes per second, the cache becomes a serial bottleneck and the database holds locks longer because the whole write takes longer.

The other failure mode is partial success. The cache update succeeds. The database transaction rolls back. Or the database commits and the cache write fails. Now the cache holds a value that does not exist anywhere else. Reads serve a phantom, and the only way to recover is to evict the key, which puts you back in event-driven territory.

{{< diagram "invalidation-partial-success.svg" "Cache commits, db rolls back" >}}

Write-through is the right pick when stale reads cost more than slow writes. Money balances, inventory counts, anything where a wrong number reaches the user. It is the wrong pick for user profiles, session metadata, or any field that updates on every save, where the write-through cost dominates and a five-second window of staleness is acceptable.

## Pick one and own the failure mode

No team runs three patterns at once. Real systems pick one mode for each class of data, then make peace with its failure mode. Pick the box that matches your data's failure tolerance, not the box that matches your favorite tool.

{{< diagram "invalidation-pick-one.svg" "Decision by data class" >}}

The version-control service picked TTL. The list was small, the value did not change often, and the only failure was a version that had been deleted from NFS while the list still claimed it. A TTL of one minute closed the window to a tolerable size. The rare false hit cost one extra download. That was a known price, cheaper than the cost of running a message broker and a refresh job for a list of strings.

Pick one pattern. Write down the failure mode. Decide how often the failure mode happens and what you lose when it does. If the price feels right, ship it. If not, you have not picked a cache yet, you have picked a research project.

## Why no fourth pattern

Plenty of teams reach for a fourth option: cache versioning, two-phase invalidation, write-around with delayed fill, CDC streams out of the database. Each one is a real technique. None of them escapes the bill. Cache versioning trades the consistency window for a versioning schema and a read-side switch. Two-phase invalidation trades it for a coordinator. CDC streams trade it for a pipeline and a sink.

Every fourth pattern is one of the three above wearing extra layers. The three modes are not three answers to three different questions. They are the three ways a cache can know what changed in the source of truth: nothing (TTL), a message (event-driven), or the write itself (write-through). Anything else is a refinement of one of those three.

You choose which bill you can stomach. You do not choose to pay none.

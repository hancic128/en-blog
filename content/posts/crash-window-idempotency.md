---
title: "The Crash Window: Why I Picked Double-Counting Over Data Loss"
date: 2026-09-26
draft: false
tags: ["distributed-systems", "messaging", "idempotency", "reliability"]
slug: crash-window-idempotency
description: "Between writing the count and setting the idempotency key is a window I cannot close. I chose double-counting over data loss because I can measure what I can see."
---

A service I worked on processed inventory messages. Millions per minute. Each message carried a list of SKU changes. The service filtered, accumulated counts, and wrote them to a distributed cache.

The message queue guaranteed at-least-once delivery. That guarantee is the problem. At-least-once means the queue redelivers a message if it thinks you did not process it. Crash during processing, ack timeout, partition rebalance. All trigger redelivery. Duplicates are not exceptional. They are the baseline.

The counts are accumulators. You add to them. Process the same message twice and the count goes too high. Wrong for 30 days, the TTL of the cache entry.

Our redelivery rate was about 0.1 percent. At millions of messages per minute, that is thousands of duplicates. Each one adds a phantom count that sticks for a month. Without deduplication, the numbers drift far enough to corrupt downstream decisions within hours.

## Two operations, one window

The idempotency design splits message processing into two steps. Before handling a message, check whether a key exists in the cache. If it does, skip. If it does not, process the message, write the accumulated count, then set the key with a TTL of 10 minutes. Next time the queue redelivers, the key is there. Skip.

Writing the count and setting the key are two separate cache operations. Between them is a window. If the process crashes in that window, the service has written the count but the key is missing. The queue redelivers. The pre-check finds no key. The service processes the message again. The count goes up by one extra.

I call this the crash window. It is a few milliseconds wide. It cannot close.

Distributed caches do not support cross-key transactions. You cannot atomically write the count and set the idempotency key in a single operation. You could introduce an external transaction coordinator, or a distributed lock, or a two-phase commit. Each adds a new component that can fail, and each new component fails in ways that are harder to observe than a double-count.

## Which failure to live with

I considered reversing the order. Set the key first, then write the count. If the process crashes between them, the key exists but the count is missing. The redelivery finds the key, skips, and the service skips the write. You lose the data.

Double-counting and data loss are both failures. They differ in one way that matters. I can see double-counting. I cannot see data loss.

Double-counting produces a number that is too high. I can compare the total against the source. I can estimate the deviation from the known redelivery rate and the crash recovery time. I can alert on it.

Data loss produces nothing. The count is too low, but there is no signal. The missing data does not trigger an alert. It does not show up in any dashboard. The downstream business discovers it days later when the numbers do not match, and by then the trail is cold.

I write the count first, then set the key. If I crash between them, I double-count. I accept that deviation because I can measure it, bound it, and catch it. I cannot do any of those things with data loss.

## Bounding instead of closing

The idempotency key has a TTL of 10 minutes. Crash recovery on our infrastructure takes 1 to 3 minutes. If the process comes back within the TTL, the key is still there. The redelivery hits the pre-check, finds the key, and skips. No double-count.

The real risk is a crash where recovery takes longer than 10 minutes. This happens when the container scheduler cannot find a healthy node, or when the service mesh gets stuck on a bad configuration push. The key has expired, the pre-check misses, and the service processes the message again. The deviation is one count per message in that window.

I know the redelivery rate. I know the crash recovery distribution. I know the TTL. The product of those three numbers is the expected deviation per month. It is not zero. It is small, it is bounded, and I can tell you what it is.

I track three counters. Messages received. Idempotent keys hit, which means duplicates caught. Occupation failures after successful writes, which means the crash window fired. The third counter tells me how many times the window happened. If it spikes, something changed in the crash pattern. If it stays flat, the deviation is within the expected range.

Batch processing narrows the window. The service collects a batch of messages, aggregates them in memory, and submits one pipeline write to the cache. The crash window exists between that single write and the subsequent key occupation. A batch of 500 messages produces one window, not 500. If the batch fails, the queue redelivers the whole batch. The idempotency check catches it.

## The trade I made

At-least-once delivery plus accumulator semantics plus distributed cache gives you a choice. You can add transactions, locks, and external state stores to approach exactly-once. Or you can accept a known, measured deviation and keep the system simple enough to reason about.

I picked the second option. The deviation in my system is not zero. I know where it comes from, I know how large it can be, and I know when it fires. That is enough for the business. The complexity of closing the last few milliseconds was not worth the failure modes it would introduce.

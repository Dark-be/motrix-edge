# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Product and Trade Secret source code contains trade secrets of Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

"""Bounded non-blocking recording streams with one MCAP backend owner."""

from __future__ import annotations

import collections
import copy
import queue
import threading
import time


class MultiStreamRecorder:
    """Own one collector on a writer thread and admit state/image events independently.

    Producers never call the collector and never wait for disk writes. A recording session opens
    both bounded queues, rejects new events when sealed, drains accepted events on normal finish,
    and discards them on cancel. The wrapped collector remains the schema/metadata single source.
    """

    def __init__(self, collector, capacity: int = 512, image_names=()):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("recording.queue_size must be a positive integer")
        self.collector = collector
        self.capacity = capacity
        self.image_names = tuple(image_names)
        self._lock = threading.Lock()
        self._queues = self._new_queues()
        self._accepted = collections.Counter()
        self._dropped = collections.Counter()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._admitting = False
        self._cancelled = False
        self.phase = "idle"
        self.error: str | None = None
        self.last_summary: dict | None = None

    @property
    def save_dir(self):
        return self.collector.save_dir

    @property
    def meta(self):
        return self.collector.meta

    @property
    def active(self) -> bool:
        with self._lock:
            return self._admitting

    def _new_queues(self):
        queues = {
            "state": queue.Queue(self.capacity),
            "action": queue.Queue(self.capacity),
            # Capture metadata is control-plane state, not a lossy sensor stream.
            "meta": queue.Queue(),
        }
        if self.image_names:
            queues.update({f"image:{name}": queue.Queue(self.capacity) for name in self.image_names})
        else:
            queues["image"] = queue.Queue(self.capacity)
        return queues

    def set_meta(self, meta: dict) -> None:
        # Metadata is small and is finalized by the writer. Serializing updates through a queue
        # avoids mutating collector state concurrently with finish().
        payload = copy.deepcopy(dict(meta or {}))
        with self._lock:
            if self._admitting:
                self._put_locked("meta", payload)
                return
        self.collector.set_meta(payload)

    def start(self) -> None:
        with self._lock:
            if self._admitting or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("Recording already active")
            self._queues = self._new_queues()
            self._accepted.clear()
            self._dropped.clear()
            self._stop.clear()
            self._ready.clear()
            self._cancelled = False
            self.error = None
            self.last_summary = None
            self.phase = "preparing"
            self._admitting = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="multistream-mcap-writer")
            self._thread.start()
        if not self._ready.wait(5.0):
            self._seal(cancel=True)
            raise TimeoutError("Recorder did not start")
        if self.error is not None:
            self._seal(cancel=True)
            raise RuntimeError(self.error)

    def publish_state(self, observation: dict) -> bool:
        return self._publish("state", observation)

    def publish_action(self, observation: dict) -> bool:
        return self._publish("action", observation)

    def publish_image(self, observation: dict, name: str | None = None) -> bool:
        kind = f"image:{name}" if name is not None else "image"
        return self._publish(kind, observation)

    def _publish(self, kind: str, observation: dict) -> bool:
        payload = copy.deepcopy(observation)
        with self._lock:
            if not self._admitting:
                return False
            return self._put_locked(kind, payload)

    def _put_locked(self, kind: str, payload) -> bool:
        target = self._queues[kind]
        try:
            target.put_nowait(payload)
        except queue.Full:
            self._dropped[kind] += 1
            return False
        self._accepted[kind] += 1
        return True

    def finish(self) -> dict | None:
        return self._seal(cancel=False)

    def cancel(self) -> dict | None:
        return self._seal(cancel=True)

    def _seal(self, cancel: bool) -> dict | None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return self.last_summary
            self._admitting = False
            self._cancelled = cancel
            self.phase = "cancelling" if cancel else "finalizing"
            if cancel:
                self._discard_queued_locked()
            self._stop.set()
        thread.join(10.0)
        if thread.is_alive():
            raise TimeoutError("MCAP writer did not stop; closing continues in background")
        return self.last_summary

    def _discard_queued_locked(self) -> None:
        for kind, events in self._queues.items():
            while True:
                try:
                    events.get_nowait()
                except queue.Empty:
                    break
                self._dropped[f"{kind}_cancelled"] += 1

    def status(self) -> dict:
        with self._lock:
            depths = {kind: events.qsize() for kind, events in self._queues.items()}
            return {
                "phase": self.phase,
                "accepted": dict(self._accepted),
                "dropped": dict(self._dropped),
                "queue_depth": depths,
                "error": self.error,
                "last_summary": copy.deepcopy(self.last_summary),
            }

    def _run(self) -> None:
        started_ns = time.time_ns()
        errors: list[str] = []
        try:
            self.collector.start()
            self.phase = "recording"
            self._ready.set()
            while not self._stop.is_set() or self._has_events():
                if not self._drain_once():
                    self._stop.wait(0.002)
            summary = self._summary(started_ns, errors)
            if self._cancelled:
                self.collector.cancel()
            else:
                self.collector.set_meta({"recording": summary})
                self.collector.finish()
        except Exception as exc:  # noqa: BLE001 writer boundary records and contains backend failures
            self.error = f"{type(exc).__name__}: {exc}"
            errors.append(self.error)
            try:
                self.collector.cancel()
            except Exception as cancel_exc:  # noqa: BLE001 best-effort cleanup after the primary failure
                errors.append(f"cancel: {type(cancel_exc).__name__}: {cancel_exc}")
        finally:
            self._ready.set()
            final_summary = self._summary(started_ns, errors)
            with self._lock:
                self._admitting = False
                self.last_summary = final_summary
                self.phase = "cancelled" if self._cancelled and not errors else "failed" if errors else "completed"
                self._thread = None

    def _has_events(self) -> bool:
        return any(not events.empty() for events in self._queues.values())

    def _drain_once(self) -> bool:
        worked = False
        order = ("state", "action", *[f"image:{name}" for name in self.image_names], "image", "meta")
        for kind in order:
            events = self._queues.get(kind)
            if events is None:
                continue
            for _ in range(8):
                try:
                    payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "state":
                    collect = getattr(self.collector, "collect_qpos", None)
                    if collect is None:
                        collect = getattr(self.collector, "collect_state", None)
                    if collect is None:
                        collect = self.collector.collect
                    collect(payload)
                elif kind == "action":
                    collect = getattr(self.collector, "collect_action", None)
                    if collect is None:
                        collect = self.collector.collect
                    collect(payload)
                elif kind.startswith("image"):
                    collect = getattr(self.collector, "collect_images", None)
                    if collect is None:
                        collect = self.collector.collect
                    collect(payload)
                else:
                    self.collector.set_meta(payload)
                worked = True
        return worked

    def _summary(self, started_ns: int, errors: list[str]) -> dict:
        with self._lock:
            accepted = dict(self._accepted)
            dropped = dict(self._dropped)
        return {
            "started_ns": started_ns,
            "stopped_ns": time.time_ns(),
            "accepted": accepted,
            "dropped": dropped,
            "data_loss": bool(dropped) or bool(errors),
            "errors": list(errors),
            "cancelled": self._cancelled,
        }

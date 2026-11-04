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

"""Asynchronous multi-stream admission, draining, cancellation and failure isolation."""

import threading
from pathlib import Path

from streams.recording import MultiStreamRecorder


class Collector:
    def __init__(self, directory: Path):
        self.save_dir = directory
        self.meta = {}
        self.calls = []
        self.block_state = None
        self.state_started = threading.Event()
        self.fail_images = False

    def start(self):
        self.calls.append("start")

    def collect_state(self, payload):
        self.calls.append(("state", payload))
        self.state_started.set()
        if self.block_state is not None:
            self.block_state.wait(2)

    def collect_images(self, payload):
        if self.fail_images:
            raise OSError("disk failed")
        self.calls.append(("image", payload))

    def set_meta(self, payload):
        self.meta.update(payload)
        self.calls.append(("meta", payload))

    def finish(self):
        self.calls.append("finish")

    def cancel(self):
        self.calls.append("cancel")


def test_finish_drains_independent_streams_and_records_summary(tmp_path):
    collector = Collector(tmp_path)
    recorder = MultiStreamRecorder(collector, capacity=4)

    recorder.start()
    assert recorder.publish_state({"seq": 1})
    assert recorder.publish_image({"camera": "head"})
    recorder.set_meta({"operator": "tester"})
    summary = recorder.finish()

    assert ("state", {"seq": 1}) in collector.calls
    assert ("image", {"camera": "head"}) in collector.calls
    assert collector.calls[-1] == "finish"
    assert collector.meta["operator"] == "tester"
    assert collector.meta["recording"]["accepted"] == {"state": 1, "image": 1, "meta": 1}
    assert summary["accepted"] == {"state": 1, "image": 1, "meta": 1}
    assert summary["dropped"] == {}
    assert recorder.status()["phase"] == "completed"


def test_full_stream_queue_drops_without_blocking_producer(tmp_path):
    collector = Collector(tmp_path)
    collector.block_state = threading.Event()
    recorder = MultiStreamRecorder(collector, capacity=2)
    recorder.start()

    assert recorder.publish_state({"seq": 0})
    assert collector.state_started.wait(1)
    assert recorder.publish_state({"seq": 1})
    assert recorder.publish_state({"seq": 2})
    assert not recorder.publish_state({"seq": 3})

    collector.block_state.set()
    summary = recorder.finish()

    assert summary["accepted"]["state"] == 3
    assert summary["dropped"]["state"] == 1
    assert summary["data_loss"] is True


def test_cancel_discards_queued_events_and_calls_backend_cancel(tmp_path):
    collector = Collector(tmp_path)
    collector.block_state = threading.Event()
    recorder = MultiStreamRecorder(collector, capacity=4)
    recorder.start()
    assert recorder.publish_state({"seq": 0})
    assert collector.state_started.wait(1)
    assert recorder.publish_image({"camera": "head"})

    result = []
    stop_thread = threading.Thread(target=lambda: result.append(recorder.cancel()))
    stop_thread.start()
    collector.block_state.set()
    stop_thread.join(2)
    summary = result[0]

    assert "cancel" in collector.calls
    assert "finish" not in collector.calls
    assert summary["cancelled"] is True
    assert recorder.status()["phase"] == "cancelled"


def test_writer_failure_is_contained_and_reported(tmp_path):
    collector = Collector(tmp_path)
    collector.fail_images = True
    recorder = MultiStreamRecorder(collector)
    recorder.start()
    assert recorder.publish_image({"camera": "head"})

    summary = recorder.finish()

    assert summary["errors"] == ["OSError: disk failed"]
    assert summary["data_loss"] is True
    assert recorder.status()["phase"] == "failed"
    assert "cancel" in collector.calls

"""The pipeline reports through logging, and a failed stage stays failed."""

from __future__ import annotations

import logging

import pytest

from kvasir.storm.logging_wrapper import LoggingWrapper


class _LMConfigs:
    def collect_and_reset_lm_usage(self):
        return {}

    def collect_and_reset_lm_history(self):
        return []


def test_a_failing_stage_reaches_the_caller(caplog):
    """Upstream caught, printed and fell through, so a failed stage looked successful."""
    wrapper = LoggingWrapper(_LMConfigs())

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match="no sources"),
        wrapper.log_pipeline_stage("warm start"),
    ):
        raise ValueError("no sources")

    assert "warm start" in caplog.text
    # The stage is still closed out, so its wall time is recorded rather than lost.
    assert wrapper.dump_logging_and_reset()["warm start"]["total_wall_time"] >= 0


def test_timestamps_are_utc():
    """Upstream rendered every Co-STORM timestamp in America/Los_Angeles."""
    from datetime import UTC, datetime

    from kvasir.storm.logging_wrapper import EventLog

    event = EventLog("search")
    event.record_start_time()
    event.record_end_time()

    assert event.start_time.tzinfo is UTC
    expected = datetime.now(UTC).strftime("%Y-%m-%d %H")
    assert event.get_start_time().startswith(expected)


def test_an_unstarted_event_has_no_timestamp():
    from kvasir.storm.logging_wrapper import EventLog

    assert EventLog("search").get_start_time() is None


def test_an_overlapping_stage_warns_rather_than_printing(caplog, capsys):
    wrapper = LoggingWrapper(_LMConfigs())
    wrapper._pipeline_stage_start("first")

    with caplog.at_level(logging.WARNING), wrapper.log_pipeline_stage("second"):
        pass

    assert "already active" in caplog.text
    assert capsys.readouterr().out == ""

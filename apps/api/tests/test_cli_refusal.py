import json

import pytest

from app.cli._refusal import REFUSED_EXIT_CODE, run_command


class Args:
    command = "freeze"


def test_a_governance_refusal_is_reported_as_a_result(capsys):
    def refuses(_args):
        raise ValueError("Run 9 is a discovery run.")

    with pytest.raises(SystemExit) as exit_info:
        run_command(refuses, Args(), banner="banner")

    # Non-zero, so a shell loop still stops -- it just says what happened
    # instead of printing a stack trace that reads like a defect.
    assert exit_info.value.code == REFUSED_EXIT_CODE
    payload = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert payload["refused"] is True
    assert payload["command"] == "freeze"
    assert "discovery run" in payload["reason"]


def test_a_successful_command_prints_its_result(capsys):
    with_result = run_command(lambda _args: {"ok": True}, Args(), banner="banner")

    payload = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert with_result is None
    assert payload == {"ok": True}


def test_an_unexpected_failure_is_not_disguised_as_a_refusal():
    def breaks(_args):
        raise KeyError("missing_column")

    # A real defect must still surface as a defect.
    with pytest.raises(KeyError):
        run_command(breaks, Args(), banner="banner")

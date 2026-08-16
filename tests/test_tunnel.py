from pathlib import Path

from aitlc.adapters.lambdatest.tunnel import check_status


def test_missing_log_is_unhealthy(tmp_path: Path):
    result = check_status(tmp_path / "does-not-exist.log")
    assert not result.healthy


def test_healthy_launch_marker(tmp_path: Path):
    log = tmp_path / "tunnel.log"
    log.write_text(
        "INFO Launching tunnel\nINFO You can start testing now\nINFO Tunnel ID: 123\n"
    )
    result = check_status(log)
    assert result.healthy


def test_ctrl_conn_down_is_unhealthy_even_with_earlier_healthy_marker(tmp_path: Path):
    # Real observed scenario: tunnel launches healthy, runs a while,
    # THEN the control connection dies later in the same log file.
    log = tmp_path / "tunnel.log"
    log.write_text(
        "INFO You can start testing now\n"
        "...\n"
        "ERROR ERR::WS::CTRL::CONN::DWN : Control websocket connection closed\n"
    )
    result = check_status(log)
    assert not result.healthy
    assert "ERR::WS::CTRL::CONN::DWN" in result.detail


def test_max_attempt_reached_is_unhealthy(tmp_path: Path):
    log = tmp_path / "tunnel.log"
    log.write_text("ERROR Err occurred: ERR::CTRL::CONN::MAX::ATTEMPT\n")
    result = check_status(log)
    assert not result.healthy


def test_tunnel_not_running_message_is_unhealthy(tmp_path: Path):
    # Real message returned by LambdaTest's own CDP endpoint.
    log = tmp_path / "tunnel.log"
    log.write_text("some log content, tunnel is not running or disconnected\n")
    result = check_status(log)
    assert not result.healthy


def test_neither_marker_present_is_unclear_and_unhealthy(tmp_path: Path):
    log = tmp_path / "tunnel.log"
    log.write_text("INFO Launching tunnel\n")
    result = check_status(log)
    assert not result.healthy

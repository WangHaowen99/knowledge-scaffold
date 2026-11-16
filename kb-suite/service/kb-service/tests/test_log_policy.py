"""The HTTP client libraries stay at warnings, so request URLs (model service addresses) are not logged."""

import logging

from kb_service import log_policy


def test_http_client_request_lines_are_not_logged():
    log_policy.quiet_http_clients()
    for name in log_policy.QUIET_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
        assert logging.getLogger(name).isEnabledFor(logging.WARNING)

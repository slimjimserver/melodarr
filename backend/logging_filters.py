"""Keep Room invitation capabilities out of development HTTP request logs."""

import logging
import re


class RoomInviteFilter(logging.Filter):
    def filter(self, record):
        message = re.sub(
            r"(/rooms/[^?\s\"<>]+)\?[^\s\"<>]+", r"\1?[redacted]", record.getMessage()
        )
        record.msg = re.sub(r"([?&]invite=)[^&\s\"<>]+", r"\1[redacted]", message)
        record.args = ()
        return True


def protect_invite_logs():
    logger = logging.getLogger("werkzeug")
    if not any(isinstance(item, RoomInviteFilter) for item in logger.filters):
        logger.addFilter(RoomInviteFilter())

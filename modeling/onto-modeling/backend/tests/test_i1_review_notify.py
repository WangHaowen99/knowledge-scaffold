"""I1 审查修复第 5 条：同组未读通知被替换时，用户流事件带 `replaces`（被替换的编号），前端照着替换，未读数不会越加越多。"""

import logging

from app import config
from app.services import auth, notifications, streams

logger = logging.getLogger(__name__)


def test_group_replacement_is_signalled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", True)
    streams.reset_all()
    published = []
    monkeypatch.setattr(streams, "publish_user", lambda user, kind, data, project_id=None: published.append((kind, data)))
    for _ in range(3):
        notifications.session_event("task-x", "c1", {"type": "result", "subtype": "success"}, owner="alice")
    listed = notifications.list_notifications("alice")
    logger.info("stored: %d unread: %d | pushed: %d | pushed ids: %s | stored id: %s", len(listed),
                notifications.unread_count("alice"), len(published), [data["id"] for _kind, data in published],
                listed[0]["id"])
    assert len(listed) == 1 and len(published) == 3
    ids = [data["id"] for _kind, data in published]
    assert published[0][1].get("replaces", []) == []
    assert published[1][1]["replaces"] == [ids[0]] and published[2][1]["replaces"] == [ids[1]]
    assert listed[0]["id"] == ids[2] and notifications.unread_count("alice") == 1

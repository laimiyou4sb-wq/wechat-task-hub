from datetime import datetime

from app.config import Settings
from app.utils import in_quiet_hours
from app.weflow import WeFlowClient


def test_stream_event_conversion():
    payload = {
        "event": "message.new",
        "sessionId": "123@chatroom",
        "sessionType": "group",
        "rawid": "r1",
        "sourceName": "张三",
        "groupName": "项目群",
        "content": "明天交材料",
        "timestamp": 1760000000,
    }
    message = WeFlowClient._convert_stream_event(payload)
    assert message["rawid"] == "r1"
    assert message["group_name"] == "项目群"
    assert message["sender"] == "张三"
    assert message["is_group"] is True
    assert message["ts"] == 1760000000


def test_quiet_hours_cross_midnight():
    assert in_quiet_hours(datetime(2026, 10, 23, 23, 30), "23:00", "07:30") is True
    assert in_quiet_hours(datetime(2026, 10, 24, 3, 0), "23:00", "07:30") is True
    assert in_quiet_hours(datetime(2026, 10, 24, 9, 0), "23:00", "07:30") is False
    assert in_quiet_hours(datetime(2026, 10, 23, 12, 0), "12:00", "13:00") is True


def test_session_whitelist_matching():
    client = WeFlowClient(Settings(group_whitelist=["项目"], include_private=False))
    assert client._session_allowed({"username": "1@chatroom", "displayName": "项目群", "type": 2}) is True
    assert client._session_allowed({"username": "2@chatroom", "displayName": "闲聊群", "type": 2}) is False
    assert client._session_allowed({"username": "wxid_1", "displayName": "张三", "type": 1}) is False

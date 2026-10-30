from app.config import Settings
from app.wcda import WcdaClient


def _payload(message: dict, **extra) -> dict:
    payload = {
        "type": "message",
        "account": "wxid_me",
        "username": "123@chatroom",
        "conversationName": "项目群",
        "received": True,
        "cursor": message.get("localId", 0),
        "ts": 1760000000000,
        "message": message,
    }
    payload.update(extra)
    return payload


def test_convert_group_message():
    payload = _payload({
        "id": "message_0.db:Msg_abc:101",
        "localId": 101,
        "serverId": 6116895530414915131,
        "createTime": 1760000000,
        "senderUsername": "wxid_a",
        "from": "张三",
        "isSent": False,
        "content": "明天下午3点开会，记得带材料",
        "type": 1,
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["rawid"] == "message_0.db:Msg_abc:101"
    assert message["session_id"] == "123@chatroom"
    assert message["group_name"] == "项目群"
    assert message["sender"] == "张三"
    assert message["content"].startswith("明天下午3点")
    assert message["ts"] == 1760000000
    assert message["is_group"] is True


def test_convert_private_message_and_skip_sent():
    payload = _payload({
        "id": "message_0.db:Msg_abc:102",
        "localId": 102,
        "createTime": 1760000001,
        "senderUsername": "wxid_b",
        "isSent": True,
        "content": "收到",
    }, username="wxid_b", conversationName="")
    assert WcdaClient.convert_message(payload) is None


def test_skip_system_and_join_messages():
    system_payload = _payload({
        "id": "message_0.db:Msg_abc:104",
        "localId": 104,
        "createTime": 1760000002,
        "type": 10000,
        "renderType": "system",
        "content": "\"$username$\"邀请\"$names$\"加入了群聊",
        "isSent": False,
    })
    assert WcdaClient.convert_message(system_payload) is None

    privacy_payload = _payload({
        "id": "message_0.db:Msg_abc:105",
        "localId": 105,
        "createTime": 1760000003,
        "type": 10000,
        "renderType": "system",
        "content": "\"菲\"与群里其他人都不是朋友关系，请注意隐私安全",
        "isSent": False,
    })
    assert WcdaClient.convert_message(privacy_payload) is None


def test_link_card_keeps_render_type():
    payload = _payload({
        "id": "message_0.db:Msg_abc:106",
        "localId": 106,
        "createTime": 1760000004,
        "type": 141733920817,
        "renderType": "link",
        "content": "群接龙",
        "isSent": False,
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["render_type"] == "link"
    assert message["msg_type"] == 141733920817


def test_convert_falls_back_to_server_id_and_converts_ms():
    payload = _payload({
        "localId": 103,
        "serverId": 6116895530414915133,
        "createTime": 1760000000123,
        "senderUsername": "wxid_c",
        "isSent": False,
        "content": "10月28日前交作业",
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["rawid"] == "6116895530414915133"
    assert message["ts"] == 1760000000


def test_configured_when_base_present():
    assert WcdaClient(Settings(wcda_base="http://127.0.0.1:10392")).configured is True
    assert WcdaClient(Settings(wcda_base="")).configured is False


def test_convert_keeps_at_mentions():
    """微信原始 @ 名单要透传到上层，身份判定优先用它。"""
    payload = _payload({
        "id": "message_0.db:Msg_abc:107",
        "localId": 107,
        "createTime": 1760000005,
        "senderUsername": "wxid_a",
        "isSent": False,
        "content": "@001大王 明天交表",
        "type": 1,
        "atUsernames": ["wxid_me"],
        "atUsers": [{"username": "wxid_me", "displayName": "001大王", "avatar": ""}],
        "quoteUsername": "wxid_other",
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["at_usernames"] == ["wxid_me"]
    assert message["at_names"] == ["001大王"]
    assert message["quote_username"] == "wxid_other"


def test_convert_at_all():
    payload = _payload({
        "id": "message_0.db:Msg_abc:108",
        "localId": 108,
        "createTime": 1760000006,
        "senderUsername": "wxid_a",
        "isSent": False,
        "content": "@所有人 明天停水",
        "type": 1,
        "atUsernames": ["notify@all"],
        "atUsers": [],
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["at_usernames"] == ["notify@all"]


def test_convert_keeps_link_card_fields():
    payload = _payload({
        "id": "message_0.db:Msg_abc:109",
        "localId": 109,
        "createTime": 1760000007,
        "senderUsername": "wxid_a",
        "isSent": False,
        "content": "11月1日科创破冰活动人数统计",
        "type": 141733920817,
        "renderType": "link",
        "title": "11月1日科创破冰活动人数统计",
        "url": "https://example.com/form",
        "linkType": "url",
    })
    message = WcdaClient.convert_message(payload)
    assert message is not None
    assert message["render_type"] == "link"
    assert message["link_title"] == "11月1日科创破冰活动人数统计"
    assert message["link_url"] == "https://example.com/form"


def test_convert_api_message_for_catchup():
    """补采用：/api/chat/messages 的原始消息也要能转成内部标准 dict。"""
    raw = {
        "id": "message_3:Msg_abc:290",
        "localId": 290,
        "createTime": 1792767783,
        "senderUsername": "wxid_a",
        "senderDisplayName": "25机电金正豪",
        "isSent": False,
        "content": "11月1日科创破冰活动人数统计",
        "type": 141733920817,
        "renderType": "link",
        "title": "11月1日科创破冰活动人数统计",
        "url": "https://example.com/form",
        "atUsernames": [],
    }
    message = WcdaClient.convert_api_message("49675822102@chatroom", "项目群", raw)
    assert message is not None
    assert message["rawid"] == "message_3:Msg_abc:290"
    assert message["session_id"] == "49675822102@chatroom"
    assert message["group_name"] == "项目群"
    assert message["content"] == "11月1日科创破冰活动人数统计"
    assert message["link_title"] == "11月1日科创破冰活动人数统计"
    assert message["ts"] == 1792767783

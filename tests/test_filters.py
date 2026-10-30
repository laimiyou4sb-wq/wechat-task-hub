from app.filters import importance, is_allowed_group, should_keep


def test_noise_filtered():
    assert should_keep("[图片]") is False
    assert should_keep("收到") is False
    assert should_keep("哈哈哈哈") is False
    assert should_keep("https://example.com/a") is False
    assert should_keep("") is False


def test_meaningful_messages_kept():
    assert should_keep("明天下午3点开组会，记得带材料") is True
    assert should_keep("@小明 周五前把报名表交了") is True


def test_group_whitelist():
    assert is_allowed_group("1@chatroom", "项目群", [], True, False) is True
    assert is_allowed_group("wxid_1", "", [], False, False) is False
    assert is_allowed_group("wxid_1", "", [], False, True) is True
    assert is_allowed_group("1@chatroom", "项目群", ["项目"], True, False) is True
    assert is_allowed_group("1@chatroom", "闲聊群", ["项目"], True, False) is False


def test_importance_mentions_me_is_higher():
    plain = importance("周五前提交材料")
    mentioned = importance("@小明 周五前提交材料", ["小明"])
    assert mentioned > plain

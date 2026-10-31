from app.filters import importance, is_allowed_group, should_keep, should_send_to_llm


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


def test_noise_word_does_not_kill_real_todo():
    """P2-1：正文含“收到/已满”等噪音子串，但同时有动作/时间信号时不能整条过滤。"""
    text = "记得今晚8点前交材料，收到请回复"
    assert should_keep(text) is True
    assert should_send_to_llm(text) is True


def test_noise_word_rescued_by_time_signal_only():
    assert should_send_to_llm("收到，今晚8点前交材料") is True


def test_noise_word_rescued_by_mentions_me():
    assert should_keep("@小明 收到，材料在哪", ["小明"]) is True


def test_pure_noise_still_filtered_after_rescue_rule():
    """没有动作/时间/截止/@我信号的纯噪音，仍然要过滤。"""
    for text in ("收到", "哈哈哈哈", "人数满了，结束了", "已满，结束了",
                 "https://example.com/a", "可以吗"):
        assert should_keep(text) is False, text
        assert should_send_to_llm(text) is False, text

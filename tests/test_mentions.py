"""@/点名判定单测：只做“明确指向某人”的判定，其余保持 none。"""
from app.identity import IdentityContext
from app.mentions import classify_message, parse_at_tokens


def _identity(**overrides) -> IdentityContext:
    data = dict(
        session_id="54199850704@chatroom",
        group_name="祥转老大粉丝群",
        is_group=True,
        account="wxid_me",
        aliases=["大王", "001大王", "laimiyou001"],
        group_nickname="001大王",
        members={"某某": "wxid_wudi", "417龙嘉杰": "wxid_long"},
        member_ids={"wxid_wudi": "某某", "wxid_long": "417龙嘉杰"},
        auto=True,
    )
    data.update(overrides)
    return IdentityContext(**data)


def _verdict(text, **kwargs):
    kwargs.setdefault("identity", _identity())
    return classify_message(text, **kwargs)


def test_at_usernames_is_authoritative():
    assert _verdict("@001大王 明天交表", at_usernames=["wxid_me"]).kind == "me"
    other = _verdict("@某某 吧唧的好贵", at_usernames=["wxid_wudi"])
    assert other.kind == "other"
    assert other.targets == ["某某"]
    assert _verdict("@所有人 明天停水", at_usernames=["notify@all"]).kind == "all"
    # 群公告的 at 名单是 announcement@all，也属于 @所有人（不能被当成“他人”丢掉）
    assert _verdict("@所有人 群公告", at_usernames=["announcement@all"]).kind == "all"
    # 同时 @ 我和别人：按“需要我参与”保留
    assert _verdict("@我 @他", at_usernames=["wxid_me", "wxid_wudi"]).kind == "me"


def test_at_other_targets_prefer_text_nicknames():
    """@ 名单是 wxid，正文里是发送者看到的昵称；能对齐时用正文昵称更好读。"""
    identity = _identity(members={"Funny": "wxid_teacher"}, member_ids={"wxid_teacher": "Funny"})
    verdict = classify_message("@资助中心 张老师\u2005这个咋回事", at_usernames=["wxid_teacher"],
                              identity=identity)
    assert verdict.kind == "other"
    assert verdict.targets == ["资助中心 张老师"]


def test_text_at_fallback_when_no_at_list():
    assert _verdict("@001大王 明天交表").kind == "me"
    assert _verdict("@001大王\u2005明天交表").kind == "me"
    other = _verdict("@某某\u2005你去谈一个")
    assert other.kind == "other"
    assert other.reason.startswith("手动@他人")


def test_text_at_typo_matches_me():
    # 打错一个字 / 同音字
    assert _verdict("@001大旺 明天交表").kind == "me"


def test_leading_vocative():
    assert _verdict("大王，明早八点来一趟").kind == "me"
    assert _verdict("大旺，明早八点来一趟").kind == "me"
    other = _verdict("某某，你去谈一个")
    assert other.kind == "other"
    assert _verdict("417龙嘉杰 把表交了").kind == "other"


def test_assignment_patterns():
    assert _verdict("麻烦某某交一下表").kind == "other"
    assert _verdict("让大王把表交了").kind == "me"
    assert _verdict("某某负责周三值日").kind == "other"
    # 不要误伤“邀请”等词
    assert _verdict("邀请大王参加晚会").kind == "none"


def test_no_directed_person_keeps_none():
    """没 @、没点名：必须保持 none，原待办逻辑不受影响。"""
    for text in (
        "明天下午三点开组会，记得带材料",
        "本周五18:00前提交报名表，逾期不候",
        "大王卡很便宜，快去买",
        "小王八真好吃",
        "老师说明天交表",
    ):
        assert _verdict(text).kind == "none", text


def test_unknown_at_token_does_not_discard():
    # 没拉到群成员时，无法确定 @ 的是谁 → 不丢弃（保守）
    identity = _identity(members={}, member_ids={})
    verdict = classify_message("@路人甲 你去交表", identity=identity)
    assert verdict.kind == "none"


def test_account_unknown_falls_back_to_text():
    identity = _identity(account="")
    assert classify_message("@001大王 明天交表", identity=identity).kind == "me"


def test_quote_me_is_me():
    verdict = _verdict("刚才那个材料在哪", quote_username="wxid_me")
    assert verdict.kind == "me"
    assert verdict.source == "quote"
    # 明确指向别人时，引用我的消息不能盖过结论
    verdict = _verdict("@某某 你发一下", at_usernames=["wxid_wudi"], quote_username="wxid_me")
    assert verdict.kind == "other"


def test_fuzzy_can_be_disabled():
    assert _verdict("大旺，明早八点来一趟", fuzzy=False).kind == "none"
    assert _verdict("@001大旺 明天交表", fuzzy=False).kind == "none"


def test_parse_at_tokens():
    assert parse_at_tokens("@001大王\u2005明天交表") == ["001大王"]
    assert parse_at_tokens("请 @某某 交表") == ["某某"]
    assert parse_at_tokens("没有at的消息") == []


def test_at_all_text_variants():
    assert _verdict("@所有人 明天停水").kind == "all"
    assert _verdict("@全体成员 注意").kind == "all"

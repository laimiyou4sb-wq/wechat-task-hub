"""身份解析单测：自动同步 + 手动别名 + 失败兜底 + 缓存。"""
import asyncio

from app.config import Settings
from app.identity import IdentityResolver, fuzzy_equal, normalize_name


class FakeWcda:
    configured = True

    def __init__(self, account="wxid_me", profile=None, members=None, fail_groups=False):
        self.account = account
        self.profile = profile or {"displayName": "001大王", "nickname": "001大王",
                                   "remark": "", "alias": "laimiyou001"}
        self.members = members if members is not None else [
            {"username": "wxid_me", "displayName": "001大王"},
            {"username": "wxid_wudi", "displayName": "某某"},
        ]
        self.fail_groups = fail_groups
        self.calls = {"account": 0, "profile": 0, "members": 0}

    async def fetch_current_account(self):
        self.calls["account"] += 1
        return self.account

    async def fetch_contact_profile(self, username):
        self.calls["profile"] += 1
        return dict(self.profile)

    async def fetch_group_members(self, username, limit=300):
        self.calls["members"] += 1
        if self.fail_groups:
            raise RuntimeError("boom")
        return list(self.members)


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        data_source="wcda",
        db_path=tmp_path / "identity.db",
        app_token="test",
        me_names=[],
        me_wxid="",
        identity_ttl_seconds=600,
    )
    data.update(overrides)
    return Settings(**data)


def test_normalize_name():
    assert normalize_name("  ＠001大王 ") == "001大王"
    assert normalize_name("001\u2005大王") == "001 大王"
    assert normalize_name("ＡＢＣ") == "abc"
    assert normalize_name("") == ""


def test_fuzzy_equal():
    assert fuzzy_equal("大王", "大旺")            # 打错一个字（同音）
    assert fuzzy_equal("001大王", "001大旺")      # 带群昵称前缀
    assert fuzzy_equal("大王", "大王")            # 精确
    assert not fuzzy_equal("大王", "大王卡")       # 长度不同，不是同一个人
    assert not fuzzy_equal("王", "大王")          # 单字不做模糊
    assert fuzzy_equal("abcdef", "abcdeg")       # 英文 ID 允许 1 个字符差异
    assert not fuzzy_equal("ab", "ac")           # 太短不做模糊


def test_resolver_auto_identity_and_cache(tmp_path):
    wcda = FakeWcda()
    resolver = IdentityResolver(_settings(tmp_path), wcda)

    context = asyncio.run(resolver.for_session("room@chatroom", "项目群"))
    assert context.auto is True
    assert context.account == "wxid_me"
    assert context.group_nickname == "001大王"
    assert context.members == {"某某": "wxid_wudi"}
    assert "001大王" in context.aliases and "laimiyou001" in context.aliases

    # 第二次命中缓存，不再请求群成员
    asyncio.run(resolver.for_session("room@chatroom", "项目群"))
    assert wcda.calls["members"] == 1

    snapshot = resolver.snapshot()
    assert snapshot["account"] == "wxid_me"
    assert snapshot["account_source"] == "auto"
    assert snapshot["groups"][0]["my_nickname"] == "001大王"


def test_resolver_falls_back_to_manual_aliases(tmp_path):
    wcda = FakeWcda(fail_groups=True)
    resolver = IdentityResolver(_settings(tmp_path, me_names=["老王"]), wcda)
    context = asyncio.run(resolver.for_session("room@chatroom", "项目群"))
    assert context.auto is False
    assert "老王" in context.aliases
    assert context.error
    # 失败后不退化成“没有别名”
    assert "老王" in resolver.plain_context("room@chatroom").aliases


def test_manual_alias_refresh_updates_cached_context(tmp_path):
    settings = _settings(tmp_path, me_names=["老王"])
    resolver = IdentityResolver(settings, FakeWcda())
    context = asyncio.run(resolver.for_session("room@chatroom", "项目群"))
    assert "老王" in context.aliases
    settings.me_names = ["老王", "大王"]
    resolver.refresh_manual()
    assert "大王" in context.aliases


def test_manual_wxid_override(tmp_path):
    wcda = FakeWcda(account="wxid_other")
    resolver = IdentityResolver(_settings(tmp_path, me_wxid="wxid_manual"), wcda)
    resolver.snapshot()
    asyncio.run(resolver.for_session("room@chatroom", "项目群"))
    assert resolver.account == "wxid_manual"
    assert resolver.account_source == "manual"
    assert wcda.calls["account"] == 0


def test_resolver_without_wcda_uses_manual_only(tmp_path):
    resolver = IdentityResolver(_settings(tmp_path, me_names=["大王"], me_wxid="wxid_manual"), None)
    context = asyncio.run(resolver.for_session("wxid_contact", "", is_group=False))
    assert context.auto is False
    assert context.aliases == ["大王"]
    assert resolver.snapshot()["account"] == "wxid_manual"

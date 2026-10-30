"""Brotli 中间件测试：br 命中、gzip 回退、JSON 一致性、304/小响应不压缩。"""
from __future__ import annotations

import gzip
import random

import pytest
from fastapi import FastAPI, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app.main import BrotliMiddleware

HEADERS = {"X-Auth-Token": "test-token"}

BIG_HTML = "<!doctype html><html><body>" + ("<p>微信待办</p>" * 200) + "</body></html>"
BIG_JSON = '{"items":[' + ",".join(f'"item-{i}"' for i in range(400)) + "]}"


def _build_app() -> FastAPI:
    """与 create_app 相同的中间件顺序：GZip 内层、Brotli 外层。"""
    app = FastAPI()

    @app.get("/big.html")
    def big_html() -> Response:
        return Response(BIG_HTML, media_type="text/html")

    @app.get("/big.json")
    def big_json() -> Response:
        return Response(BIG_JSON, media_type="application/json")

    @app.get("/small.html")
    def small_html() -> Response:
        return Response("<html>ok</html>", media_type="text/html")

    @app.get("/logo.png")
    def logo_png() -> Response:
        return Response(b"\x89PNG\r\n\x1a\n" + b"0" * 2000, media_type="image/png")

    @app.get("/already-encoded.html")
    def already_encoded() -> Response:
        return Response(
            gzip.compress(BIG_HTML.encode("utf-8")),
            media_type="text/html",
            headers={"content-encoding": "gzip"},
        )

    @app.get("/incompressible.html")
    def incompressible() -> Response:
        # 随机数据压缩后反而更大 → 中间件应放弃压缩
        return Response(random.Random(42).randbytes(3000), media_type="text/html")

    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.add_middleware(BrotliMiddleware, minimum_size=500, quality=11)
    return app


@pytest.fixture(scope="module")
def br_client():
    with TestClient(_build_app()) as test_client:
        yield test_client


def test_br_preferred_over_gzip(br_client):
    for accept in ("br", "br, gzip", "gzip, br", "br;q=0.9, gzip;q=0.9", "br;q=1.0, gzip;q=0"):
        response = br_client.get("/big.html", headers={"Accept-Encoding": accept})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "br", accept
        # httpx 自动解码；解压后必须与原文完全一致（也证明没有被 gzip 二次压缩）
        assert response.text == BIG_HTML, accept
        assert int(response.headers["content-length"]) < len(BIG_HTML.encode("utf-8"))


def test_br_response_has_vary_header(br_client):
    response = br_client.get("/big.html", headers={"Accept-Encoding": "br"})
    assert "accept-encoding" in response.headers.get("vary", "").lower()


@pytest.mark.parametrize("accept", [
    "gzip",
    "gzip, deflate",
    "br;q=0.5, gzip;q=1.0",
    "br;q=0, gzip;q=1",
])
def test_falls_back_to_gzip(br_client, accept):
    response = br_client.get("/big.html", headers={"Accept-Encoding": accept})
    assert response.headers.get("content-encoding") == "gzip"
    assert response.text == BIG_HTML


def test_br_rejected_q0_alone_keeps_identity(br_client):
    response = br_client.get("/big.html", headers={"Accept-Encoding": "br;q=0"})
    assert "content-encoding" not in response.headers
    assert response.text == BIG_HTML


def test_identity_request_not_compressed(br_client):
    response = br_client.get("/big.html", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in response.headers
    assert response.text == BIG_HTML


def test_json_roundtrip_matches_identity(br_client):
    br_response = br_client.get("/big.json", headers={"Accept-Encoding": "br"})
    plain_response = br_client.get("/big.json", headers={"Accept-Encoding": "identity"})
    assert br_response.headers.get("content-encoding") == "br"
    assert br_response.content == plain_response.content
    assert br_response.json() == plain_response.json()


def test_small_response_not_compressed(br_client):
    response = br_client.get("/small.html", headers={"Accept-Encoding": "br"})
    assert "content-encoding" not in response.headers
    assert response.text == "<html>ok</html>"


def test_non_text_content_type_not_compressed(br_client):
    response = br_client.get("/logo.png", headers={"Accept-Encoding": "br"})
    assert "content-encoding" not in response.headers
    assert response.content.startswith(b"\x89PNG")


def test_already_encoded_not_double_compressed(br_client):
    response = br_client.get("/already-encoded.html", headers={"Accept-Encoding": "br, gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert response.text == BIG_HTML


def test_compressed_larger_than_original_is_discarded(br_client):
    response = br_client.get("/incompressible.html", headers={"Accept-Encoding": "br"})
    assert "content-encoding" not in response.headers
    assert response.content == random.Random(42).randbytes(3000)


def test_static_css_brotli_and_length(client):
    response = client.get("/styles.css", headers={"Accept-Encoding": "br"})
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/css")
    assert response.headers.get("content-encoding") == "br"
    assert "accept-encoding" in response.headers.get("vary", "").lower()
    assert int(response.headers["content-length"]) < len(response.text.encode("utf-8"))


def test_api_json_brotli_roundtrip(client):
    # 造足够多的待办，确保 /api/tasks 响应体 > 500B，真正走到 br 分支
    for index in range(8):
        created = client.post(
            "/api/tasks",
            headers=HEADERS,
            json={"title": f"Brotli 压缩测试待办 {index}", "notes": "压缩测试" * 20},
        )
        assert created.status_code == 201
    for _ in range(3):
        br_response = client.get("/api/tasks", headers={**HEADERS, "Accept-Encoding": "br"})
        plain_response = client.get("/api/tasks", headers={**HEADERS, "Accept-Encoding": "identity"})
        assert br_response.status_code == 200
        assert br_response.headers.get("content-encoding") == "br"
        assert "content-encoding" not in plain_response.headers
        if br_response.json() == plain_response.json():
            return
    pytest.fail("br 解压后的 JSON 与 identity 响应不一致")


def test_small_api_response_not_compressed(client):
    # /api/health 通常只有几百字节：<500B 时不压缩；>=500B 时压缩后必须能还原
    identity = client.get("/api/health", headers={"Accept-Encoding": "identity"})
    brotli = client.get("/api/health", headers={"Accept-Encoding": "br"})
    assert identity.status_code == brotli.status_code == 200
    assert brotli.content == identity.content
    if len(identity.content) < 500:
        assert "content-encoding" not in brotli.headers
    else:
        assert brotli.headers.get("content-encoding") == "br"


def test_304_not_compressed(client):
    for _ in range(3):
        first = client.get("/", headers={"Accept-Encoding": "br"})
        assert first.status_code == 200
        etag = first.headers.get("etag")
        if not etag:
            pytest.fail("/ 响应缺少 ETag，无法校验 304 行为")
        second = client.get(
            "/", headers={"Accept-Encoding": "br", "If-None-Match": etag}
        )
        if second.status_code == 304:
            assert "content-encoding" not in second.headers
            assert second.content == b""
            return
    pytest.fail("静态文件在测试期间被修改，未能稳定复现 304")

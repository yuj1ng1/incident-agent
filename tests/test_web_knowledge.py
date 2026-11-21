"""HTTP API tests for versioned knowledge base routes, auth, search, and errors."""
import hashlib
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from db.config import get_engine
from db.tables import create_runtime_schema
from knowledge.embeddings import OfflineHashEmbeddingProvider
from knowledge.models import EmbeddingError
from knowledge.store import KnowledgeStore
from mysql_support import empty_database_url
from runtime_support import FixtureAgent, create_runtime, create_session, response as model_response
from investigation.worker import Worker
from web_api.app import create_app
from web_api.auth import WebAuthorization

TOKENS = {
    "kb-writer": "k" * 40,
    "requester": "r" * 40,
    "approver": "a" * 40,
    "executor": "e" * 40,
}


def _build_auth() -> WebAuthorization:
    return WebAuthorization(
        json.dumps(
            [
                {
                    "actor": "kb-writer",
                    "token_sha256": hashlib.sha256(
                        TOKENS["kb-writer"].encode()
                    ).hexdigest(),
                    "permissions": ["knowledge_write"],
                },
                {
                    "actor": "requester",
                    "token_sha256": hashlib.sha256(
                        TOKENS["requester"].encode()
                    ).hexdigest(),
                    "permissions": ["request"],
                },
                {
                    "actor": "approver",
                    "token_sha256": hashlib.sha256(
                        TOKENS["approver"].encode()
                    ).hexdigest(),
                    "permissions": ["approve"],
                },
                {
                    "actor": "executor",
                    "token_sha256": hashlib.sha256(
                        TOKENS["executor"].encode()
                    ).hexdigest(),
                    "permissions": ["execute"],
                },
            ]
        )
    )


def _auth_headers(actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[actor]}"}


class _FailingEmbeddingProvider:
    model_name = "broken-embed-model"
    dimension = 128

    def embed_texts(self, texts):
        raise EmbeddingError("upstream_secret_key_99999 failed")

    def embed_query(self, text):
        raise EmbeddingError("upstream_secret_key_99999 failed")


@pytest.fixture
def kb_api(tmp_path):
    url = empty_database_url(tmp_path / "kb")
    create_runtime_schema(get_engine(url))
    provider = OfflineHashEmbeddingProvider(dimension=128)
    app = create_app(
        incident_url=url,
        runtime_url=url,
        remediation_url=url,
        knowledge_url=url,
        embedding_provider=provider,
        authorization=_build_auth(),
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 32200),
    ) as client:
        yield url, provider, client


def test_create_app_is_lazy_for_knowledge_store_and_embeddings(tmp_path):
    url = empty_database_url(tmp_path / "lazy")
    with (
        patch.object(
            KnowledgeStore,
            "__init__",
            side_effect=AssertionError("startup opened KnowledgeStore"),
        ),
        patch(
            "web_api.knowledge_views.build_embedding_provider",
            side_effect=AssertionError("startup initialized embedding provider"),
        ),
    ):
        app = create_app(knowledge_url=url)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/healthz").status_code == 200


def test_knowledge_write_auth_and_remediation_isolation(kb_api):
    url, provider, client = kb_api

    # Operator endpoint returns knowledge_write cleanly.
    op = client.get("/api/operator", headers=_auth_headers("kb-writer"))
    assert op.status_code == 200
    assert op.json() == {
        "actor": "kb-writer",
        "permissions": ["knowledge_write"],
    }

    payload = {
        "service": "order-service",
        "title": "订单 5xx 排查手册",
        "source": "runbooks/order-5xx.md",
        "document_id": "order-5xx-auth-test",
        "raw_markdown": "# 订单 5xx\n\n## 检查\n出现 HighHttp5xxRate 检查发布版本。\n",
        "publish": True,
    }

    # Missing or invalid credentials -> 401.
    assert client.post("/api/knowledge/documents", json=payload).status_code == 401
    assert (
        client.post(
            "/api/knowledge/documents",
            headers={"Authorization": "Bearer " + "x" * 40},
            json=payload,
        ).status_code
        == 401
    )

    # Untrusted Origin -> 403.
    untrusted = client.post(
        "/api/knowledge/documents",
        headers={
            **_auth_headers("kb-writer"),
            "Origin": "https://evil.example",
        },
        json=payload,
    )
    assert untrusted.status_code == 403
    assert untrusted.json() == {"detail": "local_operator_required"}

    # Remediation roles do NOT grant knowledge_write -> 403.
    for role in ("requester", "approver", "executor"):
        denied = client.post(
            "/api/knowledge/documents",
            headers=_auth_headers(role),
            json=payload,
        )
        assert denied.status_code == 403
        assert denied.json() == {"detail": "operator_permission_denied"}

    # knowledge_write does NOT grant remediation permissions -> 403.
    remediation_cmd = {
        "command_id": "kb-writer-forbidden-cmd",
        "proposal_digest": "a" * 64,
    }
    for path, body in (
        ("/api/remediations/prop-1/approve", remediation_cmd),
        ("/api/remediations/prop-1/reject", {**remediation_cmd, "reason": "no"}),
        ("/api/remediations/prop-1/execute", remediation_cmd),
        (
            "/api/remediations/prop-1/compensation-requests",
            {**remediation_cmd, "reason": "no"},
        ),
    ):
        denied_rem = client.post(
            path,
            headers=_auth_headers("kb-writer"),
            json=body,
        )
        assert denied_rem.status_code == 403
        assert denied_rem.json() == {"detail": "operator_permission_denied"}

    # Authorized knowledge_write succeeds.
    created = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json=payload,
    )
    assert created.status_code == 200, created.text
    version_id = created.json()["version"]["version_id"]

    # Remediation roles also rejected on POST /versions.
    for role in ("requester", "approver", "executor"):
        denied_ver = client.post(
            "/api/knowledge/documents/order-5xx-auth-test/versions",
            headers=_auth_headers(role),
            json={"publish_version_id": version_id},
        )
        assert denied_ver.status_code == 403

    # Disabled auth returns 503 for writes while keeping read-only GET & CORS intact.
    ro_app = create_app(
        knowledge_url=url,
        embedding_provider=provider,
        authorization=WebAuthorization("[]"),
    )
    with TestClient(
        ro_app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 32201),
    ) as ro_client:
        assert ro_client.get("/healthz").json() == {"status": "ok", "read_only": True}
        write_denied = ro_client.post(
            "/api/knowledge/documents",
            headers=_auth_headers("kb-writer"),
            json=payload,
        )
        assert write_denied.status_code == 503
        assert write_denied.json() == {"detail": "write_auth_not_configured"}

        ver_denied = ro_client.post(
            "/api/knowledge/documents/order-5xx-auth-test/versions",
            headers=_auth_headers("kb-writer"),
            json={"publish_version_id": version_id},
        )
        assert ver_denied.status_code == 503
        assert ver_denied.json() == {"detail": "write_auth_not_configured"}

        # Read-only CORS allows GET /api/knowledge/search and /documents, rejects POST preflight.
        origin = "http://127.0.0.1:5173"
        preflight_get = ro_client.options(
            "/api/knowledge/search",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight_get.status_code == 200
        assert preflight_get.headers["access-control-allow-methods"] == "GET"

        preflight_post = ro_client.options(
            "/api/knowledge/search",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight_post.status_code == 400

        search_get = ro_client.get(
            "/api/knowledge/search",
            params={"service": "order-service", "query": "HighHttp5xxRate"},
            headers={"Origin": origin},
        )
        assert search_get.status_code == 200
        assert search_get.headers["access-control-allow-origin"] == origin
        assert search_get.json()["status"] == "ok"


def test_import_idempotency_pagination_filters_and_version_detail(kb_api):
    _, _, client = kb_api
    markdown_v1 = "\n".join(
        [
            "# order-service 5xx 排障指南",
            "",
            "## 快速定位",
            "遇到 ERR_ORDER_TIMEOUT 时优先检查下游库存超时。",
            "",
            "## 回滚确认",
            "核对当前运行版本 commit_sha 与发布记录。",
        ]
    )

    first = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-service",
            "title": "order-service 5xx 排障指南",
            "source": "docs/order-5xx.md",
            "document_id": "order-5xx-guide",
            "raw_markdown": markdown_v1,
            "publish": True,
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["idempotent"] is False
    assert first_body["chunk_count"] >= 2
    assert first_body["document"]["published_status"] == "published"
    assert first_body["document"]["version_count"] == 1
    v1_id = first_body["version"]["version_id"]
    assert first_body["document"]["published_version_id"] == v1_id

    # Exact duplicate import is idempotent.
    second = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-service",
            "title": "order-service 5xx 排障指南",
            "source": "docs/order-5xx.md",
            "document_id": "order-5xx-guide",
            "raw_markdown": markdown_v1,
            "publish": True,
        },
    )
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert second.json()["version"]["version_id"] == v1_id
    assert second.json()["document"]["version_count"] == 1

    # Create an unpublished draft document for order-db.
    draft_doc = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-db",
            "source": "docs/order-db-draft.md",
            "document_id": "order-db-draft",
            "raw_markdown": "# order-db 草稿\n\n## 连接池\nPG_CONN_REFUSED 草稿说明。\n",
            "publish": False,
        },
    )
    assert draft_doc.status_code == 200
    assert draft_doc.json()["document"]["published_status"] == "unpublished"
    assert draft_doc.json()["document"]["published_version_id"] is None

    # List, filter, and paginate.
    all_page = client.get("/api/knowledge/documents", params={"limit": 1, "offset": 0})
    assert all_page.status_code == 200
    assert all_page.json()["total"] == 2
    assert len(all_page.json()["items"]) == 1
    assert all_page.json()["has_more"] is True

    published_only = client.get(
        "/api/knowledge/documents",
        params={"status": "published"},
    )
    assert [i["document_id"] for i in published_only.json()["items"]] == [
        "order-5xx-guide"
    ]

    unpublished_only = client.get(
        "/api/knowledge/documents",
        params={"status": "unpublished"},
    )
    assert [i["document_id"] for i in unpublished_only.json()["items"]] == [
        "order-db-draft"
    ]

    by_service = client.get(
        "/api/knowledge/documents",
        params={"service": "order-db"},
    )
    assert [i["document_id"] for i in by_service.json()["items"]] == [
        "order-db-draft"
    ]

    # Version detail preserves exact line coordinates and never exposes embedding blobs.
    detail_resp = client.get(
        f"/api/knowledge/documents/order-5xx-guide/versions/{v1_id}"
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["document_id"] == "order-5xx-guide"
    assert detail["version_id"] == v1_id
    assert detail["status"] == "published"
    assert detail["raw_markdown"] == markdown_v1
    lines = detail["raw_markdown"].split("\n")
    assert len(detail["chunks"]) == first_body["chunk_count"]
    for chunk in detail["chunks"]:
        assert "embedding_vector" not in chunk
        assert chunk["embedding_model"] == "offline-ngram-v1"
        assert chunk["embedding_dim"] == 128
        expected_slice = "\n".join(
            lines[chunk["start_line"] - 1 : chunk["end_line"]]
        )
        assert chunk["chunk_text"] == expected_slice


def test_version_upload_failed_embedding_preserves_published_and_switches_cleanly(
    kb_api,
):
    url, _, client = kb_api
    v1_md = "# order-db 手册\n\n## 连接故障\n出现 PG_CONN_REFUSED 检查 5432 端口。\n"
    v1_resp = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-db",
            "source": "docs/order-db.md",
            "document_id": "order-db-ops",
            "raw_markdown": v1_md,
            "publish": True,
        },
    )
    assert v1_resp.status_code == 200
    v1_id = v1_resp.json()["version"]["version_id"]

    # Attempt uploading v2 when embedding provider fails -> 503, v1 preserved intact.
    v2_md = "# order-db 手册 v2\n\n## 锁等待\n出现 PG_LOCK_WAIT_TIMEOUT 排查长事务。\n"
    broken_app = create_app(
        knowledge_url=url,
        embedding_provider=_FailingEmbeddingProvider(),
        authorization=_build_auth(),
    )
    with TestClient(
        broken_app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 32202),
    ) as broken_client:
        failed = broken_client.post(
            "/api/knowledge/documents/order-db-ops/versions",
            headers=_auth_headers("kb-writer"),
            json={"raw_markdown": v2_md, "publish": True},
        )
        assert failed.status_code == 503
        assert failed.json() == {"detail": "embedding_unavailable"}
        assert "upstream_secret_key_99999" not in failed.text
        assert "PG_LOCK_WAIT_TIMEOUT" not in failed.text

    # Confirm v1 is still the sole published version and still searchable.
    docs = client.get("/api/knowledge/documents", params={"service": "order-db"}).json()
    assert docs["items"][0]["published_version_id"] == v1_id
    assert docs["items"][0]["version_count"] == 1
    before_switch = client.get(
        "/api/knowledge/search",
        params={"service": "order-db", "query": "PG_CONN_REFUSED"},
    ).json()
    assert before_switch["status"] == "ok"
    assert before_switch["hits"][0]["version_id"] == v1_id

    # Upload v2 as draft with healthy provider -> v1 remains published.
    v2_draft = client.post(
        "/api/knowledge/documents/order-db-ops/versions",
        headers=_auth_headers("kb-writer"),
        json={"raw_markdown": v2_md, "publish": False},
    )
    assert v2_draft.status_code == 200
    v2_id = v2_draft.json()["version"]["version_id"]
    assert v2_draft.json()["version"]["status"] == "draft"
    assert v2_draft.json()["document"]["published_version_id"] == v1_id
    assert v2_draft.json()["document"]["version_count"] == 2
    assert (
        client.get(
            "/api/knowledge/search",
            params={
                "service": "order-db",
                "query": "PG_LOCK_WAIT_TIMEOUT",
                "method": "mysql_ngram",
            },
        ).json()["status"]
        == "no_match"
    )

    # Switching to nonexistent version returns 404 without changing published version.
    missing_switch = client.post(
        "/api/knowledge/documents/order-db-ops/versions",
        headers=_auth_headers("kb-writer"),
        json={"publish_version_id": "ver-nonexistent"},
    )
    assert missing_switch.status_code == 404
    assert missing_switch.json() == {"detail": "version_not_found"}

    # Publish v2 -> v1 archived, v2 becomes active in search.
    switched = client.post(
        "/api/knowledge/documents/order-db-ops/versions",
        headers=_auth_headers("kb-writer"),
        json={"publish_version_id": v2_id},
    )
    assert switched.status_code == 200
    assert switched.json()["idempotent"] is False
    assert switched.json()["document"]["published_version_id"] == v2_id
    assert switched.json()["version"]["status"] == "published"

    # Re-publishing already active v2 is idempotent.
    switched_again = client.post(
        "/api/knowledge/documents/order-db-ops/versions",
        headers=_auth_headers("kb-writer"),
        json={"publish_version_id": v2_id},
    )
    assert switched_again.status_code == 200
    assert switched_again.json()["idempotent"] is True

    v1_detail = client.get(
        f"/api/knowledge/documents/order-db-ops/versions/{v1_id}"
    ).json()
    assert v1_detail["status"] == "archived"

    after_switch = client.post(
        "/api/knowledge/search",
        json={"service": "order-db", "query": "PG_LOCK_WAIT_TIMEOUT", "limit": 5},
    ).json()
    assert after_switch["status"] == "ok"
    assert after_switch["hits"][0]["version_id"] == v2_id
    assert (
        client.get(
            "/api/knowledge/search",
            params={
                "service": "order-db",
                "query": "PG_CONN_REFUSED",
                "method": "mysql_ngram",
            },
        ).json()["status"]
        == "no_match"
    )


def test_cross_service_isolation_and_search_get_post_parity_and_unavailable(
    kb_api,
    tmp_path,
):
    url, _, client = kb_api
    assert (
        client.post(
            "/api/knowledge/documents",
            headers=_auth_headers("kb-writer"),
            json={
                "service": "order-service",
                "source": "order.md",
                "document_id": "doc-order-only",
                "raw_markdown": "# 订单服务\n\n## 错误码\n发生 ERR_ORDER_TIMEOUT 核查订单超时链路。\n",
                "publish": True,
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/knowledge/documents",
            headers=_auth_headers("kb-writer"),
            json={
                "service": "inventory-service",
                "source": "inventory.md",
                "document_id": "doc-inventory-only",
                "raw_markdown": "# 库存服务\n\n## 背压\n出现 INV_503_BACKPRESSURE 执行库存限流保护。\n",
                "publish": True,
            },
        ).status_code
        == 200
    )

    # Reusing document_id under another service is rejected with 422.
    mismatch = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "inventory-service",
            "source": "order.md",
            "document_id": "doc-order-only",
            "raw_markdown": "# 跨服务覆盖尝试\n",
            "publish": True,
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json() == {"detail": "document_service_mismatch"}

    # GET & POST search parity across methods, plus strict cross-service isolation.
    for method in ("hybrid_rrf", "mysql_ngram", "vector_cosine"):
        get_ok = client.get(
            "/api/knowledge/search",
            params={
                "service": "order-service",
                "query": "ERR_ORDER_TIMEOUT 订单超时",
                "limit": 5,
                "method": method,
            },
        )
        post_ok = client.post(
            "/api/knowledge/search",
            json={
                "service": "order-service",
                "query": "ERR_ORDER_TIMEOUT 订单超时",
                "limit": 5,
                "method": method,
            },
        )
        assert get_ok.status_code == post_ok.status_code == 200
        assert get_ok.json() == post_ok.json()
        body = get_ok.json()
        assert body["status"] == "ok"
        assert body["reason"] is None
        assert body["candidate_count"] >= 1
        assert all(hit["service"] == "order-service" for hit in body["hits"])
        assert body["hits"][0]["document_id"] == "doc-order-only"
        assert "#L" in body["hits"][0]["citation"]

        # Cross-service query returns explicit no_match.
        cross = client.get(
            "/api/knowledge/search",
            params={
                "service": "order-service",
                "query": "INV_503_BACKPRESSURE 库存限流",
                "method": method,
            },
        )
        assert cross.status_code == 200
        assert cross.json()["status"] == "no_match"
        assert cross.json()["hits"] == []
        assert cross.json()["reason"] is None

    # Search unavailable when embedding provider fails on hybrid/vector, but ngram still works.
    broken_embed_app = create_app(
        knowledge_url=url,
        embedding_provider=_FailingEmbeddingProvider(),
    )
    with TestClient(broken_embed_app, base_url="http://127.0.0.1") as broken_client:
        for method in ("hybrid_rrf", "vector_cosine"):
            unavail = broken_client.get(
                "/api/knowledge/search",
                params={
                    "service": "order-service",
                    "query": "ERR_ORDER_TIMEOUT",
                    "method": method,
                },
            )
            assert unavail.status_code == 200
            assert unavail.json()["status"] == "unavailable"
            assert unavail.json()["reason"] == "embedding_unavailable"
            assert unavail.json()["hits"] == []
            assert "upstream_secret_key_99999" not in unavail.text

        ngram_still_ok = broken_client.get(
            "/api/knowledge/search",
            params={
                "service": "order-service",
                "query": "ERR_ORDER_TIMEOUT",
                "method": "mysql_ngram",
            },
        )
        assert ngram_still_ok.status_code == 200
        assert ngram_still_ok.json()["status"] == "ok"

    # Unmigrated DB returns 503 on list/write and status=unavailable on search.
    unmigrated_url = empty_database_url(tmp_path / "unmigrated")
    unmigrated_app = create_app(
        knowledge_url=unmigrated_url,
        embedding_provider=OfflineHashEmbeddingProvider(dimension=128),
        authorization=_build_auth(),
    )
    with TestClient(
        unmigrated_app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 32203),
    ) as unmigrated_client:
        list_err = unmigrated_client.get("/api/knowledge/documents")
        assert list_err.status_code == 503
        assert list_err.json() == {"detail": "knowledge_schema_not_migrated"}

        search_unavail = unmigrated_client.post(
            "/api/knowledge/search",
            json={"service": "order-service", "query": "ERR_ORDER_TIMEOUT"},
        )
        assert search_unavail.status_code == 200
        assert search_unavail.json()["status"] == "unavailable"
        assert search_unavail.json()["reason"] == "knowledge_schema_not_migrated"
        assert search_unavail.json()["hits"] == []


def test_safe_error_mapping_and_masked_validation_errors(kb_api):
    _, _, client = kb_api

    # Masked 422 on POST /api/knowledge/documents never echoes caller input or secrets.
    bad_extra = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-service",
            "source": "a.md",
            "raw_markdown": "# secret-markdown-body-do-not-echo",
            "unexpected_field": "secret-field-value-do-not-echo",
        },
    )
    assert bad_extra.status_code == 422
    assert bad_extra.json() == {"detail": "invalid_request"}
    assert "secret-markdown-body-do-not-echo" not in bad_extra.text
    assert "secret-field-value-do-not-echo" not in bad_extra.text

    # Masked 422 on POST /versions when neither or both modes provided.
    for body in (
        {},
        {"raw_markdown": "# secret-v2-body", "publish_version_id": "ver-abc"},
    ):
        bad_ver = client.post(
            "/api/knowledge/documents/some-doc/versions",
            headers=_auth_headers("kb-writer"),
            json=body,
        )
        assert bad_ver.status_code == 422
        assert bad_ver.json() == {"detail": "invalid_request"}
        assert "secret-v2-body" not in bad_ver.text

    # Domain 422 codes: unknown service, NUL byte, oversized UTF-8 bytes.
    unknown_post = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "unregistered-svc",
            "source": "x.md",
            "raw_markdown": "# test\n",
        },
    )
    assert unknown_post.status_code == 422
    assert unknown_post.json() == {"detail": "unknown_service"}

    assert (
        client.get(
            "/api/knowledge/documents",
            params={"service": "unregistered-svc"},
        ).json()
        == {"detail": "unknown_service"}
    )
    assert (
        client.get(
            "/api/knowledge/search",
            params={"service": "unregistered-svc", "query": "test"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/knowledge/search",
            json={"service": "unregistered-svc", "query": "test"},
        ).status_code
        == 422
    )

    nul_resp = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-service",
            "source": "nul.md",
            "raw_markdown": "# secret-nul-text\x00tail",
        },
    )
    assert nul_resp.status_code == 422
    assert nul_resp.json() == {"detail": "markdown_contains_nul_byte"}
    assert "secret-nul-text" not in nul_resp.text

    oversized_utf8 = "中" * (200 * 1024)  # 600KB UTF-8 bytes > 512KB limit
    big_resp = client.post(
        "/api/knowledge/documents",
        headers=_auth_headers("kb-writer"),
        json={
            "service": "order-service",
            "source": "big.md",
            "raw_markdown": oversized_utf8,
        },
    )
    assert big_resp.status_code == 422
    assert big_resp.json() == {"detail": "markdown_exceeds_max_bytes"}
    assert "中中中" not in big_resp.text

    # 404 for missing document / version.
    missing_doc = client.get(
        "/api/knowledge/documents/missing-doc/versions/ver-1"
    )
    assert missing_doc.status_code == 404
    assert missing_doc.json() == {"detail": "document_not_found"}


def test_storage_and_env_embedding_config_failures_return_safe_503_or_unavailable(
    kb_api,
    monkeypatch,
):
    url, _, _ = kb_api
    # Malformed embedding env config produces safe embedding_unavailable without 500.
    monkeypatch.setenv("INCIDENT_AGENT_EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("INCIDENT_AGENT_EMBEDDING_DIM", "not-an-int")
    env_app = create_app(
        knowledge_url=url,
        authorization=_build_auth(),
    )
    with TestClient(
        env_app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 32204),
    ) as client:
        write_resp = client.post(
            "/api/knowledge/documents",
            headers=_auth_headers("kb-writer"),
            json={
                "service": "order-service",
                "source": "env-fail.md",
                "raw_markdown": "# test\n\nbody\n",
            },
        )
        assert write_resp.status_code == 503
        assert write_resp.json() == {"detail": "embedding_unavailable"}

        search_resp = client.get(
            "/api/knowledge/search",
            params={"service": "order-service", "query": "test"},
        )
        assert search_resp.status_code == 200
        assert search_resp.json()["status"] == "unavailable"
        assert search_resp.json()["reason"] == "embedding_unavailable"

    # Storage failure maps to safe knowledge_unavailable without leaking SQL.
    from knowledge.models import KnowledgeStorageError

    with patch.object(
        KnowledgeStore,
        "list_documents",
        side_effect=KnowledgeStorageError("SELECT secret_sql_should_not_leak"),
    ):
        db_app = create_app(knowledge_url=url)
        with TestClient(db_app, base_url="http://127.0.0.1") as client:
            resp = client.get("/api/knowledge/documents")
            assert resp.status_code == 503
            assert resp.json() == {"detail": "knowledge_unavailable"}
            assert "secret_sql_should_not_leak" not in resp.text


def test_session_detail_exposes_sanitized_runbook_citations(tmp_path):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    Worker(manager, FixtureAgent([model_response(answer="已排查完成")])).run_once()

    app = create_app(
        incident_url=manager.incident_store.database_url,
        runtime_url=manager.store.database_url,
        knowledge_url=manager.store.database_url,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Without search_runbooks records, runbook_citations defaults to [].
        empty_detail = client.get(f"/api/sessions/{session.session_id}")
        assert empty_detail.status_code == 200
        assert empty_detail.json()["runbook_citations"] == []

        # Persist search_runbooks records in checkpoint (with duplicate + secret in snippet).
        with manager.store.transaction() as db:
            current = manager.store.get(session.session_id, db)
            checkpoint = manager.store.load_checkpoint(current, db)
            hit = {
                "document_id": "order-5xx-guide",
                "version_id": "ver-123456",
                "chunk_id": "chk-abcdef",
                "service": "order-service",
                "title": "订单服务 5xx 手册",
                "heading_path": "订单服务 5xx 手册 > 定位步骤",
                "start_line": 4,
                "end_line": 9,
                "snippet": "检查 password=super-secret-value 和 ERR_ORDER_TIMEOUT",
                "score": 0.91,
                "citation": "order-5xx-guide@ver-123456#L4-L9",
            }
            checkpoint["records"].append(
                {
                    "observation_id": "obs-runbook-1",
                    "name": "search_runbooks",
                    "arguments": {"service": "order-service", "query": "ERR_ORDER_TIMEOUT"},
                    "call_id": "call-rb-1",
                    "result": {
                        "status": "ok",
                        "service": "order-service",
                        "hits": [hit, hit],
                    },
                    "success": True,
                    "collected_at": "2026-11-21T00:00:00+00:00",
                    "identity": None,
                    "cache_key": "ck-1",
                }
            )
            checkpoint["records"].append(
                {
                    "observation_id": "obs-runbook-2",
                    "name": "search_runbooks",
                    "arguments": {"service": "order-service", "query": "missing"},
                    "call_id": "call-rb-2",
                    "result": {"status": "no_data", "hits": []},
                    "success": False,
                    "collected_at": "2026-11-21T00:00:01+00:00",
                    "identity": None,
                    "cache_key": "ck-2",
                }
            )
            manager.store.checkpoint(current, checkpoint, db)
            manager.store.save(current, db)

        detail_resp = client.get(f"/api/sessions/{session.session_id}")
        assert detail_resp.status_code == 200
        assert "super-secret-value" not in detail_resp.text
        citations = detail_resp.json()["runbook_citations"]
        assert len(citations) == 1
        assert citations[0] == {
            "document_id": "order-5xx-guide",
            "version_id": "ver-123456",
            "chunk_id": "chk-abcdef",
            "service": "order-service",
            "title": "订单服务 5xx 手册",
            "heading_path": "订单服务 5xx 手册 > 定位步骤",
            "start_line": 4,
            "end_line": 9,
            "citation": "order-5xx-guide@ver-123456#L4-L9",
            "snippet": "检查 password=[redacted] 和 ERR_ORDER_TIMEOUT",
            "score": 0.91,
        }

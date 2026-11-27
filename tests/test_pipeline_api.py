"""End-to-end: upload → OCR (real PaddleOCR output replayed from fixtures) → rules → review, via the API."""
import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Membership, Organization, Vendor
from apps.documents.models import AuditEvent, Document
from apps.ocr.base import OcrResult
from apps.rules.models import RuleSet

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def replay_ocr(monkeypatch):
    """Replace the OCR call with recorded PaddleOCR output chosen by upload filename."""

    def fake_run_ocr(source, pages_dir, engine_name, media_root=None):
        doc = Document.objects.get(file=str(Path(source).relative_to(media_root)).replace("\\", "/"))
        name = Path(doc.original_name).stem
        return OcrResult.from_json(json.loads((FIXTURES / f"{name}.json").read_text()))

    monkeypatch.setattr("apps.documents.pipeline.run_ocr", fake_run_ocr)


@pytest.fixture
def seeded(transactional_db):
    call_command("seed_demo", password="pw-for-tests-only", stdout=io.StringIO())
    return Organization.objects.get(slug="acme-retail")


def client_for(username):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user__username=username).key}")
    return c


def png(name):
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PNG")
    buf.name = name
    buf.seek(0)
    return buf


def upload(client, name):
    r = client.post("/api/documents/", {"file": png(f"{name}.png"), "document_type": "invoice"}, format="multipart")
    assert r.status_code == 201, r.content
    return r.json()


@pytest.mark.parametrize("name,status,queue", [
    ("01-approved-office-supplies", "approved", ""),
    ("02-review-high-value", "needs_review", "finance-manager"),
    ("03-reject-invalid-gstin", "rejected", ""),
    ("04-review-unknown-vendor-scan", "needs_review", "finance-manager"),
    ("05-review-totals-mismatch", "needs_review", "ap-clerk"),
])
def test_sample_invoices_get_the_expected_decision(seeded, replay_ocr, name, status, queue):
    doc = upload(client_for("reviewer"), name)
    assert doc["status"] == status, doc["reasons"]
    assert doc["queue"] == queue
    assert doc["decided_by"] == "rules"
    assert any(f["key"] == "total_amount" and f["value"] for f in doc["fields"])


def test_decision_detail_includes_trace_and_audit(seeded, replay_ocr):
    doc = upload(client_for("reviewer"), "03-reject-invalid-gstin")
    rule = next(r for r in doc["rule_results"] if r["name"].startswith("Reject: vendor GSTIN"))
    assert rule["matched"] and rule["trace"]["children"][1]["actual"] is False
    assert doc["facts"]["vendor_gstin_valid"] is False
    assert [e["action"] for e in doc["events"]] == ["document.uploaded", "ocr.completed", "rules.evaluated"]


def test_duplicate_upload_is_rejected(seeded, replay_ocr):
    c = client_for("reviewer")
    assert upload(c, "01-approved-office-supplies")["status"] == "approved"
    second = upload(c, "01-approved-office-supplies")
    assert second["status"] == "rejected"
    assert second["facts"]["is_duplicate"] is True


def test_correcting_a_field_re_runs_rules(seeded, replay_ocr):
    c = client_for("reviewer")
    doc = upload(c, "05-review-totals-mismatch")
    assert doc["status"] == "needs_review"
    r = c.patch(f"/api/documents/{doc['id']}/fields/total_amount/", {"value": "10640.00"}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "approved", (body["reasons"], body["facts"])
    total = next(f for f in body["fields"] if f["key"] == "total_amount")
    assert total["is_corrected"] and total["final_value"] == "10640.00" and total["value"] == "11250.00"
    ev = AuditEvent.objects.get(action="field.corrected")
    assert ev.data == {"key": "total_amount", "before": "11250.00", "after": "10640.00"}


def test_bad_correction_is_refused(seeded, replay_ocr):
    c = client_for("reviewer")
    doc = upload(c, "05-review-totals-mismatch")
    r = c.patch(f"/api/documents/{doc['id']}/fields/invoice_date/", {"value": "31/31/2026"}, format="json")
    assert r.status_code == 400
    r = c.patch(f"/api/documents/{doc['id']}/fields/not_a_field/", {"value": "x"}, format="json")
    assert r.status_code == 400


def test_person_decision_is_final_until_reopened(seeded, replay_ocr):
    c = client_for("reviewer")
    doc = upload(c, "02-review-high-value")
    r = c.post(f"/api/documents/{doc['id']}/review/", {"decision": "approve", "note": "CFO ok'd on email"},
               format="json")
    assert r.json()["status"] == "approved" and r.json()["decided_by"] == "person"
    # rules no longer override it
    assert c.post(f"/api/documents/{doc['id']}/reevaluate/").json()["status"] == "approved"
    reopened = c.post(f"/api/documents/{doc['id']}/reopen/").json()
    assert reopened["status"] == "needs_review" and reopened["decided_by"] == "rules"


def test_approving_vendor_in_master_changes_outcome(seeded, replay_ocr):
    c = client_for("reviewer")
    doc = upload(c, "04-review-unknown-vendor-scan")
    assert doc["status"] == "needs_review"
    admin = client_for("admin")
    r = admin.post("/api/vendors/", {"name": "Ganesh Packaging Co", "gstin": "27AAKFG3390B1ZJ"}, format="json")
    assert r.status_code == 201, r.content
    assert c.post(f"/api/documents/{doc['id']}/reevaluate/").json()["status"] == "approved"


def test_low_confidence_blocks_auto_approval(seeded, replay_ocr):
    dtype = seeded.document_types.get(slug="invoice")
    dtype.confidence_threshold = 0.99
    dtype.save()
    doc = upload(client_for("reviewer"), "01-approved-office-supplies")
    assert doc["status"] == "needs_review"
    assert doc["reasons"][0].startswith("Auto-approval blocked")


class TestRulesApi:
    def test_fact_catalog_for_flutter(self, seeded):
        facts = client_for("reviewer").get("/api/document-types/invoice/facts/").json()
        by_key = {f["key"]: f for f in facts}
        assert by_key["total_amount"]["type"] == "number"
        assert "greaterThan" in {o["name"] for o in by_key["total_amount"]["operators"]}
        assert by_key["vendor_approved"]["type"] == "bool"

    def test_only_admins_edit_rules(self, seeded):
        body = {"name": "Big", "document_type": "invoice", "action": "review",
                "tree": {"type": "all", "rules": [{"fact": "total_amount", "operator": "greaterThan", "value": 1}]}}
        assert client_for("reviewer").post("/api/rulesets/", body, format="json").status_code == 403
        r = client_for("admin").post("/api/rulesets/", body, format="json")
        assert r.status_code == 201, r.content
        assert AuditEvent.objects.filter(action="ruleset.created", data__name="Big").exists()

    def test_invalid_tree_rejected_with_reason(self, seeded):
        body = {"name": "Bad", "document_type": "invoice", "action": "review",
                "tree": {"type": "all", "rules": [{"fact": "vendor_name", "operator": "greaterThan", "value": 5}]}}
        r = client_for("admin").post("/api/rulesets/", body, format="json")
        assert r.status_code == 400
        assert "cannot be used with string" in json.dumps(r.json())

    def test_version_bumps_on_change(self, seeded):
        admin = client_for("admin")
        rs = RuleSet.objects.get(name__startswith="Review: high value")
        tree = rs.tree
        tree["rules"][0]["value"] = 300000
        r = admin.patch(f"/api/rulesets/{rs.id}/", {"tree": tree}, format="json")
        assert r.status_code == 200 and r.json()["version"] == 2
        history = admin.get(f"/api/rulesets/{rs.id}/history/").json()
        assert history[-1]["data"]["version"] == 2

    def test_dry_run_against_a_document(self, seeded, replay_ocr):
        doc = upload(client_for("reviewer"), "02-review-high-value")
        tree = {"type": "all", "rules": [{"fact": "total_amount", "operator": "greaterThan", "value": 500000}]}
        r = client_for("admin").post("/api/rulesets/test/", {"tree": tree, "document_id": doc["id"]}, format="json")
        assert r.status_code == 200 and r.json()["matched"] is True
        assert r.json()["trace"]["children"][0]["actual"] == 686052

    def test_broken_rule_sends_to_review_not_approve(self, seeded, replay_ocr):
        RuleSet.objects.filter(action="approve").update(
            tree={"type": "all", "rules": [{"fact": "gone_field", "operator": "exists"}]})
        doc = upload(client_for("reviewer"), "01-approved-office-supplies")
        assert doc["status"] == "needs_review"


class TestTenancy:
    def test_other_org_cannot_see_documents_or_files(self, seeded, replay_ocr, django_user_model, client):
        doc = upload(client_for("reviewer"), "01-approved-office-supplies")
        other = Organization.objects.create(name="Other Co", slug="other")
        u = django_user_model.objects.create_user("mallory", password="x-long-password-1")
        Membership.objects.create(user=u, organization=other, role="admin")
        token = Token.objects.create(user=u)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        assert c.get(f"/api/documents/{doc['id']}/").status_code == 404
        assert c.get("/api/documents/").json()["count"] == 0
        client.force_login(u)
        path = Document.objects.get(pk=doc["id"]).file.name
        assert client.get(f"/files/{path}").status_code == 404

    def test_user_without_membership_is_refused(self, seeded, django_user_model):
        u = django_user_model.objects.create_user("drifter", password="x-long-password-1")
        c = APIClient()
        c.force_authenticate(u)
        assert c.get("/api/documents/").status_code == 403

    def test_viewer_cannot_upload(self, seeded, django_user_model):
        u = django_user_model.objects.create_user("vic", password="x-long-password-1")
        Membership.objects.create(user=u, organization=seeded, role="viewer")
        c = APIClient()
        c.force_authenticate(u)
        r = c.post("/api/documents/", {"file": png("a.png"), "document_type": "invoice"}, format="multipart")
        assert r.status_code == 403


def test_upload_rejects_unsupported_type(seeded):
    f = io.BytesIO(b"MZ fake exe")
    f.name = "invoice.exe"
    r = client_for("reviewer").post("/api/documents/", {"file": f, "document_type": "invoice"}, format="multipart")
    assert r.status_code == 400 and "Unsupported file type" in json.dumps(r.json())


def test_vendor_gstin_validated(seeded):
    r = client_for("admin").post("/api/vendors/", {"name": "X", "gstin": "07AAGFQ5521M1ZX"}, format="json")
    assert r.status_code == 400


def test_ocr_failure_marks_document_failed(seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model crashed")

    monkeypatch.setattr("apps.documents.pipeline.run_ocr", boom)
    doc = upload(client_for("reviewer"), "01-approved-office-supplies")
    assert doc["status"] == "failed" and "model crashed" in doc["error"]
    assert Vendor.objects.count() == 3   # nothing else touched


def test_duplicate_vendor_gstin_is_a_400_not_a_crash(seeded):
    admin = client_for("admin")
    g = Vendor.objects.get(name="Sharma Stationers").gstin
    r = admin.post("/api/vendors/", {"name": "Sharma Copy", "gstin": g}, format="json")
    assert r.status_code == 400 and "already exists" in json.dumps(r.json())


def test_cors_preflight_allows_local_flutter_web(seeded, settings):
    c = APIClient()
    r = c.options("/api/rulesets/", HTTP_ORIGIN="http://localhost:5173",
                  HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                  HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type")
    assert r.status_code == 200
    assert r["access-control-allow-origin"] == "http://localhost:5173"
    assert "authorization" in r["access-control-allow-headers"].lower()
    # non-API paths (dashboard, files) are never opened up cross-origin
    r = c.get("/login/", HTTP_ORIGIN="http://localhost:5173")
    assert "access-control-allow-origin" not in r

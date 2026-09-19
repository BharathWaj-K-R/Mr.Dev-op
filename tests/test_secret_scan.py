"""
Unit tests for the secret scan utility.
Verifies that common secret patterns are detected and that
actual secret values are NEVER returned in findings.
"""
import pytest
from tools import scan_for_secrets


class TestSecretDetection:
    def test_groq_api_key_detected(self):
        content = "GROQ_API_KEY=gsk_abc123def456ghi789jkl012mno345"
        findings = scan_for_secrets(content)
        assert len(findings) >= 1
        assert not any("gsk_abc123" in f["redacted_match"] for f in findings), \
            "Actual key value must not appear in findings"

    def test_openai_style_key_detected(self):
        content = "api_key = sk-abc123xyz987qwerty456uiop789asdf"
        findings = scan_for_secrets(content)
        assert len(findings) >= 1

    def test_aws_access_key_detected(self):
        content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        findings = scan_for_secrets(content)
        assert len(findings) >= 1

    def test_private_key_detected(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        findings = scan_for_secrets(content)
        assert len(findings) >= 1

    def test_github_token_detected(self):
        content = "GITHUB_TOKEN=ghp_16C7e42F292c6912E7710c838347Ae178B4a"
        findings = scan_for_secrets(content)
        assert len(findings) >= 1

    def test_clean_content_no_findings(self):
        content = "DEPLOYMENT_ENV=staging\nSERVICE=api\nPORT=8080"
        findings = scan_for_secrets(content)
        assert findings == []

    def test_empty_content_no_findings(self):
        assert scan_for_secrets("") == []

    def test_finding_includes_line_number(self):
        content = "line1\nGROQ_API_KEY=gsk_abc123def456ghi789jkl012\nline3"
        findings = scan_for_secrets(content)
        assert any(f["line"] == 2 for f in findings)

    def test_finding_redacts_actual_value(self):
        """The actual secret value must NEVER appear verbatim in any finding."""
        secret_value = "gsk_SuperSecretKey1234567890abcdefghij"
        content = f"export API_KEY={secret_value}"
        findings = scan_for_secrets(content)
        for finding in findings:
            assert secret_value not in finding.get("redacted_match", ""), \
                "Secret value must be redacted in findings"

    def test_multiline_scan(self):
        content = "\n".join([
            "SERVICE_NAME=my-api",
            "PORT=3000",
            "STRIPE_SECRET_KEY=sk-test_SecretKey_1234567890abcdefghij",
            "GROQ_API_KEY=gsk_abc123def456ghi789jkl012mno345",
            "DEBUG=true",
        ])
        findings = scan_for_secrets(content)
        assert len(findings) >= 1

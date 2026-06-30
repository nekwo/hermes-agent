from agent_runtime.redaction import BasicRedactionScanner, RedactionStatus


def test_basic_redaction_scanner_detects_secret(tmp_path):
    p=tmp_path/"x.txt"; p.write_text("API_KEY='abcdefghijklmnop'", encoding="utf-8")
    assert BasicRedactionScanner().scan_text(p) == RedactionStatus.UNSAFE


def test_basic_redaction_scanner_marks_plain_text_safe(tmp_path):
    p=tmp_path/"x.txt"; p.write_text("hello", encoding="utf-8")
    assert BasicRedactionScanner().scan_text(p) == RedactionStatus.SAFE

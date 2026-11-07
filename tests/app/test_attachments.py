from __future__ import annotations

import base64

import pytest

from compute.app.attachments import IMAGE_MAX_BYTES, TEXT_MAX_BYTES, AttachmentError, extract

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_text_file_is_decoded_with_replacement():
    part = extract("notes.md", "hello ".encode() + b"\xff world")
    assert part["kind"] == "text"
    assert part["name"] == "notes.md"
    assert part["size"] == 13
    assert part["text"].startswith("hello ") and "�" in part["text"]


def test_code_file_is_text():
    assert extract("main.py", b"print(1)\n")["kind"] == "text"


def test_image_becomes_data_url():
    part = extract("Photo.JPG", PNG)
    assert part["kind"] == "image"
    assert part["data_url"] == "data:image/jpeg;base64," + base64.b64encode(PNG).decode()


@pytest.mark.parametrize("name", ["report.pdf", "deck.pptx", "sheet.xlsx", "noext"])
def test_unsupported_is_415(name):
    with pytest.raises(AttachmentError) as e:
        extract(name, b"x")
    assert e.value.status == 415
    assert "PDF" in e.value.message


def test_text_cap():
    extract("ok.txt", b"a" * TEXT_MAX_BYTES)
    with pytest.raises(AttachmentError) as e:
        extract("big.txt", b"a" * (TEXT_MAX_BYTES + 1))
    assert e.value.status == 413


def test_image_cap():
    with pytest.raises(AttachmentError) as e:
        extract("big.png", b"a" * (IMAGE_MAX_BYTES + 1))
    assert e.value.status == 413


# -------------------------------------------------------------- /api/attach


def test_attach_endpoint_text(make_client):
    client, _ = make_client(lambda _r: None)
    r = client.post("/api/attach", files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 200
    assert r.json() == {"kind": "text", "name": "data.csv", "size": 8, "text": "a,b\n1,2\n"}


def test_attach_endpoint_image(make_client):
    client, _ = make_client(lambda _r: None)
    r = client.post("/api/attach", files={"file": ("pic.png", PNG, "image/png")})
    assert r.status_code == 200
    assert r.json()["data_url"].startswith("data:image/png;base64,")


def test_attach_endpoint_415(make_client):
    client, _ = make_client(lambda _r: None)
    r = client.post("/api/attach", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 415
    assert "PDF" in r.json()["detail"]


def test_attach_endpoint_text_too_big(make_client):
    client, _ = make_client(lambda _r: None)
    r = client.post("/api/attach", files={"file": ("big.log", b"x" * (TEXT_MAX_BYTES + 10), "text/plain")})
    assert r.status_code == 413

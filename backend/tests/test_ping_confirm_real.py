"""Gerçek Ansible ile uçtan uca ping onayı (T-204B2).

Stub'ların kapatamadığı boşluk: preview'ın ürettiği dondurulmuş snapshot,
gerçek `ansible` tarafından beklediğimiz gibi okunuyor mu ve gerçek bir
başarısız bağlantı **altyapı hatası değil**, geçerli bir sonuç olarak mı
raporlanıyor?

Hedef `127.0.0.1:1`'dir: kapalı bir porttur, dışarıya hiçbir bağlantı
kurulmaz ve sonuç deterministik biçimde `unreachable` olur.

Bu dosya **atlanmaz**: doğrulama ortamı Linux'tur ve `ansible` orada
zorunludur (ADR-017'deki "Windows control node desteklenmez" sınırı).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.jobs.artifacts import RESULT_FILENAME
from tests.support import real_parser_available

CLOSED_PORT_INVENTORY = """\
[lab]
closed-host ansible_host=127.0.0.1 ansible_port=1
"""


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> httpx.Response:
    return cast(httpx.Response, client.post(url, json=payload))


def test_real_ping_against_a_closed_port_is_a_failed_job_not_an_error(
    client: TestClient, inventory_root: Path, settings: Settings
) -> None:
    """Uçtan uca: kayıt → preview → confirm.

    Beklenen sözleşme: HTTP 200, `failed` Job, `unreachable` host. Ansible'ın
    sıfırdan farklı çıkış kodu burada bir arıza değil, ölçülen gerçektir.
    """
    assert os.name == "posix"
    assert real_parser_available(), "Linux doğrulama ortamında ansible zorunludur"

    path = inventory_root / "hosts.ini"
    path.write_text(CLOSED_PORT_INVENTORY, encoding="utf-8")
    created = _post(
        client,
        "/api/inventories",
        {"name": "lab", "path": str(path), "source_type": "ini"},
    )
    assert created.status_code == 201
    inventory_id = int(created.json()["id"])

    preview = _post(client, f"/api/inventories/{inventory_id}/ping/preview", {})
    assert preview.status_code == 200
    token = preview.json()["preview_token"]
    assert preview.json()["plan"]["hosts"] == ["closed-host"]

    response = _post(client, f"/api/inventories/{inventory_id}/ping", {"preview_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["job_type"] == "ping"
    assert body["return_code"] not in (None, 0)
    assert body["summary"] == {
        "total": 1,
        "reachable": 0,
        "unreachable": 1,
        "failed": 0,
        "no_result": 0,
    }
    assert [host["name"] for host in body["hosts"]] == ["closed-host"]
    assert body["hosts"][0]["status"] == "unreachable"
    assert body["hosts"][0]["message"]

    # Cevap ne token ne de sunucu tarafı ayrıntısı taşır.
    for leak in (token, str(settings.app_data_dir), "127.0.0.1", "inventory-targets"):
        assert leak not in response.text

    result = settings.app_data_dir / "jobs" / body["job_id"] / RESULT_FILENAME
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["schema_version"] == 1
    assert document["hosts"][0]["status"] == "unreachable"
    assert "127.0.0.1" not in result.read_text(encoding="utf-8")

    # Token tüketilmiştir; aynı plan ikinci kez çalıştırılamaz.
    replay = _post(client, f"/api/inventories/{inventory_id}/ping", {"preview_token": token})
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "ping_preview_invalid"

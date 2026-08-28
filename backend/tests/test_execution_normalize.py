"""R1-V3C1B normalize katmanının şeması, sınırları ve iki ayrı yüzeyi.

Sızıntı testlerinin kuralı şudur: bir sentetik secret'ın normalize sonuçta
**bulunmadığını** göstermek tek başına hiçbir şey kanıtlamaz — secret girdide de
yoksa test her koşulda geçer. Bu yüzden her sızıntı testi önce secret'ın **ham
event akışında gerçekten bulunduğunu** doğrular, sonra sonuçta bulunmadığını
ölçer.

R1-V3J3A'dan beri "sonuç" tek bir yüzey değildir ve testler bunu ayırır:

- **structured yüzey** — ``recap``/``events`` ve belgenin geri kalanı. Sızmazlık
  iddiası burada geçerlidir ve gevşetilmez: ``event_data``, ``res``,
  ``task_args``, ``task_path`` ve maskelenmemiş bağlantı değerleri buraya
  **hiçbir** yolda giremez.
- **display yüzeyi** — ``ansible_output``. Event'lerin üst düzey ``stdout``
  satırlarını **ham** taşır ve "secret-free" **değildir**. Buradaki testler onun
  temiz olduğunu değil, gerçekten **ham** olduğunu kanıtlar: sentetik bir
  sentinel üst düzey ``stdout``'a konur, girdide bulunduğu doğrulanır ve
  ``ansible_output``'ta **bulunduğu** ölçülür.

Aynı sentinel'in structured yüzeye karışmadığı ayrıca ölçülür; iki iddia
birbirinin yerine geçmez.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import PLAYBOOK_RUNNER_MIN_RESULT_BYTES
from app.services.execution.normalize import (
    ERROR_PLAYBOOK_FAILED,
    ERROR_RESULT_LIMIT_EXCEEDED,
    ERROR_RUNNER_FAILED,
    ERROR_RUNNER_NO_HOSTS,
    ERROR_RUNNER_OUTPUT_INVALID,
    ERROR_RUNNER_TIMEOUT,
    LEGACY_SCHEMA_VERSION,
    MAX_ANSIBLE_OUTPUT_BYTES,
    OUTCOME_FAILED,
    OUTCOME_SUCCESSFUL,
    SCHEMA_VERSION,
    normalize_runner_output,
)

JOB_ID = "3f2b7c1a-9d4e-4a6b-8c1d-5e7f9a0b2c3d"
KNOWN_HOSTS = ("web-1", "db-1")

# Ham event akışına bilinçli olarak yerleştirilen sentetik değerler. Hiçbiri
# gerçek bir credential değildir; hepsi normalize sonuçta **bulunmamalıdır**.
SENTINELS = {
    "private_key": (
        "-----BEGIN OPENSSH PRIVATE KEY----- SENTINELKEYBODY -----END OPENSSH PRIVATE KEY-----"
    ),
    "vault": "$ANSIBLE_VAULT;1.1;AES256 6162636465",
    "token": "Bearer SENTINEL-API-TOKEN-9f3a",
    "password": "ansible_become_password=SENTINEL-BECOME-PW",
    "absolute_path": "/srv/gizli/dondurulmus/site.yml",
    "command": "/usr/bin/sudo /bin/sh -c 'echo SENTINEL-ARGV'",
    "digest": "sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
    "traceback": "Traceback (most recent call last): File '/opt/app/x.py', line 3",
    "no_log_payload": "SENTINEL-NO-LOG-PAYLOAD",
}

# Snapshot bağlantı değerleri. Uzun değer kısa değeri **içerir**: maskeleme
# uzundan kısaya yapılmazsa uzun değerin geri kalanı metinde açıkta kalırdı.
CONNECTION_VALUES = ("deploy-operator", "deploy", "22")


def event(name: str, **event_data: Any) -> str:
    """Tek bir runner stdout satırı."""
    return json.dumps({"event": name, "event_data": event_data, "counter": 1})


def stats_event(**counters: Any) -> str:
    """Terminal ``playbook_on_stats`` satırı."""
    payload: dict[str, Any] = {
        "ok": {},
        "changed": {},
        "failures": {},
        "dark": {},
        "skipped": {},
        "rescued": {},
        "ignored": {},
        "processed": {},
    }
    payload.update(counters)
    return json.dumps({"event": "playbook_on_stats", "event_data": payload})


def hostile_stream() -> str:
    """Bilinen bütün yasak alanları taşıyan gerçekçi bir event akışı.

    Alan adları ve iç yapı gerçek `ansible-runner` 2.4.3 çıktısından alınmıştır
    (ölçüldü): üst düzey ``stdout``, ``event_data.res``, ``task_args``,
    ``task_path`` ve ``resolved_action``.
    """
    return "\n".join(
        [
            json.dumps(
                {
                    "event": "playbook_on_task_start",
                    "stdout": f"TASK [{SENTINELS['command']}]",
                    "event_data": {
                        "task": "gizli task",
                        "task_args": SENTINELS["command"],
                        "task_path": f"{SENTINELS['absolute_path']}:11",
                        "playbook": SENTINELS["absolute_path"],
                    },
                }
            ),
            json.dumps(
                {
                    "event": "runner_on_ok",
                    "stdout": f"ok: [web-1] => {SENTINELS['no_log_payload']}",
                    "event_data": {
                        "host": "web-1",
                        "task": "no-log task",
                        "task_args": SENTINELS["password"],
                        "task_path": SENTINELS["absolute_path"],
                        "remote_addr": "10.0.0.9",
                        "res": {
                            "changed": True,
                            "censored": SENTINELS["no_log_payload"],
                            "stdout": SENTINELS["token"],
                            "invocation": {"module_args": {"key": SENTINELS["private_key"]}},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "runner_on_failed",
                    "stdout": SENTINELS["traceback"],
                    "event_data": {
                        "host": "db-1",
                        "task": "gizli task",
                        "res": {
                            "failed": True,
                            "msg": SENTINELS["vault"],
                            "exception": SENTINELS["traceback"],
                            "checksum": SENTINELS["digest"],
                        },
                        "env": {"ANSIBLE_VAULT_PASSWORD_FILE": SENTINELS["absolute_path"]},
                    },
                }
            ),
            stats_event(
                ok={"web-1": 1},
                changed={"web-1": 1},
                failures={"db-1": 1},
                processed={"web-1": 1, "db-1": 1},
            ),
        ]
    )


def structured_document(result: Any) -> str:
    """Sonucun **display yüzeyi çıkarılmış** canonical metni.

    Sızmazlık iddiası artık yalnız bu yüzey için geçerlidir: ``ansible_output``
    bilinçle ham taşındığı için onu da kapsayan bir "hiçbir sentinel yok"
    assertion'ı, ya yanlış bir gizlilik iddiası olurdu ya da ham çıktı
    sözleşmesini sessizce iptal ederdi.
    """
    document = result.to_document()
    document.pop("ansible_output")
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize(
    stdout: str,
    *,
    return_code: int = 0,
    timed_out: bool = False,
    oversized_stream: str | None = None,
    raw_limit_exceeded: bool = False,
    known_hosts: tuple[str, ...] = KNOWN_HOSTS,
    max_events: int = 1000,
    max_result_bytes: int = 1_000_000,
) -> Any:
    """Testler için kısa çağrı sarmalayıcısı."""
    return normalize_runner_output(
        job_id=JOB_ID,
        stdout_text=stdout,
        return_code=return_code,
        timed_out=timed_out,
        oversized_stream=oversized_stream,
        raw_limit_exceeded=raw_limit_exceeded,
        known_hosts=known_hosts,
        connection_values=CONNECTION_VALUES,
        max_events=max_events,
        max_result_bytes=max_result_bytes,
    )


# --- 16-17: recap ------------------------------------------------------------


def test_a_valid_event_stream_produces_a_numeric_recap() -> None:
    """Geçerli akıştan sayısal recap kurulur ve sonuç başarılıdır."""
    stream = "\n".join(
        [
            event("playbook_on_task_start", task="paket kur"),
            event("runner_on_ok", host="web-1", task="paket kur", res={"changed": True}),
            event("runner_on_skipped", host="db-1", task="paket kur"),
            stats_event(
                ok={"web-1": 1},
                changed={"web-1": 1},
                skipped={"db-1": 1},
                processed={"web-1": 1, "db-1": 1},
            ),
        ]
    )

    result = normalize(stream)

    assert result.schema_version == SCHEMA_VERSION
    assert result.outcome == OUTCOME_SUCCESSFUL
    assert result.error_code is None
    assert result.events_truncated is False
    assert result.result_truncated is False
    assert result.recap["web-1"].to_document() == {
        "ok": 1,
        "changed": 1,
        "failures": 0,
        "unreachable": 0,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
    }
    assert result.recap["db-1"].skipped == 1
    assert [item.event for item in result.events] == [
        "playbook_on_task_start",
        "runner_on_ok",
        "runner_on_skipped",
    ]
    assert result.events[1].changed is True
    assert result.events[1].failed is False


def test_an_unknown_host_can_never_enter_the_recap() -> None:
    """Bilinmeyen host recap'e girmez; sonuç fail-closed düşer."""
    stream = stats_event(ok={"yabanci-host": 1}, processed={"yabanci-host": 1})

    result = normalize(stream)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}
    assert result.events == ()
    assert "yabanci-host" not in result.serialize()


@pytest.mark.parametrize("count", ["3", -1, True, None, 1.5])
def test_a_non_numeric_recap_counter_is_rejected(count: Any) -> None:
    """Sayaç yerine metin, negatif sayı veya boolean kabul edilmez."""
    result = normalize(stats_event(ok={"web-1": count}))
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID


# --- 18-21: şekil ve sınır eşlemeleri ----------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        "bu bir JSON degil",
        "[1, 2, 3]",
        '"sadece metin"',
        "42",
        json.dumps({"no_event_field": True}),
        json.dumps({"event": 7}),
    ],
    ids=["not-json", "array", "string", "number", "missing-event", "event-not-string"],
)
def test_invalid_json_or_shape_fails_closed(stdout: str) -> None:
    """Geçersiz JSON veya beklenmeyen şekil kısmi sonuç üretmez."""
    result = normalize(stdout)
    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}
    assert result.events == ()


def test_the_event_count_limit_fails_closed() -> None:
    """Event sayısı sınırı aşılırsa sonuç kırpılmış hâliyle sunulmaz."""
    stream = "\n".join(
        [event("playbook_on_task_start", task=f"task-{index}") for index in range(5)]
    )
    stream = f"{stream}\n{stats_event(ok={'web-1': 1})}"

    result = normalize(stream, max_events=3)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RESULT_LIMIT_EXCEEDED
    assert result.events_truncated is True
    assert result.events == ()
    assert result.recap == {}


def test_the_result_byte_limit_fails_closed() -> None:
    """Serileştirilmiş sonuç bütçeyi aşarsa fail-closed düşülür.

    Arıza zarfının boyutu bütçeye değil **yapısına** bağlıdır: sabit alanlardan
    oluşur, çalıştırmadan gelen hiçbir veri taşımaz ve bu yüzden girdi ne kadar
    büyürse büyüsün büyümez.

    Bütçe olarak yapılandırılabilecek **en küçük** değer kullanılır. Ölçülen ve
    sabitlenen şudur: geçerli sonuç o bütçeyi aşar ve fail-closed'a düşer, ama
    ortaya çıkan arıza zarfının kendisi bütçenin **içinde** kalır. Aksi hâlde
    normalizer, kendi okuyucusunun (:mod:`app.services.execution.result`) aynı
    bütçeyle reddedeceği bir belge yayımlardı; ``PLAYBOOK_RUNNER_MIN_RESULT_BYTES``
    tam olarak bu yüzden ``1`` değildir.
    """
    stream = "\n".join(
        [event("playbook_on_task_start", task=f"uzun task adi {index}") for index in range(50)]
        + [stats_event(ok={"web-1": 1}, processed={"web-1": 1})]
    )

    result = normalize(stream, max_result_bytes=PLAYBOOK_RUNNER_MIN_RESULT_BYTES)
    document = result.serialize()

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RESULT_LIMIT_EXCEEDED
    assert result.result_truncated is True
    assert result.events == ()
    assert result.recap == {}
    assert "uzun task adi" not in document
    # Arıza zarfı, aşıldığı bildirilen bütçenin kendisine sığar.
    assert len(document.encode("utf-8")) <= PLAYBOOK_RUNNER_MIN_RESULT_BYTES


@pytest.mark.parametrize(
    ("kwargs", "expected", "truncated"),
    [
        ({"timed_out": True, "return_code": -1}, ERROR_RUNNER_TIMEOUT, True),
        ({"oversized_stream": "stdout"}, ERROR_RESULT_LIMIT_EXCEEDED, True),
        ({"oversized_stream": "stderr"}, ERROR_RESULT_LIMIT_EXCEEDED, True),
        ({"raw_limit_exceeded": True}, ERROR_RESULT_LIMIT_EXCEEDED, True),
    ],
    ids=["timeout", "stdout-oversize", "stderr-oversize", "raw-oversize"],
)
def test_process_level_failures_map_to_fixed_error_codes(
    kwargs: dict[str, Any], expected: str, truncated: bool
) -> None:
    """Timeout ve sınır aşımları sabit kodlara eşlenir; içerik taşınmaz.

    Girdi geçerli ve tam bir akış olsa bile süreç katmanı bir sınır bildirdiyse
    sonuç güvenilir sayılmaz.
    """
    result = normalize(
        "\n".join(
            [
                event("runner_on_ok", host="web-1", task="paket kur"),
                stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
            ]
        ),
        **kwargs,
    )

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == expected
    assert result.events_truncated is truncated
    assert result.events == ()
    assert result.recap == {}


def test_a_non_zero_return_code_with_a_terminal_event_keeps_the_recap() -> None:
    """rc != 0 meşru bir sonuçtur: recap korunur, outcome ``failed`` olur.

    Kanıt tam olduğu için (tek ve son terminal event, boş olmayan ``processed``,
    kapsamıyla tutarlı recap) ve güvenilir terminal recap gerçekten bir task
    failure raporladığı için kod ``playbook_failed``'dır. Kod kök nedeni
    **sınıflandırmaz**; yalnız bu raporun nerede bulunduğunu söyler.
    """
    stream = "\n".join(
        [
            event("runner_on_failed", host="db-1", task="paket kur", res={"failed": True}),
            stats_event(failures={"db-1": 1}, ok={"web-1": 1}, processed={"web-1": 1, "db-1": 1}),
        ]
    )

    result = normalize(stream, return_code=2)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_PLAYBOOK_FAILED
    assert result.return_code == 2
    assert result.recap["db-1"].failures == 1
    assert result.events[0].failed is True
    assert result.events_truncated is False


def test_a_missing_terminal_event_never_looks_complete() -> None:
    """Terminal event yoksa kısmi event listesi taşınmaz."""
    partial = event("runner_on_ok", host="web-1", task="paket kur")

    failed_run = normalize(partial, return_code=1)
    assert failed_run.error_code == ERROR_RUNNER_FAILED
    assert failed_run.events_truncated is True
    assert failed_run.events == ()

    zero_rc_run = normalize(partial, return_code=0)
    assert zero_rc_run.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert zero_rc_run.events == ()


# --- Boş çalıştırma ve terminal event tutarlılığı ----------------------------


def test_an_execution_that_processed_no_host_is_not_successful() -> None:
    """``rc=0`` ve geçerli terminal event yetmez: kimseye dokunulmadıysa başarı yoktur.

    Bu, sessiz bir yanlış pozitifin tam yeridir: inventory'si hiçbir host'la
    eşleşmemiş bir playbook `rc=0` döner ve recap boştur. "Başarılı" demek,
    hiç çalışmamış bir işi çalışmış gibi kaydetmek olurdu.
    """
    stream = "\n".join(
        [
            event("playbook_on_task_start", task="paket kur"),
            stats_event(processed={}),
        ]
    )

    result = normalize(stream)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_NO_HOSTS
    assert result.recap == {}
    assert result.events == ()
    assert OUTCOME_SUCCESSFUL not in result.serialize()


@pytest.mark.parametrize(
    "processed",
    [
        {"yabanci-host": 1},
        {"web-1": "1"},
        {"web-1": -1},
        {"web-1": True},
        {"web-1": 1.5},
        {"web-1": None},
        {7: 1},
        [],
        "web-1",
        None,
    ],
    ids=[
        "unknown-host",
        "string-count",
        "negative-count",
        "bool-count",
        "float-count",
        "none-count",
        "non-string-host",
        "list",
        "string",
        "missing",
    ],
)
def test_a_structurally_invalid_processed_field_fails_closed(processed: Any) -> None:
    """``processed`` yapısal olarak doğrulanır; şekli bozuksa sonuç taşınmaz."""
    payload: dict[str, Any] = {"ok": {}, "processed": processed}
    if processed is None:
        payload.pop("processed")
    stream = json.dumps({"event": "playbook_on_stats", "event_data": payload})

    result = normalize(stream)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}


def test_a_counter_host_outside_the_processed_set_fails_closed() -> None:
    """Sayaçlarda görünen bir host ``processed`` kümesinde de bulunmalıdır.

    Aksi hâlde recap, terminal event'in kendi içinde çelişmesine rağmen tutarlı
    bir özet gibi sunulurdu.
    """
    result = normalize(stats_event(ok={"db-1": 1}, processed={"web-1": 1}))

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}


def test_processed_hosts_are_represented_even_with_zero_counters() -> None:
    """Hiçbir sayacı olmayan bir processed host da recap'te görünür.

    Yalnız ``skipped``/``dark`` sayacı olan hostlar sonuçtan düşseydi, recap
    çalıştırmanın kapsamını olduğundan dar gösterirdi.
    """
    stream = "\n".join(
        [
            event("runner_on_skipped", host="web-1", task="paket kur"),
            stats_event(
                skipped={"web-1": 1},
                dark={"db-1": 1},
                processed={"web-1": 1, "db-1": 1},
            ),
        ]
    )

    result = normalize(stream, return_code=4)

    assert sorted(result.recap) == ["db-1", "web-1"]
    assert result.recap["web-1"].to_document() == {
        "ok": 0,
        "changed": 0,
        "failures": 0,
        "unreachable": 0,
        "skipped": 1,
        "rescued": 0,
        "ignored": 0,
    }
    assert result.recap["db-1"].unreachable == 1
    assert result.recap["db-1"].skipped == 0


@pytest.mark.parametrize(
    "counters",
    [{"failures": {"db-1": 1}}, {"dark": {"db-1": 1}}],
    ids=["failures", "unreachable"],
)
def test_a_zero_return_code_with_failed_hosts_is_not_successful(counters: dict[str, Any]) -> None:
    """``rc=0`` recap'teki başarısız/erişilemeyen hostu geçersiz kılmaz.

    Ansible, ``ignore_errors``/``--check`` bileşimlerinde sıfır dönerken recap'te
    başarısız host bildirebilir; sonucu çıkış koduna bakarak "successful"
    işaretlemek o hostları görünmez kılardı.
    """
    stream = stats_event(ok={"web-1": 1}, processed={"web-1": 1, "db-1": 1}, **counters)

    result = normalize(stream)

    assert result.return_code == 0
    assert result.outcome == OUTCOME_FAILED
    # `rc` sınıfın önkoşulu değildir: kanıt recap'tedir.
    assert result.error_code == ERROR_PLAYBOOK_FAILED
    # Recap **korunur**: bu bir çıktı bozukluğu değil, meşru bir başarısızlıktır.
    assert sorted(result.recap) == ["db-1", "web-1"]


# --- Başarısızlık sınıfı (R1-V3G1B) ------------------------------------------


@pytest.mark.parametrize(
    ("return_code", "counters", "expected"),
    [
        # Kanıt tam ve recap failure bildiriyor: sınıf `rc`'den bağımsızdır.
        pytest.param(2, {"failures": {"db-1": 1}}, ERROR_PLAYBOOK_FAILED, id="failures-rc2"),
        pytest.param(0, {"failures": {"db-1": 1}}, ERROR_PLAYBOOK_FAILED, id="failures-rc0"),
        # `unreachable` de aynı sınıfa girer: sayaç güvenilir terminal recap'in
        # içinde raporlanmıştır. Kök neden SSH, ağ ya da hedef yapılandırması
        # gibi operasyonel bir sebep olabilir ve kod bunu sınıflandırmaz; ayrımı
        # yapan tek şey, raporun executor'ın iç arıza yollarından
        # (artifact/cleanup/lease) değil Ansible'ın terminal sonucundan
        # gelmesidir.
        pytest.param(4, {"dark": {"db-1": 1}}, ERROR_PLAYBOOK_FAILED, id="unreachable-rc4"),
        pytest.param(0, {"dark": {"db-1": 1}}, ERROR_PLAYBOOK_FAILED, id="unreachable-rc0"),
        # İkisi birden: tek bir host failure yeter, hepsi aranmaz.
        pytest.param(
            2,
            {"failures": {"db-1": 1}, "dark": {"web-1": 1}},
            ERROR_PLAYBOOK_FAILED,
            id="both",
        ),
        # Recap temiz ama `rc` sıfır değil: iki sinyal ayrışmıştır ve ayrışmayı
        # playbook lehine yorumlamak bir tahmin olurdu. Legacy kod kalır.
        pytest.param(2, {"ok": {"db-1": 1}}, ERROR_RUNNER_FAILED, id="clean-recap-rc2"),
    ],
)
def test_the_failure_class_follows_the_recap_not_the_return_code(
    return_code: int, counters: dict[str, Any], expected: str
) -> None:
    """Başarısızlık sınıfı **kanıta** bakar, çıkış koduna değil.

    ``playbook_failed`` yalnız üç kanıt birlikteyken üretilir: tek ve son
    terminal event, boş olmayan ``processed`` ve kapsamıyla tutarlı recap. Kanıt
    tamken recap'te ``failures`` ya da ``unreachable`` varsa güvenilir terminal
    recap'te task failure veya unreachable host raporlanmış demektir — kök neden
    hakkında bir iddia değildir. ``rc`` bu kararın önkoşulu değildir; recap
    temizken tek başına sınıfı belirleyemez de.
    """
    stream = stats_event(processed={"web-1": 1, "db-1": 1}, **counters)

    result = normalize(stream, return_code=return_code)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == expected
    # Sınıf ne olursa olsun kanıt taşınır: recap düşürülmez.
    assert sorted(result.recap) == ["db-1", "web-1"]


@pytest.mark.parametrize(
    ("stdout", "return_code", "expected"),
    [
        # Terminal event hiç yok: erken çıkış, syntax hatası. Recap kurulamaz.
        pytest.param(
            event("runner_on_ok", host="web-1", task="paket kur"),
            2,
            ERROR_RUNNER_FAILED,
            id="no-terminal-event",
        ),
        # Terminal event geçerli ama kapsam boş: hiçbir host'a dokunulmadı.
        pytest.param(stats_event(processed={}), 2, ERROR_RUNNER_FAILED, id="empty-processed"),
    ],
)
def test_a_run_without_recap_evidence_stays_on_the_legacy_code(
    stdout: str, return_code: int, expected: str
) -> None:
    """Kanıtsız başarısızlık ``playbook_failed`` olamaz.

    İki yolun da ortak yanı recap'in **hiç kurulamamış** olmasıdır: bir yolda
    terminal event yoktur, diğerinde kapsam boştur. İkisini de "task failure
    veya unreachable host raporlandı" diye sunmak, hiç raporlanmamış bir sonucu
    uydurmak olurdu.
    """
    result = normalize(stdout, return_code=return_code)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == expected
    assert result.recap == {}
    assert result.events == ()


def test_an_event_after_the_terminal_stats_fails_closed() -> None:
    """Terminal event son satır olmalıdır; sonrasında gelen satır akışı şüpheli kılar."""
    stream = "\n".join(
        [
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
            event("runner_on_ok", host="web-1", task="gec gelen task"),
        ]
    )

    result = normalize(stream)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}
    assert result.events == ()


def test_two_terminal_stats_events_fail_closed() -> None:
    """İki terminal event, iki farklı çalıştırmanın aynı akışta birleşmesidir."""
    stream = "\n".join(
        [
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
            stats_event(ok={"db-1": 1}, processed={"db-1": 1}),
        ]
    )

    result = normalize(stream)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RUNNER_OUTPUT_INVALID
    assert result.recap == {}


# --- 22-26: sızdırmazlık -----------------------------------------------------


def test_the_hostile_fixture_really_contains_every_synthetic_secret() -> None:
    """Önce girdinin gerçekten kirli olduğu kanıtlanır.

    Bu test olmadan sonraki sızıntı testleri hiçbir şey ölçmezdi: bulunmayan
    bir secret'ın sonuçta da bulunmaması beklenen bir sonuç değil, boş bir
    tautolojidir.
    """
    stream = hostile_stream()
    for name, value in SENTINELS.items():
        assert value in stream, name


def test_no_synthetic_secret_survives_into_the_structured_surface() -> None:
    """Sentetik secret'ların hiçbiri structured yüzeye giremez.

    Ölçülen yüzey ``ansible_output`` **hariç** belgenin tamamıdır. Sözleşme
    R1-V3J3A'dan beri iki parçalıdır: structured özet sansürlüdür, display metni
    hamdır. Eski hâliyle bu test ikisini tek bir "hiçbir sentinel yok"
    iddiasında birleştiriyordu; bugün aynı iddiayı sürdürmek, üst düzey
    ``stdout``'un gerçekten taşındığı gerçeğini gizlemek olurdu.

    Structured yüzeyin iddiası ise **gevşetilmedi**: ``event_data``, ``res``,
    ``task_args``, ``task_path`` ve maskelenmemiş bağlantı değerleri buraya
    hiçbir yolda giremez.
    """
    result = normalize(hostile_stream(), return_code=2)
    document = structured_document(result)

    for name, value in SENTINELS.items():
        assert value not in document, name
        # Çok satırlı secret'ların tek satırlık parçaları da aranır.
        for fragment in value.splitlines():
            if len(fragment) > 12:
                assert fragment not in document, f"{name}: {fragment}"


@pytest.mark.parametrize(
    "field",
    [
        "res",
        "task_args",
        "task_path",
        "stdout",
        "stderr",
        "env",
        "invocation",
        "module_args",
        "remote_addr",
        "playbook",
        "counter",
        "exception",
        "checksum",
        "censored",
    ],
)
def test_forbidden_event_fields_never_appear_in_the_document(field: str) -> None:
    """res/args/env/command/stdout/stderr/path/key/token/digest taşınmaz.

    Ölçülen şey **alan adı**dır: belgede ``stdout`` diye bir alan yoktur. Display
    metni ayrı ve açıkça adlandırılmış bir alanda (``ansible_output``) durur;
    runner event'inin ham alan adları hiçbir seviyede yeniden üretilmez.
    """
    document = json.loads(normalize(hostile_stream(), return_code=2).serialize())
    assert field not in document
    for entry in document["events"]:
        assert set(entry) == {"event", "host", "task", "changed", "failed"}
        assert field not in entry


def test_a_no_log_payload_never_reaches_the_structured_surface() -> None:
    """``no_log`` bir task'ın payload'ı structured yüzeye giremez.

    İddia dar ve dürüsttür: ``event_data.res`` hiçbir biçimde taşınmadığı için
    sansürlenmiş payload recap/event alanlarına **giremez**. Bu, platformun
    ``no_log`` için eksiksiz bir gizlilik garantisi verdiği anlamına **gelmez**:
    aynı değer Ansible'ın kendi ekran çıktısına düşmüşse ``ansible_output``'ta
    bulunabilir ve orası bilinçle ham taşınır.
    """
    result = normalize(hostile_stream(), return_code=2)
    document = structured_document(result)

    assert SENTINELS["no_log_payload"] not in document
    # Metaveri korunur: event türü ve host hâlâ görünür.
    assert [entry.event for entry in result.events] == [
        "playbook_on_task_start",
        "runner_on_ok",
        "runner_on_failed",
    ]
    assert result.events[1].host == "web-1"


def test_absolute_paths_and_digests_are_absent_from_the_structured_surface() -> None:
    """Sunucu yolları ve digest'ler structured yüzeyde hiç görünmez."""
    document = structured_document(normalize(hostile_stream(), return_code=2))
    assert "/srv/" not in document
    assert "sha256:" not in document
    assert "Traceback" not in document
    assert "BEGIN OPENSSH PRIVATE KEY" not in document


def test_host_and_task_text_is_redacted_and_connection_values_are_masked() -> None:
    """Host/task metinlerinde redaction ve maskeleme uygulanır.

    Bağlantı değerleri uzundan kısaya maskelenir: ``deploy`` önce maskelenseydi
    ``deploy-operator`` metninde ``***-operator`` kalırdı.
    """
    stream = "\n".join(
        [
            event(
                "runner_on_ok",
                host="web-1",
                task="deploy-operator icin ansible_password=SENTINEL-PW calistir",
            ),
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
        ]
    )

    result = normalize(stream)
    task = result.events[0].task or ""

    assert "SENTINEL-PW" not in task
    assert "deploy-operator" not in task
    assert "***-operator" not in task
    assert task.startswith("*** icin")


def test_long_host_and_task_text_is_bounded() -> None:
    """Sınırsız metin tek bir event ile byte bütçesini tüketemez."""
    stream = "\n".join(
        [
            event("runner_on_ok", host="web-1", task="A" * 5_000),
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
        ]
    )
    result = normalize(stream)
    assert len(result.events[0].task or "") == 200


# --- 27: determinizm ---------------------------------------------------------


def test_the_same_input_always_produces_the_same_document() -> None:
    """Aynı girdi baytı baytına aynı sonucu üretir.

    Determinizm bir konfor değil sınır koşuludur: byte bütçesi ölçümü
    çalışmadan çalışmaya değişemez.
    """
    stream = hostile_stream()
    documents = {normalize(stream, return_code=2).serialize() for _ in range(5)}
    assert len(documents) == 1

    valid = "\n".join(
        [
            event("runner_on_ok", host="web-1", task="paket kur"),
            event("runner_on_skipped", host="db-1", task="paket kur"),
            stats_event(ok={"web-1": 1}, skipped={"db-1": 1}, processed={"web-1": 1, "db-1": 1}),
        ]
    )
    assert normalize(valid).serialize() == normalize(valid).serialize()
    # Recap host sırası girdi sırasından bağımsızdır.
    assert list(json.loads(normalize(valid).serialize())["recap"]) == ["db-1", "web-1"]


# --- R1-V3J3A: bounded display output ----------------------------------------

# Üst düzey ``stdout``'a bilinçli olarak konan sentetik "hassas" değer. Ham
# çıktı sözleşmesinin **dürüst** testi budur: değerin sonuçta bulunmadığını
# değil, **bulunduğunu** ölçer.
DISPLAY_SENTINEL = "fatal: [web-1]: FAILED! => ansible_become_password=SENTINEL-DISPLAY-PW"


# ``display_event``'e "stdout alanını hiç koyma" demenin yolu; ``None`` başka bir
# şeydir ve ayrıca test edilir.
_ABSENT = object()


def display_event(name: str, stdout: Any, **event_data: Any) -> str:
    """Üst düzey ``stdout`` alanı taşıyan tek bir runner satırı."""
    document: dict[str, Any] = {"event": name, "event_data": event_data}
    if stdout is not _ABSENT:
        document["stdout"] = stdout
    return json.dumps(document)


def display_stream(*stdouts: Any) -> str:
    """Her biri kendi üst düzey ``stdout``'unu taşıyan geçerli bir akış."""
    lines = [display_event("runner_on_ok", value, host="web-1", task="Ping") for value in stdouts]
    lines.append(
        json.dumps(
            {
                "event": "playbook_on_stats",
                "stdout": "PLAY RECAP *********",
                "event_data": {
                    "ok": {"web-1": 1},
                    "changed": {},
                    "failures": {},
                    "dark": {},
                    "skipped": {},
                    "rescued": {},
                    "ignored": {},
                    "processed": {"web-1": 1},
                },
            }
        )
    )
    return "\n".join(lines)


def test_top_level_stdout_lines_are_joined_in_event_order() -> None:
    """Üst düzey ``stdout`` satırları sırayla ve ``\\n`` ile birleşir."""
    result = normalize(display_stream("TASK [Ping] ****", "ok: [web-1]"))

    assert result.ansible_output == "TASK [Ping] ****\nok: [web-1]\nPLAY RECAP *********"
    assert result.ansible_output_truncated is False
    # Structured sonuç değişmez.
    assert result.outcome == OUTCOME_SUCCESSFUL
    assert result.recap["web-1"].ok == 1


def test_the_json_event_document_itself_is_never_user_output() -> None:
    """Çıktı, event JSON'unun kendisi değildir: yalnız ``stdout`` değerleridir."""
    result = normalize(display_stream("ok: [web-1]"))
    output = result.ansible_output or ""

    assert "ok: [web-1]" in output
    for marker in ('"event"', '"event_data"', "runner_on_ok", "playbook_on_stats", '"host"'):
        assert marker not in output, marker


def test_only_the_top_level_stdout_field_is_a_display_source() -> None:
    """``res.stdout``/``res.stderr``/``res.msg``, task args ve process stderr alınmaz."""
    stream = "\n".join(
        [
            json.dumps(
                {
                    "event": "runner_on_ok",
                    "stdout": "ok: [web-1]",
                    "event_data": {
                        "host": "web-1",
                        "task": "Ping",
                        "task_args": "SENTINEL-TASK-ARGS",
                        "res": {
                            "changed": False,
                            "stdout": "SENTINEL-RES-STDOUT",
                            "stderr": "SENTINEL-RES-STDERR",
                            "msg": "SENTINEL-RES-MSG",
                        },
                    },
                }
            ),
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
        ]
    )
    # Girdi gerçekten kirli: aksi hâlde test boş bir tautoloji olurdu.
    for marker in ("SENTINEL-RES-STDOUT", "SENTINEL-RES-STDERR", "SENTINEL-RES-MSG"):
        assert marker in stream, marker

    result = normalize(stream)

    assert result.ansible_output == "ok: [web-1]"
    for marker in (
        "SENTINEL-RES-STDOUT",
        "SENTINEL-RES-STDERR",
        "SENTINEL-RES-MSG",
        "SENTINEL-TASK-ARGS",
    ):
        assert marker not in (result.ansible_output or ""), marker
        assert marker not in structured_document(result), marker


@pytest.mark.parametrize(
    "value",
    ["", _ABSENT, None, 7, 1.5, True, ["ok: [web-1]"], {"text": "ok"}],
    ids=["empty", "missing", "null", "int", "float", "bool", "list", "object"],
)
def test_a_non_string_or_empty_top_level_stdout_is_not_display_output(value: Any) -> None:
    """Yalnız gerçek ve boş olmayan bir ``str`` çıktıya girer."""
    result = normalize(
        "\n".join(
            [
                display_event("runner_on_ok", value, host="web-1", task="Ping"),
                stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
            ]
        )
    )

    assert result.outcome == OUTCOME_SUCCESSFUL
    assert result.ansible_output is None
    assert result.ansible_output_truncated is False


def test_a_stream_without_any_display_line_reports_no_output() -> None:
    """Hiç uygun satır yoksa ``None``/``False``; boş metin uydurulmaz."""
    result = normalize(
        "\n".join(
            [
                event("runner_on_ok", host="web-1", task="Ping"),
                stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
            ]
        )
    )

    assert result.ansible_output is None
    assert result.ansible_output_truncated is False


def test_the_display_output_limit_is_measured_in_utf8_bytes() -> None:
    """Sınır karakter değil **byte** sayar ve kesin olarak uygulanır."""
    result = normalize(display_stream("a" * (MAX_ANSIBLE_OUTPUT_BYTES + 5_000)))
    output = result.ansible_output or ""

    assert result.ansible_output_truncated is True
    assert len(output.encode("utf-8")) == MAX_ANSIBLE_OUTPUT_BYTES
    assert output.startswith("aaaa")
    # Sınır aşımında kullanıcı metni eklenmez; yalnız bayrak konur.
    assert output.endswith("a")


def test_a_multibyte_character_is_never_split_in_half() -> None:
    """Çok baytlı karakter ortadan bölünmez; kesme karakter sınırına iner.

    ``€`` üç byte'tır ve 131072 üçe tam bölünmez: naif bir ``[:limit]``
    dilimlemesi yarım bir dizi bırakır ve metin ya decode edilemez ya da bir
    ``U+FFFD`` ile "düzeltilmiş" görünürdü.
    """
    assert MAX_ANSIBLE_OUTPUT_BYTES % 3 != 0

    result = normalize(display_stream("€" * MAX_ANSIBLE_OUTPUT_BYTES))
    output = result.ansible_output or ""

    assert result.ansible_output_truncated is True
    assert set(output) == {"€"}
    assert "�" not in output
    assert len(output.encode("utf-8")) == (MAX_ANSIBLE_OUTPUT_BYTES // 3) * 3
    assert len(output.encode("utf-8")) <= MAX_ANSIBLE_OUTPUT_BYTES


def test_display_output_is_dropped_before_a_valid_recap_is_dropped() -> None:
    """Bütçe yetmiyorsa önce ham çıktı bırakılır, recap/event korunur.

    Ham çıktı, geçerli bir sonucu ``result_limit_exceeded`` yapmamalıdır:
    kullanıcının kararı structured özette, en büyük ve en az yapısal parça ise
    display metnindedir.
    """
    lines = [
        event("runner_on_ok", host="web-1", task="Ping"),
        stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
    ]
    plain = normalize("\n".join(lines))
    budget = len(plain.serialize().encode("utf-8"))

    noisy = "\n".join(
        [
            display_event("runner_on_ok", "x" * 5_000, host="web-1", task="Ping"),
            stats_event(ok={"web-1": 1}, processed={"web-1": 1}),
        ]
    )
    result = normalize(noisy, max_result_bytes=budget)

    assert result.outcome == OUTCOME_SUCCESSFUL
    assert result.error_code is None
    assert result.result_truncated is False
    assert result.recap["web-1"].ok == 1
    assert [item.event for item in result.events] == ["runner_on_ok"]
    assert result.ansible_output is None
    assert result.ansible_output_truncated is True
    assert len(result.serialize().encode("utf-8")) <= budget


def test_a_result_that_does_not_fit_without_output_still_fails_closed() -> None:
    """Çıktısız belge de sığmıyorsa mevcut fail-closed davranış korunur."""
    stream = "\n".join(
        [
            display_event("runner_on_ok", "x" * 5_000, host="web-1", task=f"uzun task adi {index}")
            for index in range(50)
        ]
        + [stats_event(ok={"web-1": 1}, processed={"web-1": 1})]
    )

    result = normalize(stream, max_result_bytes=PLAYBOOK_RUNNER_MIN_RESULT_BYTES)
    document = result.serialize()

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == ERROR_RESULT_LIMIT_EXCEEDED
    assert result.result_truncated is True
    assert result.recap == {}
    assert result.events == ()
    assert result.ansible_output is None
    assert result.ansible_output_truncated is False
    assert "xxxx" not in document
    assert len(document.encode("utf-8")) <= PLAYBOOK_RUNNER_MIN_RESULT_BYTES


@pytest.mark.parametrize(
    ("stdout", "kwargs", "expected"),
    [
        pytest.param(
            display_stream("ok: [web-1]"), {"timed_out": True}, ERROR_RUNNER_TIMEOUT, id="timeout"
        ),
        pytest.param(
            display_stream("ok: [web-1]"),
            {"oversized_stream": "stdout"},
            ERROR_RESULT_LIMIT_EXCEEDED,
            id="stdout-oversize",
        ),
        pytest.param(
            display_stream("ok: [web-1]"),
            {"raw_limit_exceeded": True},
            ERROR_RESULT_LIMIT_EXCEEDED,
            id="raw-oversize",
        ),
        pytest.param(
            '{"event": "runner_on_ok", "stdout": "ok: [web-1]"}\nbu bir JSON degil',
            {},
            ERROR_RUNNER_OUTPUT_INVALID,
            id="invalid-json",
        ),
        pytest.param(
            display_stream("ok: [web-1]") + "\n" + stats_event(processed={"web-1": 1}),
            {},
            ERROR_RUNNER_OUTPUT_INVALID,
            id="two-terminal-events",
        ),
        pytest.param(
            display_event("runner_on_ok", "ok: [web-1]", host="web-1", task="Ping"),
            {"return_code": 2},
            ERROR_RUNNER_FAILED,
            id="no-terminal-event",
        ),
    ],
)
def test_a_fail_closed_result_never_rescues_the_display_output(
    stdout: str, kwargs: dict[str, Any], expected: str
) -> None:
    """Güvenilmez bir akıştan ham metin kurtarılmaz.

    Girdi gerçekten display satırı taşır; sonuçta bulunmaması bu yüzden boş bir
    tautoloji değildir.
    """
    assert "ok: [web-1]" in stdout

    result = normalize(stdout, **kwargs)

    assert result.outcome == OUTCOME_FAILED
    assert result.error_code == expected
    assert result.ansible_output is None
    assert result.ansible_output_truncated is False
    assert "ok: [web-1]" not in result.serialize()


def test_a_reported_host_failure_still_carries_the_display_output() -> None:
    """``playbook_failed`` geçerli bir terminal sonuçtur: çıktı taşınır."""
    stream = "\n".join(
        [
            display_event(
                "runner_on_failed",
                DISPLAY_SENTINEL,
                host="db-1",
                task="Harden",
                res={"failed": True},
            ),
            stats_event(failures={"db-1": 1}, processed={"db-1": 1}),
        ]
    )

    result = normalize(stream, return_code=2)

    assert result.error_code == ERROR_PLAYBOOK_FAILED
    assert result.ansible_output == DISPLAY_SENTINEL


def test_the_display_output_is_really_raw_and_is_not_claimed_to_be_safe() -> None:
    """Dürüst ham çıktı testi: sentinel girdide **ve** ``ansible_output``'ta bulunur.

    Bu test bir gizlilik iddiası **değildir**; tam tersini kanıtlar. Üst düzey
    ``stdout``'a konan hassas görünümlü bir değer, sansürlenmeden kullanıcıya
    ulaşır — trusted-operator/CLI-equivalent modelin gereği budur.

    İkinci yarısı ise iddianın sınırını çizer: aynı sentinel structured
    host/task/recap alanlarına **karışmaz**. İki yüzey ayrıdır ve biri
    diğerinin yerine geçmez.
    """
    stream = "\n".join(
        [
            display_event("runner_on_failed", DISPLAY_SENTINEL, host="db-1", task="Harden"),
            stats_event(failures={"db-1": 1}, processed={"db-1": 1}),
        ]
    )
    assert DISPLAY_SENTINEL in stream

    result = normalize(stream, return_code=2)

    # Ham yüzey gerçekten hamdır.
    assert result.ansible_output == DISPLAY_SENTINEL
    assert "SENTINEL-DISPLAY-PW" in (result.ansible_output or "")

    # Structured yüzey ondan hiç etkilenmez.
    assert DISPLAY_SENTINEL not in structured_document(result)
    assert "SENTINEL-DISPLAY-PW" not in structured_document(result)
    assert [item.task for item in result.events] == ["Harden"]
    assert [item.host for item in result.events] == ["db-1"]
    assert sorted(result.recap) == ["db-1"]


def test_the_display_output_never_enters_the_run_repr() -> None:
    """``repr`` ham çıktıyı basmaz: tek bir log satırı onu dışarı taşırdı."""
    result = normalize(display_stream(DISPLAY_SENTINEL))

    assert result.ansible_output is not None
    assert DISPLAY_SENTINEL not in repr(result)
    assert "SENTINEL-DISPLAY-PW" not in repr(result)
    assert "ansible_output=" not in repr(result)
    # Bayrağın kendisi gizlenmez: içerik değil, yalnız metin dışarıda kalır.
    assert "ansible_output_truncated=" in repr(result)


def test_the_writer_always_produces_the_current_schema_version() -> None:
    """Yeni writer her yolda ``schema_version=2`` üretir; sürüm 1 yalnız okunur."""
    assert SCHEMA_VERSION == 2
    assert LEGACY_SCHEMA_VERSION == 1

    successful = normalize(display_stream("ok: [web-1]"))
    failed = normalize("bu bir JSON degil")

    for result in (successful, failed):
        assert result.schema_version == SCHEMA_VERSION
        assert json.loads(result.serialize())["schema_version"] == 2
        assert set(result.to_document()) == {
            "schema_version",
            "job_id",
            "return_code",
            "outcome",
            "error_code",
            "recap",
            "events",
            "events_truncated",
            "result_truncated",
            "ansible_output",
            "ansible_output_truncated",
        }

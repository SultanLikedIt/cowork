"""The mail-compose step: one model call, checked, rewritten once, then a template."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import mail_compose
from mail_compose import EmailDraft


class _Model:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


GOOD = EmailDraft(
    subject="Deep research modu bulguları",
    body="Merhaba Sinan,\n\nDeep research modunda iki sorun tespit ettik.\n\nRapor ektedir.\n\nİyi çalışmalar.",
)


def _compose(model: _Model, **overrides: Any) -> mail_compose.ComposedEmail:
    args = dict(
        instruction="bu raporu sinan@sirket.com'a gönder, mailde deep research sorunlarını anlat",
        answer="Rapor: deep research modu güvenilir sitelere yönlenmiyor.",
        attachments=["rapor.pdf"],
        complete=model,
    )
    args.update(overrides)
    return asyncio.run(mail_compose.compose_email(**args))


def test_a_good_draft_is_used_as_is() -> None:
    result = _compose(_Model(GOOD))
    assert (result.subject, result.body, result.source) == (GOOD.subject, GOOD.body, "llm")


@pytest.mark.parametrize(
    "body, reason",
    [
        ("Merhaba,\n\n```python\nsend_email(to='x')\n```", "code"),
        ("Merhaba, send_email(to='x') ile gönderiyorum, rapor ektedir.", "tool call"),
        ("Üzgünüm, e-posta gönderemiyorum. Metni kopyalayabilirsiniz.", "cannot send"),
        ("Dosyadan raporun tam metnini okuyorum ve ardından mail yazacağım.", "narrates"),
        ("Tamam.", "too short"),
    ],
)
def test_bad_drafts_are_caught(body: str, reason: str) -> None:
    found = mail_compose.problems(EmailDraft(subject="Rapor", body=body))
    assert any(reason in item for item in found), found


def test_a_subject_that_repeats_the_instruction_is_caught() -> None:
    found = mail_compose.problems(EmailDraft(subject="bu raporu gönderir misin", body=GOOD.body))
    assert any("instruction" in item for item in found)


def test_a_bad_draft_is_rewritten_once_with_the_reason() -> None:
    bad = EmailDraft(subject="Rapor", body="Üzgünüm, e-posta gönderemiyorum ama metin şu: ...")
    model = _Model(bad, GOOD)
    result = _compose(model)
    assert result.source == "llm_retry" and result.body == GOOD.body
    assert "REJECTED" in model.calls[1][-1]["content"]


def test_two_failures_fall_back_to_the_template() -> None:
    result = _compose(_Model(RuntimeError("down"), "not json"))
    assert result.source == "template"
    assert result.subject == "rapor"
    assert "rapor.pdf" in result.body
    assert mail_compose.problems(EmailDraft(subject=result.subject, body=result.body)) == []


def test_the_template_never_carries_code_from_the_answer() -> None:
    result = _compose(
        _Model(RuntimeError("down"), RuntimeError("down")),
        answer="Okuyorum.\n```python\nsend_email(to='x')\n```\nÖzet: iki sorun var.",
    )
    assert "send_email" not in result.body
    assert "iki sorun var" in result.body


def test_the_material_is_fenced_and_stripped_of_code() -> None:
    model = _Model(GOOD)
    _compose(model, answer="Bulgular.\n```python\nimport os\nos.system('x')\n```\nSon.")
    prompt = model.calls[0][-1]["content"]
    assert "ALINTI" in prompt  # quoted as data, not instructions
    assert "os.system" not in prompt
    assert "Bulgular." in prompt and "Son." in prompt
    assert "rapor.pdf" in prompt


def test_an_attachment_claim_needs_an_attachment() -> None:
    draft = EmailDraft(subject="Altın sunumu", body="Merhaba,\n\nSunu bu e-postaya eklenmiştir.\n\nİyi çalışmalar.")
    assert any("attached" in p for p in mail_compose.problems(draft, []))
    assert mail_compose.problems(draft, ["altin.pptx"]) == []
    assert mail_compose.problems(draft) == []  # unknown: not judged

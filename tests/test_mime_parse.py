"""Reading mail somebody else wrote (inbound email, Phase B).

Every fixture here is raw bytes, written the way the wire carries them, because
that is the only honest way to test a parser: a message built with the same
library that reads it agrees with the reader about everything, including the
things both get wrong. The one round trip through the outbound builder is there
to prove the two modules agree, not to prove either is right.

The cases are the ones that have to be survived rather than handled well: a
charset nobody has heard of, bytes that are not in the charset they claim, NUL
bytes that Postgres will not store, a multipart nested a thousand deep. Nothing
here needs a database.
"""

from __future__ import annotations

import pytest
from seskit_core.email import build_message, message_bytes
from seskit_core.email.parse import (
    MAX_BODY_CHARS,
    MAX_DEPTH,
    MAX_HEADERS,
    MAX_PARTS,
    parse_message,
    read_attachment,
)
from seskit_core.providers import Attachment, OutboundEmail

CRLF = "\r\n"


def _mail(*lines: str) -> bytes:
    return (CRLF.join(lines)).encode("utf-8")


SIMPLE = _mail(
    "From: Ada Lovelace <Ada@Example.org>",
    "To: support@example.com, Other <other@example.com>",
    "Cc: cc@example.com",
    "Subject: Hello there",
    "Message-ID: <abc123@example.org>",
    "Date: Mon, 05 Oct 2026 10:00:00 +0000",
    "",
    "Hi.",
    "",
    "Ada",
    "",
)


# ------------------------------------------------------------ the ordinary ---


def test_a_plain_message_is_read_into_its_fields() -> None:
    parsed = parse_message(SIMPLE)

    assert parsed.from_address == "ada@example.org"
    assert parsed.from_name == "Ada Lovelace"
    assert parsed.to == ["support@example.com", "other@example.com"]
    assert parsed.cc == ["cc@example.com"]
    assert parsed.subject == "Hello there"
    assert parsed.message_id == "abc123@example.org"
    assert parsed.text == "Hi.\n\nAda\n"
    assert parsed.html == ""
    assert parsed.attachments == []
    assert parsed.sent_at is not None
    assert parsed.sent_at.year == 2026
    assert parsed.size == len(SIMPLE)
    assert not parsed.truncated
    assert not parsed.parse_failed


def test_alternative_gives_both_bodies() -> None:
    raw = _mail(
        "From: a@example.org",
        "Subject: both",
        "MIME-Version: 1.0",
        'Content-Type: multipart/alternative; boundary="B"',
        "",
        "--B",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "plain version",
        "--B",
        "Content-Type: text/html; charset=utf-8",
        "",
        "<p>html version</p>",
        "--B--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.text.strip() == "plain version"
    assert parsed.html.strip() == "<p>html version</p>"
    assert parsed.attachments == []


def test_an_attachment_is_described_and_its_bytes_come_back_by_index() -> None:
    raw = _mail(
        "From: a@example.org",
        "Subject: with file",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: text/plain",
        "",
        "see attached",
        "--M",
        "Content-Type: application/pdf",
        'Content-Disposition: attachment; filename="report.pdf"',
        "Content-Transfer-Encoding: base64",
        "",
        "JVBERi0xLjQK",  # "%PDF-1.4\n"
        "--M--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.text.strip() == "see attached"
    (attachment,) = parsed.attachments
    assert attachment.filename == "report.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.size == 9
    assert attachment.index == 0
    assert not attachment.inline

    content = read_attachment(raw, 0)
    assert content is not None
    assert content.data == b"%PDF-1.4\n"
    assert content.filename == "report.pdf"
    assert read_attachment(raw, 1) is None
    assert read_attachment(raw, -1) is None


def test_an_inline_image_is_marked_inline_with_its_content_id() -> None:
    raw = _mail(
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/related; boundary="R"',
        "",
        "--R",
        "Content-Type: text/html",
        "",
        '<img src="cid:logo">',
        "--R",
        "Content-Type: image/png",
        "Content-ID: <logo>",
        "Content-Transfer-Encoding: base64",
        "",
        "iVBORw0K",
        "--R--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.html.strip() == '<img src="cid:logo">'
    (image,) = parsed.attachments
    assert image.inline is True
    assert image.content_id == "logo"


def test_a_text_file_sent_as_an_attachment_is_not_mistaken_for_the_body() -> None:
    raw = _mail(
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: text/plain",
        'Content-Disposition: attachment; filename="notes.txt"',
        "",
        "these are notes",
        "--M",
        "Content-Type: text/plain",
        "",
        "the actual message",
        "--M--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.text.strip() == "the actual message"
    (attachment,) = parsed.attachments
    assert attachment.filename == "notes.txt"


def test_a_second_text_part_is_kept_as_an_attachment_not_dropped() -> None:
    """Nothing in a message is silently discarded. A body is the first text and
    the first HTML; anything further is still there to be downloaded.
    """
    raw = _mail(
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: text/plain",
        "",
        "first",
        "--M",
        "Content-Type: text/plain",
        "",
        "second",
        "--M--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.text.strip() == "first"
    (extra,) = parsed.attachments
    content = read_attachment(raw, extra.index)
    assert content is not None
    assert content.data.strip() == b"second"


def test_a_forwarded_message_is_one_attachment_and_its_body_is_not_ours() -> None:
    raw = _mail(
        "From: a@example.org",
        "Subject: Fwd",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: text/plain",
        "",
        "see below",
        "--M",
        "Content-Type: message/rfc822",
        "",
        "From: someone@else.example",
        "Subject: the original",
        "",
        "the forwarded body",
        "--M--",
        "",
    )

    parsed = parse_message(raw)

    assert parsed.text.strip() == "see below"
    (forwarded,) = parsed.attachments
    assert forwarded.content_type == "message/rfc822"
    assert forwarded.filename == "forwarded-message.eml"
    assert forwarded.size > 0


# --------------------------------------------------------------- threading ---


def test_threading_headers_are_read_without_their_brackets() -> None:
    raw = _mail(
        "From: a@example.org",
        "Message-ID: <reply-1@example.org>",
        "In-Reply-To: <first@example.com> <second@example.com>",
        "References: <root@example.com>",
        " <first@example.com>",
        "",
        "body",
    )

    parsed = parse_message(raw)

    assert parsed.message_id == "reply-1@example.org"
    # Some senders put several; the first is the one being answered.
    assert parsed.in_reply_to == "first@example.com"
    assert parsed.references == ["root@example.com", "first@example.com"]


# ------------------------------------------------------------- encodings ---


def test_encoded_words_in_the_subject_and_name_are_decoded() -> None:
    raw = _mail(
        "From: =?UTF-8?B?w4lsw6g=?= <eleve@example.org>",
        "Subject: =?ISO-8859-1?Q?caf=E9_cr=E8me?=",
        "",
        "x",
    )

    parsed = parse_message(raw)

    assert parsed.subject == "café crème"
    assert parsed.from_name == "Élè"


def test_a_body_in_latin1_is_decoded_by_its_declared_charset() -> None:
    raw = (
        b"From: a@example.org\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"caf\xe9\r\n"
    )

    assert parse_message(raw).text.strip() == "café"


def test_a_charset_nobody_has_heard_of_still_gives_text() -> None:
    raw = (
        b"From: a@example.org\r\n"
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"plain ascii survives\r\n"
    )

    parsed = parse_message(raw)

    assert "plain ascii survives" in parsed.text
    assert not parsed.parse_failed


def test_bytes_that_are_not_in_the_declared_charset_are_substituted_not_fatal() -> None:
    """Declared UTF-8, actually not. The body decodes with substitution, so this
    cannot produce a lone surrogate - 8-bit *headers* can, and
    ``test_garbage_never_raises`` is what guards that. This pins that a
    mismatched body is readable at all.
    """
    raw = (
        b"From: a@example.org\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"broken \xff\xfe\xc3 bytes\r\n"
    )

    parsed = parse_message(raw)

    assert parsed.text.startswith("broken ")
    parsed.text.encode("utf-8")  # raises on a lone surrogate
    assert not parsed.parse_failed


def test_nul_bytes_are_removed_because_postgres_cannot_store_them() -> None:
    raw = (
        b"From: a@example.org\r\n"
        b"Subject: nul\x00subject\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"body\x00with\x00nuls\r\n"
    )

    parsed = parse_message(raw)

    assert "\x00" not in parsed.subject
    assert "\x00" not in parsed.text
    assert "nulsubject" in parsed.subject or "nul" in parsed.subject
    assert "bodywithnuls" in parsed.text
    for name, value in parsed.headers:
        assert "\x00" not in name
        assert "\x00" not in value


# ------------------------------------------------------------ hostile input ---


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x01\x02\xff\xfe",
        b"this is not an email at all",
        b"\r\n\r\n\r\n",
        b"Content-Type: multipart/mixed\r\n\r\nno boundary at all",
        b"From: \xff\xfe@\x00\r\nSubject: \x80\r\n\r\nx",
    ],
)
def test_garbage_never_raises(raw: bytes) -> None:
    parsed = parse_message(raw)

    assert parsed.size == len(raw)
    # Whatever came back has to be storable.
    for value in (parsed.subject, parsed.text, parsed.html, parsed.from_address):
        value.encode("utf-8")
        assert "\x00" not in value


def test_missing_headers_give_empty_fields_not_errors() -> None:
    parsed = parse_message(b"\r\njust a body, no headers\r\n")

    assert parsed.from_address == ""
    assert parsed.subject == ""
    assert parsed.to == []
    assert parsed.message_id == ""
    assert parsed.sent_at is None
    assert "just a body" in parsed.text


def test_an_unparseable_date_is_none_not_an_error() -> None:
    raw = _mail("From: a@example.org", "Date: not a date at all", "", "x")

    assert parse_message(raw).sent_at is None


def test_an_address_header_with_no_real_addresses_gives_an_empty_list() -> None:
    raw = _mail("From: a@example.org", "To: undisclosed-recipients:;", "", "x")

    assert parse_message(raw).to == []


def test_a_multipart_nested_past_the_cap_is_bounded_and_flagged() -> None:
    depth = MAX_DEPTH + 10
    lines = ["From: a@example.org", "MIME-Version: 1.0"]
    for level in range(depth):
        lines += [f'Content-Type: multipart/mixed; boundary="b{level}"', "", f"--b{level}"]
    lines += ["Content-Type: text/plain", "", "deep"]
    for level in reversed(range(depth)):
        lines += [f"--b{level}--"]
    raw = _mail(*lines)

    parsed = parse_message(raw)

    assert parsed.truncated is True
    assert not parsed.parse_failed


def test_a_nesting_bomb_does_not_take_the_process_down() -> None:
    """Far past any recursion limit. The assertion is that this returns at all,
    with a result - raising here would leave the message on the queue to be
    retried, and failed, for ever.
    """
    depth = 3000
    lines = ["From: a@example.org", "MIME-Version: 1.0"]
    for level in range(depth):
        lines += [f'Content-Type: multipart/mixed; boundary="b{level}"', "", f"--b{level}"]
    raw = _mail(*lines, "Content-Type: text/plain", "", "x")

    parsed = parse_message(raw)

    assert parsed.size == len(raw)


def test_a_message_of_many_parts_is_capped() -> None:
    parts = [
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
    ]
    for number in range(MAX_PARTS + 50):
        parts += [
            "--M",
            "Content-Type: application/octet-stream",
            f'Content-Disposition: attachment; filename="f{number}.bin"',
            "",
            "x",
        ]
    parts += ["--M--", ""]

    parsed = parse_message(_mail(*parts))

    assert len(parsed.attachments) == MAX_PARTS
    assert parsed.truncated is True


def test_a_huge_body_is_cut_and_flagged() -> None:
    raw = _mail("From: a@example.org", "", "x" * (MAX_BODY_CHARS + 500))

    parsed = parse_message(raw)

    assert len(parsed.text) == MAX_BODY_CHARS
    assert parsed.truncated is True


def test_headers_are_capped_unfolded_and_raw() -> None:
    folded = _mail(
        "From: a@example.org",
        "Subject: a very long",
        " folded subject",
        "",
        "x",
    )
    assert ("Subject", "a very long folded subject") in parse_message(folded).headers

    many = _mail(
        "From: a@example.org",
        *[f"X-Spam-{n}: value" for n in range(MAX_HEADERS + 20)],
        "",
        "x",
    )
    parsed = parse_message(many)
    assert len(parsed.headers) == MAX_HEADERS
    assert parsed.truncated is True


def test_a_subject_with_line_breaks_is_one_line() -> None:
    raw = _mail("From: a@example.org", "Subject: first", " second\tthird", "", "x")

    assert "\n" not in parse_message(raw).subject
    assert parse_message(raw).subject == "first second third"


# ---------------------------------------------------------------- filenames ---


def _with_disposition(parameter: str) -> bytes:
    return _mail(
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: application/octet-stream",
        f"Content-Disposition: attachment; {parameter}",
        "",
        "data",
        "--M--",
        "",
    )


@pytest.mark.parametrize(
    ("parameter", "expected"),
    [
        ('filename="../../etc/passwd"', "passwd"),
        ('filename="/absolute/path/file.txt"', "file.txt"),
        ('filename=".."', "attachment"),
        ('filename=".hidden"', "hidden"),
        ('filename=""', "attachment"),
        # A backslash path through RFC 2231, which is how one actually survives
        # to be decoded: inside a plain quoted string a backslash is an escape
        # character and is consumed, leaving no separator at all.
        ("filename*=UTF-8''..%5C..%5Cwindows%5Csystem32%5Ccmd.exe", "cmd.exe"),
        ("filename*=UTF-8''..%2F..%2Fetc%2Fshadow", "shadow"),
        # Control characters in a name would split a header line when echoed.
        ("filename*=UTF-8''bad%0D%0Aname.txt", "badname.txt"),
    ],
)
def test_filenames_are_reduced_to_a_safe_basename(parameter: str, expected: str) -> None:
    """The sender chose this string and a download will echo it back in a
    header, so a path has to arrive as a name.
    """
    (attachment,) = parse_message(_with_disposition(parameter)).attachments

    assert attachment.filename == expected


def test_a_backslash_in_a_quoted_filename_is_an_escape_and_leaves_no_separator() -> None:
    """Pinned because it looks like a bug and is not. RFC 2045 makes the
    backslash inside a quoted string an escape, so the separators are gone
    before this module sees the name - which is also why it is safe.
    """
    raw = _with_disposition('filename="..\\\\..\\\\windows\\\\cmd.exe"')

    (attachment,) = parse_message(raw).attachments

    assert "/" not in attachment.filename
    assert "\\" not in attachment.filename


def test_an_rfc2231_encoded_filename_is_decoded() -> None:
    raw = _mail(
        "From: a@example.org",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="M"',
        "",
        "--M",
        "Content-Type: application/octet-stream",
        "Content-Disposition: attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf",
        "",
        "data",
        "--M--",
        "",
    )

    (attachment,) = parse_message(raw).attachments

    assert attachment.filename == "résumé.pdf"


# --------------------------------------------------------------- round trip ---


def test_what_the_outbound_builder_writes_the_parser_reads_back() -> None:
    """The two modules agree. Not proof that either is right - both use the same
    library - but a drift between them would show here first.
    """
    outbound = OutboundEmail(
        sender="Sender <sender@example.com>",
        to=["to@example.org"],
        subject="Round trip é",
        text="plain body",
        html="<p>html body</p>",
        attachments=[Attachment(filename="a.bin", content=b"\x00\x01\x02binary\xff")],
    )
    raw = message_bytes(outbound)

    parsed = parse_message(raw)

    assert parsed.from_address == "sender@example.com"
    assert parsed.to == ["to@example.org"]
    assert parsed.subject == "Round trip é"
    assert parsed.text.strip() == "plain body"
    assert parsed.html.strip() == "<p>html body</p>"
    (attachment,) = parsed.attachments
    assert attachment.filename == "a.bin"
    content = read_attachment(raw, 0)
    assert content is not None
    # Byte for byte, including the NUL and the 0xff that are not text.
    assert content.data == b"\x00\x01\x02binary\xff"
    assert build_message(outbound) is not None

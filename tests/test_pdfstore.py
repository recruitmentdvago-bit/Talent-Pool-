from sourcer.pdfstore import (parse_confidence, parse_profile_pdf, pdf_to_text,
                              render_profile_pdf, sha256_file, store_pdf)


def test_render_then_reparse_recovers_the_profile(sandbox, strong_analyst):
    dest = sandbox / "out.pdf"
    render_profile_pdf(strong_analyst, dest)
    assert dest.exists() and dest.stat().st_size > 1000

    back = parse_profile_pdf(dest)
    assert back.full_name == "Ayesha Khan"
    assert "Karachi" in back.location_raw
    titles = {e.title for e in back.experiences}
    assert "Data Analyst" in titles and "MIS Officer" in titles
    companies = {e.company for e in back.experiences}
    assert "Habib Bank Limited" in companies and "Faysal Bank" in companies
    assert abs(back.total_years() - strong_analyst.total_years()) < 0.2
    assert {"sql", "power_bi", "excel"} <= back.skills()
    conf, issues = parse_confidence(back)
    assert conf >= 0.9, issues


def test_rendered_pdf_records_provenance(sandbox, strong_analyst):
    strong_analyst.source = "pdf_inbox"
    strong_analyst.source_ref = "ayesha.pdf"
    dest = sandbox / "p.pdf"
    render_profile_pdf(strong_analyst, dest)
    text = pdf_to_text(dest)
    assert "pdf_inbox" in text and "lawful basis" in text.lower()


def test_match_note_appears_when_given(sandbox, strong_analyst):
    dest = sandbox / "m.pdf"
    render_profile_pdf(strong_analyst, dest, score_note="DA-001 - 96/100")
    assert "96/100" in pdf_to_text(dest)


def test_store_pdf_renders_when_no_source_file(sandbox, strong_analyst):
    path, sha, reused = store_pdf(strong_analyst)
    assert path.exists() and len(sha) == 64 and reused is False
    assert path.suffix == ".pdf"


def test_store_pdf_does_not_keep_two_copies_of_identical_content(sandbox,
                                                                 strong_analyst):
    src = sandbox / "src.pdf"
    render_profile_pdf(strong_analyst, src)
    p1, sha1, reused1 = store_pdf(strong_analyst, src=src)
    p2, sha2, reused2 = store_pdf(strong_analyst, src=src)
    assert sha1 == sha2
    assert reused1 is False and reused2 is True
    assert p1 == p2


def test_confidence_flags_an_unparseable_profile(sandbox):
    from sourcer.models import Candidate
    empty = Candidate(full_name="X")
    conf, issues = parse_confidence(empty)
    assert conf < 0.4
    assert "no experience parsed" in issues


def test_sha256_is_stable(sandbox, strong_analyst):
    p = sandbox / "a.pdf"
    render_profile_pdf(strong_analyst, p)
    assert sha256_file(p) == sha256_file(p)


def test_special_characters_do_not_break_rendering(sandbox):
    from sourcer.models import Candidate, Experience
    c = Candidate(full_name="A & B <Test>", headline="R&D / Analytics > Reporting",
                  location_raw="Karachi, Pakistan",
                  experiences=[Experience(title="Analyst <Senior>", company="A & B Co",
                                          start_date="Jan 2020", is_current=True,
                                          description="Used <tags> & ampersands")])
    dest = sandbox / "esc.pdf"
    render_profile_pdf(c, dest)
    text = pdf_to_text(dest)
    assert "A & B" in text and "<Test>" in text


def test_pdf_inbox_source_files_and_quarantines(sandbox, strong_analyst):
    from sourcer.sources.base import FetchContext
    from sourcer.sources.pdf_inbox import PdfInboxSource
    inbox = sandbox / "storage/pdf_inbox"
    render_profile_pdf(strong_analyst, inbox / "good.pdf")
    (inbox / "junk.pdf").write_bytes(b"%PDF-1.4 not really a pdf")

    src = PdfInboxSource()
    assert src.available()[0] is True
    res = src.fetch(FetchContext(max_results=10))
    assert len(res.candidates) == 1
    assert res.candidates[0].full_name == "Ayesha Khan"
    assert res.errors
    assert (inbox / "processed" / "good.pdf").exists()
    assert not (inbox / "good.pdf").exists()


def test_a_corrupt_pdf_is_quarantined_so_it_does_not_retry_forever(sandbox):
    """A file that cannot be parsed must leave the inbox, or every future run
    fails on it again and the daily log fills up."""
    from sourcer.sources.base import FetchContext
    from sourcer.sources.pdf_inbox import PdfInboxSource
    inbox = sandbox / "storage/pdf_inbox"
    (inbox / "broken.pdf").write_bytes(b"%PDF-1.4 definitely not a pdf")

    src = PdfInboxSource()
    first = src.fetch(FetchContext(max_results=10))
    assert first.errors and first.candidates == []
    assert not (inbox / "broken.pdf").exists()
    assert (inbox / "needs_review" / "broken.pdf").exists()
    assert (inbox / "needs_review" / "broken.pdf.reason.txt").read_text().strip()

    second = src.fetch(FetchContext(max_results=10))
    assert second.errors == []


def test_a_thin_profile_is_quarantined_with_its_reason(sandbox):
    from reportlab.pdfgen import canvas

    from sourcer.sources.base import FetchContext
    from sourcer.sources.pdf_inbox import PdfInboxSource
    inbox = sandbox / "storage/pdf_inbox"
    c = canvas.Canvas(str(inbox / "thin.pdf"))
    c.drawString(72, 720, "Page 1 of 1")
    c.save()

    res = PdfInboxSource().fetch(FetchContext(max_results=10))
    assert res.candidates == []
    assert (inbox / "needs_review" / "thin.pdf").exists()
    assert "confidence" in (inbox / "needs_review" / "thin.pdf.reason.txt").read_text()


def test_dry_run_leaves_a_broken_pdf_where_it_is(sandbox):
    from sourcer.sources.base import FetchContext
    from sourcer.sources.pdf_inbox import PdfInboxSource
    inbox = sandbox / "storage/pdf_inbox"
    (inbox / "broken.pdf").write_bytes(b"not a pdf")
    PdfInboxSource().fetch(FetchContext(max_results=10, dry_run=True))
    assert (inbox / "broken.pdf").exists()

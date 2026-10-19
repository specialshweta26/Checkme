from app.agent.attachments import is_previewable


def test_previewable_pdf_and_images():
    assert is_previewable("ARW635-funds.pdf", "application/pdf")
    assert is_previewable("shot.png", "image/png")
    assert not is_previewable("archive.zip", "application/zip")

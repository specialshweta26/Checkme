from app.agent.attachments import _kind, is_previewable


def test_previewable_pdf_and_images():
    assert is_previewable("ARW635-funds.pdf", "application/pdf")
    assert is_previewable("shot.png", "image/png")
    assert is_previewable("daily-sales.html", "text/html")
    assert not is_previewable("archive.zip", "application/zip")
    assert _kind("daily-sales.html", "text/html") == "html"
    assert _kind("chart.png", "image/png") == "image"

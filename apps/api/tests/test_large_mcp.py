from app.connectors.sanitize import infer_group


def test_groups_cover_130_tools():
    names = []
    for i in range(40):
        names.append(f"search_messages_{i}")
        names.append(f"delete_item_{i}")
        names.append(f"download_file_{i}")
    names += [f"calendar_event_{i}" for i in range(10)]
    assert len(names) == 130
    groups = {infer_group(n) for n in names}
    assert "read" in groups
    assert "destructive" in groups
    assert "attachments" in groups
    assert "calendar" in groups

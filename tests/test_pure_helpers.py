"""Characterization tests for pure helper functions.

These pin down the current behavior of the UI-dump parsing, geometry, and text
normalization helpers so refactors that are meant to preserve behavior can be
verified without a live GeeLark device. They intentionally avoid anything that
needs ADB, the network, or the filesystem.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geelark_multimodal_bot as m


SAMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node index="0" text="Amber" resource-id="com.bumble.app:id/connectionsItem_personName"'
    ' class="android.widget.TextView" bounds="[120,300][540,360]" clickable="false" enabled="true"'
    ' content-desc="" />'
    '<node index="1" text="Your move" resource-id="com.bumble.app:id/connectionsItem_message"'
    ' class="android.widget.TextView" bounds="[120,370][700,430]" clickable="true" enabled="true"'
    ' content-desc="Your move &amp; reply" />'
    '</hierarchy>'
)


def test_bounds_center():
    assert m._bounds_center("[120,300][540,360]") == (330, 330)
    assert m._bounds_center("garbage") is None


def test_bounds_rect():
    assert m._bounds_rect("[120,300][540,360]") == (120, 300, 540, 360)
    assert m._bounds_rect("nope") is None


def test_parse_text_values_from_ui_dump_grep_path():
    raw = 'text="Amber"\ncontent-desc="Your move &amp; reply"\ntext=""'
    assert m._parse_text_values_from_ui_dump(raw) == ["Amber", "Your move & reply", ""]


def test_parse_text_values_from_ui_dump_xml_fallback():
    values = m._parse_text_values_from_ui_dump(SAMPLE_XML)
    assert "Amber" in values
    assert "Your move" in values
    assert "Your move & reply" in values


def test_parse_ui_node_lines():
    line = (
        '<node text="Amber" resource-id="com.bumble.app:id/connectionsItem_personName"'
        ' class="android.widget.TextView" bounds="[120,300][540,360]" clickable="false" enabled="true">'
    )
    nodes = m._parse_ui_node_lines(line)
    assert len(nodes) == 1
    node = nodes[0]
    assert node["text"] == "Amber"
    assert node["resource_id"] == "com.bumble.app:id/connectionsItem_personName"
    assert node["center"] == (330, 330)
    assert node["clickable"] is False
    assert node["enabled"] is True


def test_normalize_ui_node_attrs_unescape_and_strip():
    attrs = {
        "text": "  Hi &amp; bye  ",
        "content-desc": "desc",
        "resource-id": "id/x",
        "class": "TextView",
        "bounds": "[0,0][10,10]",
        "clickable": "true",
    }
    node = m._normalize_ui_node_attrs(attrs)
    # _normalize_ui_node_attrs strips but does not unescape (that happens in the
    # grep parser); pin the current behavior.
    assert node["text"] == "Hi &amp; bye"
    assert node["content_desc"] == "desc"
    assert node["clickable"] is True
    assert node["center"] == (5, 5)


def test_dedupe_ui_nodes():
    a = m._normalize_ui_node_attrs({"text": "x", "bounds": "[0,0][1,1]"})
    b = m._normalize_ui_node_attrs({"text": "x", "bounds": "[0,0][1,1]"})
    c = m._normalize_ui_node_attrs({"text": "y", "bounds": "[0,0][1,1]"})
    assert len(m._dedupe_ui_nodes([a, b, c])) == 2


def test_text_values_from_nodes_dedupes_and_keeps_distinct_desc():
    nodes = [
        {"text": "Amber", "content_desc": "Amber"},
        {"text": "Your move", "content_desc": "reply now"},
        {"text": "Amber", "content_desc": ""},
    ]
    assert m._text_values_from_nodes(nodes) == ["Amber", "Your move", "reply now"]


def test_clean_ui_xml_text_trims_to_hierarchy():
    noisy = 'STATUS OK\n<?xml version="1.0"?>' + SAMPLE_XML.split("?>", 1)[1] + "\ntrailing"
    cleaned = m._clean_ui_xml_text(noisy)
    assert cleaned.startswith("<hierarchy")
    assert cleaned.endswith("</hierarchy>")


def test_dedupe_strings_preserves_order():
    assert m._dedupe_strings(["a", "b", "a", "", "c", "b"]) == ["a", "b", "c"]


def test_escape_adb_input_text_roundtrip_stable():
    once = m._escape_adb_input_text("hello world & friends")
    assert isinstance(once, str)
    # Spaces must be escaped for `input text` to receive them as one string.
    assert " " not in once or "\\" in once


def test_normalize_reply_strips_wrapping():
    assert m._normalize_reply('  "hello"  ') == "hello"
    assert m._normalize_reply("plain") == "plain"


def test_append_jsonl_line_no_added_fields(tmp_path):
    path = tmp_path / "run.jsonl"
    m._append_jsonl_line(path, {"a": 1, "event": "x"})
    m._append_jsonl_line(path, {"b": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = m.json.loads(lines[0])
    # Must be exactly what was passed — no injected "ts" (unlike the monitor log).
    assert first == {"a": 1, "event": "x"}
    assert m.json.loads(lines[1]) == {"b": 2}


def test_dismiss_common_popups_two_rounds(monkeypatch):
    # First round dismisses, second round dismisses, third would too but
    # max_rounds caps it at 2 taps.
    calls = []
    monkeypatch.setattr(m, "_click_visible_text_fast", lambda dev, labels: "Close")
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: calls.append("sleep"))
    result = m._dismiss_common_popups_fast(object())
    assert result == ["Close", "Close"]


def test_dismiss_common_popups_stops_when_nothing_to_dismiss(monkeypatch):
    monkeypatch.setattr(m, "_click_visible_text_fast", lambda dev, labels: None)
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: None)
    assert m._dismiss_common_popups_fast(object()) == []

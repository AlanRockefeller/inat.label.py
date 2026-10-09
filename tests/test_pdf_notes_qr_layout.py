import binascii
import re
from io import BytesIO

import pytest
from PIL import Image


def _label(notes_marker, notes):
    fields = [
        ("Scientific Name", "Amanita muscaria"),
        ("Location", "Marin County, California"),
        ("iNaturalist URL", "https://www.inaturalist.org/observations/123456789"),
    ]
    if notes_marker:
        fields.append(("Notes", notes))
    return [(fields, "Fungi")]


def _capture_standard_pdf_story(
    inat_module, monkeypatch, tmp_path, *, notes_marker, notes, no_qr=False
):
    story = []
    monkeypatch.setattr(
        inat_module.BaseDocTemplate,
        "build",
        lambda _self, flowables: story.extend(flowables),
    )
    inat_module.create_pdf_content(
        _label(notes_marker, notes),
        str(tmp_path / "notes-qr.pdf"),
        no_qr=no_qr,
    )
    return story[0]._content


def _notes_qr_table(inat_module, flowables):
    return next(flowable for flowable in flowables if isinstance(flowable, inat_module.Table))


def _quiet_zone_scale(inat_module):
    _, qr_size = inat_module.generate_qr_code(
        "https://www.inaturalist.org/observations/123456789"
    )
    return inat_module.qr_quiet_zone_scale(qr_size, inat_module.STANDARD_QR_BOX_SIZE)


def _cell_flowable(table, column):
    value = table._cellvalues[0][column]
    return value[0] if isinstance(value, tuple) else value


@pytest.mark.parametrize(
    "notes",
    [
        "Short field note.",
        "First observation line.\nSecond observation line near the QR code.",
        " ".join(["Long notes must remain inside their own text column."] * 8),
    ],
    ids=["short", "multi-line", "long"],
)
def test_pdf_notes_and_qr_have_explicit_columns_and_gutter(
    inat_module, monkeypatch, tmp_path, notes
):
    flowables = _capture_standard_pdf_story(
        inat_module,
        monkeypatch,
        tmp_path,
        notes_marker=True,
        notes=notes,
    )
    table = _notes_qr_table(inat_module, flowables)
    frame_width = ((8.5 - 0.5) * inat_module.inch - 0.5 * inat_module.inch) / 2

    # The Notes column is a star column so the table fills whatever width it is
    # wrapped at, including the widened width used by KeepInFrame's shrink mode.
    assert table._declared_colWidths == ["*", pytest.approx(inat_module.PDF_QR_COLUMN_WIDTH)]
    assert table._cellStyles[0][0].rightPadding == inat_module.PDF_NOTES_QR_GUTTER
    assert table._cellStyles[0][1].leftPadding == 0

    notes_cell = _cell_flowable(table, 0)
    qr_image = _cell_flowable(table, 1)
    assert table.wrap(frame_width, 10000)[0] == pytest.approx(frame_width)
    assert notes_cell.width == pytest.approx(
        frame_width - inat_module.PDF_QR_COLUMN_WIDTH - inat_module.PDF_NOTES_QR_GUTTER
    )
    assert notes_cell.style.fontSize == pytest.approx(12)

    expected_qr_size = inat_module.PDF_QR_RENDER_SIZE * _quiet_zone_scale(inat_module)
    assert qr_image.drawWidth == pytest.approx(expected_qr_size)
    assert qr_image.drawHeight == pytest.approx(expected_qr_size)


def test_table_expands_when_wrapped_wider_than_the_frame(
    inat_module, monkeypatch, tmp_path
):
    """KeepInFrame(mode='shrink') re-wraps at a wider width before scaling down."""
    flowables = _capture_standard_pdf_story(
        inat_module,
        monkeypatch,
        tmp_path,
        notes_marker=True,
        notes="Short field note.",
    )
    table = _notes_qr_table(inat_module, flowables)
    frame_width = ((8.5 - 0.5) * inat_module.inch - 0.5 * inat_module.inch) / 2

    # The table has already been wrapped once while estimating the label height.
    assert table.wrap(frame_width, 10000)[0] == pytest.approx(frame_width)
    assert table.wrap(frame_width * 2.14, 10000)[0] == pytest.approx(frame_width * 2.14)


def test_stack_order_label_keeps_qr_at_the_right_edge(
    inat_module, monkeypatch, tmp_path
):
    """A shrunk stack-order label still spans its full slot width."""
    story = []
    monkeypatch.setattr(
        inat_module.BaseDocTemplate,
        "build",
        lambda _self, flowables: story.extend(flowables),
    )
    inat_module.create_pdf_content(
        _label(True, "Short field note."),
        str(tmp_path / "stack-order.pdf"),
        stack_order_num_per_page=16,
    )

    slot = story[0]
    slot_width, slot_height = slot._argW[0], slot._argH[0]
    keep_in_frame = _cell_flowable(slot, 0)
    keep_in_frame.canv = None
    keep_in_frame.wrap(slot_width, slot_height)
    table = _notes_qr_table(inat_module, keep_in_frame._content)

    scale = keep_in_frame._scale
    assert scale > 1  # the label really was shrunk to fit its slot
    assert table._width / scale == pytest.approx(slot_width, rel=1e-3)


def test_pdf_without_notes_preserves_qr_only_layout(inat_module, monkeypatch, tmp_path):
    flowables = _capture_standard_pdf_story(
        inat_module,
        monkeypatch,
        tmp_path,
        notes_marker=False,
        notes="",
    )
    table = _notes_qr_table(inat_module, flowables)

    assert table._declared_colWidths[1] == pytest.approx(inat_module.PDF_QR_COLUMN_WIDTH)
    assert _cell_flowable(table, 0).getPlainText() == ""
    assert _cell_flowable(table, 1).drawWidth == pytest.approx(
        inat_module.PDF_QR_RENDER_SIZE * _quiet_zone_scale(inat_module)
    )


def test_pdf_with_qr_disabled_keeps_notes_as_normal_text(inat_module, monkeypatch, tmp_path):
    flowables = _capture_standard_pdf_story(
        inat_module,
        monkeypatch,
        tmp_path,
        notes_marker=True,
        notes="Notes remain visible when QR output is disabled.",
        no_qr=True,
    )

    assert not any(isinstance(flowable, inat_module.Table) for flowable in flowables)
    notes_paragraph = next(
        flowable
        for flowable in flowables
        if isinstance(flowable, inat_module.Paragraph)
        and flowable.getPlainText().startswith("Notes:")
    )
    assert notes_paragraph.style.fontSize == pytest.approx(12)


def test_pdf_with_extremely_long_notes_builds_without_layout_error(inat_module, tmp_path):
    output = tmp_path / "extremely-long-notes.pdf"
    notes = " ".join(["Extremely long notes still stay bounded in the text column."] * 60)

    inat_module.create_pdf_content(
        _label(True, notes),
        str(output),
    )

    assert output.read_bytes().startswith(b"%PDF-")


def test_pdf_with_extremely_long_notes_keeps_notes_readable(
    inat_module, monkeypatch, tmp_path
):
    """Notes too long for one frame flow full width instead of being shrunk."""
    notes = " ".join(["Extremely long notes still stay bounded in the text column."] * 60)
    flowables = _capture_standard_pdf_story(
        inat_module,
        monkeypatch,
        tmp_path,
        notes_marker=True,
        notes=notes,
    )

    # No single-row Notes/QR table, which could neither split nor stay legible.
    assert not any(isinstance(flowable, inat_module.Table) for flowable in flowables)
    notes_paragraph = next(
        flowable
        for flowable in flowables
        if isinstance(flowable, inat_module.Paragraph)
        and flowable.getPlainText().startswith("Notes:")
    )
    assert notes_paragraph.style.fontSize == pytest.approx(12)

    qr_image = next(
        flowable
        for flowable in flowables
        if isinstance(flowable, inat_module.ReportLabImage)
    )
    assert flowables.index(qr_image) > flowables.index(notes_paragraph)
    assert qr_image.hAlign == "RIGHT"


def test_generated_qr_has_four_module_quiet_zone(inat_module):
    qr_hex, _ = inat_module.generate_qr_code("https://example.org/observations/123")
    assert qr_hex is not None

    with Image.open(BytesIO(binascii.unhexlify(qr_hex))) as image:
        grayscale = image.convert("L")
        black_pixels = [
            (x, y)
            for y in range(grayscale.height)
            for x in range(grayscale.width)
            if grayscale.getpixel((x, y)) == 0
        ]

    assert min(x for x, _ in black_pixels) == (
        inat_module.QR_BORDER_MODULES * inat_module.STANDARD_QR_BOX_SIZE
    )
    assert min(y for _, y in black_pixels) == (
        inat_module.QR_BORDER_MODULES * inat_module.STANDARD_QR_BOX_SIZE
    )


def test_standard_rtf_qr_declares_the_real_bitmap_dimensions(inat_module):
    label = _label(True, "RTF sizing check")
    rtf = inat_module.create_rtf_content(label)
    pic = re.search(r"\\picw(\d+)\\pich(\d+)\\picwgoal(\d+)\\pichgoal(\d+)", rtf)

    assert pic is not None
    qr_hex, qr_size = inat_module.generate_qr_code(
        "https://www.inaturalist.org/observations/123456789"
    )
    assert qr_hex is not None
    assert qr_size is not None
    # \picw/\pich describe the source bitmap, quiet zone included, so readers
    # that derive scaling from them do not crop or stretch the QR code.
    expected = (qr_size[0] * 15, qr_size[1] * 15)
    assert tuple(map(int, pic.groups())) == expected + expected


def test_quiet_zone_scale_preserves_module_size(inat_module):
    _, qr_size = inat_module.generate_qr_code(
        "https://www.inaturalist.org/observations/123456789"
    )
    assert qr_size is not None
    scale = inat_module.qr_quiet_zone_scale(qr_size, inat_module.STANDARD_QR_BOX_SIZE)

    added = (
        2
        * (inat_module.QR_BORDER_MODULES - inat_module.QR_LEGACY_BORDER_MODULES)
        * inat_module.STANDARD_QR_BOX_SIZE
    )
    legacy_pixels = qr_size[0] - added
    # A module printed at the scaled footprint is the same size as before.
    assert (
        inat_module.PDF_QR_RENDER_SIZE * scale / qr_size[0]
        == pytest.approx(inat_module.PDF_QR_RENDER_SIZE / legacy_pixels)
    )
    assert inat_module.qr_quiet_zone_scale(None, 2) == 1.0
    assert inat_module.qr_quiet_zone_scale((0, 0), 2) == 1.0
    assert inat_module.qr_quiet_zone_scale((4, 4), 2) == 1.0

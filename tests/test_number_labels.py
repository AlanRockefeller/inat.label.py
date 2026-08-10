from types import SimpleNamespace


def _item(index, identifier, voucher=None):
    fields = [("ID", identifier), ("Scientific Name", f"Species {identifier}")]
    if voucher is not None:
        fields.append(("Voucher Number", voucher))
    return (index, (fields, "Fungi"), None)


def _args(**overrides):
    values = {
        "sort": "none",
        "title": None,
        "sort_field": None,
        "number_labels": False,
        "minilabel": False,
        "stack_order": False,
        "num_per_page": 6,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _value(tagged_label, field_name):
    fields, _ = tagged_label
    return next((value for field, value in fields if field == field_name), None)


def test_number_labels_cli_flag_defaults_off(inat_module):
    parser = inat_module.build_arg_parser()

    assert parser.parse_args([]).number_labels is False
    assert parser.parse_args(["--number-labels"]).number_labels is True


def test_numbers_are_assigned_after_non_default_sort(inat_module):
    items = [
        _item(0, "ten", "Plot 10"),
        _item(1, "two", "Plot 2"),
        _item(2, "one", "Area 1"),
    ]

    result, original_count = inat_module._sort_and_stack_labels(
        _args(sort="voucher", number_labels=True), items
    )

    assert original_count == 3
    assert [_value(label, "ID") for label in result] == ["one", "two", "ten"]
    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "2",
        "3",
    ]
    assert [label[0][0] for label in result] == [
        (inat_module.LABEL_NUMBER_FIELD, "1"),
        (inat_module.LABEL_NUMBER_FIELD, "2"),
        (inat_module.LABEL_NUMBER_FIELD, "3"),
    ]


def test_duplicate_observation_copies_share_number(inat_module):
    items = [_item(0, "A"), _item(1, "A"), _item(2, "B")]

    result, _ = inat_module._sort_and_stack_labels(_args(number_labels=True), items)

    assert [_value(label, "ID") for label in result] == ["A", "A", "B"]
    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "1",
        "2",
    ]


def test_duplicate_identity_uses_url_then_observation_number_then_fields(inat_module):
    items = [
        (
            0,
            ([("iNaturalist URL", "77"), ("ID", "url first copy")], "Fungi"),
            None,
        ),
        (
            1,
            ([("iNaturalist URL", "77"), ("ID", "url second copy")], "Fungi"),
            None,
        ),
        (
            2,
            (
                [("iNaturalist Observation Number", "77"), ("ID", "obs first copy")],
                "Fungi",
            ),
            None,
        ),
        (
            3,
            (
                [("iNaturalist Observation Number", "77"), ("ID", "obs second copy")],
                "Fungi",
            ),
            None,
        ),
        _item(4, "fallback"),
        _item(5, "fallback"),
    ]

    result, _ = inat_module._sort_and_stack_labels(_args(number_labels=True), items)

    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "1",
        "2",
        "2",
        "3",
        "3",
    ]


def test_empty_identity_values_fall_back_to_complete_fields(inat_module):
    items = [
        (
            0,
            ([("iNaturalist URL", ""), ("ID", "first")], "Fungi"),
            None,
        ),
        (
            1,
            ([("iNaturalist URL", ""), ("ID", "second")], "Fungi"),
            None,
        ),
        (
            2,
            ([("iNaturalist Observation Number", ""), ("ID", "third")], "Fungi"),
            None,
        ),
    ]

    result, _ = inat_module._sort_and_stack_labels(_args(number_labels=True), items)

    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "2",
        "3",
    ]


def test_stack_order_moves_each_label_with_its_preassigned_number(inat_module):
    items = [_item(index, str(index + 1)) for index in range(6)]

    result, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True, stack_order=True), items
    )

    assert [_value(label, "ID") for label in result] == ["1", "3", "5", "2", "4", "6"]
    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "3",
        "5",
        "2",
        "4",
        "6",
    ]


def test_stack_order_padding_repeats_final_labels_existing_number(inat_module):
    items = [_item(index, str(index + 1)) for index in range(5)]

    result, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True, stack_order=True), items
    )

    assert [_value(label, "ID") for label in result] == ["1", "3", "5", "2", "4", "5"]
    assert [_value(label, inat_module.LABEL_NUMBER_FIELD) for label in result] == [
        "1",
        "3",
        "5",
        "2",
        "4",
        "5",
    ]


def test_numbering_off_and_minilabel_mode_do_not_modify_labels(inat_module, capsys):
    items = [_item(0, "first"), _item(1, "second")]

    unnumbered, _ = inat_module._sort_and_stack_labels(_args(), items)
    minilabels, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True, minilabel=True), items
    )

    expected = [item[1] for item in items]
    assert unnumbered == expected
    assert minilabels == expected
    assert all(
        inat_module.LABEL_NUMBER_FIELD not in {field for field, _ in fields}
        for fields, _ in unnumbered + minilabels
    )

    inat_module.render_plaintext_labels(expected)
    existing_output = capsys.readouterr().out
    inat_module.render_plaintext_labels(unnumbered)
    assert capsys.readouterr().out == existing_output


def test_standard_and_fungus_fair_rtf_render_number_without_marker(inat_module):
    standard, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True),
        [
            (
                0,
                (
                    [
                        ("Scientific Name", "Amanita muscaria"),
                        ("Location", "Marin County, CA"),
                    ],
                    "Fungi",
                ),
                None,
            )
        ],
    )
    fungus_fair = [
        (
            [
                (inat_module.LABEL_NUMBER_FIELD, "2"),
                ("Scientific Name", "Amanita muscaria"),
            ],
            "Fungi",
        )
    ]

    standard_rtf = inat_module.create_rtf_content(standard, no_qr=True)
    fungus_fair_rtf = inat_module.create_rtf_content(fungus_fair, no_qr=True, fungus_fair_mode=True)

    assert r"{\b 1}\line " in standard_rtf
    assert standard_rtf.index(r"{\b 1}\line ") < standard_rtf.index("Scientific Name")
    assert inat_module.LABEL_NUMBER_FIELD not in standard_rtf
    assert r"\ql\keepn\sa0 \sb0 {\b 2}\par \pard\keep\keepn\qc" in fungus_fair_rtf
    assert inat_module.LABEL_NUMBER_FIELD not in fungus_fair_rtf


def test_pdf_flowable_text_starts_with_number_above_title(inat_module, monkeypatch, tmp_path):
    captured_story = []
    monkeypatch.setattr(
        inat_module.BaseDocTemplate,
        "build",
        lambda _self, story: captured_story.extend(story),
    )
    labels, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True),
        [
            (
                0,
                (
                    [
                        ("Scientific Name", "Amanita muscaria"),
                        ("Location", "Marin County, CA"),
                    ],
                    "Fungi",
                ),
                None,
            )
        ],
    )

    inat_module.create_pdf_content(
        labels,
        str(tmp_path / "numbered.pdf"),
        no_qr=True,
        title_field="Scientific Name",
    )

    label_flowables = captured_story[0]._content
    paragraphs = [
        flowable for flowable in label_flowables if isinstance(flowable, inat_module.Paragraph)
    ]
    pdf_text = "\n".join(paragraph.getPlainText() for paragraph in paragraphs)
    assert [paragraph.getPlainText() for paragraph in paragraphs[:2]] == [
        "1",
        "Amanita muscaria",
    ]
    assert paragraphs[0].style.alignment == 0
    assert paragraphs[0].style.fontSize == paragraphs[-1].style.fontSize
    assert inat_module.LABEL_NUMBER_FIELD not in pdf_text


def test_plaintext_and_minilabel_renderers_never_leak_marker(inat_module, monkeypatch, capsys):
    numbered, _ = inat_module._sort_and_stack_labels(
        _args(number_labels=True),
        [
            (
                0,
                (
                    [
                        ("iNaturalist Observation Number", "123"),
                        ("iNaturalist URL", "https://www.inaturalist.org/observations/123"),
                    ],
                    "Fungi",
                ),
                None,
            )
        ],
    )

    inat_module.render_plaintext_labels(numbered)
    plaintext = capsys.readouterr().out
    assert plaintext.splitlines()[0] == "1"
    assert inat_module.LABEL_NUMBER_FIELD not in plaintext

    monkeypatch.setattr(inat_module, "generate_qr_code", lambda *_args, **_kwargs: ("00", (1, 1)))
    minilabel_rtf = inat_module.create_minilabel_rtf_content(numbered)
    assert "iNat" in minilabel_rtf
    assert "123" in minilabel_rtf
    assert inat_module.LABEL_NUMBER_FIELD not in minilabel_rtf


def test_reserved_marker_is_inert_in_field_scanning_helpers(inat_module):
    fields = [
        (inat_module.LABEL_NUMBER_FIELD, "é"),
        ("Voucher Number", "Plot 7"),
        ("iNaturalist Observation Number", "123"),
        ("iNaturalist URL", "https://www.inaturalist.org/observations/123"),
    ]
    labels = [(fields, "Fungi")]

    assert inat_module.label_get(fields, "Voucher Number") == "Plot 7"
    assert inat_module.get_voucher_value(fields) == "Plot 7"
    assert inat_module._minilabel_obs_number(fields) == "123"
    assert inat_module._minilabel_source_abbr(fields) == "iNat"
    assert inat_module._minilabel_qr_url(fields).endswith("/123")
    assert inat_module.find_non_ascii_chars(labels) == set()
    assert inat_module._select_pdf_font(labels) == inat_module._select_pdf_font(
        [
            (
                [
                    (field, value)
                    for field, value in fields
                    if field != inat_module.LABEL_NUMBER_FIELD
                ],
                "Fungi",
            )
        ]
    )

import copy
import re
import sys
import textwrap
import warnings

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _ofv(name, value):
    return {'name': name, 'value': value}


def _full_inat_observation():
    return {
        'id': 183905751,
        'taxon': {
            'id': 123,
            'name': 'Amanita muscaria flavivolvata',
            'rank': 'variety',
            'preferred_common_name': 'western fly agaric',
            'iconic_taxon_name': 'Fungi',
        },
        'taxon_details': {
            'ancestors': [
                {'rank': 'genus', 'name': 'Amanita'},
                {'rank': 'species', 'name': 'Amanita muscaria'},
            ]
        },
        'place_guess': '12345 Old Logging Road, Mendocino County, California, United States',
        'geojson': {'coordinates': [-123.456789, 38.987654]},
        'positional_accuracy': 12,
        'observed_on_string': '2025-11-14 03:25 PM PST',
        'user': {'name': 'Alan Rockefeller', 'login': 'alan_rockefeller'},
        'description': (
            '<p>Growing under <strong>coast live oak</strong>; KOH '
            '<em>yellow</em>. See '
            '<a href="https://example.org/specimen/AR-2025-014">specimen page</a>.</p>'
        ),
        'ofvs': [
            _ofv('DNA Barcode ITS', 'ACGT ACGT\nACGT ACGT'),
            _ofv('DNA Barcode LSU', 'A C G T'),
            _ofv('GenBank Accession', 'OR123456'),
            _ofv('Provisional Species Name', 'Amanita aff. muscaria AR-2025'),
            _ofv('Microscopy Performed', 'Basidia 4-spored; clamps present'),
            _ofv('Fungal Microscopy', 'Spores amyloid, 9-11 x 6-8 um'),
            _ofv('Mobile or Traditional Photography?', 'traditional camera'),
            _ofv("Collector's name", 'A. Rockefeller'),
            _ofv('Herbarium Catalog Number', 'UC 2050010'),
            _ofv('Fungarium Catalog Number', 'SFSU-F-1001'),
            _ofv('Herbarium Secondary Catalog Number', 'AR-2025-014-A'),
            _ofv('Habitat', 'mixed hardwood forest with Douglas-fir'),
            _ofv('Microhabitat', 'fruiting from duff beside rotting log'),
            _ofv('Collection Number', 'AR-2025-014'),
            _ofv('Collection #', 'MO-771'),
            _ofv('Associated Species', 'Quercus agrifolia; Pseudotsuga menziesii'),
            _ofv('Herbarium Name', 'University Herbarium, UC Berkeley'),
            _ofv('Mycoportal ID', 'MYCO-987654'),
            _ofv('Voucher Number', 'AR 2025-014'),
            _ofv('Voucher Number(s)', 'AR 2025-014; UC 2050010'),
            _ofv('Accession Number', 'ACC-2025-014'),
            _ofv(
                'Mushroom Observer URL',
                'https://www.mushroomobserver.org/observer/show_observation/123456?foo=bar',
            ),
            _ofv('Fungusworld', 'FW-2025-014'),
        ],
    }


def _mushroom_observer_observation():
    return {
        'id': 'MO505283',
        'taxon': {'name': 'Psilocybe cyanescens', 'preferred_common_name': ''},
        'place_guess': 'Seattle, King County, Washington, USA',
        'geojson': {'coordinates': [-122.3331, 47.6097]},
        'positional_accuracy': None,
        'observed_on_string': '2024-12-03',
        'user': {'name': '', 'login': 'mo_user'},
        'description': 'Clustered on alder chips. Imported by Mushroom Observer 2024-12-05',
        'ofvs': [
            _ofv('Mushroom Observer URL', 'https://mushroomobserver.org/obs/505283'),
            _ofv('DNA Barcode ITS', '603 bp'),
            _ofv('Collection #', 'MOUSER 42'),
            _ofv('Herbarium Name', 'University of Washington Fungarium'),
            _ofv('Herbarium Catalog Number', 'WTU-F-55555'),
        ],
    }


def _label_dict(label_fields):
    return dict(label_fields)


def _assert_user_visible_text(actual, expected):
    actual = actual.strip()
    expected = textwrap.dedent(expected).strip()
    if actual == expected:
        return
    if _normalize_spacing(actual) == _normalize_spacing(expected):
        warnings.warn(
            'Only user-visible spacing changed in label output; review whether it is OK.',
            UserWarning,
            stacklevel=2,
        )
        return
    assert actual == expected


def _normalize_spacing(text):
    return '\n'.join(' '.join(line.split()) for line in text.strip().splitlines())


def _strip_runtime_lines(stdout):
    text = ANSI_RE.sub('', stdout).replace('\r\n', '\n')
    lines = []
    for line in text.splitlines():
        if line.startswith('Added label for ') or line.startswith('Summary: '):
            continue
        lines.append(line.rstrip())
    return '\n'.join(lines).strip()


def test_real_world_inaturalist_label_includes_user_visible_fields(inat_module):
    label, taxon = inat_module.create_inaturalist_label(
        copy.deepcopy(_full_inat_observation()),
        'Fungi',
        show_common_names=True,
    )

    assert taxon == 'Fungi'
    assert label == [
        (
            'Scientific Name',
            '__ITALIC_START__Amanita muscaria__ITALIC_END__ var. '
            '__ITALIC_START__flavivolvata__ITALIC_END__',
        ),
        ('Common Name', 'western fly agaric'),
        ('iNaturalist Observation Number', '183905751'),
        ('iNaturalist URL', 'https://www.inaturalist.org/observations/183905751'),
        ('Location', 'Mendocino County, California, USA'),
        ('Coordinates', '38.98765, -123.45679 (\u00b112m)'),
        ('Date Observed', '2025-11-14'),
        ('Observer', 'Alan Rockefeller (alan_rockefeller)'),
        ('DNA Barcode ITS', '16 bp'),
        ('DNA Barcode LSU', '4 bp'),
        ('GenBank Accession Number', 'OR123456'),
        ('Provisional Species Name', 'Amanita aff. muscaria AR-2025'),
        ('Microscopy Performed', 'Basidia 4-spored; clamps present'),
        ('Fungal Microscopy', 'Spores amyloid, 9-11 x 6-8 um'),
        ('Mobile or Traditional Photography', 'traditional camera'),
        ("Collector's name", 'A. Rockefeller'),
        ('Herbarium Catalog Number', 'UC 2050010'),
        ('Fungarium Catalog Number', 'SFSU-F-1001'),
        ('Herbarium Secondary Catalog Number', 'AR-2025-014-A'),
        ('Habitat', 'mixed hardwood forest with Douglas-fir'),
        ('Microhabitat', 'fruiting from duff beside rotting log'),
        ('Collection #', 'MO MO-771; iNat AR-2025-014'),
        ('Associated Species', 'Quercus agrifolia; Pseudotsuga menziesii'),
        ('Herbarium Name', 'University Herbarium, UC Berkeley'),
        ('Mycoportal ID', 'MYCO-987654'),
        ('Voucher Number', 'AR 2025-014'),
        ('Voucher Number(s)', 'AR 2025-014; UC 2050010'),
        ('Accession Number', 'ACC-2025-014'),
        ('Mushroom Observer URL', 'https://mushroomobserver.org/obs/123456'),
        (
            'Notes',
            'Growing under __BOLD_START__coast live oak__BOLD_END__; KOH '
            '__ITALIC_START__yellow__ITALIC_END__. See specimen page '
            '(https://example.org/specimen/AR-2025-014).',
        ),
    ]


def test_custom_fields_species_override_and_omit_notes_are_visible(inat_module):
    observation = _full_inat_observation()
    observation['ofvs'].append(_ofv('Species Name Override', 'Amanita sp. AR-2025-014'))

    label, _taxon = inat_module.create_inaturalist_label(
        observation,
        'Fungi',
        show_common_names=True,
        omit_notes=True,
        custom_add=['Fungusworld'],
        custom_remove=['Coordinates', 'Observer'],
    )
    fields = _label_dict(label)

    assert fields['Scientific Name'] == '__ITALIC_START__Amanita sp. AR-2025-014__ITALIC_END__'
    assert fields['Fungusworld'] == 'FW-2025-014'
    assert 'Coordinates' not in fields
    assert 'Observer' not in fields
    assert 'Notes' not in fields


def test_private_and_obscured_coordinates_match_user_expectations(inat_module):
    private_obs = _full_inat_observation()
    private_obs['geoprivacy'] = 'private'

    private_label, _taxon = inat_module.create_inaturalist_label(private_obs, 'Fungi')
    private_fields = _label_dict(private_label)

    assert 'Location' not in private_fields
    assert private_fields['Coordinates'] == 'private'

    obscured_obs = _full_inat_observation()
    obscured_obs['id'] = 183905752
    obscured_obs['geoprivacy'] = 'obscured'
    obscured_obs['obscured'] = True
    obscured_obs['positional_accuracy'] = 50
    obscured_obs['geojson'] = {'coordinates': [-122.56789, 38.12345]}

    obscured_label, _taxon = inat_module.create_inaturalist_label(obscured_obs, 'Fungi')
    obscured_fields = _label_dict(obscured_label)

    assert obscured_fields['Location'] == 'Mendocino County, California, USA'
    assert obscured_fields['Coordinates'] == '38.12, -122.57 (\u00b128km)'


def test_scientific_name_rank_formatting_for_common_real_world_cases(inat_module):
    cases = [
        (
            {
                'taxon': {'name': 'Morchella americana', 'rank': 'species'},
            },
            '__ITALIC_START__Morchella americana__ITALIC_END__',
        ),
        (
            {
                'taxon': {'name': 'Armillaria mellea', 'rank': 'complex'},
            },
            '__ITALIC_START__Armillaria mellea__ITALIC_END__ complex',
        ),
        (
            {
                'taxon': {'name': 'Telamonia', 'rank': 'subgenus'},
                'taxon_details': {'ancestors': [{'rank': 'genus', 'name': 'Cortinarius'}]},
            },
            '__ITALIC_START__Cortinarius__ITALIC_END__ subg. __ITALIC_START__Telamonia__ITALIC_END__',
        ),
        (
            {
                'taxon': {'name': 'Amanita muscaria flavivolvata', 'rank': 'variety'},
                'taxon_details': {
                    'ancestors': [{'rank': 'species', 'name': 'Amanita muscaria'}]
                },
            },
            '__ITALIC_START__Amanita muscaria__ITALIC_END__ var. __ITALIC_START__flavivolvata__ITALIC_END__',
        ),
    ]

    for observation_data, expected in cases:
        assert inat_module.format_scientific_name(observation_data) == expected


def test_cli_plaintext_real_world_labels_warns_only_on_spacing_drift(
    inat_module, monkeypatch, capsys
):
    observations = {
        '183905751': (_full_inat_observation(), 'Fungi'),
        'MO505283': (_mushroom_observer_observation(), 'Fungi'),
    }

    def fake_get_observation_data(observation_id):
        return copy.deepcopy(observations[str(observation_id)])

    monkeypatch.setattr(inat_module, 'get_observation_data', fake_get_observation_data)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'inat.label.py',
            '183905751',
            'MO505283',
            '--common-names',
            '--workers',
            '1',
        ],
    )

    inat_module.main()
    captured = capsys.readouterr()

    assert captured.err == ''
    _assert_user_visible_text(
        _strip_runtime_lines(captured.out),
        """
        Scientific Name: Psilocybe cyanescens
        Mushroom Observer Number: 505283
        https://mushroomobserver.org/obs/505283
        Location: Seattle, King County, Washington, USA
        Coordinates: 47.60970, -122.33310
        Date Observed: 2024-12-03
        Observer: mo_user
        DNA Barcode ITS: 603 bp
        Herbarium Catalog Number: WTU-F-55555
        Collection #: MOUSER 42
        Herbarium Name: University of Washington Fungarium
        Notes: Clustered on alder chips.


        Scientific Name: Amanita muscaria var. flavivolvata
        Common Name: western fly agaric
        iNaturalist Observation Number: 183905751
        https://www.inaturalist.org/observations/183905751
        Location: Mendocino County, California, USA
        Coordinates: 38.98765, -123.45679 (\u00b112m)
        Date Observed: 2025-11-14
        Observer: Alan Rockefeller (alan_rockefeller)
        DNA Barcode ITS: 16 bp
        DNA Barcode LSU: 4 bp
        GenBank Accession Number: OR123456
        Provisional Species Name: Amanita aff. muscaria AR-2025
        Microscopy Performed: Basidia 4-spored; clamps present
        Fungal Microscopy: Spores amyloid, 9-11 x 6-8 um
        Mobile or Traditional Photography: traditional camera
        Collector's name: A. Rockefeller
        Herbarium Catalog Number: UC 2050010
        Fungarium Catalog Number: SFSU-F-1001
        Herbarium Secondary Catalog Number: AR-2025-014-A
        Habitat: mixed hardwood forest with Douglas-fir
        Microhabitat: fruiting from duff beside rotting log
        Collection #: MO MO-771; iNat AR-2025-014
        Associated Species: Quercus agrifolia; Pseudotsuga menziesii
        Herbarium Name: University Herbarium, UC Berkeley
        Mycoportal ID: MYCO-987654
        Voucher Number: AR 2025-014
        Voucher Number(s): AR 2025-014; UC 2050010
        Accession Number: ACC-2025-014
        https://mushroomobserver.org/obs/123456
        Notes: Growing under coast live oak; KOH yellow. See specimen page (https://example.org/specimen/AR-2025-014).
        """,
    )


def test_rtf_output_preserves_formatting_markers_as_rtf(inat_module):
    label = inat_module.create_inaturalist_label(
        copy.deepcopy(_full_inat_observation()),
        'Fungi',
        show_common_names=True,
    )

    rtf = inat_module.create_rtf_content([label], no_qr=True)

    assert r'{\scaps\ul\b Scientific Name:} {\i Amanita muscaria} var. {\i flavivolvata}\line ' in rtf
    assert r'{\scaps\ul\b Coordinates:} 38.98765, -123.45679 (\u177?12m)\line ' in rtf
    assert r'{\b coast live oak}' in rtf
    assert r'{\i yellow}' in rtf
    assert 'https://mushroomobserver.org/obs/123456' in rtf
    assert '__ITALIC_START__' not in rtf
    assert '__BOLD_START__' not in rtf


def test_fungus_fair_csv_stdout_and_warnings_are_user_visible(
    inat_module, monkeypatch, capsys, tmp_path
):
    csv_file = tmp_path / 'fair.csv'
    csv_file.write_text(
        textwrap.dedent(
            """\
            Scientific Name,Common Name,Habitat,Spore Print,Edibility
            Armillaria mellea,honey mushroom,on buried wood,white,edible
            Chlorophyllum molybdites,green-spored parasol,lawns,green,toxic
            Mystery cup,,duff,,maybe
            ,missing name,wood chips,brown,inedible
            """
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(sys, 'argv', ['inat.label.py', '--fungusfair', str(csv_file)])

    inat_module.main()
    captured = capsys.readouterr()

    assert "Warning on line 4: Invalid edibility value 'maybe'. Defaulting to 'unknown'" in captured.err
    assert "Error on line 5: Missing required value for 'Scientific Name'." in captured.err
    _assert_user_visible_text(
        _strip_runtime_lines(captured.out),
        """
        Scientific Name: Armillaria mellea
        Common Name: honey mushroom
        Habitat: on buried wood
        Spore Print: white
        Edibility: Edible


        Scientific Name: Chlorophyllum molybdites
        Common Name: green-spored parasol
        Habitat: lawns
        Spore Print: green
        Edibility: Poisonous


        Scientific Name: Mystery cup
        Habitat: duff
        Edibility: Unknown
        """,
    )

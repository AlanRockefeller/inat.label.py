def test_normalize_edibility_maps_synonyms(inat_module):
    assert inat_module.normalize_edibility('edible') == 'edible'
    assert inat_module.normalize_edibility('inedible') == 'nonedible'
    assert inat_module.normalize_edibility('toxic') == 'poisonous'
    assert inat_module.normalize_edibility('Poisonous!') == 'poisonous'


def test_normalize_edibility_handles_missing_or_unknown(inat_module):
    assert inat_module.normalize_edibility(None) is None
    assert inat_module.normalize_edibility('') is None
    assert inat_module.normalize_edibility('maybe') is None

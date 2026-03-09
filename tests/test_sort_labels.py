def _item(index, fields, taxon='Fungi'): 
    return (index, (fields, taxon)) 
 
def _value(tagged_label, field_name): 
    label_fields, _ = tagged_label 
    return next((v for f, v in label_fields if f == field_name), None) 
 
def test_sort_labels_none_preserves_index_order(inat_module): 
    items = [ 
        _item(2, [('iNaturalist Observation Number', '20')]), 
        _item(0, [('iNaturalist Observation Number', '10')]), 
        _item(1, [('iNaturalist Observation Number', '30')]), 
    ] 
 
    result = inat_module.sort_labels(items, 'none') 
    assert [_value(x, 'iNaturalist Observation Number') for x in result] == ['10', '30', '20'] 
 
def test_sort_labels_default_uses_numeric_observation_number(inat_module): 
    items = [ 
        _item(0, [('iNaturalist Observation Number', '10')]), 
        _item(1, [('iNaturalist Observation Number', '2')]), 
        _item(2, [('Mushroom Observer Number', '5')]), 
    ] 
 
    result = inat_module.sort_labels(items, None) 
    assert [_value(x, 'iNaturalist Observation Number') or _value(x, 'Mushroom Observer Number') for x in result] == [ 
        '2', 
        '5', 
        '10', 
    ] 
 
def test_sort_labels_voucher_sorts_alpha_then_trailing_number(inat_module): 
    items = [ 
        _item(0, [('Voucher Number', 'Plot 10')]), 
        _item(1, [('Voucher Number', 'Plot 2')]), 
        _item(2, [('Voucher Number', 'Area 1')]), 
        _item(3, [('Scientific Name', 'No Voucher')]), 
    ] 
 
    result = inat_module.sort_labels(items, 'voucher') 
    assert [_value(x, 'Voucher Number') for x in result] == ['Area 1', 'Plot 2', 'Plot 10', None] 
 
def test_sort_labels_custom_uses_requested_field(inat_module): 
    items = [ 
        _item(0, [('Collection Number', 'B 9')]), 
        _item(1, [('Collection Number', 'A 12')]), 
        _item(2, [('Collection Number', 'A 2')]), 
    ] 
 
    result = inat_module.sort_labels(items, 'custom', sort_field_name='Collection Number') 
    assert [_value(x, 'Collection Number') for x in result] == ['A 2', 'A 12', 'B 9']

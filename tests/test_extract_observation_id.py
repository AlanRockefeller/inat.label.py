def test_extract_observation_id_supports_numeric_and_urls(inat_module):
    assert inat_module.extract_observation_id('183905751') == '183905751'
    assert (
        inat_module.extract_observation_id('https://www.inaturalist.org/observations/106191917')
        == '106191917'
    )
    assert inat_module.extract_observation_id('MO505283') == 'MO505283'
    assert (
        inat_module.extract_observation_id('https://mushroomobserver.org/obs/585855?foo=bar')
        == 'MO585855'
    )


def test_extract_observation_id_rejects_invalid_values(inat_module):
    assert inat_module.extract_observation_id('not-an-observation') is None
    assert inat_module.extract_observation_id('mo12345') is None

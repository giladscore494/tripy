"""Regression coverage for the implemented subset of original PR36 Part B."""
import pytest
from src.fields import resolve_requested_fields, sanity_specs
from src.candidate_harvest import harvest_text, collect_candidate_rejections
from src.ui.live_state import candidate_table_rows, format_value_unit


def values(text, field, *, segment=None, gross=None, propulsion='battery_electric'):
    specs = resolve_requested_fields(None, propulsion=propulsion)
    specs = sanity_specs(specs, payload={'identity': {'segment': segment}, 'structure': {'gross_weight_kg': gross}})
    return [c['value'] for c in harvest_text(text, specs) if c['field'] == field]


@pytest.mark.parametrize('text', ['Length x Width x Height: 4750 x 1920 x 1650 mm',
                                  'L x W x H: 4750 x 1920 x 1650 mm',
                                  'אורך/רוחב/גובה: 4750/1920/1650 מ"מ'])
def test_ordered_dimension_triples(text):
    assert values(text, 'length_mm') == [4750]
    assert values(text, 'width_mm') == [1920]
    assert values(text, 'height_mm') == [1650]


def test_dimension_siblings_cannot_claim_each_others_numbers():
    text = 'Width: 1920 mm Height: 1650 mm Wheelbase: 2890 mm'
    assert values(text, 'width_mm') == [1920]
    assert values(text, 'height_mm') == [1650]
    assert values(text, 'wheelbase_mm') == [2890]


def test_tyres_are_contiguous_and_unlabelled_multiple_sizes_do_not_pick_axles():
    text = 'Tyres: 255/45R20, 235/60R18'
    assert values(text, 'tire_size_front') == []
    assert values(text, 'tire_size_rear') == []
    assert values('Front tyres 255/45R20; rear tyres 235/60R18', 'tire_size_front') == ['255/45 R20']
    assert values('Tyres 235/60R18', 'tire_size_rear') == ['235/60 R18']


@pytest.mark.parametrize('segment,gross,expected', [('private', 2200, []), ('commercial', 3500, []),
                                                   ('commercial', 7500, [17.5]), (None, None, [])])
def test_half_inches_need_recorded_heavy_commercial_context(segment, gross, expected):
    assert values('Rim diameter: 17.5 in', 'rim_diameter_in', segment=segment, gross=gross) == expected
    assert values('Rim diameter: 20 in', 'rim_diameter_in', segment=segment, gross=gross) == [20]


def test_price_minimum_uses_private_segment_and_unknown_keeps_global_bounds():
    assert values('List price: 6000 ILS', 'list_price', segment='private') == []
    assert values('List price: 6000 ILS', 'list_price') == [6000]
    assert values('List price: 200000 ILS', 'list_price', segment='private') == [200000]


def test_bev_torque_minimum_is_scoped_and_kgfm_is_converted():
    assert values('Torque: 67.3 Nm', 'torque_nm') == []
    assert values('Torque: 67.3 Nm', 'torque_nm', propulsion='conventional') == [67.3]
    # PR #44: kgf·m converts to whole Nm (67.3 x 9.80665 = 659.99 -> 660), as torque is published
    assert values('Torque: 67.3 kgf·m', 'torque_nm') == [660]


@pytest.mark.parametrize('field,bad,good', [
    ('screen_size_in', 'Driver display: 10.2 in', 'Center display: 14.96 in'),
    ('curb_weight_kg', 'Curb weight braked towing: 750 kg', 'משקל עצמי: 2100 ק"ג'),
    ('dc_max_charging_power_kw', 'DC charging V2L: 5 kW', 'DC charging power: 451 kW'),
    ('ac_max_charging_power_kw', 'AC charging discharge: 5 kW', 'AC charging power: 11 kW'),
    ('cargo_volume_l', 'Cargo volume seats folded: 1374 l', 'Cargo volume: 571 l'),
    ('vehicle_warranty', 'Battery warranty: 8 years / 160000 km', 'Vehicle warranty: 5 years / 100000 km'),
    ('local_trim_name', 'Trim: P7+ G6 G9 X9', 'Trim: Premium'),
    ('list_price', 'List price deposit: 6000 ILS', 'List price: 200000 ILS'),
])
def test_semantic_exclusion_does_not_remove_normal_phrasing(field, bad, good):
    assert values(bad, field) == []
    assert values(good, field)


def test_towing_sibling_phrase_does_not_remove_curb_weight():
    assert values('Curb weight: 2100 kg towing: 750 kg', 'curb_weight_kg') == [2100]


def test_rejected_cargo_keeps_its_reason_at_origin():
    with collect_candidate_rejections() as rejected:
        assert values('Cargo volume seats folded: 1374 l', 'cargo_volume_l') == []
    assert any(r['field'] == 'cargo_volume_l' and r['value'] == 1374 and 'seats-folded' in r['rejection']
               for r in rejected)


def test_unit_formatter_never_duplicates_charging_time_unit():
    assert format_value_unit('12 min', 'min') == '12 min'
    assert format_value_unit(12, 'min') == '12 min'


def test_candidate_table_separates_live_candidates_from_rejections_and_keeps_provenance():
    specs = resolve_requested_fields(['cargo_volume_l'], propulsion='battery_electric')
    events = [
        {'kind': 'candidates_harvested', 'document_id': 'doc-live', 'candidates': [
            {'field': 'cargo_volume_l', 'value': 571, 'unit': 'l', 'origin': 'table',
             'document_id': 'doc-live', 'block': 'table:0:row:2'}]},
        {'kind': 'candidate_rejected', 'field': 'cargo_volume_l', 'value': 1374,
         'rejection': 'seats-folded capacity, not standard cargo volume', 'origin': 'line',
         'document_id': 'doc-rejected', 'block': 'Cargo volume seats folded: 1374 l'},
    ]
    row = candidate_table_rows(events, specs)[0]
    assert row['מועמדים שנמצאו'] == '571 l' and '1374' not in row['מועמדים שנמצאו']
    assert 'table · doc-live · table:0:row:2' in row['origin']
    assert '1374: seats-folded' in row['rejection']

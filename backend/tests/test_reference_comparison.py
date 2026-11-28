import pytest
from app.reference_comparison import assessments, compare


@pytest.mark.parametrize(('value', 'reference', 'expected'), [
    ('12', '12 - 15', 'within'), ('15', '12–15', 'within'),
    ('11.99', '12 to 15', 'below'), ('15.01', '12—15', 'above'),
    ('5.4', '<5.7', 'within'), ('5.7', '<5.7', 'above'),
    ('5.7', '≤5.7', 'within'), ('5.7', '>5.7', 'below'),
    ('<148', '187 - 883', 'below'), ('<148', '148 - 883', 'below'),
    ('<=148', '148 - 883', 'unable_to_determine'), ('>20', '10 - 20', 'above'),
    ('<6', '5 - 10', 'unable_to_determine'), ('9.1', 'Deficient <20', 'unable_to_determine'),
    ('1,000', '0 - 2000', 'unable_to_determine'), ('1.54 H*', '<1', 'unable_to_determine'),
    ('negative', 'negative', 'unable_to_determine'), ('1e3', '0 - 1000', 'unable_to_determine'),
    ('4', '5 - 1', 'unable_to_determine'), ('4', None, 'unable_to_determine'),
    ('-2', '-3 - -1', 'within'),
])
def test_compare_numeric_intervals_conservatively(value, reference, expected):
    assert compare(value, reference) == expected


def test_conflicting_reported_flag_or_cross_page_source_is_not_classified():
    def field(value, page=1):
        return {'value':value, 'origin':'source', 'evidence':{'page':page, 'location_type':'page', 'layer':'primary'}}
    row = {'test_name':field('synthetic'), 'observed_value':field('5'), 'reference_range':field('1 - 10')}
    assert assessments({'laboratory_tests':[row]}) == ['within']
    assert assessments({'laboratory_tests':[{**row, 'observed_value':field('<148 L*'), 'reference_range':field('187 - 883')}]}) == ['below']
    assert assessments({'laboratory_tests':[{**row, 'observed_value':field('1.54 H*'), 'reference_range':field('<1.00')}]}) == ['above']
    assert assessments({'laboratory_tests':[{**row, 'observed_value':field('1.54 L*'), 'reference_range':field('<1.00')}]}) == ['unable_to_determine']
    assert assessments({'laboratory_tests':[{**row, 'abnormality_flag':field('H*')}]}) == ['unable_to_determine']
    assert assessments({'laboratory_tests':[{**row, 'reference_range':field('1 - 10', page=2)}]}) == ['unable_to_determine']

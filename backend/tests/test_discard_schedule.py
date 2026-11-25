from app.document_purger import discard_check_delay


def test_discard_checks_back_off_without_stopping_and_retry_errors_sooner():
    assert discard_check_delay(1000, 1000, 'absent') == 300
    assert discard_check_delay(1000, 4600, 'absent') == 900
    assert discard_check_delay(1000, 1000+86400, 'removed') == 3600
    assert discard_check_delay(1000, 1000+7*86400, 'absent') == 21600
    assert discard_check_delay(1000, 1000+7*86400, 'error') == 3600
    assert discard_check_delay(1000, 900, 'absent') == 300

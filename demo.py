from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from candidate.assemble import prepare_candidate
from candidate.replace import replace_candidate
from candidate.requests import initialize_replacement_store
from candidate.store import initialize_candidate_store
from inspection.delivery import redeliver_receipt
from inspection.receipts import initialize_check_receipts, inspect_candidate, load_check_receipt
from inspection.standards import CheckStandard, initialize_check_standards, register_check_standard, seed_check_standards
from publication.admission import admit_replacement, deliver_receipt, initialize_publication
from publication.ledger import open_publication
from receipt.lookup import read_report
from receipt.manifest import package_digest
from receipt.package import export_report
from receipt.verify import verify_package
from source.calibrations import load_calibration, register_calibration
from source.database import initialize_station, open_station
from source.registry import StationRegistry
from source.revisions import register_revision


STATION = 'QC-07'
DAY = '2026-10-14'
CHANNEL = 'displacement'


def initialize_source(db):
    initialize_station(db)
    initialize_candidate_store(db)
    initialize_replacement_store(db)
    initialize_check_standards(db)
    initialize_check_receipts(db)


def seed_demo(db):
    registry = StationRegistry(db)
    registry.register_station(STATION, '岸桥检测站', only_missing=True)
    channel = registry.register_channel(STATION, CHANNEL, 'mV', 'mm', only_missing=True)
    for calibration_id, offset in (('C1', '1/4'), ('C2', '1/4'), ('C3', '0')):
        register_calibration(db, STATION, CHANNEL, calibration_id, '1/2', offset, only_missing=True)
    seed_check_standards(db, STATION, CHANNEL, channel.report_unit)


def trial_samples(span=3):
    return [('reading', CHANNEL, 3), ('zero-trial', CHANNEL, 0), ('gap', CHANNEL, None),
            ('mid', CHANNEL, 2), ('span-trial', CHANNEL, span), ('repeat', CHANNEL, 3)]


def run_demo():
    with TemporaryDirectory(prefix='quay-handover-') as directory:
        root = Path(directory)
        with closing(open_publication(root / 'publication.sqlite')) as publication:
            initialize_publication(publication)
            with closing(open_station(root / 'station.sqlite')) as station:
                initialize_source(station)
                seed_demo(station)
                for number, calibration in enumerate(('C1', 'C1', 'C2')):
                    previous = f'D{number - 1}' if number else None
                    register_revision(station, f'D{number}', STATION, DAY, trial_samples(),
                                      {CHANNEL: calibration}, corrects=previous)
                    prepare_candidate(station, f'D{number}', f'R{number}')
                    for kind in ('zero', 'span'):
                        inspect_candidate(station, f'R{number}', kind, f'R{number}-{kind}')
                send = partial(redeliver_receipt, station, port=partial(deliver_receipt, publication))
                select = partial(replace_candidate, station, STATION, DAY,
                                 port=partial(admit_replacement, publication))
                send('R0-span')
                initial = send('R0-zero').issue
                select('R1', 'select-R1')
                send('R1-zero')
                select('R2', 'select-R2')
                retired = send('R1-span')
                incomplete = send('R2-zero')
                interim = read_report(publication, station=STATION, day=DAY)
                if retired.status != 'retired' or incomplete.status != 'waiting' or interim != initial:
                    raise RuntimeError('DAILY_REPORT_WRONG_REVISION')
                signed = send('R2-span').issue
                if signed is None or send('R2-span').issue != signed:
                    raise RuntimeError('R2 issue did not converge')
            current = read_report(publication, station=STATION, day=DAY)
            history = read_report(publication, issue_id='issue:R0')
            package = export_report(publication, 'issue:R2')
            actual = [current.receipt.issue_id, history.receipt.issue_id, verify_package(package)]
            expected = ['issue:R2', 'issue:R0', True]
            expected_values = ['7/4', '1/4', None]
            actual_values = [None if row.value is None else str(row.value) for row in current.report.rows[:3]]
            if actual != expected or actual_values != expected_values:
                raise RuntimeError('unexpected handover values or provenance')
            evidence = {
                'case_id': 'late_receipts',
                'input': {'station': STATION, 'day': DAY, 'order': ['R1-zero', 'select-R2', 'R1-span', 'R2-zero', 'R2-span']},
                'expected': ['retired', 'waiting', 'issue:R0'],
                'actual': [retired.status, incomplete.status, interim.receipt.issue_id],
                'downstream_expected': {'issues_and_package': expected, 'first_values': expected_values},
                'downstream_actual': {'issues_and_package': actual, 'first_values': actual_values},
                'code_paths': ['candidate/replace.py', 'publication/admission.py', 'publication/issue.py', 'receipt/lookup.py', 'receipt/verify.py'],
                'oracle': 'R2 only becomes current after both R2 checks. Manually applying 1/2 × raw + 1/4 to 3 and 0 gives 7/4 and 1/4. Missing stays missing.',
            }
            print('POINTWISE_SCENARIO ' + json.dumps(evidence, ensure_ascii=False, sort_keys=True))


def load_paired_span_cases():
    path = Path(__file__).resolve().with_name('paired_span_cases.json')
    return json.loads(path.read_text(encoding='utf-8'))


def _reference_error(spec):
    return None if spec is None else tuple(spec)


def run_paired_span_demo():
    cases = load_paired_span_cases()
    high_channel = cases['high_channel']
    low_channel = cases['low_channel']
    high_trial = (high_channel, 'span-trial')
    low_trial = (low_channel, 'span-low')
    samples = [tuple(row) for row in cases['samples']]
    with TemporaryDirectory(prefix='quay-paired-span-') as directory:
        root = Path(directory)
        publication_path = root / 'publication.sqlite'
        with closing(open_station(root / 'station.sqlite')) as station, \
                closing(open_publication(publication_path)) as publication:
            initialize_source(station)
            initialize_publication(publication)
            seed_demo(station)
            registry = StationRegistry(station)
            registry.register_channel(STATION, low_channel, 'mV', 'mm', only_missing=True)
            report_unit = registry.require_channel(STATION, high_channel).report_unit
            span_standard = CheckStandard('span', *high_trial, report_unit, cases['standard_reference'],
                                          *cases['allowed'], low_trial=low_trial)
            register_check_standard(station, STATION, span_standard)
            verdicts = {}
            previous_revision = None
            for case in cases['cases']:
                case_id = case['id']
                for channel, suffix, key in ((high_channel, 'high', 'high_error'), (low_channel, 'low', 'low_error')):
                    register_calibration(station, STATION, channel, f'PS-{case_id}-{suffix}',
                                         cases['scale'], cases['offset'],
                                         reference_error=_reference_error(case[key]))
                revision_id = f'day-PS-{case_id}'
                register_revision(station, revision_id, STATION, DAY, samples,
                                  {high_channel: f'PS-{case_id}-high', low_channel: f'PS-{case_id}-low'},
                                  corrects=previous_revision)
                previous_revision = revision_id
                candidate = prepare_candidate(station, revision_id, f'PS-{case_id}')
                zero = inspect_candidate(station, candidate.candidate_id, 'zero', f'PS-{case_id}-zero')
                span = inspect_candidate(station, candidate.candidate_id, 'span', f'PS-{case_id}-span')
                interval = None if span.finding.interval is None else [str(v) for v in span.finding.interval]
                verdicts[case_id] = {'interval': interval, 'passed': span.passed}
                positions = (span.finding.position, span.finding.low_position)
                expected_positions = (cases['expected_positions']['high'], cases['expected_positions']['low'])
                if (not zero.passed or interval != case['interval'] or span.passed != case['passed']
                        or positions != expected_positions or str(span.finding.value) != cases['expected_nominal']):
                    raise RuntimeError('PAIRED_SPAN_VERDICT_MISMATCH')
            send = partial(redeliver_receipt, station, port=partial(deliver_receipt, publication))
            select = partial(replace_candidate, station, STATION, DAY,
                             port=partial(admit_replacement, publication))
            if send('PS-shared-zero').status != 'waiting':
                raise RuntimeError('PAIRED_SPAN_DELIVERY_STATE_WRONG')
            shared_issue = send('PS-shared-span').issue
            if shared_issue is None or shared_issue.receipt.issue_id != 'issue:PS-shared':
                raise RuntimeError('PAIRED_SPAN_SHARED_NOT_ISSUED')
            if select('PS-independent', 'select-PS-independent').previous_candidate_id != 'PS-shared':
                raise RuntimeError('PAIRED_SPAN_SELECTION_WRONG')
            independent_span = load_check_receipt(station, 'PS-independent-span')
            swapped = replace(independent_span, finding=replace(independent_span.finding, low_position=1))
            try:
                deliver_receipt(publication, swapped)
                raise RuntimeError('PAIRED_SPAN_SWAPPED_POSITION_ACCEPTED')
            except ValueError:
                pass
            if send('PS-independent-zero').status != 'waiting':
                raise RuntimeError('PAIRED_SPAN_DELIVERY_STATE_WRONG')
            if send('PS-independent-span').status != 'waiting':
                raise RuntimeError('PAIRED_SPAN_DELIVERY_STATE_WRONG')
            if read_report(publication, station=STATION, day=DAY) != shared_issue:
                raise RuntimeError('PAIRED_SPAN_CURRENT_CHANGED')
            failure_receipts = {}
            failure_samples = {
                'missing-value': [('span-low', low_channel, None), ('zero-trial', high_channel, 0),
                                  ('span-trial', high_channel, 3)],
                'missing-sample': [('zero-trial', high_channel, 0), ('span-trial', high_channel, 3)],
            }
            for name, case_samples in failure_samples.items():
                register_calibration(station, STATION, high_channel, f'PS-fail-{name}-high',
                                     cases['scale'], cases['offset'])
                register_calibration(station, STATION, low_channel, f'PS-fail-{name}-low',
                                     cases['scale'], cases['offset'])
                revision_id = f'day-PS-fail-{name}'
                register_revision(station, revision_id, STATION, DAY, case_samples,
                                  {high_channel: f'PS-fail-{name}-high', low_channel: f'PS-fail-{name}-low'},
                                  corrects=previous_revision)
                previous_revision = revision_id
                prepare_candidate(station, revision_id, f'PS-fail-{name}')
                failure_receipts[name] = inspect_candidate(station, f'PS-fail-{name}', 'span',
                                                           f'PS-fail-{name}-span')
            raw_channel = 'low-raw'
            registry.register_channel(STATION, raw_channel, 'mV', 'mV', only_missing=True)
            register_calibration(station, STATION, raw_channel, 'PS-fail-wrong-unit-low', 1, 0)
            register_calibration(station, STATION, high_channel, 'PS-fail-wrong-unit-high',
                                 cases['scale'], cases['offset'])
            register_revision(station, 'day-PS-fail-wrong-unit', STATION, DAY,
                              [('span-low', raw_channel, 0), ('zero-trial', high_channel, 0),
                               ('span-trial', high_channel, 3)],
                              {high_channel: 'PS-fail-wrong-unit-high', raw_channel: 'PS-fail-wrong-unit-low'},
                              corrects=previous_revision)
            previous_revision = 'day-PS-fail-wrong-unit'
            prepare_candidate(station, 'day-PS-fail-wrong-unit', 'PS-fail-wrong-unit')
            register_check_standard(station, STATION, CheckStandard(
                'span', *high_trial, report_unit, cases['standard_reference'], *cases['allowed'],
                low_trial=(raw_channel, 'span-low')))
            failure_receipts['wrong-unit'] = inspect_candidate(station, 'PS-fail-wrong-unit', 'span',
                                                               'PS-fail-wrong-unit-span')
            register_check_standard(station, STATION, span_standard)
            expected_reasons = {'missing-value': 'missing_value', 'missing-sample': 'missing_sample',
                                'wrong-unit': 'wrong_unit'}
            failures = {}
            for name, reason in expected_reasons.items():
                receipt = failure_receipts[name]
                if (receipt.passed or receipt.finding.reason != reason
                        or receipt.finding.value is not None or receipt.finding.interval is not None
                        or load_check_receipt(station, receipt.receipt_id) != receipt):
                    raise RuntimeError('PAIRED_SPAN_FAILURE_RECEIPT_WRONG')
                failures[name] = receipt.finding.reason
            try:
                register_calibration(station, STATION, high_channel, 'PS-conflict', cases['scale'],
                                     cases['offset'], reference_error=('E', '1/2', 1))
                raise RuntimeError('PAIRED_SPAN_RADIUS_CONFLICT_ACCEPTED')
            except ValueError:
                pass
            try:
                load_calibration(station, 'PS-conflict')
                raise RuntimeError('PAIRED_SPAN_PARTIAL_REGISTRATION')
            except KeyError:
                pass
            select('PS-exact', 'select-PS-exact')
            if send('PS-exact-zero').status != 'waiting':
                raise RuntimeError('PAIRED_SPAN_DELIVERY_STATE_WRONG')

            class CommitInterrupted(Exception):
                pass

            def interrupt():
                raise CommitInterrupted('simulated interruption before durable issue')

            try:
                redeliver_receipt(station, 'PS-exact-span',
                                  partial(deliver_receipt, publication, before_commit=interrupt))
                raise RuntimeError('PAIRED_SPAN_INTERRUPTION_MISSING')
            except CommitInterrupted:
                pass
            if (read_report(publication, station=STATION, day=DAY) != shared_issue
                    or read_report(publication, issue_id='issue:PS-exact') is not None):
                raise RuntimeError('PAIRED_SPAN_PRE_COMMIT_STATE_WRONG')
            exact_issue = send('PS-exact-span').issue
            if exact_issue is None or read_report(publication, station=STATION, day=DAY) != exact_issue:
                raise RuntimeError('PAIRED_SPAN_REDELIVERY_FAILED')
            register_check_standard(station, STATION, CheckStandard(
                'span', *high_trial, report_unit, '13/8', '13/8', '7/4', low_trial=low_trial))
            if str(load_check_receipt(station, 'PS-shared-span').standard.upper) != '7/4':
                raise RuntimeError('PAIRED_SPAN_RECEIPT_CHANGED')
            print('POINTWISE_SCENARIO ' + json.dumps({
                'case_id': 'paired_span_verdicts',
                'input': {'station': STATION, 'day': DAY, 'high_trial': list(high_trial),
                          'low_trial': list(low_trial),
                          'standard': {'reference': cases['standard_reference'], 'allowed': cases['allowed']},
                          'references': {case['id']: {'high': case['high_error'], 'low': case['low_error']}
                                         for case in cases['cases']}},
                'expected': {case['id']: {'interval': case['interval'], 'passed': case['passed']}
                             for case in cases['cases']},
                'actual': verdicts,
                'downstream_expected': {'current_after_independent': 'issue:PS-shared',
                                        'failure_reasons': expected_reasons,
                                        'radius_conflict': 'rejected'},
                'downstream_actual': {'current_after_independent': shared_issue.receipt.issue_id,
                                      'failure_reasons': failures,
                                      'radius_conflict': 'rejected'},
                'code_paths': ['source/calibrations.py', 'candidate/snapshot.py', 'inspection/span.py',
                               'inspection/receipts.py', 'publication/evidence.py'],
                'oracle': 'A shared reference cancels in high minus low ([3/2, 3/2] passes); '
                          'independent references vary freely and their margins add ([1, 2] fails).',
            }, ensure_ascii=False, sort_keys=True))
        with closing(open_publication(publication_path)) as night:
            claimed = read_report(night, issue_id='issue:PS-shared')
            evidence = claimed.span_evidence
            kept_standard = [str(evidence.standard.reference), str(evidence.standard.lower),
                             str(evidence.standard.upper)]
            kept_interval = [str(v) for v in evidence.finding.interval]
            if kept_standard != ['3/2', '3/2', '7/4'] or kept_interval != ['3/2', '3/2']:
                raise RuntimeError('PAIRED_SPAN_CLAIM_CHANGED')
            if read_report(night, station=STATION, day=DAY).receipt.issue_id != 'issue:PS-exact':
                raise RuntimeError('PAIRED_SPAN_CURRENT_WRONG')
            package_shared = export_report(night, 'issue:PS-shared')
            package_exact = export_report(night, 'issue:PS-exact')
            if package_shared['manifest']['version'] != 2 or package_exact['manifest']['version'] != 2:
                raise RuntimeError('PAIRED_SPAN_MANIFEST_VERSION_WRONG')
            if not verify_package(package_shared) or not verify_package(package_exact):
                raise RuntimeError('PAIRED_SPAN_VERIFY_FAILED')
            tampered = deepcopy(package_shared)
            tampered['manifest']['span_evidence']['standard']['upper'] = '2'
            tampered['digest'] = package_digest(tampered['manifest'])
            swapped_evidence = deepcopy(package_shared)
            swapped_evidence['manifest']['span_evidence'] = deepcopy(package_exact['manifest']['span_evidence'])
            swapped_evidence['digest'] = package_digest(swapped_evidence['manifest'])
            if verify_package(tampered) or verify_package(swapped_evidence):
                raise RuntimeError('PAIRED_SPAN_TAMPER_ACCEPTED')
            print('POINTWISE_SCENARIO ' + json.dumps({
                'case_id': 'paired_span_offline_claim',
                'input': {'interrupted_delivery': 'PS-exact-span', 'redelivered': 'PS-exact-span',
                          'new_standard': {'reference': '13/8', 'lower': '13/8', 'upper': '7/4'},
                          'source_closed': True, 'claim': 'issue:PS-shared'},
                'expected': {'pre_commit_current': 'issue:PS-shared', 'current': 'issue:PS-exact',
                             'claimed_standard': ['3/2', '3/2', '7/4'], 'claimed_interval': ['3/2', '3/2'],
                             'verify': True, 'tampered_verify': False, 'swapped_evidence_verify': False},
                'actual': {'pre_commit_current': shared_issue.receipt.issue_id,
                           'current': read_report(night, station=STATION, day=DAY).receipt.issue_id,
                           'claimed_standard': kept_standard, 'claimed_interval': kept_interval,
                           'verify': verify_package(package_shared),
                           'tampered_verify': verify_package(tampered),
                           'swapped_evidence_verify': verify_package(swapped_evidence)},
                'code_paths': ['publication/admission.py', 'publication/ledger.py', 'receipt/lookup.py',
                               'receipt/package.py', 'receipt/verify.py'],
                'oracle': 'The first issue binds the span receipt body; after a standard change and source '
                          'shutdown the night shift recomputes the interval from the package alone.',
            }, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    run_demo()
    run_paired_span_demo()
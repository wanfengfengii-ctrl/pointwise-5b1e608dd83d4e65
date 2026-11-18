from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from candidate.assemble import prepare_candidate
from candidate.snapshot import decode_snapshot, encode_snapshot, snapshot_digest
from candidate.store import load_candidate
from demo import DAY, STATION, initialize_source
from inspection.receipts import (
    check_receipt_digest, inspect_candidate, list_pending_receipts, load_check_receipt,
)
from inspection.standards import CheckStandard, load_check_standard, register_check_standard
from publication.admission import deliver_receipt, initialize_publication
from publication.ledger import open_publication
from receipt.lookup import read_report
from receipt.manifest import package_digest
from receipt.package import export_report
from receipt.verify import verify_package
from source.calibrations import load_calibration, register_calibration
from source.database import open_station
from source.registry import StationRegistry
from source.revisions import register_revision
from tests.fixtures import StationCase


CASES = json.loads((Path(__file__).resolve().parent.parent / 'paired_span_cases.json').read_text(encoding='utf-8'))
HIGH = 'displacement'
LOW = 'low-displacement'

LEGACY_STATION_DDL = '''
CREATE TABLE stations (station TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE channels (station TEXT NOT NULL REFERENCES stations(station), channel TEXT NOT NULL,
    input_unit TEXT NOT NULL, report_unit TEXT NOT NULL, PRIMARY KEY(station, channel));
CREATE TABLE calibrations (calibration_id TEXT PRIMARY KEY, station TEXT NOT NULL, channel TEXT NOT NULL,
    input_unit TEXT NOT NULL, report_unit TEXT NOT NULL, scale TEXT NOT NULL, offset TEXT NOT NULL,
    FOREIGN KEY(station, channel) REFERENCES channels(station, channel));
CREATE TABLE day_revisions (revision_id TEXT PRIMARY KEY, station TEXT NOT NULL REFERENCES stations(station),
    day TEXT NOT NULL, corrects TEXT REFERENCES day_revisions(revision_id));
CREATE TABLE revision_calibrations (revision_id TEXT NOT NULL REFERENCES day_revisions(revision_id),
    channel TEXT NOT NULL, calibration_id TEXT NOT NULL REFERENCES calibrations(calibration_id),
    PRIMARY KEY(revision_id, channel));
CREATE TABLE raw_samples (revision_id TEXT NOT NULL, position INTEGER NOT NULL CHECK(position >= 0),
    sample_id TEXT NOT NULL, channel TEXT NOT NULL, raw TEXT,
    PRIMARY KEY(revision_id, position), UNIQUE(revision_id, sample_id),
    FOREIGN KEY(revision_id, channel) REFERENCES revision_calibrations(revision_id, channel));
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES day_revisions(revision_id), snapshot TEXT NOT NULL);
CREATE TABLE replacement_requests (request_id TEXT PRIMARY KEY, station TEXT NOT NULL, day TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id), input_digest TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0, 1)), previous_candidate_id TEXT);
CREATE TABLE check_standards (station TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('zero', 'span')),
    channel TEXT NOT NULL, sample_id TEXT NOT NULL, report_unit TEXT NOT NULL,
    reference TEXT NOT NULL, lower TEXT NOT NULL, upper TEXT NOT NULL,
    PRIMARY KEY(station, kind), FOREIGN KEY(station, channel) REFERENCES channels(station, channel));
CREATE TABLE check_receipts (receipt_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    kind TEXT NOT NULL CHECK(kind IN ('zero', 'span')), body TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0, 1)), UNIQUE(candidate_id, kind));
'''

LEGACY_PUBLICATION_DDL = '''
CREATE TABLE received_candidates (candidate_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE received_evidence (receipt_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES received_candidates(candidate_id),
    kind TEXT NOT NULL CHECK(kind IN ('zero', 'span')),
    passed INTEGER NOT NULL CHECK(passed IN (0, 1)), body TEXT NOT NULL, UNIQUE(candidate_id, kind));
CREATE TABLE active_candidates (station TEXT NOT NULL, day TEXT NOT NULL,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES received_candidates(candidate_id),
    PRIMARY KEY(station, day));
CREATE TABLE retired_candidates (candidate_id TEXT PRIMARY KEY REFERENCES received_candidates(candidate_id));
CREATE TABLE issues (issue_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
    snapshot TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE current_issues (station TEXT NOT NULL, day TEXT NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id), PRIMARY KEY(station, day));
CREATE TABLE admitted_replacements (request_id TEXT PRIMARY KEY, request_body TEXT NOT NULL,
    previous_candidate_id TEXT REFERENCES received_candidates(candidate_id));
'''


def reference(spec):
    return None if spec is None else tuple(spec)


class PairedSpanCase(StationCase):
    def setUp(self):
        super().setUp()
        StationRegistry(self.station).register_channel(STATION, LOW, 'mV', 'mm', only_missing=True)
        self.standard = CheckStandard('span', HIGH, 'span-trial', 'mm', CASES['standard_reference'],
                                      *CASES['allowed'], low_trial=(LOW, 'span-low'))
        register_check_standard(self.station, STATION, self.standard)

    def prepare_case(self, name, high_error, low_error, *, samples=None, corrects=None):
        register_calibration(self.station, STATION, HIGH, f'{name}-high', '1/2', '1/4',
                             reference_error=high_error)
        register_calibration(self.station, STATION, LOW, f'{name}-low', '1/2', '1/4',
                             reference_error=low_error)
        if samples is None:
            samples = [tuple(row) for row in CASES['samples']]
        register_revision(self.station, f'day-{name}', STATION, DAY, samples,
                          {HIGH: f'{name}-high', LOW: f'{name}-low'}, corrects=corrects)
        return prepare_candidate(self.station, f'day-{name}', name)


class RegistrationTests(PairedSpanCase):
    def test_shared_reference_radius_must_agree(self):
        register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4',
                             reference_error=('E', '1/4', 1))
        with self.assertRaises(ValueError):
            register_calibration(self.station, STATION, HIGH, 'cal-b', '1/2', '1/4',
                                 reference_error=('E', '1/2', 1))
        with self.assertRaises(KeyError):
            load_calibration(self.station, 'cal-b')
        again = register_calibration(self.station, STATION, HIGH, 'cal-c', '1/2', '1/4',
                                     reference_error=('E', '1/4', -1))
        self.assertEqual((again.reference_id, again.error_radius), ('E', Fraction(1, 4)))
        other = register_calibration(self.station, STATION, HIGH, 'cal-d', '1/2', '1/4',
                                     reference_error=('F', '1/4', 1))
        self.assertEqual(other.reference_id, 'F')

    def test_other_station_reuses_reference_id_independently(self):
        register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4',
                             reference_error=('E', '1/4', 1))
        registry = StationRegistry(self.station)
        registry.register_station('QC-08', '相邻检测站')
        registry.register_channel('QC-08', HIGH, 'mV', 'mm')
        independent = register_calibration(self.station, 'QC-08', HIGH, 'cal-b', '1/2', '1/4',
                                           reference_error=('E', '1/2', 1))
        self.assertEqual(independent.error_radius, Fraction(1, 2))

    def test_calibration_identity_conflicts(self):
        register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4',
                             reference_error=('E', '1/4', 1))
        with self.assertRaises(ValueError):
            register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4')
        with self.assertRaises(ValueError):
            register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4',
                                 reference_error=('F', '1/4', 1))
        same = register_calibration(self.station, STATION, HIGH, 'cal-a', '1/2', '1/4',
                                    reference_error=('E', '1/4', 1))
        self.assertEqual(same.error_coefficient, 1)
        kept = register_calibration(self.station, STATION, HIGH, 'cal-a', '1/3', 0, only_missing=True)
        self.assertEqual((kept.scale, kept.reference_id), (Fraction(1, 2), 'E'))

    def test_invalid_reference_error_rejected(self):
        for bad in [('E', '-1/4', 1), ('E', '1/4'), ('', '1/4', 1), 'not-a-triple', ('E', '1/4', 1, 0)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                register_calibration(self.station, STATION, HIGH, 'cal-bad', '1/2', '1/4',
                                     reference_error=bad)
        with self.assertRaises(KeyError):
            load_calibration(self.station, 'cal-bad')


class FreezeTests(PairedSpanCase):
    def test_input_digest_binds_reference_relations(self):
        shared = self.prepare_case('shared', ('E', '1/4', 1), ('E', '1/4', 1))
        independent = self.prepare_case('independent', ('E', '1/4', 1), ('F', '1/4', 1),
                                        corrects='day-shared')
        exact = self.prepare_case('exact', None, None, corrects='day-independent')
        self.assertEqual(shared.value_digest, independent.value_digest)
        self.assertEqual(shared.value_digest, exact.value_digest)
        self.assertNotEqual(snapshot_digest(shared), snapshot_digest(independent))
        self.assertNotEqual(snapshot_digest(shared), snapshot_digest(exact))
        row = load_candidate(self.station, 'shared').rows[0]
        self.assertEqual((row.reference_id, row.error_radius, row.error_coefficient),
                         ('E', Fraction(1, 4), Fraction(1)))
        self.assertIsNone(load_candidate(self.station, 'exact').rows[0].reference_id)

    def test_exact_candidate_encoding_omits_reference_keys(self):
        exact = self.prepare_case('exact', None, None)
        for row in json.loads(encode_snapshot(exact))['rows']:
            self.assertNotIn('reference_id', row)
        bound = self.prepare_case('bound', ('E', '1/4', 1), ('E', '1/4', 1), corrects='day-exact')
        encoded = json.loads(encode_snapshot(bound))
        self.assertEqual(encoded['rows'][0]['error_radius'], '1/4')
        self.assertEqual(encoded['rows'][0]['error_coefficient'], '1')
        self.assertEqual(decode_snapshot(encode_snapshot(bound)), bound)

    def test_contradictory_candidate_rejected(self):
        bound = self.prepare_case('bound', ('E', '1/4', 1), ('E', '1/4', 1))
        payload = json.loads(encode_snapshot(bound))
        payload['rows'][0]['error_radius'] = '1/2'
        with self.assertRaises(ValueError):
            decode_snapshot(json.dumps(payload))
        payload = json.loads(encode_snapshot(bound))
        del payload['rows'][0]['error_radius']
        with self.assertRaises(ValueError):
            decode_snapshot(json.dumps(payload))


class PairedSpanVerdictTests(PairedSpanCase):
    def test_json_cases(self):
        previous = None
        for case in CASES['cases']:
            with self.subTest(case=case['id']):
                name = f"case-{case['id']}"
                candidate = self.prepare_case(name, reference(case['high_error']),
                                              reference(case['low_error']), corrects=previous)
                previous = f'day-{name}'
                zero = inspect_candidate(self.station, candidate.candidate_id, 'zero', f'{name}-zero')
                span = inspect_candidate(self.station, candidate.candidate_id, 'span', f'{name}-span')
                self.assertTrue(zero.passed)
                self.assertEqual(span.passed, case['passed'])
                self.assertEqual([str(v) for v in span.finding.interval], case['interval'])
                self.assertEqual(str(span.finding.value), CASES['expected_nominal'])
                self.assertEqual((span.finding.position, span.finding.low_position),
                                 (CASES['expected_positions']['high'], CASES['expected_positions']['low']))
                self.assertEqual(span.standard, self.standard)
                reread = inspect_candidate(self.station, candidate.candidate_id, 'span', f'{name}-span-2')
                self.assertEqual(reread, span)
                self.assertEqual(load_check_receipt(self.station, span.receipt_id), span)

    def test_standard_change_keeps_recorded_receipt(self):
        self.prepare_case('shared', ('E', '1/4', 1), ('E', '1/4', 1))
        span = inspect_candidate(self.station, 'shared', 'span', 'shared-span')
        register_check_standard(self.station, STATION, CheckStandard(
            'span', HIGH, 'span-trial', 'mm', '13/8', '13/8', '7/4', low_trial=(LOW, 'span-low')))
        self.assertEqual(load_check_standard(self.station, STATION, 'span').reference, Fraction(13, 8))
        self.assertEqual(inspect_candidate(self.station, 'shared', 'span', 'shared-span-2'), span)
        self.prepare_case('fresh', None, None, corrects='day-shared')
        narrowed = inspect_candidate(self.station, 'fresh', 'span', 'fresh-span')
        self.assertFalse(narrowed.passed)
        self.assertEqual(narrowed.finding.reason, 'below_span')
        self.assertEqual(narrowed.finding.interval, (Fraction(3, 2), Fraction(3, 2)))


class UnavailableRowTests(PairedSpanCase):
    def test_missing_value(self):
        samples = [('span-low', LOW, None), ('zero-trial', HIGH, 0), ('span-trial', HIGH, 3)]
        self.prepare_case('fault-value', None, None, samples=samples)
        span = inspect_candidate(self.station, 'fault-value', 'span', 'fault-value-span')
        self.assertEqual((span.passed, span.finding.reason), (False, 'missing_value'))
        self.assertEqual((span.finding.position, span.finding.low_position), (2, 0))
        self.assertIsNone(span.finding.value)
        self.assertIsNone(span.finding.interval)
        self.assertEqual(load_check_receipt(self.station, 'fault-value-span'), span)

    def test_missing_sample(self):
        self.prepare_case('fault-row', None, None,
                          samples=[('zero-trial', HIGH, 0), ('span-trial', HIGH, 3)])
        span = inspect_candidate(self.station, 'fault-row', 'span', 'fault-row-span')
        self.assertEqual(span.finding.reason, 'missing_sample')
        self.assertEqual((span.finding.position, span.finding.low_position), (1, None))
        self.assertIsNone(span.finding.interval)
        self.prepare_case('fault-high', None, None,
                          samples=[('span-low', LOW, 0), ('zero-trial', HIGH, 0)], corrects='day-fault-row')
        high_missing = inspect_candidate(self.station, 'fault-high', 'span', 'fault-high-span')
        self.assertEqual(high_missing.finding.reason, 'missing_sample')
        self.assertEqual((high_missing.finding.position, high_missing.finding.low_position), (None, 0))

    def test_wrong_unit(self):
        StationRegistry(self.station).register_channel(STATION, 'low-raw', 'mV', 'mV', only_missing=True)
        register_calibration(self.station, STATION, 'low-raw', 'fault-unit-low', 1, 0)
        register_calibration(self.station, STATION, HIGH, 'fault-unit-high', '1/2', '1/4')
        samples = [('span-low', 'low-raw', 0), ('zero-trial', HIGH, 0), ('span-trial', HIGH, 3)]
        register_revision(self.station, 'day-fault-unit', STATION, DAY, samples,
                          {HIGH: 'fault-unit-high', 'low-raw': 'fault-unit-low'})
        prepare_candidate(self.station, 'day-fault-unit', 'fault-unit')
        register_check_standard(self.station, STATION, CheckStandard(
            'span', HIGH, 'span-trial', 'mm', '3/2', '3/2', '7/4', low_trial=('low-raw', 'span-low')))
        span = inspect_candidate(self.station, 'fault-unit', 'span', 'fault-unit-span')
        self.assertEqual((span.passed, span.finding.reason), (False, 'wrong_unit'))
        self.assertEqual((span.finding.position, span.finding.low_position), (2, 0))
        self.assertIsNone(span.finding.value)
        self.assertIsNone(span.finding.interval)

    def test_failed_receipts_never_issue(self):
        samples = [('span-low', LOW, None), ('zero-trial', HIGH, 0), ('span-trial', HIGH, 3)]
        self.prepare_case('fault-value', None, None, samples=samples)
        self.inspect('fault-value', 'zero')
        self.inspect('fault-value', 'span')
        self.assertEqual(self.send('fault-value', 'zero').status, 'waiting')
        self.assertEqual(self.send('fault-value', 'span').status, 'waiting')
        self.assertIsNone(self.current())
        self.assertEqual(self.issue_count(), 0)


class StandardValidationTests(PairedSpanCase):
    def test_low_trial_only_on_span(self):
        with self.assertRaises(ValueError):
            CheckStandard('zero', HIGH, 'zero-trial', 'mm', 0, '-1/4', '1/4',
                          low_trial=(LOW, 'span-low'))

    def test_selectors_must_differ(self):
        with self.assertRaises(ValueError):
            CheckStandard('span', HIGH, 'span-trial', 'mm', '3/2', '3/2', '7/4',
                          low_trial=(HIGH, 'span-trial'))

    def test_low_channel_must_be_registered(self):
        standard = CheckStandard('span', HIGH, 'span-trial', 'mm', '3/2', '3/2', '7/4',
                                 low_trial=('absent', 'span-low'))
        with self.assertRaises(KeyError):
            register_check_standard(self.station, STATION, standard)

    def test_reference_must_lie_in_interval(self):
        with self.assertRaises(ValueError):
            CheckStandard('span', HIGH, 'span-trial', 'mm', 3, '3/2', '7/4',
                          low_trial=(LOW, 'span-low'))


class IsolatedRecomputeTests(PairedSpanCase):
    def setUp(self):
        super().setUp()
        self.prepare_case('shared', ('E', '1/4', 1), ('E', '1/4', 1))
        self.zero = inspect_candidate(self.station, 'shared', 'zero', 'shared-zero')
        self.span = inspect_candidate(self.station, 'shared', 'span', 'shared-span')

    def test_tampered_findings_rejected(self):
        snapshot = load_candidate(self.station, 'shared')
        for finding in (
            replace(self.span.finding, interval=(Fraction(3, 2), Fraction(7, 4))),
            replace(self.span.finding, low_position=1),
            replace(self.span.finding, position=0),
            replace(self.span.finding, passed=False),
            replace(self.span.finding, value=Fraction(1)),
        ):
            with self.subTest(finding=finding), self.assertRaises(ValueError):
                deliver_receipt(self.publication, replace(self.span, finding=finding), snapshot)

    def test_equal_nominal_positions_still_bound(self):
        snapshot = load_candidate(self.station, 'shared')
        self.assertEqual(snapshot.rows[0].value, snapshot.rows[1].value)
        moved = replace(self.span, finding=replace(self.span.finding, low_position=1))
        with self.assertRaises(ValueError):
            deliver_receipt(self.publication, moved, snapshot)

    def test_swapped_standard_receipt_conflicts(self):
        self.assertEqual(self.send('shared', 'zero').status, 'waiting')
        self.assertIsNotNone(self.send('shared', 'span').issue)
        relaxed = replace(self.span, standard=replace(self.span.standard, lower=Fraction(1)))
        with self.assertRaises(ValueError):
            deliver_receipt(self.publication, relaxed)

    def test_issue_binds_span_evidence(self):
        self.assertEqual(self.send('shared', 'zero').status, 'waiting')
        issue = self.send('shared', 'span').issue
        self.assertEqual(issue.span_evidence, self.span)
        self.assertEqual(issue.receipt.span_evidence_digest, check_receipt_digest(self.span))
        package = export_report(self.publication, 'issue:shared')
        manifest = package['manifest']
        self.assertEqual(manifest['version'], 2)
        self.assertEqual(set(manifest), {'version', 'report', 'receipt', 'span_evidence'})
        self.assertTrue(verify_package(package))
        tampered = deepcopy(package)
        tampered['manifest']['span_evidence']['standard']['upper'] = '2'
        tampered['digest'] = package_digest(tampered['manifest'])
        self.assertFalse(verify_package(tampered))
        relaxed = deepcopy(package)
        relaxed['manifest']['span_evidence']['standard']['lower'] = '1'
        relaxed['digest'] = package_digest(relaxed['manifest'])
        self.assertFalse(verify_package(relaxed))
        self.prepare_case('exact', None, None, corrects='day-shared')
        self.select('exact')
        self.assertIsNotNone(self.sign('exact'))
        exact_package = export_report(self.publication, 'issue:exact')
        swapped = deepcopy(package)
        swapped['manifest']['span_evidence'] = deepcopy(exact_package['manifest']['span_evidence'])
        swapped['digest'] = package_digest(swapped['manifest'])
        self.assertFalse(verify_package(swapped))


class CommitBoundaryTests(PairedSpanCase):
    def test_interrupted_first_issue_rolls_back(self):
        self.prepare_case('shared', ('E', '1/4', 1), ('E', '1/4', 1))
        self.inspect('shared', 'zero')
        self.inspect('shared', 'span')
        self.assertEqual(self.send('shared', 'zero').status, 'waiting')

        class CommitFailure(Exception):
            pass

        def fail_commit():
            raise CommitFailure('simulated interruption before durable issue')

        with self.assertRaises(CommitFailure):
            self.send('shared', 'span',
                      port=partial(deliver_receipt, self.publication, before_commit=fail_commit))
        self.reopen()
        self.assertIsNone(self.current())
        self.assertIsNone(self.history('shared'))
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual([item.receipt_id for item in list_pending_receipts(self.station)],
                         ['shared-span'])
        issue = self.send('shared', 'span').issue
        self.assertIsNotNone(issue)
        self.assertEqual(issue.span_evidence.finding.interval, (Fraction(3, 2), Fraction(3, 2)))
        self.assertEqual(self.current(), issue)
        self.assertEqual(self.issue_count(), 1)


class ClaimTests(PairedSpanCase):
    def test_claim_after_standard_change_and_source_close(self):
        self.prepare_case('shared', ('E', '1/4', 1), ('E', '1/4', 1))
        issue = self.sign('shared')
        register_check_standard(self.station, STATION, CheckStandard(
            'span', HIGH, 'span-trial', 'mm', '13/8', '13/8', '7/4', low_trial=(LOW, 'span-low')))
        self.assertEqual(str(load_check_receipt(self.station, 'shared-span').standard.upper), '7/4')
        self.close_station()
        self.publication.close()
        self.publication = open_publication(self.publication_path)
        initialize_publication(self.publication)
        claimed = self.history('shared')
        self.assertEqual(claimed, issue)
        evidence = claimed.span_evidence
        self.assertEqual([str(v) for v in evidence.finding.interval], ['3/2', '3/2'])
        self.assertEqual((str(evidence.standard.reference), str(evidence.standard.upper)),
                         ('3/2', '7/4'))
        self.assertEqual(evidence.standard.low_trial, (LOW, 'span-low'))
        package = export_report(self.publication, 'issue:shared')
        self.assertEqual(package['manifest']['version'], 2)
        self.assertTrue(verify_package(package))


class SingleRowModeTests(PairedSpanCase):
    def test_single_row_standard_uses_nominal_values(self):
        register_check_standard(self.station, STATION,
                                CheckStandard('span', HIGH, 'span-trial', 'mm', '7/4', '3/2', 2))
        self.prepare_case('bound', ('E', '1/4', 1), ('F', '1/4', 1))
        span = inspect_candidate(self.station, 'bound', 'span', 'bound-span')
        self.assertTrue(span.passed)
        self.assertEqual(span.finding.value, Fraction(7, 4))
        self.assertIsNone(span.finding.interval)
        self.assertIsNone(span.finding.low_position)
        issue = self.sign('bound')
        self.assertIsNotNone(issue)
        self.assertIsNone(issue.span_evidence)
        package = export_report(self.publication, 'issue:bound')
        self.assertEqual(package['manifest']['version'], 1)
        self.assertNotIn('span_evidence_digest', package['manifest']['receipt'])
        self.assertTrue(verify_package(package))


class HistoricalV1Tests(PairedSpanCase):
    def _copy_legacy_store(self, path, ddl, statements, opener):
        legacy = opener(path)
        try:
            legacy.executescript(ddl)
            legacy.execute('ATTACH DATABASE ? AS src', (str(self.station_path if opener is open_station
                                                          else self.publication_path),))
            for statement in statements:
                legacy.execute(statement)
            legacy.execute('DETACH DATABASE src')
        finally:
            legacy.close()

    def test_v1_ledger_remains_readable(self):
        register_check_standard(self.station, STATION,
                                CheckStandard('span', HIGH, 'span-trial', 'mm', '7/4', '3/2', 2))
        prior = self.sign(self.prepare('R0').candidate_id)
        self.assertIsNotNone(prior)
        folder = TemporaryDirectory(prefix='quay-v1-')
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        station_path = root / 'station.sqlite'
        publication_path = root / 'publication.sqlite'
        self._copy_legacy_store(station_path, LEGACY_STATION_DDL, (
            'INSERT INTO stations SELECT * FROM src.stations',
            'INSERT INTO channels SELECT * FROM src.channels',
            'INSERT INTO calibrations SELECT calibration_id, station, channel, input_unit, '
            'report_unit, scale, offset FROM src.calibrations',
            'INSERT INTO day_revisions SELECT * FROM src.day_revisions',
            'INSERT INTO revision_calibrations SELECT * FROM src.revision_calibrations',
            'INSERT INTO raw_samples SELECT * FROM src.raw_samples',
            'INSERT INTO candidates SELECT * FROM src.candidates',
            'INSERT INTO replacement_requests SELECT * FROM src.replacement_requests',
            'INSERT INTO check_standards SELECT station, kind, channel, sample_id, report_unit, '
            'reference, lower, upper FROM src.check_standards',
            'INSERT INTO check_receipts SELECT * FROM src.check_receipts',
        ), open_station)
        self._copy_legacy_store(publication_path, LEGACY_PUBLICATION_DDL, (
            'INSERT INTO received_candidates SELECT * FROM src.received_candidates',
            'INSERT INTO received_evidence SELECT * FROM src.received_evidence',
            'INSERT INTO active_candidates SELECT * FROM src.active_candidates',
            'INSERT INTO retired_candidates SELECT * FROM src.retired_candidates',
            'INSERT INTO issues SELECT issue_id, candidate_id, snapshot, receipt FROM src.issues',
            'INSERT INTO current_issues SELECT * FROM src.current_issues',
            'INSERT INTO admitted_replacements SELECT * FROM src.admitted_replacements',
        ), open_publication)
        with closing(open_station(station_path)) as station, \
                closing(open_publication(publication_path)) as publication:
            initialize_source(station)
            initialize_publication(publication)
            candidate = load_candidate(station, 'R0')
            self.assertEqual(candidate, prior.report)
            self.assertIsNone(load_calibration(station, 'C1').reference_id)
            receipt = load_check_receipt(station, 'R0-span')
            self.assertIsNone(receipt.standard.low_trial)
            self.assertIsNone(receipt.finding.interval)
            self.assertIsNone(load_check_standard(station, STATION, 'span').low_trial)
            claimed = read_report(publication, issue_id='issue:R0')
            self.assertEqual(claimed, prior)
            self.assertIsNone(claimed.span_evidence)
            self.assertIsNone(claimed.receipt.span_evidence_digest)
            package = export_report(publication, 'issue:R0')
            self.assertEqual(package['manifest']['version'], 1)
            self.assertNotIn('span_evidence_digest', package['manifest']['receipt'])
            self.assertTrue(verify_package(package))
            bound = register_calibration(station, STATION, 'displacement', 'C9', '1/2', '1/4',
                                         reference_error=('E', '1/4', 1))
            self.assertEqual(bound.reference_id, 'E')
            self.assertEqual(load_calibration(station, 'C9').error_radius, Fraction(1, 4))

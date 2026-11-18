from dataclasses import dataclass
from fractions import Fraction

from source.database import ensure_columns, station_transaction
from source.registry import StationRegistry, require_name
from source.samples import exact_number, optional_number


def require_check_kind(kind):
    if kind not in ('zero', 'span'):
        raise ValueError(f'unknown check kind {kind!r}')
    return kind


@dataclass(frozen=True)
class CheckStandard:
    kind: str
    channel: str
    sample_id: str
    report_unit: str
    reference: Fraction
    lower: Fraction
    upper: Fraction
    low_trial: tuple | None = None

    def __post_init__(self):
        require_check_kind(self.kind)
        for value in (self.channel, self.sample_id, self.report_unit):
            require_name(value)
        for field in ('reference', 'lower', 'upper'):
            object.__setattr__(self, field, exact_number(getattr(self, field)))
        if not within_closed_interval(self.reference, self.lower, self.upper):
            raise ValueError('check reference must lie in the closed interval')
        if self.kind == 'zero' and self.reference != 0:
            raise ValueError('zero check must use a zero reference')
        if self.low_trial is not None:
            if self.kind != 'span':
                raise ValueError('only span checks may name a low trial')
            if isinstance(self.low_trial, str):
                raise ValueError('low trial must be a (channel, sample_id) pair')
            try:
                trial = tuple(self.low_trial)
            except TypeError as exc:
                raise ValueError('low trial must be a (channel, sample_id) pair') from exc
            if len(trial) != 2:
                raise ValueError('low trial must be a (channel, sample_id) pair')
            for value in trial:
                require_name(value)
            if trial == (self.channel, self.sample_id):
                raise ValueError('high and low trials must differ')
            object.__setattr__(self, 'low_trial', trial)


@dataclass(frozen=True)
class CheckFinding:
    position: int | None
    value: Fraction | None
    passed: bool
    reason: str
    low_position: int | None = None
    interval: tuple | None = None

    def __post_init__(self):
        for field in ('position', 'low_position'):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('invalid evidence position')
        if type(self.passed) is not bool:
            raise ValueError('invalid check verdict')
        object.__setattr__(self, 'value', optional_number(self.value))
        if self.interval is not None:
            if isinstance(self.interval, str):
                raise ValueError('interval must be a pair of exact endpoints')
            try:
                endpoints = tuple(self.interval)
            except TypeError as exc:
                raise ValueError('interval must be a pair of exact endpoints') from exc
            if len(endpoints) != 2:
                raise ValueError('interval must be a pair of exact endpoints')
            object.__setattr__(self, 'interval', tuple(exact_number(v) for v in endpoints))
        require_name(self.reason)


def initialize_check_standards(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS check_standards (
            station TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('zero', 'span')),
            channel TEXT NOT NULL,
            sample_id TEXT NOT NULL,
            report_unit TEXT NOT NULL,
            reference TEXT NOT NULL,
            lower TEXT NOT NULL,
            upper TEXT NOT NULL,
            low_channel TEXT,
            low_sample_id TEXT,
            PRIMARY KEY(station, kind),
            FOREIGN KEY(station, channel) REFERENCES channels(station, channel)
        )
    ''')
    ensure_columns(db, 'check_standards', {
        'low_channel': 'TEXT',
        'low_sample_id': 'TEXT',
    })


def _store_standard(db, station, standard, only_missing):
    registry = StationRegistry(db)
    channel = registry.require_channel(station, standard.channel)
    if channel.report_unit != standard.report_unit:
        raise ValueError('check unit differs from channel report unit')
    low_channel = low_sample_id = None
    if standard.low_trial is not None:
        low_channel, low_sample_id = standard.low_trial
        registry.require_channel(station, low_channel)
    conflict = 'DO NOTHING' if only_missing else (
        'DO UPDATE SET channel=excluded.channel, sample_id=excluded.sample_id, '
        'report_unit=excluded.report_unit, reference=excluded.reference, '
        'lower=excluded.lower, upper=excluded.upper, '
        'low_channel=excluded.low_channel, low_sample_id=excluded.low_sample_id'
    )
    db.execute(
        'INSERT INTO check_standards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
        f'ON CONFLICT(station, kind) {conflict}',
        (station, standard.kind, standard.channel, standard.sample_id, standard.report_unit,
         str(standard.reference), str(standard.lower), str(standard.upper),
         low_channel, low_sample_id),
    )


def register_check_standard(db, station, standard):
    with station_transaction(db):
        _store_standard(db, station, standard, False)


def seed_check_standards(db, station, channel, report_unit):
    standards = (
        CheckStandard('zero', channel, 'zero-trial', report_unit, 0, '-1/4', '1/4'),
        CheckStandard('span', channel, 'span-trial', report_unit, '7/4', '3/2', 2),
    )
    with station_transaction(db):
        for standard in standards:
            _store_standard(db, station, standard, True)


def load_check_standard(db, station, kind):
    require_check_kind(kind)
    row = db.execute(
        'SELECT kind, channel, sample_id, report_unit, reference, lower, upper, '
        'low_channel, low_sample_id FROM check_standards WHERE station = ? AND kind = ?',
        (station, kind),
    ).fetchone()
    if row is None:
        raise KeyError((station, kind))
    low_trial = None
    if row['low_channel'] is not None or row['low_sample_id'] is not None:
        low_trial = (row['low_channel'], row['low_sample_id'])
    return CheckStandard(row['kind'], row['channel'], row['sample_id'], row['report_unit'],
                         row['reference'], row['lower'], row['upper'], low_trial=low_trial)


def locate_evidence(snapshot, standard):
    return locate_trial(snapshot, (standard.channel, standard.sample_id))


def locate_trial(snapshot, selector):
    channel, sample_id = selector
    return next((row for row in snapshot.rows
                 if (row.channel, row.sample_id) == (channel, sample_id)), None)


def unavailable_evidence(row, standard):
    if row is None:
        return CheckFinding(None, None, False, 'missing_sample')
    if row.value is None:
        return CheckFinding(row.position, None, False, 'missing_value')
    if row.report_unit != standard.report_unit:
        return CheckFinding(row.position, row.value, False, 'wrong_unit')
    return None


def within_closed_interval(value, lower, upper):
    return lower <= value <= upper

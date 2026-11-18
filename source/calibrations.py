from dataclasses import dataclass
from fractions import Fraction

from source.database import station_transaction
from source.registry import StationRegistry, require_name
from source.samples import encode_number, exact_number, optional_number


@dataclass(frozen=True)
class CalibrationRevision:
    calibration_id: str
    station: str
    channel: str
    input_unit: str
    report_unit: str
    scale: Fraction
    offset: Fraction
    reference_id: str | None = None
    error_radius: Fraction | None = None
    error_coefficient: Fraction | None = None

    def __post_init__(self):
        fields = (self.reference_id, self.error_radius, self.error_coefficient)
        if all(value is None for value in fields):
            return
        if any(value is None for value in fields):
            raise ValueError('incomplete reference error binding')
        require_name(self.reference_id)
        radius = exact_number(self.error_radius)
        if radius < 0:
            raise ValueError('reference error radius must be nonnegative')
        object.__setattr__(self, 'error_radius', radius)
        object.__setattr__(self, 'error_coefficient', exact_number(self.error_coefficient))


def normalize_reference_error(reference_error):
    if reference_error is None:
        return None
    if isinstance(reference_error, str):
        raise ValueError('reference error must be a (reference_id, radius, coefficient) triple')
    try:
        reference_id, radius, coefficient = reference_error
    except (TypeError, ValueError) as exc:
        raise ValueError('reference error must be a (reference_id, radius, coefficient) triple') from exc
    require_name(reference_id)
    radius = exact_number(radius)
    if radius < 0:
        raise ValueError('reference error radius must be nonnegative')
    return reference_id, radius, exact_number(coefficient)


def register_calibration(db, station, channel, calibration_id, scale, offset, *,
                         only_missing=False, reference_error=None):
    require_name(calibration_id)
    scale = exact_number(scale)
    offset = exact_number(offset)
    reference = normalize_reference_error(reference_error)
    with station_transaction(db):
        definition = StationRegistry(db).require_channel(station, channel)
        wanted = CalibrationRevision(
            calibration_id, station, channel, definition.input_unit, definition.report_unit,
            scale, offset, *(reference if reference is not None else (None, None, None)),
        )
        if reference is not None:
            reference_id, radius, _ = reference
            db.execute(
                'INSERT INTO reference_definitions VALUES (?, ?, ?) '
                'ON CONFLICT(station, reference_id) DO NOTHING',
                (station, reference_id, str(radius)),
            )
            bound = db.execute(
                'SELECT radius FROM reference_definitions WHERE station = ? AND reference_id = ?',
                (station, reference_id),
            ).fetchone()
            if exact_number(bound['radius']) != radius:
                raise ValueError(f'conflicting reference radius for {reference_id}')
        db.execute(
            'INSERT INTO calibrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(calibration_id) DO NOTHING',
            (calibration_id, station, channel, wanted.input_unit, wanted.report_unit,
             str(scale), str(offset), wanted.reference_id,
             encode_number(wanted.error_radius), encode_number(wanted.error_coefficient)),
        )
        actual = load_calibration(db, calibration_id)
        if actual != wanted and (not only_missing or (actual.station, actual.channel) != (station, channel)):
            raise ValueError(f'conflicting calibration {calibration_id}')
    return actual


def load_calibration(db, calibration_id):
    require_name(calibration_id)
    row = db.execute(
        'SELECT * FROM calibrations WHERE calibration_id = ?', (calibration_id,)
    ).fetchone()
    if row is None:
        raise KeyError(calibration_id)
    channel = StationRegistry(db).require_channel(row['station'], row['channel'])
    if (row['input_unit'], row['report_unit']) != (channel.input_unit, channel.report_unit):
        raise ValueError(f'invalid calibration units for {calibration_id}')
    return CalibrationRevision(
        calibration_id, channel.station, channel.channel, channel.input_unit, channel.report_unit,
        exact_number(row['scale']), exact_number(row['offset']),
        row['reference_id'], optional_number(row['error_radius']),
        optional_number(row['error_coefficient']),
    )

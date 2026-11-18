from fractions import Fraction

from inspection.standards import (
    CheckFinding, locate_evidence, locate_trial, unavailable_evidence, within_closed_interval,
)


def paired_span_interval(high, low):
    difference = high.value - low.value
    terms = {}
    for row, sign in ((high, 1), (low, -1)):
        if row.reference_id is None:
            continue
        coefficient, radius = terms.get(row.reference_id, (Fraction(0), row.error_radius))
        terms[row.reference_id] = (coefficient + sign * row.error_coefficient, radius)
    margin = sum((abs(coefficient) * radius for coefficient, radius in terms.values()), Fraction(0))
    return difference - margin, difference + margin


def check_paired_span(snapshot, standard):
    high = locate_trial(snapshot, (standard.channel, standard.sample_id))
    low = locate_trial(snapshot, standard.low_trial)
    position = None if high is None else high.position
    low_position = None if low is None else low.position
    for row in (high, low):
        unavailable = unavailable_evidence(row, standard)
        if unavailable is not None:
            return CheckFinding(position, None, False, unavailable.reason, low_position, None)
    interval = paired_span_interval(high, low)
    difference = high.value - low.value
    if all(within_closed_interval(endpoint, standard.lower, standard.upper) for endpoint in interval):
        return CheckFinding(position, difference, True, 'within_span', low_position, interval)
    reason = 'below_span' if interval[0] < standard.lower else 'above_span'
    return CheckFinding(position, difference, False, reason, low_position, interval)


def check_span(snapshot, standard):
    if standard.kind != 'span':
        raise ValueError('span check requires a span standard')
    if standard.low_trial is not None:
        return check_paired_span(snapshot, standard)
    row = locate_evidence(snapshot, standard)
    unavailable = unavailable_evidence(row, standard)
    if unavailable is not None:
        return unavailable
    error = row.value - standard.reference
    lower_error = standard.lower - standard.reference
    upper_error = standard.upper - standard.reference
    if within_closed_interval(error, lower_error, upper_error):
        return CheckFinding(row.position, row.value, True, 'within_span')
    reason = 'below_span' if error < lower_error else 'above_span'
    return CheckFinding(row.position, row.value, False, reason)

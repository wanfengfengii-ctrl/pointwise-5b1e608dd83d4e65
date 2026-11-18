from publication.eligibility import can_issue
from publication.evidence import load_passing_evidence
from publication.ledger import append_issue, load_issue_for_candidate


def issue_if_eligible(db, snapshot):
    if not db.in_transaction:
        raise RuntimeError('issue_if_eligible requires an active write transaction')
    signed = load_issue_for_candidate(db, snapshot.candidate_id)
    if signed is not None:
        return signed, False
    if not can_issue(db, snapshot):
        return None, False
    receipts = load_passing_evidence(db, snapshot.candidate_id)
    if receipts.keys() != {'zero', 'span'}:
        return None, False
    span = receipts['span']
    evidence = span if span.standard.low_trial is not None else None
    signed = append_issue(db, snapshot, receipts['zero'].receipt_id, span.receipt_id,
                          span_evidence=evidence)
    return signed, True

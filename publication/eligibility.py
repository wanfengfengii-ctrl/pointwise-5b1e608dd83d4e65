def initialize_eligibility(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS active_candidates (
            station TEXT NOT NULL,
            day TEXT NOT NULL,
            candidate_id TEXT NOT NULL UNIQUE REFERENCES received_candidates(candidate_id),
            PRIMARY KEY(station, day)
        );
        CREATE TABLE IF NOT EXISTS retired_candidates (
            candidate_id TEXT PRIMARY KEY REFERENCES received_candidates(candidate_id)
        );
    ''')


def find_active_candidate(db, station, day):
    row = db.execute(
        'SELECT candidate_id FROM active_candidates WHERE station = ? AND day = ?', (station, day)
    ).fetchone()
    return None if row is None else row['candidate_id']


def is_retired(db, candidate_id):
    return db.execute(
        'SELECT 1 FROM retired_candidates WHERE candidate_id = ?', (candidate_id,)
    ).fetchone() is not None


def admit_initial_candidate(db, snapshot):
    db.execute(
        'INSERT INTO active_candidates VALUES (?, ?, ?) ON CONFLICT(station, day) DO NOTHING',
        (snapshot.station, snapshot.day, snapshot.candidate_id),
    )


def replace_eligibility(db, snapshot):
    if is_retired(db, snapshot.candidate_id):
        raise ValueError('cannot select a retired candidate')
    previous = find_active_candidate(db, snapshot.station, snapshot.day)
    if previous is not None and previous != snapshot.candidate_id:
        db.execute('INSERT INTO retired_candidates VALUES (?)', (previous,))
    db.execute(
        'INSERT INTO active_candidates VALUES (?, ?, ?) '
        'ON CONFLICT(station, day) DO UPDATE SET candidate_id = excluded.candidate_id',
        (snapshot.station, snapshot.day, snapshot.candidate_id),
    )
    return previous


def can_issue(db, snapshot):
    return find_active_candidate(db, snapshot.station, snapshot.day) == snapshot.candidate_id

from datetime import date, timedelta

from sourcer import store


def test_upsert_creates_then_updates(conn, strong_analyst):
    cid, action = store.upsert(conn, strong_analyst)
    assert action == "created"
    strong_analyst.headline = "Lead Data Analyst"
    cid2, action2 = store.upsert(conn, strong_analyst)
    assert cid2 == cid and action2 == "updated"
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    assert store.load(conn, cid).headline == "Lead Data Analyst"


def test_children_are_replaced_not_duplicated(conn, strong_analyst):
    cid, _ = store.upsert(conn, strong_analyst)
    store.upsert(conn, strong_analyst)
    n = conn.execute("SELECT COUNT(*) FROM experiences WHERE candidate_id=?",
                     (cid,)).fetchone()[0]
    assert n == len(strong_analyst.experiences)


def test_derived_columns_are_persisted(conn, strong_analyst):
    cid, _ = store.upsert(conn, strong_analyst)
    r = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    assert r["location_city"] == "Clifton"
    assert r["current_title"] == "Data Analyst"
    assert r["current_company"] == "Habib Bank Limited"
    assert r["total_years"] > 7
    assert r["lawful_basis"]
    assert r["delete_after"] > date.today().isoformat()


def test_skills_are_stored_canonically(conn, strong_analyst):
    cid, _ = store.upsert(conn, strong_analyst)
    skills = {r["skill"] for r in conn.execute(
        "SELECT skill FROM candidate_skills WHERE candidate_id=?", (cid,))}
    assert {"sql", "power_bi", "excel", "statistics"} <= skills


def test_load_round_trip(conn, strong_analyst):
    cid, _ = store.upsert(conn, strong_analyst)
    back = store.load(conn, cid)
    assert back.full_name == strong_analyst.full_name
    assert len(back.experiences) == len(strong_analyst.experiences)
    assert back.education_level() == "bachelors"
    assert abs(back.total_years() - strong_analyst.total_years()) < 0.1


def test_existing_pdf_path_is_not_blanked(conn, strong_analyst):
    strong_analyst.profile_pdf_path = "/tmp/a.pdf"
    cid, _ = store.upsert(conn, strong_analyst)
    strong_analyst.profile_pdf_path = ""
    store.upsert(conn, strong_analyst)
    assert conn.execute("SELECT profile_pdf_path FROM candidates WHERE id=?",
                        (cid,)).fetchone()[0] == "/tmp/a.pdf"


def test_status_transitions_are_audited(conn, strong_analyst):
    cid, _ = store.upsert(conn, strong_analyst)
    store.set_status(conn, cid, "shortlisted", note="good fit")
    r = conn.execute("SELECT status, notes FROM candidates WHERE id=?", (cid,)).fetchone()
    assert r["status"] == "shortlisted" and r["notes"] == "good fit"
    actions = [a["action"] for a in conn.execute("SELECT action FROM audit")]
    assert "candidate.status" in actions


def test_retention_purge_deletes_expired_but_keeps_people_in_process(conn,
                                                                     strong_analyst,
                                                                     weak_candidate):
    keep_id, _ = store.upsert(conn, strong_analyst)
    drop_id, _ = store.upsert(conn, weak_candidate)
    past = (date.today() - timedelta(days=1)).isoformat()
    conn.execute("UPDATE candidates SET delete_after=?", (past,))
    store.set_status(conn, keep_id, "shortlisted")

    assert store.purge_expired(conn) == 1
    remaining = {r["id"] for r in conn.execute("SELECT id FROM candidates")}
    assert remaining == {keep_id}
    assert drop_id not in remaining


def test_purge_cascades_to_child_rows(conn, weak_candidate):
    cid, _ = store.upsert(conn, weak_candidate)
    conn.execute("UPDATE candidates SET delete_after=?",
                 ((date.today() - timedelta(days=1)).isoformat(),))
    store.purge_expired(conn)
    for table in ("experiences", "educations", "candidate_skills"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE candidate_id=?",
                            (cid,)).fetchone()[0] == 0

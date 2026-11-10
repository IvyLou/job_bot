"""Job-search dashboard.

Open http://localhost:8501 once `python -m jobsearch install-schedule` has run, or start it by hand with
`python -m jobsearch dashboard`.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from typing import Optional, Tuple
from urllib.parse import quote

import altair as alt
import pandas as pd
import streamlit as st

from jobsearch import config, crm, db, gmail, targeting
from jobsearch.crm import STAGES

st.set_page_config(page_title="Job search", page_icon=":material/work:", layout="wide")

# Chart styling: the dataviz reference palette, light theme. SERIES is its categorical order (fixed, never cycled).
SURFACE, SECONDARY, MUTED, GRID, AXIS = "#fcfcfb", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
ACTIVITY = {"sent": "Outreach", "followup": "Follow-ups", "reply": "Replies", "application": "Applications",
            "call": "Calls & interviews", "interview": "Calls & interviews"}
ACTIVITY_ORDER = ["Outreach", "Follow-ups", "Replies", "Applications", "Calls & interviews"]
EMAIL_BASIS = {"published": "published on their site", "given": "confirmed", "pattern": "company's email format",
               "guess": "guessed, verify first", "bounced": "BOUNCED, use LinkedIn"}
LOC = {tag: loc.get("label", tag) for tag, loc in config.locations().items()}  # from search.yaml, in its order


def connect() -> sqlite3.Connection:
    return db.connect()


def flash(message: str) -> None:
    st.session_state["flash"] = message
    st.rerun()


def gmail_compose(to: str, subject: str, body: str) -> str:
    return (f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(to)}&su={quote(subject)}&body={quote(body)}")


def linkedin_search(company: str) -> str:
    words = crm.settings().get("linkedin_people_search") or "founder"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(f'{company} {words}')}"


def style(chart: alt.TopLevelMixin) -> alt.TopLevelMixin:
    return (chart.configure(background=SURFACE, font=FONT).configure_view(strokeWidth=0)
            .configure_axis(labelColor=MUTED, titleColor=SECONDARY, gridColor=GRID, domainColor=AXIS, tickColor=AXIS,
                            labelFontSize=12, titleFontSize=12, titleFontWeight="normal")
            .configure_legend(labelColor=SECONDARY, labelFontSize=12, symbolType="square", orient="top", title=None))


# --- shared pieces --------------------------------------------------------------------------------------------

def inbox_person(conn: sqlite3.Connection, company_id: int, inbox: str) -> int:
    """Treat a shared inbox (careers@, hello@ ...) as a contact so follow-ups and Gmail matching work for it."""
    name = f"{inbox.split('@')[0].capitalize()} inbox"
    db.upsert_person(conn, company_id, name, "site", email=inbox, email_confidence="published", priority=3,
                     role_kind="inbox")
    conn.commit()
    return conn.execute("SELECT id FROM people WHERE company_id = ? AND full_name = ?", (company_id, name)).fetchone()[0]


def edit_person(conn: sqlite3.Connection, *, key: str, person_id: int) -> None:
    """Fix a contact's name, title, email or LinkedIn. Messages already logged to them stay with them."""
    p = conn.execute("SELECT company_id, full_name, title, email, email_confidence, linkedin_url FROM people "
                     "WHERE id = ?", (person_id,)).fetchone()
    with st.form(f"{key}-edit-{person_id}"):
        e1, e2 = st.columns(2)
        name, title = e1.text_input("Name", p["full_name"]), e2.text_input("Title", p["title"] or "")
        email, linkedin = e1.text_input("Email", p["email"] or ""), e2.text_input("LinkedIn URL", p["linkedin_url"] or "")
        if not st.form_submit_button("Save"):
            return
    name, email = name.strip(), email.strip()
    if not name:
        st.toast("The name can't be empty.")
        return
    if conn.execute("SELECT 1 FROM people WHERE company_id = ? AND full_name = ? AND id != ?",
                    (p["company_id"], name, person_id)).fetchone():
        st.toast(f"There's already a contact named {name} at this company.")
        return
    confidence = p["email_confidence"] if email == (p["email"] or "") else ("given" if email else None)
    conn.execute("UPDATE people SET full_name = ?, title = ?, email = ?, email_confidence = ?, linkedin_url = ? "
                 "WHERE id = ?", (name, title.strip() or None, email or None, confidence,
                                  linkedin.strip() or None, person_id))
    conn.commit()
    flash(f"Saved {name}")


def md(text: Optional[str], limit: int = 170) -> str:
    """Posting text, shortened and safe to show as markdown ($ would start a formula)."""
    text = " ".join((text or "").split())
    text = text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"
    for ch in "\\$*_`[]#<>":
        text = text.replace(ch, "\\" + ch)
    return text


def apply_step(conn: sqlite3.Connection, *, key: str, company_id: int, company: str,
               job_id: Optional[int] = None) -> Tuple[Optional[int], Optional[str]]:
    """Step 1: the company's matching open roles, each with its posting and an application log.

    Returns (job id, title) of the role the message should mention, or (None, None) when nothing matching is open.
    """
    roles = conn.execute("""
        SELECT j.id, j.title, j.location, j.url, j.posted_at, j.years_label, j.years_quote, j.background_quote, j.priority_match,
               (SELECT MIN(happened_on) FROM activities a WHERE a.job_id = j.id AND a.kind = 'application') AS applied_on
        FROM jobs j WHERE j.company_id = ? AND j.is_open = 1 AND j.relevance > 0
        ORDER BY j.id = ? DESC, j.priority_match DESC, j.relevance DESC LIMIT 5""", (company_id, job_id or -1)).fetchall()
    if not roles:
        return None, None
    st.markdown("**Step 1 · Apply**")
    for r in roles:
        c1, c2, c3 = st.columns([5, 1.4, 1.6], vertical_alignment="center")
        details = " · ".join(x for x in (r["location"], f"asks for {r['years_label']}" if r["years_label"] else None,
                                          f"posted {r['posted_at']}" if r["posted_at"] else None) if x)
        c1.markdown(f"{'★ ' if r['priority_match'] else ''}{r['title']}  \n:gray[{details}]")
        if r["priority_match"]:
            c1.caption(f"Priority match. Asks for: “{md(r['years_quote'])}”  \nBackground: “{md(r['background_quote'])}”")
        if r["url"]:
            c2.link_button("Open posting", r["url"])
        if r["applied_on"]:
            c3.markdown(f"Applied {r['applied_on']}")
        elif c3.button("Log application", key=f"{key}-apply-{r['id']}"):
            crm.log(conn, company_id, "application", job_id=r["id"], channel="application")
            flash(f"Application to {company} logged. Now reach out below")
    chosen = roles[0]
    if len(roles) > 1:
        pick = st.selectbox("Role to mention in your message", [r["id"] for r in roles], key=f"{key}-role",
                            format_func=lambda i: next(r["title"] for r in roles if r["id"] == i))
        chosen = next(r for r in roles if r["id"] == pick)
    st.markdown("**Step 2 · Reach out**")
    return chosen["id"], chosen["title"]


def choose_contact(conn: sqlite3.Connection, *, key: str, company_id: int, company: str, job_id: Optional[int],
                   job_title: Optional[str]) -> Tuple[Optional[dict], Optional[targeting.Target]]:
    """Who to write to for this company and role, by company size. Lets you add the person when we don't know them."""
    targets = targeting.targets(conn, company_id, job_id)
    co = conn.execute("SELECT team_size, sources FROM companies WHERE id = ?", (company_id,)).fetchone()
    st.caption(targeting.BAND_ADVICE[targeting.size_band(co["team_size"], co["sources"])])
    labels = [f"{t.label}: {t.people[0]['full_name']}" if t.people else f"{t.label}: not found yet" for t in targets]
    target = targets[st.radio("Write to", range(len(targets)), format_func=labels.__getitem__, horizontal=True,
                              key=f"{key}-who", label_visibility="collapsed")] if len(targets) > 1 else targets[0]
    if target.people:
        if len(target.people) == 1:
            return target.people[0], target
        pid = st.selectbox(target.label, [p["id"] for p in target.people], key=f"{key}-person-{target.kind}",
                           format_func=lambda i: next(f"{p['full_name']} ({p['title'] or p['role_kind'] or 'contact'})"
                                                      for p in target.people if p["id"] == i))
        return next(p for p in target.people if p["id"] == pid), target
    st.markdown(f"Look for **{target.look_for}**.")
    links = st.columns(2)
    if job_title:
        links[0].link_button("See who posted it on LinkedIn", targeting.linkedin_job_search(job_title, company))
    links[1].link_button(f"Find the {target.label.lower()} on LinkedIn", target.linkedin_search)
    with st.form(f"{key}-add-{target.kind}", clear_on_submit=True):
        a1, a2, a3 = st.columns(3)
        name, title, linkedin = a1.text_input("Name"), a2.text_input("Title"), a3.text_input("LinkedIn URL")
        email = st.text_input("Email (optional)")
        if st.form_submit_button(f"Add as {target.label.lower()}") and name.strip():
            kind = target.kind if target.kind in ("ceo", "leader", "hiring_manager", "recruiter") else "contact"
            db.upsert_person(conn, company_id, name.strip(), "manual", title=title or target.label,
                             linkedin_url=linkedin or None, email=email or None,
                             email_confidence="given" if email else None, priority=1, role_kind=kind)
            conn.commit()
            flash(f"Added {name.strip()}")
    return None, target


def outreach_panel(conn: sqlite3.Connection, *, key: str, company_id: int, company: str,
                   person: Optional[dict] = None, template: Optional[str] = None, role: Optional[str] = None,
                   inbox: Optional[str] = None, follow_up: bool = False, job_id: Optional[int] = None) -> None:
    """Apply (when the company has matching roles), then reach out to the right person for the company's size."""
    job_id, job_title = apply_step(conn, key=key, company_id=company_id, company=company, job_id=job_id)
    role = job_title or role
    target = None
    if person is None:  # follow-ups pass the person; everywhere else, pick by company size and role
        person, target = choose_contact(conn, key=key, company_id=company_id, company=company, job_id=job_id,
                                        job_title=job_title)
    if person and person.get("role_kind") != "inbox":
        with st.popover(f"Edit {person['full_name']}"):
            edit_person(conn, key=key, person_id=person["id"])
    available, pitches = crm.templates(), crm.pitches()
    names = list(available)
    template = template or (target.template if target else "founder_outreach")
    suggested = crm.default_pitch(conn, company_id, job_id)
    t1, t2 = st.columns(2)
    template = t1.selectbox("Template", names, index=names.index(template), key=f"{key}-tpl-{template}",
                            format_func=lambda k: available[k]["label"])
    pitch = t2.selectbox("Numbers to lead with", list(pitches), index=list(pitches).index(suggested),
                         key=f"{key}-pitch-{suggested}", format_func=lambda k: pitches[k]["label"])
    subject, body, note = crm.render(template, person_name=person["full_name"] if person else None, company=company,
                                     role=role, pitch=pitch)
    email = (person or {}).get("email") or (None if target and target.kind in ("hiring_manager", "recruiter") else inbox)
    email_tab, linkedin_tab = st.tabs(["Email", "LinkedIn note"])
    with email_tab:
        if email:
            basis = EMAIL_BASIS.get((person or {}).get("email_confidence") or "published", "")
            st.caption(f"To: {email} ({basis})")
        elif not person:
            st.caption("Add the person above to address this to them.")
        st.code(subject, language=None)
        st.code(body, language=None, wrap_lines=True)
        if "[" in subject + body:
            st.caption(":orange[Fill in the part in brackets before sending.]")
        if email:
            st.link_button("Open in Gmail", gmail_compose(email, subject, body))
    with linkedin_tab:
        st.code(note, language=None, wrap_lines=True)
        if "[" in note:
            st.caption(":orange[Fill in the part in brackets before sending.]")
        over = " :orange[over the free-account limit]" if len(note) > 200 else ""
        st.caption(f"{len(note)} characters (connection notes: 200 on a free account, 300 with Premium){over}")
        if person and person.get("linkedin_url"):
            st.link_button("Open LinkedIn profile", person["linkedin_url"])
        else:
            st.link_button("Search LinkedIn", target.linkedin_search if target else linkedin_search(company))

    def who() -> Optional[int]:
        if person:
            return person["id"]
        return inbox_person(conn, company_id, inbox) if inbox else None

    cols = st.columns(4)
    if cols[0].button("Log email sent", key=f"{key}-email", disabled=not email):
        pid = who()
        crm.log(conn, company_id, crm.next_outbound_kind(conn, pid) if pid else "sent", person_id=pid, channel="email",
                template=template)
        flash(f"Logged email to {company}")
    if cols[1].button("Log LinkedIn sent", key=f"{key}-li"):
        pid = who()
        crm.log(conn, company_id, crm.next_outbound_kind(conn, pid) if pid else "sent", person_id=pid,
                channel="linkedin", template=template)
        flash(f"Logged LinkedIn message to {company}")
    if cols[2].button("They replied", key=f"{key}-reply"):
        crm.log(conn, company_id, "reply", person_id=who(), channel="email" if email else "linkedin")
        flash(f"{company} moved to In conversation")
    if follow_up:
        if cols[3].button("Stop following up", key=f"{key}-stop"):
            crm.log(conn, company_id, "stop", person_id=who())
            flash("No more follow-up reminders for this person")
    elif cols[3].button("Skip company", key=f"{key}-skip"):
        crm.set_stage(conn, company_id, "Closed", notes="Skipped: not a fit")
        conn.commit()
        flash(f"Skipped {company}")


# --- pages ----------------------------------------------------------------------------------------------------

def page_today() -> None:
    conn = connect()
    settings = crm.settings()
    targets, counts = settings["targets"], crm.today_counts(conn)
    st.title("Today")
    tiles = st.columns(4)
    for col, (k, label) in zip(tiles, [("new_outreach", "New outreach"), ("follow_ups", "Follow-ups"),
                                       ("applications", "Applications")]):
        col.metric(label, f"{counts[k]} / {targets[k]}")
        col.progress(min(counts[k] / targets[k], 1.0))
    countdown = settings.get("countdown") or {}
    if countdown.get("date"):
        tiles[3].metric(countdown.get("label") or "Days to go",
                        (date.fromisoformat(str(countdown["date"])) - date.today()).days)
    else:
        week = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        replies = conn.execute("SELECT COUNT(*) FROM activities WHERE kind = 'reply' AND happened_on >= ?",
                               (week,)).fetchone()[0]
        tiles[3].metric("Replies this week", replies)

    due = crm.followups_due(conn)
    st.subheader(f"Follow-ups due ({len(due)})")
    if not due:
        st.caption("Nothing due. Follow-ups appear here 4 and 10 days after you reach out, until someone replies.")
    for d in due[:30]:
        with st.expander(f"{d['full_name']} · {d['company']}: follow-up {d['sent']} "
                         f"(last {d['channel'] or 'message'} {d['last_on']})"):
            outreach_panel(conn, key=f"fu{d['person_id']}", company_id=d["company_id"], company=d["company"],
                           person={**d, "id": d["person_id"]}, template=d["template"], role=None, follow_up=True)

    matches = crm.priority_matches(conn)
    st.subheader(f"Priority matches ({len(matches)})")
    st.caption(f"{config.search()['priority_match']['description']} Each box quotes the lines it matched. A company "
               "stays here until you've applied and reached out.")
    for r in matches:
        title = f"★ {r['name']} · {r['title']} · {LOC.get(r['location_tag'], '?')} · {r['years_label']}"
        if r["n_roles"] > 1:
            title += f" · +{r['n_roles'] - 1} more"
        with st.expander(title):
            outreach_panel(conn, key=f"pm{r['id']}", company_id=r["id"], company=r["name"], inbox=r["contact_email"],
                           job_id=r["job_id"])

    st.subheader("Next up")
    st.caption("Not reached out to yet (applying doesn't count), best fit first. Each has a suggested message; "
               "change the template if it fits better.")
    shown = st.session_state.setdefault("next_up_n", 15)
    for r in crm.next_up(conn, shown):
        title = f"Tier {r['tier']} · {r['name']} · {LOC.get(r['location_tag'], '?')}"
        if r["top_role"]:
            title += f" · hiring: {r['top_role']}"
        with st.expander(title):
            facts = [r["description"][:220] if r["description"] else None,
                     f"Investors: {r['investors']}" if r["investors"] else None]
            st.write("  \n".join(f for f in facts if f))
            if r["website"]:
                st.caption(r["website"])
            outreach_panel(conn, key=f"nu{r['id']}", company_id=r["id"], company=r["name"], inbox=r["contact_email"])
            if st.button("Open company page", key=f"nu{r['id']}-open"):
                st.session_state["company_id"] = r["id"]
                st.switch_page(PAGES["company"])
    if st.button("Show more"):
        st.session_state["next_up_n"] = shown + 15
        st.rerun()

    st.subheader("New since yesterday")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    new_roles = pd.read_sql("""SELECT c.name AS Company, j.title AS Role, j.location AS Location, j.url AS Posting
                               FROM jobs j JOIN companies c ON c.id = j.company_id
                               WHERE j.first_seen >= ? AND j.is_open = 1 AND j.relevance > 0
                               ORDER BY j.relevance DESC""", conn, params=(yesterday,))
    new_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE first_seen >= ?", (yesterday,)).fetchone()[0]
    st.caption(f"{new_companies} new companies and {len(new_roles)} new matching roles.")
    if len(new_roles):
        st.dataframe(new_roles, hide_index=True, column_config={"Posting": st.column_config.LinkColumn(display_text="Open")})


def page_pipeline() -> None:
    conn = connect()
    st.title("Pipeline")
    stage_counts = dict(conn.execute("SELECT stage, COUNT(*) FROM pipeline GROUP BY stage").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    stage_counts["Not started"] = total - sum(stage_counts.values())
    for col, stage in zip(st.columns(len(STAGES)), STAGES):
        col.metric(stage, stage_counts.get(stage, 0))

    f1, f2, f3, f4 = st.columns([3, 1, 1, 2])
    stages = f1.multiselect("Stage", STAGES, default=[s for s in STAGES if s not in ("Not started", "Closed")])
    tiers = f2.multiselect("Tier", [1, 2, 3], default=[1, 2, 3])
    locs = f3.multiselect("Location", list(LOC), default=list(LOC), format_func=LOC.get)
    search = f4.text_input("Search name, category or investor")
    df = pd.read_sql("""
        SELECT c.id, c.name AS Company, COALESCE(pl.stage, 'Not started') AS Stage, c.tier AS Tier,
               c.location_tag AS Loc, c.lanes AS Categories, c.investors AS Investors,
               (SELECT MAX(happened_on) FROM activities a WHERE a.company_id = c.id) AS "Last activity",
               (SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1 AND j.relevance > 0) AS "Open roles",
               pl.notes AS Notes, c.website AS Website
        FROM companies c LEFT JOIN pipeline pl ON pl.company_id = c.id""", conn)
    df = df[df["Stage"].isin(stages) & df["Tier"].isin(tiers) & df["Loc"].isin(locs)]
    if search:
        hay = (df["Company"] + " " + df["Categories"].fillna("") + " " + df["Investors"].fillna("")).str.lower()
        df = df[hay.str.contains(search.lower(), regex=False)]
    df = df.sort_values(["Last activity", "Tier"], ascending=[False, True], na_position="last")
    st.caption(f"{len(df)} companies. Select a row to open it.")
    event = st.dataframe(df.drop(columns=["id"]), hide_index=True, on_select="rerun", selection_mode="single-row",
                         column_config={"Website": st.column_config.LinkColumn(display_text="Site"),
                                        "Loc": st.column_config.TextColumn(width="small")})
    if event.selection.rows:
        st.session_state["company_id"] = int(df.iloc[event.selection.rows[0]]["id"])
        st.switch_page(PAGES["company"])


def page_company() -> None:
    conn = connect()
    companies = pd.read_sql("SELECT id, name, tier, location_tag FROM companies ORDER BY name", conn)
    ids = companies["id"].tolist()
    labels = {r.id: f"{r.name} (Tier {r.tier}, {LOC.get(r.location_tag, '?')})" for r in companies.itertuples()}
    current = st.session_state.get("company_id")
    cid = st.selectbox("Company", ids, index=ids.index(current) if current in ids else 0, format_func=labels.get)
    st.session_state["company_id"] = cid
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()

    st.title(co["name"])
    facts = [f"Tier {co['tier']}", LOC.get(co["location_tag"], "location unknown"), co["stage"], co["industry"],
             f"categories: {co['lanes']}" if co["lanes"] else None, f"investors: {co['investors']}" if co["investors"] else None,
             f"last raise ${co['last_raise_usd'] / 1e6:.1f}M ({co['last_raise_date']})" if co["last_raise_usd"] else None]
    st.caption(" · ".join(str(f) for f in facts if f))
    if co["description"]:
        st.write(co["description"])
    links = st.columns(3)
    if co["website"]:
        links[0].link_button("Website", co["website"])
    links[1].link_button("Find people on LinkedIn", linkedin_search(co["name"]))
    if co["contact_email"]:
        links[2].caption(f"Shared inbox: {co['contact_email']}")

    row = conn.execute("SELECT stage, notes FROM pipeline WHERE company_id = ?", (cid,)).fetchone()
    with st.form("stage"):
        s1, s2 = st.columns([1, 3])
        stage = s1.selectbox("Stage", STAGES, index=STAGES.index(row["stage"]) if row else 0)
        notes = s2.text_area("Notes", value=(row["notes"] if row else "") or "", height=80)
        if st.form_submit_button("Save"):
            crm.set_stage(conn, cid, stage, notes)
            conn.commit()
            flash("Saved")

    st.subheader("Apply and reach out")
    people = conn.execute("""SELECT id, full_name, COALESCE(title, role_kind) AS role, priority, email, email_confidence,
                                    linkedin_url, warmth, source FROM people WHERE company_id = ?
                             ORDER BY priority, full_name""", (cid,)).fetchall()
    if people:
        st.dataframe(pd.DataFrame([{"Name": p["full_name"], "Role": p["role"], "Email": p["email"],
                                    "Email basis": p["email_confidence"], "LinkedIn": p["linkedin_url"],
                                    "Warm": "yes" if p["warmth"] == "warm" else ""} for p in people]),
                     hide_index=True, column_config={"LinkedIn": st.column_config.LinkColumn(display_text="Profile")})
    with st.container(border=True):
        outreach_panel(conn, key=f"co{cid}", company_id=cid, company=co["name"], inbox=co["contact_email"])
    with st.expander("Add a contact"):
        with st.form("add_contact", clear_on_submit=True):
            a1, a2 = st.columns(2)
            name, title = a1.text_input("Name"), a2.text_input("Title")
            email, linkedin = a1.text_input("Email"), a2.text_input("LinkedIn URL")
            warm = st.checkbox("I know this person (warm)")
            if st.form_submit_button("Add") and name.strip():
                db.upsert_person(conn, cid, name.strip(), "manual", title=title or None, email=email or None,
                                 email_confidence="given" if email else None, linkedin_url=linkedin or None,
                                 priority=0 if warm else 1, role_kind="contact")
                if warm:
                    conn.execute("UPDATE people SET warmth = 'warm' WHERE company_id = ? AND full_name = ?",
                                 (cid, name.strip()))
                conn.commit()
                flash(f"Added {name}")
    if people:
        with st.expander("Edit a contact"):
            pid = st.selectbox("Contact", [p["id"] for p in people], key=f"cp{cid}-pick",
                               format_func=lambda i: next(f"{p['full_name']} ({p['role'] or 'contact'})"
                                                          for p in people if p["id"] == i))
            edit_person(conn, key=f"cp{cid}", person_id=pid)

    all_roles = pd.read_sql("""SELECT j.title AS Role, j.location AS Location, j.relevance AS Fit, j.url AS Posting
                               FROM jobs j WHERE j.company_id = ? AND j.is_open = 1 AND j.relevance > 0
                               ORDER BY j.relevance DESC""", conn, params=(cid,))
    if len(all_roles) > 5:
        with st.expander(f"All {len(all_roles)} matching open roles"):
            st.dataframe(all_roles, hide_index=True,
                         column_config={"Posting": st.column_config.LinkColumn(display_text="Open")})

    st.subheader("Activity")
    with st.form("log_other", clear_on_submit=True):
        l1, l2, l3 = st.columns([1, 1, 2])
        kind = l1.selectbox("What happened", ["call", "interview", "reply", "note"],
                            format_func=lambda k: {"call": "Call / coffee chat", "interview": "Interview",
                                                   "reply": "Reply", "note": "Note"}[k])
        when = l2.date_input("When", value=date.today())
        note = l3.text_input("Details")
        if st.form_submit_button("Log"):
            crm.log(conn, cid, kind, note=note or None, happened_on=when.isoformat())
            flash("Logged")
    history = pd.read_sql("""SELECT a.happened_on AS Date, a.kind AS What, a.channel AS Channel, p.full_name AS Person,
                                    a.template AS Template, a.note AS Details, a.source AS Source
                             FROM activities a LEFT JOIN people p ON p.id = a.person_id
                             WHERE a.company_id = ? ORDER BY a.happened_on DESC, a.id DESC""", conn, params=(cid,))
    if len(history):
        st.dataframe(history, hide_index=True)


def page_roles() -> None:
    conn = connect()
    st.title("Open roles")
    df = pd.read_sql("""
        SELECT j.id, c.id AS company_id, CASE WHEN j.priority_match = 1 THEN '★' ELSE '' END AS Priority,
               j.relevance AS Fit, c.tier AS Tier, c.name AS Company, j.title AS Role,
               j.family AS Family, j.years_label AS Experience, j.location AS Location, j.location_tag AS Loc,
               j.first_seen AS "First seen",
               CASE WHEN EXISTS (SELECT 1 FROM activities a WHERE a.job_id = j.id AND a.kind = 'application')
                    THEN 'yes' ELSE '' END AS Applied, j.url AS Posting
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.is_open = 1 AND j.relevance > 0
        ORDER BY j.priority_match DESC, j.relevance DESC, j.first_seen DESC""", conn)
    f1, f2, f3, f4, f5 = st.columns(5)
    locs = f1.multiselect("Location", list(LOC), default=list(LOC), format_func=LOC.get)
    fams = f2.multiselect("Role type", sorted(df["Family"].dropna().unique()))
    min_fit = f3.slider("Minimum fit", 0, 100, 60)
    hide_applied = f4.toggle("Hide applied", value=True)
    priority_only = f5.toggle("Priority matches only", help=config.search()["priority_match"]["description"])
    df = df[df["Loc"].isin(locs) & (df["Fit"] >= min_fit)]
    if priority_only:
        df = df[df["Priority"] == "★"]
    if fams:
        df = df[df["Family"].isin(fams)]
    if hide_applied:
        df = df[df["Applied"] != "yes"]
    st.caption(f"{len(df)} roles. Select one to log an application or open the company.")
    ids = df["id"].tolist()

    def remember_pick() -> None:
        # Streamlit clears a table's selection whenever its rows change (logging an application does), so the box
        # follows the role picked here rather than the table's current selection.
        rows = st.session_state["roles_table"]["selection"]["rows"]
        st.session_state["roles_job"] = int(ids[rows[0]]) if rows else None

    st.dataframe(df.drop(columns=["id", "company_id", "Loc"]), hide_index=True, on_select=remember_pick,
                 key="roles_table", selection_mode="single-row",
                 column_config={"Posting": st.column_config.LinkColumn(display_text="Open"),
                                "Fit": st.column_config.NumberColumn(format="%.0f")})
    job = st.session_state.get("roles_job")
    picked = conn.execute("""SELECT c.id, c.name, c.contact_email FROM jobs j JOIN companies c ON c.id = j.company_id
                             WHERE j.id = ?""", (job,)).fetchone() if job else None
    if picked:
        with st.container(border=True):
            st.subheader(picked["name"])
            outreach_panel(conn, key=f"rl{job}", company_id=picked["id"], company=picked["name"],
                           inbox=picked["contact_email"], job_id=job)
            b1, b2 = st.columns(2)
            if b1.button(f"Open {picked['name']} company page", key=f"rl{job}-open"):
                st.session_state["company_id"] = picked["id"]
                st.switch_page(PAGES["company"])
            if b2.button("Close", key=f"rl{job}-close"):
                st.session_state["roles_job"] = None
                st.rerun()


def page_results() -> None:
    conn = connect()
    st.title("Results")
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    contacted = q("SELECT COUNT(DISTINCT person_id) FROM activities WHERE kind = 'sent'")
    replied = q("""SELECT COUNT(DISTINCT person_id) FROM activities WHERE kind = 'reply'
                   AND person_id IN (SELECT person_id FROM activities WHERE kind = 'sent')""")
    tiles = st.columns(5)
    tiles[0].metric("Companies reached", q("SELECT COUNT(*) FROM pipeline WHERE max_stage != 'Not started'"))
    tiles[1].metric("People contacted", contacted)
    tiles[2].metric("Reply rate", f"{replied / contacted:.0%}" if contacted else "–")
    tiles[3].metric("Applications", q("SELECT COUNT(*) FROM activities WHERE kind = 'application'"))
    tiles[4].metric("Interviews", q("SELECT COUNT(DISTINCT company_id) FROM activities WHERE kind = 'interview'"))
    if not q("SELECT COUNT(*) FROM activities"):
        st.info("Log your first outreach on the Today page. Your funnel, weekly activity and reply rates show up here.")
        return

    st.subheader("Funnel: companies reaching each stage")
    funnel = pd.DataFrame(crm.funnel(conn), columns=["Stage", "Companies"])
    reached = funnel["Companies"].iloc[0] or 1
    funnel["Of reached"] = funnel["Companies"] / reached
    funnel["Label"] = funnel.apply(lambda r: f"{r.Companies}  ({r['Of reached']:.0%})", axis=1)
    base = alt.Chart(funnel).encode(
        y=alt.Y("Stage:N", sort=list(funnel["Stage"]), title=None),
        x=alt.X("Companies:Q", title="Companies"),
        tooltip=["Stage", "Companies", alt.Tooltip("Of reached:Q", format=".0%", title="Share of companies reached")])
    chart = base.mark_bar(color=SERIES[0], cornerRadiusEnd=4, size=20) + base.mark_text(
        align="left", dx=6, color=SECONDARY, fontSize=12).encode(text="Label:N")
    st.altair_chart(style(chart.properties(height=44 * len(funnel))), theme=None, use_container_width=True)

    st.subheader("Weekly activity")
    weekly = pd.DataFrame([dict(r) for r in crm.weekly_activity(conn)])
    if len(weekly):
        weekly["Activity"] = weekly["kind"].map(ACTIVITY)
        weekly = weekly.groupby(["week", "Activity"], as_index=False)["n"].sum()
        weekly["Week of"] = pd.to_datetime(weekly["week"]).dt.strftime("%b %d")
        weekly["order"] = weekly["Activity"].map(ACTIVITY_ORDER.index)
        chart = alt.Chart(weekly).mark_bar(stroke=SURFACE, strokeWidth=2, size=26).encode(
            x=alt.X("Week of:O", sort=list(dict.fromkeys(weekly.sort_values("week")["Week of"])), title=None,
                    axis=alt.Axis(labelAngle=0)),
            y=alt.Y("sum(n):Q", title="Activities"),
            color=alt.Color("Activity:N", scale=alt.Scale(domain=ACTIVITY_ORDER, range=SERIES)),
            order=alt.Order("order:Q"),
            tooltip=["Week of", "Activity", alt.Tooltip("n:Q", title="Count")])
        st.altair_chart(style(chart.properties(height=280)), theme=None, use_container_width=True)
        with st.expander("Weekly activity as a table"):
            st.dataframe(weekly.pivot_table(index="Week of", columns="Activity", values="n", aggfunc="sum", fill_value=0),
                         use_container_width=True)

    st.subheader("Reply rate")
    by = st.radio("Split by", ["channel", "template", "tier", "lane", "source"], horizontal=True,
                  format_func={"channel": "Channel", "template": "Template", "tier": "Tier", "lane": "Category",
                               "source": "Where the company came from"}.get)
    rates = pd.DataFrame([dict(r) for r in crm.reply_rates(conn, by)])
    if len(rates):
        rates["grp"] = rates["grp"].astype(str)
        rates["Reply rate"] = rates["replied"] / rates["contacted"]
        rates["Label"] = rates.apply(lambda r: f"{r.replied} of {r.contacted}  ({r['Reply rate']:.0%})", axis=1)
        base = alt.Chart(rates).encode(
            y=alt.Y("grp:N", sort="-x", title=None), x=alt.X("Reply rate:Q", axis=alt.Axis(format="%"), title="Reply rate"),
            tooltip=[alt.Tooltip("grp:N", title=by.capitalize()), "contacted", "replied",
                     alt.Tooltip("Reply rate:Q", format=".0%")])
        chart = base.mark_bar(color=SERIES[0], cornerRadiusEnd=4, size=18) + base.mark_text(
            align="left", dx=6, color=SECONDARY, fontSize=12).encode(text="Label:N")
        st.altair_chart(style(chart.properties(height=max(120, 36 * len(rates)))), theme=None, use_container_width=True)
        st.caption("Only people you've contacted count; small groups swing a lot, so read rates next to their counts.")


PAGES = {
    "today": st.Page(page_today, title="Today", icon=":material/today:", default=True),
    "pipeline": st.Page(page_pipeline, title="Pipeline", icon=":material/view_kanban:"),
    "company": st.Page(page_company, title="Company", icon=":material/apartment:"),
    "roles": st.Page(page_roles, title="Open roles", icon=":material/work:"),
    "results": st.Page(page_results, title="Results", icon=":material/insights:"),
}


def sidebar() -> None:
    conn = connect()
    with st.sidebar:
        st.caption(f"Data last refreshed: {db.get_meta(conn, 'last_daily_run') or 'not yet'}")
        running = subprocess.run(["pgrep", "-f", "jobsearch daily"], capture_output=True).returncode == 0
        if running:
            st.caption("A refresh is running now.")
        elif st.button("Refresh data now"):
            log = open(config.DATA_DIR / "logs" / "daily.log", "a")
            subprocess.Popen([sys.executable, "-m", "jobsearch", "daily"], cwd=config.ROOT, stdout=log, stderr=log,
                             start_new_session=True)
            flash("Refresh started. It takes 10–30 minutes; this page updates as data lands.")
        if gmail.connected():
            st.caption(f"Gmail: connected, synced through {db.get_meta(conn, 'gmail_synced_through') or 'never'}")
            if st.button("Sync Gmail now"):
                stats = gmail.sync(conn)
                flash(f"Gmail: {stats.get('sent', 0)} sent, {stats.get('replies', 0)} replies, "
                      f"{stats.get('bounces', 0)} bounces logged")
        else:
            st.caption("Gmail: not connected (run `python -m jobsearch gmail-connect`)")


(config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))
sidebar()
st.navigation(list(PAGES.values())).run()

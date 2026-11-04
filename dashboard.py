"""Streamlit admin console. Run: streamlit run dashboard.py"""

import os

import streamlit as st

from dashboard_client import BackendClient, BackendError

LABELS = {
    "token_bucket": "Token bucket",
    "fixed_window": "Fixed time slot",
    "sliding_window": "Rolling timestamp queue",
}
DESCRIPTIONS = {
    "token_bucket": "Spend one token per request. Tokens refill continuously, allowing short bursts.",
    "fixed_window": "Count requests in aligned time slots. The allowance resets at each boundary.",
    "sliding_window": "Keep admitted timestamps in a queue and expire them after the rolling duration.",
}

st.set_page_config(page_title="Rate Limit Console", page_icon="🚦", layout="wide")
st.title("Rate Limit Console")
st.caption("One active algorithm · Equal limits for every user · In-memory state")
client = BackendClient(os.environ.get("RATE_LIMIT_API_URL", "http://127.0.0.1:8000"))

try:
    configuration = client.configuration()
except BackendError as exc:
    st.error(str(exc))
    st.button("Retry connection")
    st.stop()

if "session_id" not in configuration:
    st.warning("Restart the backend to load the updated session history API. "
               "This starts a new in-memory session and clears existing usage/history.")
    st.code(".venv/bin/python -m limiter.api", language="bash")
    st.stop()

policy_identity = (configuration["session_id"], configuration["policy_version"])
if st.session_state.get("rendered_policy") != policy_identity:
    st.session_state.pop("burst_results", None)
    # Synchronize the selector too when another admin changes the shared backend.
    st.session_state.algorithm = configuration["active_algorithm"]
    st.session_state.rendered_policy = policy_identity

with st.sidebar:
    st.header("Active policy")
    st.success(LABELS[configuration["active_algorithm"]])
    st.caption(f"Policy version {configuration['policy_version']}")
    st.caption(f"Backend session started {configuration['started_at']} (UTC)")
    st.caption("Limits are read from config.json at API startup.")
    # Store the version shown with this form, not a newly fetched version on submit.
    if "displayed_version" not in st.session_state:
        st.session_state.displayed_version = configuration["policy_version"]
    with st.form("policy_form"):
        selected = st.selectbox(
            "Algorithm", list(LABELS),
            index=list(LABELS).index(configuration["active_algorithm"]),
            format_func=lambda key: LABELS[key], key="algorithm",
        )
        st.caption("Applying a different algorithm resets all user allowances. History is kept.")
        apply = st.form_submit_button("Apply algorithm", type="primary")
    if apply:
        try:
            updated = client.switch(selected, st.session_state.displayed_version)
            st.session_state.displayed_version = updated["policy_version"]
            st.session_state.policy_notice = f"Backend active: {LABELS[updated['active_algorithm']]} · policy v{updated['policy_version']}"
            st.rerun()
        except BackendError as exc:
            st.error(str(exc))
    st.session_state.displayed_version = configuration["policy_version"]
    if "policy_notice" in st.session_state:
        st.info(st.session_state.pop("policy_notice"))
    st.divider()
    st.caption("Local MVP: authentication is disabled. Keep these services on localhost.")
    st.caption("Restarting the API clears state and restores the configured default algorithm.")

left, right = st.columns([1.2, 1])
with left:
    st.subheader("Policy settings")
    for name, label in LABELS.items():
        active = name == configuration["active_algorithm"]
        with st.expander(f"{label} · {'active' if active else 'inactive settings'}", expanded=active):
            st.write(DESCRIPTIONS[name])
            st.json(configuration["settings"][name])
    st.caption("The rolling queue stores timestamps; it does not delay or execute requests.")

with right:
    st.subheader("Try a request burst")
    st.caption(f"Requests use the backend's active policy: {LABELS[configuration['active_algorithm']]}. "
               "A dropdown selection takes effect only after Apply algorithm.")
    with st.form("traffic_form"):
        user_id = st.text_input("User ID", value="demo-user", max_chars=128, key="user_id")
        count = st.number_input("Number of requests", min_value=1, max_value=100, value=15, step=1, key="burst_count")
        send = st.form_submit_button("Send requests", type="primary")
    if send:
        results = []
        try:
            for _ in range(int(count)):
                results.append(client.check(user_id))
            st.session_state.burst_results = results
        except BackendError as exc:
            st.session_state.burst_results = results
            st.error(str(exc))
    if "burst_results" in st.session_state and st.session_state.burst_results:
        results = st.session_state.burst_results
        allowed = sum(result["allowed"] for result in results)
        st.info(f"Last burst: {allowed} allowed · {len(results) - allowed} rate-limited")
        used = dict.fromkeys((result["algorithm"], result["policy_version"]) for result in results)
        st.caption("Evaluated by: " + "; ".join(f"{LABELS[name]} · policy v{version}" for name, version in used))
        st.dataframe(results, hide_index=True, width="stretch")

st.divider()
st.subheader("Users rate-limited in the selected period")
st.caption("Latest rejected users first. Algorithm and version describe each user's last rejection; "
           "earlier-policy rows are historical and do not indicate current routing.")
search_col, period_col = st.columns([2, 1])
with search_col:
    search = st.text_input("Search user ID", key="search")
with period_col:
    period = st.selectbox("Period", [None, 60, 300, 3600],
                          format_func=lambda value: {None: "Current backend session", 60: "Last minute", 300: "Last 5 minutes", 3600: "Last hour"}[value],
                          key="period")
auto_refresh = st.checkbox("Refresh monitoring every 5 seconds", value=True)


@st.fragment(run_every="5s" if auto_refresh else None)
def monitoring():
    st.button("Refresh monitoring")
    try:
        data = client.dashboard(search=search, lookback_seconds=period)
    except BackendError as exc:
        st.error(str(exc))
        return
    actual = data["configuration"]
    if (actual["session_id"], actual["policy_version"]) != policy_identity:
        # A fragment refresh alone leaves the sidebar and burst panel stale.
        st.rerun(scope="app")
    metrics = data["metrics"]
    columns = st.columns(4)
    for column, label, key in zip(columns, ["Allowed since startup", "Rejected since startup", "Tracked users", "Recent rejection events"], ["allowed", "rejected", "tracked_users", "retained_events"]):
        column.metric(label, metrics[key])
    st.caption(f"Backend active: {LABELS[data['configuration']['active_algorithm']]} · policy v{data['configuration']['policy_version']}")
    if data["users"]:
        st.dataframe(data["users"], hide_index=True, width="stretch", height=420,
                     column_config={
                         "algorithm": "Last rejection algorithm",
                         "policy_version": "Last rejection policy version",
                         "policy_status": "Policy at last rejection",
                         "rejection_count": "Rejections while listed" if period is None else "Retained rejections in period",
                     })
    else:
        st.info("No rate-limited users for the selected filters.")
    st.caption(f"Showing {len(data['users']):,} of {data['total_users']:,} matching users · latest 1,000 maximum · scroll the table · timestamps in UTC")
    storage = data["configuration"]["settings"]["storage"]
    if period is None:
        st.caption(f"Current session = since backend startup. Keeps the latest {storage['max_session_users']:,} distinct rejected users "
                   "across policy changes, until restart or displacement by newer users. Counts start again if an evicted user returns.")
    else:
        st.caption(f"Period history keeps at most {storage['max_events']:,} rejections for {storage['event_retention_seconds']:g} seconds. "
                   "Counts reflect retained events only.")


monitoring()


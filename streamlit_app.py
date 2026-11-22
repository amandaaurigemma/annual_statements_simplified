from __future__ import annotations

import hashlib
import hmac

import streamlit as st

from app import (
    K_AUTHED,
    K_SESSION_ID,
    get_config,
    init_session,
    render_history,
    render_queue,
    render_run,
    render_settings,
    render_status,
)
from core.utils import configure_logging

st.set_page_config(page_title="Annual Donor Statements", layout="wide")

config = get_config()
configure_logging(config.logs_dir)
init_session()


def _passcode_gate() -> bool:
    if not config.ui_passcode_sha256:
        return True
    if st.session_state.get(K_AUTHED):
        return True
    st.title("Annual Donor Statements")
    passcode = st.text_input("Passcode", type="password")
    if st.button("Enter"):
        digest = hashlib.sha256(passcode.encode("utf-8")).hexdigest()
        if hmac.compare_digest(digest, config.ui_passcode_sha256):
            st.session_state[K_AUTHED] = True
            st.rerun()
        else:
            st.error("Incorrect passcode.")
    return False


if not _passcode_gate():
    st.stop()

pages = [
    st.Page(render_run, title="Statement Run", url_path="run", default=True),
    st.Page(render_queue, title="Email Queue", url_path="queue"),
    st.Page(render_history, title="History", url_path="history"),
    st.Page(render_settings, title="Settings", url_path="settings"),
    st.Page(render_status, title="Status", url_path="status"),
]

with st.sidebar:
    st.caption(f"{config.app_name}")
    st.caption(f"Session: {st.session_state.get(K_SESSION_ID, '')}")

nav = st.navigation(pages)
nav.run()

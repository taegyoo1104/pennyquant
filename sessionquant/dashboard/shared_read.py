"""Bounded cross-session snapshots for anonymous visitors; never cache commands."""
import streamlit as st


@st.cache_data(ttl=5, max_entries=32, show_spinner=False)
def snapshot(path, *, page, news_section=None, panel=None):
    from .session_app import load_database
    return load_database(path, page=page, news_section=news_section, panel=panel)


def read_page(path, *, page, news_section=None, panel=None):
    from .access import is_admin, shared_dashboard
    from .session_app import load_database
    reader = snapshot if shared_dashboard() and not is_admin() else load_database
    return reader(path, page=page, news_section=news_section, panel=panel)
